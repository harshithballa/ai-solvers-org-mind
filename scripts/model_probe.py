"""Feasibility probe: newer Gemini models x Vertex locations, with the app's generation config vs library defaults.

Read-only with respect to the app: imports schemas/prompts from app/ but never goes through get_llm(), limits or
tracing. Each call is made once (no retries) so errors are reported exactly as Vertex returns them.

    .venv/bin/python scripts/model_probe.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.llm import ANSWER_MAX_TOKENS
from app.models import AnswerOut, GuardrailOut, SkillsOut
from app.prompts import GUARDRAIL_SYSTEM, SKILL_EXTRACT_SYSTEM

PROJECT = "arched-jetty-502906-q6"
MODELS = ["gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash"]
LOCATIONS = ["us-central1", "global"]
MAX_CALLS = 60

# $ per 1M tokens (input, output); thought tokens are billed as output.
PRICES = {"gemini-2.5-flash": (0.30, 2.50), "gemini-3.5-flash-lite": (0.30, 2.50), "gemini-3.8-flash": (0.75, 3.75)}

CHAT_SYSTEM = ("You are Dev Agent, the internal engineering assistant at Acme Corp. "
               "Be concise and practical, using short numbered steps when giving instructions. "
               "If you do not know an Acme-specific detail, say so plainly instead of inventing it.")
CHAT_USER = "How do I clear the local pip cache before re-running our CI build?"

SKILLS_USER = """Conversation:
[0] EMPLOYEE: I need to run a BigQuery query from my terminal against project acme-sbx-dana-42, how?
[1] AGENT: Install the Cloud SDK, run `gcloud auth login`, then `bq query --project_id=<project> --use_legacy_sql=false 'SELECT 1'`.
[2] EMPLOYEE: Worked after I ran gcloud auth login. bq query --project_id=acme-sbx-dana-42 --use_legacy_sql=false 'SELECT 1' returned 1.
[3] AGENT: Great. Remember to pass --use_legacy_sql=false for standard SQL.
[4] EMPLOYEE: Thanks, all good."""

GUARD_TEXT = """Content type: playbook
<<<
To fix the CI cache error, run `make clean-cache` and re-trigger the pipeline in ci.acme.internal.
I tested this in my sandbox acme-sbx-ravi-7 and my phone is 98765 43210 if anyone gets stuck.
>>>"""

TASKS = [  # (name, system, contents, schema, is_answer)
    ("chat", CHAT_SYSTEM, CHAT_USER, AnswerOut, True),
    ("skills", SKILL_EXTRACT_SYSTEM, SKILLS_USER, SkillsOut, False),
    ("guard", GUARDRAIL_SYSTEM, GUARD_TEXT, GuardrailOut, False),
]


def config(pass_name: str, system: str, schema, is_answer: bool) -> types.GenerateContentConfig:
    if pass_name == "app":  # exactly what VertexLLM.generate_json sends (think=False)
        return types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json", response_schema=schema,
            temperature=0.0, thinking_config=types.ThinkingConfig(thinking_budget=0),
            max_output_tokens=ANSWER_MAX_TOKENS if is_answer else None)
    if pass_name == "app-nothink":  # diagnostic: app temperature, no thinking_config
        return types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json", response_schema=schema,
            temperature=0.0, max_output_tokens=ANSWER_MAX_TOKENS if is_answer else None)
    # "default": library defaults for temperature and thinking, same schema/mime/output cap
    return types.GenerateContentConfig(
        system_instruction=system, response_mime_type="application/json", response_schema=schema,
        max_output_tokens=ANSWER_MAX_TOKENS if is_answer else None)


def one_call(client, model, pass_name, task) -> dict:
    name, system, contents, schema, is_answer = task
    row = {"model": model, "pass": pass_name, "task": name, "ok": False, "ms": None,
           "in": None, "out": None, "think": None, "json": "-", "cost": None, "err": ""}
    t0 = time.perf_counter()
    try:
        resp = client.models.generate_content(model=model, contents=contents,
                                              config=config(pass_name, system, schema, is_answer))
    except Exception as e:
        row["ms"] = round((time.perf_counter() - t0) * 1000)
        row["err"] = f"{type(e).__name__}: {e}"
        return row
    row["ms"] = round((time.perf_counter() - t0) * 1000)
    row["ok"] = True
    u = resp.usage_metadata
    if u is not None:
        row["in"], row["out"], row["think"] = u.prompt_token_count or 0, u.candidates_token_count or 0, u.thoughts_token_count or 0
        pin, pout = PRICES[model]
        row["cost"] = (row["in"] * pin + (row["out"] + row["think"]) * pout) / 1e6
    try:
        schema.model_validate_json(resp.text or "")
        row["json"] = "ok"
    except ValidationError as e:
        row["json"] = "BAD"
        row["err"] = f"schema: {str(e).splitlines()[0]} | finish={resp.candidates[0].finish_reason if resp.candidates else None}"
    return row


def main():
    rows, calls = [], 0
    for loc in LOCATIONS:
        client = genai.Client(vertexai=True, project=PROJECT, location=loc)
        for model in MODELS:
            for pass_name in ("app", "default"):
                for task in TASKS:
                    if calls >= MAX_CALLS:
                        break
                    r = one_call(client, model, pass_name, task)
                    calls += 1
                    r["loc"] = loc
                    rows.append(r)
                    print(f"  [{calls:2}] {loc:11} {model:22} {pass_name:7} {task[0]:6} "
                          f"{'ok' if r['ok'] else 'ERR'} {r['ms']}ms", file=sys.stderr, flush=True)
            # Isolate the cause when the app config fails but defaults work: retry once with temperature only.
            app_err = [r for r in rows if r["loc"] == loc and r["model"] == model and r["pass"] == "app" and not r["ok"]]
            dflt_ok = any(r["ok"] for r in rows if r["loc"] == loc and r["model"] == model and r["pass"] == "default")
            if app_err and dflt_ok and calls < MAX_CALLS:
                r = one_call(client, model, "app-nothink", TASKS[0])
                calls += 1
                r["loc"] = loc
                rows.append(r)

    hdr = f"{'location':11} {'model':22} {'pass':11} {'task':6} {'status':6} {'ms':>6} {'in':>5} {'out':>5} {'think':>5} {'json':4} {'$/call':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        f = lambda v: "-" if v is None else str(v)
        cost = "-" if r["cost"] is None else f"{r['cost']:.6f}"
        print(f"{r['loc']:11} {r['model']:22} {r['pass']:11} {r['task']:6} {'ok' if r['ok'] else 'ERROR':6} "
              f"{f(r['ms']):>6} {f(r['in']):>5} {f(r['out']):>5} {f(r['think']):>5} {r['json']:4} {cost:>9}")
    print(f"\n{calls} calls total.")
    errs = [r for r in rows if r["err"]]
    if errs:
        print("\nErrors (verbatim):")
        for r in errs:
            print(f"- {r['loc']} / {r['model']} / {r['pass']} / {r['task']}:\n    {r['err']}")


if __name__ == "__main__":
    main()
