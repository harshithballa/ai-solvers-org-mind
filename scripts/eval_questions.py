"""Fixed eval questions: baseline vs playbook answers, plus raw retrieval scores for threshold tuning.

In-process (seeds a fresh memory store and distills):
    python -m scripts.eval_questions [--scores-only]
Against a running server (uses whatever playbooks it already has):
    python -m scripts.eval_questions --url https://orgmind-xxx.run.app
"""
import argparse
import json
import os
import sys
import textwrap
import urllib.request

# (agent_id, question, playbook expected?)
QUESTIONS = [
    ("a_dev", "My staging deploy is failing with ACME_VAULT_TOKEN expired. How do I fix it?", True),
    ("a_dev", "The CI deploy job says vault permission denied even though I refreshed my token locally.", True),
    ("a_dev", "I'm new. `acme deploy staging` gives a vault permission denied error. What access do I need?", True),
    ("a_dev", "How do I make my Docker builds faster? pip reinstalls everything every time.", False),
    ("a_dev", "How should I structure pytest fixtures for async database tests?", False),
    ("a_hr", "My home office stipend claim was rejected with 'Missing cost center'. What do I do?", True),
    ("a_hr", "What's the limit for home-office equipment reimbursement and which expense category should I use?", True),
    ("a_hr", "How many PTO days can I carry over to next year?", False),
]


def _post(url: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def _wrap(text: str) -> str:
    return textwrap.indent(text.strip(), "    ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="base URL of a running OrgMind server")
    ap.add_argument("--scores-only", action="store_true", help="print retrieval scores only (in-process)")
    args = ap.parse_args()

    if args.url:
        compare = lambda agent_id, q: _post(args.url.rstrip("/"), "/api/compare", {"agent_id": agent_id, "question": q})
    else:
        os.environ.setdefault("ORGMIND_STORE", "memory")
        from app.config import get_settings
        from app.distiller import distill
        from app.main import compare as compare_endpoint, CompareRequest
        from app.retrieval import score_playbooks
        from app.seed import AGENTS, ORG_ID, seed
        from app.store import get_store

        seed(get_store())
        report = distill(ORG_ID)
        s = get_settings()
        print(f"distill: created={report.playbooks_created} updated={report.playbooks_updated} "
              f"cluster_threshold={s.cluster_threshold} retrieval_threshold={s.retrieval_threshold}")
        for c in report.clusters:
            print(f"  [{c.agent_kind}] {c.outcome:9s} employees={c.employee_ids} resolved={len(c.resolved_session_ids)}"
                  f"/{len(c.session_ids)} {c.title or c.reason}")
        kinds = {a.id: a.kind for a in AGENTS}
        print("\nretrieval scores (question -> best playbook score):")
        for agent_id, q, expected in QUESTIONS:
            scored = score_playbooks(ORG_ID, kinds[agent_id], q)
            best = f"{scored[0][1]:.3f} {scored[0][0].title}" if scored else "-"
            hit = bool(scored) and scored[0][1] >= s.retrieval_threshold
            flag = "ok " if hit == expected else "BAD"
            print(f"  {flag} expect={'Y' if expected else 'N'} {best}\n      q: {q}")
        if args.scores_only:
            return
        compare = lambda agent_id, q: compare_endpoint(CompareRequest(agent_id=agent_id, question=q))

    for agent_id, q, expected in QUESTIONS:
        res = compare(agent_id, q)
        print("\n" + "=" * 100 + f"\n[{agent_id}] {q}\n" + "=" * 100)
        print("--- BASELINE (no playbooks)\n" + _wrap(res["baseline"]["answer"]))
        cited = ", ".join(c["title"] for c in res["with_playbooks"]["cited"]) or "none"
        print(f"--- WITH PLAYBOOKS (cited: {cited})\n" + _wrap(res["with_playbooks"]["answer"]))


if __name__ == "__main__":
    sys.exit(main())
