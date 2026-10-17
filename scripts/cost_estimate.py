"""Estimate cost per operation from real token usage, and check the daily caps against a budget.

    GOOGLE_CLOUD_PROJECT=<project> python -m scripts.cost_estimate [--usd-inr 88] [--budget-inr 1000] [--days 7]

Runs real requests in-process (memory store, limits off): one distill, chat messages with each toggle, one skill
extraction and one compare. Token counts come from the recorded llm_calls (usage_metadata on generate calls).
Embedding calls don't report tokens, so their input is estimated as characters/4 of the recorded excerpt.

Assumed prices (USD, standard paid tier, no free tier, no batch/caching discounts):
- Gemini 2.5 Flash on Vertex AI: $0.30 / 1M input tokens, $2.50 / 1M output tokens (thinking billed as output).
  Source: third-party price trackers (getmaxim.ai, llmreference.com), which agree; verify on
  https://cloud.google.com/vertex-ai/generative-ai/pricing
- gemini-embedding-001: $0.15 / 1M input tokens (NOT verified this session; embeddings are a tiny share anyway).
- Vertex AI Search, Enterprise edition, general pricing: $4.00 / 1,000 queries (extractive content needs
  Enterprise), first 10,000 queries per month free. Source: https://cloud.google.com/generative-ai-app-builder/pricing
- USD -> INR: --usd-inr (default 88, an assumption; check the current rate).
"""
import argparse
import os
import sys
from collections import defaultdict

PRICE_IN, PRICE_OUT, PRICE_EMBED = 0.30 / 1e6, 2.50 / 1e6, 0.15 / 1e6
PRICE_SEARCH_QUERY = 4.00 / 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--usd-inr", type=float, default=88.0)
    ap.add_argument("--budget-inr", type=float, default=1000.0)
    ap.add_argument("--days", type=int, default=7, help="length of the judging window")
    args = ap.parse_args()
    os.environ.update({"ORGMIND_STORE": "memory", "ORGMIND_LLM": "vertex", "ORGMIND_RAG_PROVIDER": "off",
                       "ORGMIND_LIMITS": "off", "ORGMIND_TRACING": "off"})
    if not os.getenv("GOOGLE_CLOUD_PROJECT"):
        sys.exit("set GOOGLE_CLOUD_PROJECT")

    from fastapi.testclient import TestClient

    from app.config import DAILY_LIMITS
    from app.main import app
    from app.store import get_store

    with TestClient(app) as c:
        c.post("/api/distill", json={})
        for flags, q in [({}, "How do I make my Docker builds faster?"),
                         ({"use_playbooks": True}, "My staging deploy fails with ACME_VAULT_TOKEN expired. How do I fix it?"),
                         ({"use_playbooks": True, "use_skills": True}, "How should I structure pytest fixtures for async tests?")]:
            sid = c.post("/api/sessions", json={"employee_id": "e_dana", "agent_id": "a_dev"}).json()["id"]
            c.post(f"/api/sessions/{sid}/messages", json={"content": q, **flags})
        c.post("/api/skills/extract", json={"session_id": "ses_seed_14", "employee_id": "e_dana"})
        c.post("/api/compare", json={"agent_id": "a_dev", "question": "My staging deploy fails with ACME_VAULT_TOKEN expired."})

    calls = get_store().list_llm_calls(500)
    by_req: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for x in calls:
        by_req[x.request][x.trace_id].append(x)

    print(f"Prices: Gemini 2.5 Flash ${PRICE_IN * 1e6:.2f}/1M in, ${PRICE_OUT * 1e6:.2f}/1M out; "
          f"embeddings ${PRICE_EMBED * 1e6:.2f}/1M (unverified); Vertex AI Search ${PRICE_SEARCH_QUERY * 1000:.2f}/1k queries; "
          f"1 USD = {args.usd_inr} INR (assumed)\n")
    per_op = {}
    print(f"{'operation':<16}{'requests':>9}{'gemini calls':>14}{'in tok':>9}{'out tok':>9}{'USD/op':>11}{'INR/op':>9}")
    for req in ("chat", "distill", "skills_extract", "compare"):
        traces = by_req.get(req, {})
        if not traces:
            continue
        tot_usd = n_calls = tin = tout = 0
        for cs in traces.values():
            for x in cs:
                n_calls += 1
                if x.operation.startswith("embed:"):
                    tot_usd += max(1, len(x.prompt_excerpt) // 4) * PRICE_EMBED
                else:
                    tin += x.input_tokens or 0
                    tout += x.output_tokens or 0
                    tot_usd += (x.input_tokens or 0) * PRICE_IN + (x.output_tokens or 0) * PRICE_OUT
        n = len(traces)
        per_op[req] = tot_usd / n
        print(f"{req:<16}{n:>9}{n_calls / n:>14.1f}{tin / n:>9.0f}{tout / n:>9.0f}{tot_usd / n:>11.5f}{tot_usd / n * args.usd_inr:>9.3f}")
    # A share scan is one guardrail generate call plus nothing else; approximate it with a chat answer.
    per_op.setdefault("share_scans", per_op.get("chat", 0))

    L = DAILY_LIMITS
    worst = (L["chat_messages"] * (per_op.get("chat", 0) + PRICE_SEARCH_QUERY) + L["distill"] * per_op.get("distill", 0)
             + L["skills_extract"] * per_op.get("skills_extract", 0) + L["compare"] * per_op.get("compare", 0)
             + L["share_scans"] * per_op["share_scans"])
    max_call_usd = max(per_op.values()) / 1  # rough upper bound per op
    print(f"\nWorst case per UTC day if EVERY bucket hits its cap (ignores the {L['gemini_calls']} Gemini-call ceiling, "
          f"and charges a paid Search query on every chat message): ${worst:.2f} = INR {worst * args.usd_inr:.0f}")
    print(f"Over a {args.days}-day judging window: INR {worst * args.usd_inr * args.days:.0f} "
          f"vs budget INR {args.budget_inr:.0f}")
    print("The gemini_calls ceiling binds first in practice: a busy day of distills uses ~42 calls each.")
    print(f"(most expensive single operation measured: ${max_call_usd:.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
