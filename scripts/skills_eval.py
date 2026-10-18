"""Skills eval against REAL Gemini (in-process, memory store, RAG off):

    GOOGLE_CLOUD_PROJECT=<project> python -m scripts.skills_eval [--runs 3]

1. BigQuery session -> exactly 1 skill, code in steps traceable to the transcript, parameters are names only,
   the personal value appears nowhere in the skill (repeated --runs times, archiving the previous run's skill so
   it may be learned again; variance reported). Learning it again while it exists -> 0 new, 1 already learned.
2. Plain factual Q&A (resolved) -> 0 skills.     3. Unresolved / abandoned sessions -> 0 skills.
4. Approved skill + related questions with use_skills -> applies steps, asks for the parameter, cites the skill.
5. Unrelated questions -> no skill cited.        6. Another employee asking -> never sees A's private skill.
Also: distill still yields exactly the 2 expected playbooks, and score ranges for threshold tuning.
"""
import argparse
import json
import os
import re
import sys
import time

RELATED = [
    "How do I run a query against the acme-analytics orders table in BigQuery from my laptop?",
    "bq query fails with Access Denied: User does not have bigquery.jobs.create permission on acme-analytics. What do I do?",
    "I want to count the rows in acme-analytics.events.orders from the command line.",
    "bq query says Syntax error: Unexpected \"-\" near the table name",
]
RELATED_VAULT = "My staging deploy is failing with ACME_VAULT_TOKEN expired. How do I fix it?"
UNRELATED = [
    "How do I make my Docker builds faster? pip reinstalls everything every time.",
    "My staging deploy is failing with ACME_VAULT_TOKEN expired. How do I fix it?",
    "How should I structure pytest fixtures for async database tests?",
    "Cypress tests time out in CI on the checkout page.",
    "How do I export a Google Sheet to CSV?",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    os.environ.setdefault("ORGMIND_LIMITS", "off")  # eval runs are not demo traffic
    os.environ.update({"ORGMIND_STORE": "memory", "ORGMIND_LLM": "vertex", "ORGMIND_RAG_PROVIDER": "off",
                       "ORGMIND_TRACING": "off"})
    if not os.getenv("GOOGLE_CLOUD_PROJECT"):
        sys.exit("set GOOGLE_CLOUD_PROJECT")

    from fastapi.testclient import TestClient

    from app import skills
    from app.config import get_settings
    from app.main import app
    from app.models import Skill
    from app.seed import BIGQUERY_PERSONAL_VALUE as PERSONAL
    from app.seed import BIGQUERY_SESSION_ID as BQ
    from app.seed import ORG_ID, SESSIONS
    from app.store import get_store

    def sid_where(pred):
        return next(f"ses_seed_{i:02d}" for i, s in enumerate(SESSIONS) if pred(s))

    pto = sid_where(lambda s: "PTO" in s[3][0])
    tom_vault = sid_where(lambda s: s[0] == "e_tom" and "VAULT" in s[3][0])
    kafka = sid_where(lambda s: s[0] == "e_priya" and "Kafka" in s[3][0])
    owner = {f"ses_seed_{i:02d}": s[0] for i, s in enumerate(SESSIONS)}
    failures: list[str] = []
    store = get_store()

    with TestClient(app) as client:
        def extract(sid):
            t0 = time.monotonic()
            r = client.post("/api/skills/extract", json={"session_id": sid, "employee_id": owner[sid]})
            assert r.status_code == 200, r.text
            return r.json(), time.monotonic() - t0

        # ---- distill unchanged ----
        rep = client.post("/api/distill", json={}).json()
        created = [c for c in rep["clusters"] if c["outcome"] == "created"]
        print(f"distill: sessions={rep['sessions_scanned']} clusters={len(rep['clusters'])} "
              f"created={rep['playbooks_created']} -> {[c['title'] for c in created]}")
        for c in rep["clusters"]:
            print(f"  {c['outcome']:<8} {c['session_ids']} {c['reason']}")
        if rep["playbooks_created"] != 2 or any(BQ in c["session_ids"] for c in created):
            failures.append("distill: expected exactly 2 playbooks, none from the BigQuery session")

        # ---- 1. BigQuery session, N runs ----
        transcript = "\n".join(m.content for m in store.list_messages(BQ))
        runs = []
        for n in range(1, args.runs + 1):
            if runs and runs[-1]:   # re-learning is only allowed once the earlier skill is archived (de-dup)
                client.post(f"/api/skills/{runs[-1][0]['id']}/archive", json={"employee_id": owner[BQ]})
            res, secs = extract(BQ)
            found = res["skills"]
            print(f"\n[1] BigQuery run {n}: {len(found)} skill(s), proposed={res['proposed']} rejected={res['rejected']} "
                  f"redacted={res['redacted']} ({secs:.1f}s)")
            problems = []
            if len(found) != 1:
                problems.append(f"expected 1 skill, got {len(found)}")
            for sk in found:
                blob = json.dumps(sk)
                if PERSONAL.lower() in blob.lower() or "dkim" in blob.lower():
                    problems.append("personal value leaked")
                if missing := skills.untraceable_code(sk["steps"], transcript):
                    problems.append(f"code not in transcript: {missing}")
                if not sk["parameters"]:
                    problems.append("no parameters (expected the sandbox project)")
                for p in sk["parameters"]:
                    if not re.fullmatch(r"[a-z][a-z0-9_]*", p["name"]):
                        problems.append(f"parameter name not snake_case: {p['name']!r}")
                if hits := skills.secret_hits(skills.skill_text(Skill.model_validate(sk))):
                    problems.append(f"secret patterns {hits}")
                print(json.dumps({k: sk[k] for k in ("name", "description", "trigger", "steps", "parameters",
                                                     "evidence")}, indent=2))
            print("  ->", "PASS" if not problems else "FAIL " + "; ".join(problems))
            failures += [f"[1] run {n}: {p}" for p in problems]
            runs.append(found)
        names = {f[0]["name"] for f in runs if f}
        print(f"\n[1] variance: {len(names)} distinct names {sorted(names)}; step counts "
              f"{[len(f[0]['steps']) if f else 0 for f in runs]}; parameters "
              f"{[[p['name'] for p in f[0]['parameters']] if f else [] for f in runs]}")

        # Learning the same chat again while its skill is still there: nothing new, reported as already learned.
        res, _ = extract(BQ)
        ok = res["skills"] == [] and len(res["already_learned"]) == 1
        print(f"[1] learn again (de-dup): {len(res['skills'])} new, already learned "
              f"{[k['name'] for k in res['already_learned']]} -> {'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append(f"[1] learn again: expected 0 new + 1 already learned, got {len(res['skills'])} new, "
                            f"{res['already_learned']}")

        # A chat where the agent only repeated playbook answers and the employee never confirmed anything
        # (the Phase 3 over-trigger): must yield no skill.
        from app.models import Message, Session
        vault_twice = "ses_eval_vault_twice"
        store.put(Session(id=vault_twice, org_id=ORG_ID, employee_id="e_dana", agent_id="a_dev", agent_kind="dev"))
        pb_ids = [p["id"] for p in client.get("/api/playbooks?agent_kind=dev").json()]
        pb_text = client.get(f"/api/playbooks/{pb_ids[0]}").json()["playbook"] if pb_ids else {"title": "", "steps": []}
        pb_answer = f"Per the playbook *{pb_text['title']}*:\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(pb_text["steps"], 1))
        for role, text, cited in (("user", RELATED_VAULT, []), ("assistant", pb_answer, pb_ids[:1]),
                                  ("user", RELATED_VAULT, []), ("assistant", pb_answer, pb_ids[:1])):
            store.add_message(Message(session_id=vault_twice, role=role, content=text, cited_playbook_ids=cited))
        owner[vault_twice] = "e_dana"

        # ---- 2/3. no-skill sessions ----
        for label, sid in (("[2] factual Q&A (PTO carryover)", pto), ("[3] unresolved (Tom, vault)", tom_vault),
                           ("[3] unresolved (Priya, Kafka)", kafka),
                           ("[3] agent repeated playbook answers, no confirmation (Dana)", vault_twice)):
            counts = []
            for _ in range(args.runs):
                res, _ = extract(sid)
                counts.append(len(res["skills"]))
                for sk in res["skills"]:
                    print(f"  unexpected skill from {sid}: {sk['name']} {sk['steps']}")
            ok = all(c == 0 for c in counts)
            print(f"{label} {sid}: skills per run {counts} -> {'PASS' if ok else 'FAIL'}")
            if not ok:
                failures.append(f"{label}: expected 0 skills, got {counts}")

        # ---- approve the BigQuery skill ----
        [sk] = [s for s in client.get("/api/skills?employee_id=e_dana").json()
                if s["source_session_id"] == BQ and s["status"] == "draft"]
        assert client.post(f"/api/skills/{sk['id']}/approve", json={"employee_id": "e_dana"}).status_code == 200
        param_names = [p["name"] for p in sk["parameters"]]
        print(f"\napproved {sk['id']} {sk['name']!r} parameters={param_names}")

        # ---- scores ----
        print(f"\nskill retrieval scores (ORGMIND_SKILL_MIN_SCORE={get_settings().skill_min_score}):")
        rel_scores, unrel_scores = [], []
        for label, qs, bucket in (("related", RELATED, rel_scores), ("unrelated", UNRELATED, unrel_scores)):
            for q in qs:
                [(_, sc)] = skills.score_skills(ORG_ID, "dev", "e_dana", q)
                bucket.append(sc)
                print(f"  {label:<9} {sc:.3f}  {q}")
        print(f"  related range {min(rel_scores):.3f}-{max(rel_scores):.3f}; "
              f"unrelated range {min(unrel_scores):.3f}-{max(unrel_scores):.3f}")

        def chat(emp, q):
            sid = client.post("/api/sessions", json={"employee_id": emp, "agent_id": "a_dev"}).json()["id"]
            return client.post(f"/api/sessions/{sid}/messages", json={"content": q, "use_skills": True}).json()

        # ---- 4. related ----
        for q in RELATED[:2]:
            r = chat("e_dana", q)
            ans = r["answer"]
            problems = []
            if r["message"]["cited_skill_ids"] != [sk["id"]]:
                problems.append(f"cited {r['message']['cited_skill_ids']}")
            if "--use_legacy_sql=false" not in ans:
                problems.append("did not apply the legacy-SQL step")
            if PERSONAL in ans:
                problems.append("personal value in answer")
            if "?" not in ans:
                problems.append("did not ask for the parameter")
            print(f"\n[4] e_dana: {q}\n  retrieved={r['skills']['retrieved']} -> {'PASS' if not problems else 'FAIL ' + '; '.join(problems)}")
            print("  " + ans.replace("\n", "\n  "))
            failures += [f"[4] {q[:40]}: {p}" for p in problems]

        # ---- 5. unrelated ----
        for q in UNRELATED[:2]:
            r = chat("e_dana", q)
            ok = r["message"]["cited_skill_ids"] == []
            print(f"\n[5] e_dana: {q}\n  retrieved={r['skills']['retrieved']} cited={r['message']['cited_skill_ids']} "
                  f"-> {'PASS' if ok else 'FAIL'}")
            if not ok:
                failures.append(f"[5] cited a skill for: {q}")

        # ---- 6. another employee ----
        r = chat("e_priya", RELATED[0])
        listed = [s["id"] for s in client.get("/api/skills?employee_id=e_priya").json()]
        ok = r["message"]["cited_skill_ids"] == [] and r["skills"]["retrieved"] == [] and sk["id"] not in listed
        print(f"\n[6] e_priya: {RELATED[0]}\n  listed={listed} retrieved={r['skills']['retrieved']} "
              f"cited={r['message']['cited_skill_ids']} -> {'PASS' if ok else 'FAIL'}")
        print("  " + r["answer"].replace("\n", "\n  ")[:600])
        if not ok:
            failures.append("[6] another employee saw or cited the private skill")

    print("\nSKILLS EVAL", "PASS" if not failures else "FAIL")
    for f in failures:
        print("  -", f)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
