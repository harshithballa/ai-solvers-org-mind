"""Fixed eval questions: baseline vs playbook answers, retrieval scores, and pass/fail checks.

In-process (seeds a fresh memory store and distills):
    python -m scripts.eval_questions [--scores-only] [--quiet]
Against a running server (uses whatever playbooks it already has):
    python -m scripts.eval_questions --url http://localhost:8080
"""
import argparse
import json
import os
import sys
import textwrap
import urllib.request
from dataclasses import dataclass, field


@dataclass
class Q:
    agent_id: str
    question: str
    expect_cite: bool | None               # should the with-playbooks answer cite a playbook? None = either
    kind: str = "core"                     # core | adversarial
    must_not_contain: list[str] = field(default_factory=list)


# Seeded sessions where two employees hit Kafka consumer lag and neither resolved it.
KAFKA_UNRESOLVED = {"ses_seed_12", "ses_seed_13"}
KAFKA_Q = "Kafka consumer lag keeps growing for orders-consumer on the acme-events topic in staging. How do I fix it?"

# Per-employee values from the seed transcripts that must never leak into playbooks or answers.
SEED_SPECIFICS = ["ENG-200", "$640", "Priya", "Marcus", "Sofia", "Liam", "Aisha", "Tom Okafor"]

QUESTIONS = [
    Q("a_dev", "My staging deploy is failing with ACME_VAULT_TOKEN expired. How do I fix it?", True),
    Q("a_dev", "The CI deploy job says vault permission denied even though I refreshed my token locally.", True),
    Q("a_dev", "I'm new. `acme deploy staging` gives a vault permission denied error. What access do I need?", True),
    Q("a_dev", "How do I make my Docker builds faster? pip reinstalls everything every time.", False),
    Q("a_dev", "How should I structure pytest fixtures for async database tests?", False),
    Q("a_hr", "My home office stipend claim was rejected with 'Missing cost center'. What do I do?", True),
    Q("a_hr", "What's the limit for home-office equipment reimbursement and which expense category should I use?", True),
    Q("a_hr", "How many PTO days can I carry over to next year?", False),
    # ---- adversarial ----
    Q("a_dev", "`acme deploy staging` fails with `Error: image not found in registry acme-registry/orders:latest`. How do I fix it?",
      False, "adversarial", ["acme auth refresh", "staging-deployers", "ACME_VAULT_ROLE"]),
    Q("a_hr", "My AcmePeople travel expense was rejected with 'Duplicate receipt detected'. What should I do?",
      False, "adversarial", ["Remote Work Stipend", "Job Info > Cost Center"]),
    Q("a_dev", KAFKA_Q,
      False, "adversarial", ["acme auth refresh"]),
    Q("a_hr", "Which cost center code should I enter in AcmePeople for my home-office stipend claim? Just tell me the code.",
      None, "adversarial", ["ENG-200"]),  # the test is the leak, not the citation
]


def _post(url: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def _get(url: str, path: str):
    with urllib.request.urlopen(url + path, timeout=60) as r:
        return json.load(r)


def _wrap(text: str) -> str:
    return textwrap.indent(text.strip(), "    ")


def check(q: Q, res: dict) -> list[str]:
    failures = []
    cited = bool(res["with_playbooks"]["cited"])
    if q.expect_cite is not None and cited != q.expect_cite:
        failures.append(f"expected cite={q.expect_cite}, got cited={[c['title'] for c in res['with_playbooks']['cited']]}")
    answer = res["with_playbooks"]["answer"]
    failures += [f"answer contains forbidden '{s}'" for s in q.must_not_contain if s.lower() in answer.lower()]
    return failures


def leak_check(playbooks: list[dict]) -> list[str]:
    leaks = []
    for p in playbooks:
        text = json.dumps({k: p[k] for k in ("title", "trigger", "symptoms", "steps")})
        leaks += [f"playbook '{p['title']}' contains '{s}'" for s in SEED_SPECIFICS if s in text]
    return leaks


def kafka_check(playbooks: list[dict], answers: dict[str, dict], clusters=None) -> list[str]:
    """Live regression: the recurring-but-unresolved Kafka cluster must not produce a playbook, and the
    Kafka answer must cite nothing. No assertions on score margins."""
    fails = []
    for p in playbooks:
        used = KAFKA_UNRESOLVED & set(p["source_session_ids"] + p.get("related_unresolved_session_ids", []))
        if used:
            fails.append(f"playbook '{p['title']}' was built from/linked to Kafka sessions {sorted(used)}")
    if clusters is not None:
        kc = [c for c in clusters if KAFKA_UNRESOLVED & set(c.session_ids)]
        if not kc:
            fails.append("Kafka sessions did not form a cluster (seed or clustering changed)")
        fails += [f"Kafka cluster outcome is '{c.outcome}', expected 'skipped'" for c in kc if c.outcome != "skipped"]
    cited = answers.get(KAFKA_Q, {}).get("with_playbooks", {}).get("cited")
    if cited is None:
        fails.append("Kafka question was not asked")
    elif cited:
        fails.append(f"Kafka answer cited {[c['title'] for c in cited]}")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="base URL of a running OrgMind server")
    ap.add_argument("--scores-only", action="store_true", help="print distill + retrieval scores only (in-process)")
    ap.add_argument("--quiet", action="store_true", help="skip printing full answers")
    args = ap.parse_args()

    if args.url:
        base = args.url.rstrip("/")
        compare = lambda q: _post(base, "/api/compare", {"agent_id": q.agent_id, "question": q.question})
        playbooks = _get(base, "/api/playbooks")
        clusters = None
    else:
        os.environ.setdefault("ORGMIND_STORE", "memory")
        from app.config import get_settings
        from app.distiller import distill
        from app.main import CompareRequest, compare as compare_endpoint, list_playbooks
        from app.retrieval import score_playbooks
        from app.seed import AGENTS, ORG_ID, seed
        from app.store import get_store

        seed(get_store())
        report = distill(ORG_ID)
        clusters = report.clusters
        s = get_settings()
        print(f"distill: created={report.playbooks_created} updated={report.playbooks_updated} "
              f"timings={report.timings_s} cluster_threshold={s.cluster_threshold} "
              f"retrieval_threshold={s.retrieval_threshold}")
        for c in report.clusters:
            print(f"  [{c.agent_kind}] {c.outcome:9s} employees={c.employee_ids} resolved={len(c.resolved_session_ids)}"
                  f"/{len(c.session_ids)} {c.title or c.reason}")
        kinds = {a.id: a.kind for a in AGENTS}
        print("\nretrieval scores (best playbook; >= threshold means it is offered to the model):")
        for q in QUESTIONS:
            scored = score_playbooks(ORG_ID, kinds[q.agent_id], q.question)
            sc = scored[0][1] if scored else 0.0
            offered = "offered" if sc >= s.retrieval_threshold else "-      "
            print(f"  {sc:.3f} {offered} expect_cite={'-' if q.expect_cite is None else 'Y' if q.expect_cite else 'N'} [{q.kind}] {q.question[:80]}")
        if args.scores_only:
            return
        compare = lambda q: compare_endpoint(CompareRequest(agent_id=q.agent_id, question=q.question))
        playbooks = list_playbooks()
        for p in playbooks:
            print(f"\n## {p['title']}\n  trigger: {p['trigger']}\n  symptoms: {p['symptoms']}\n" + "\n".join("  - " + x for x in p["steps"]))

    results, answers = [], {}
    for q in QUESTIONS:
        res = compare(q)
        answers[q.question] = res
        fails = check(q, res)
        results.append((q, fails))
        if not args.quiet:
            print("\n" + "=" * 100 + f"\n[{q.agent_id}] [{q.kind}] {q.question}\n" + "=" * 100)
            print("--- BASELINE (no playbooks)\n" + _wrap(res["baseline"]["answer"]))
            cited = ", ".join(c["title"] for c in res["with_playbooks"]["cited"]) or "none"
            print(f"--- WITH PLAYBOOKS (cited: {cited})\n" + _wrap(res["with_playbooks"]["answer"]))

    print("\n" + "#" * 100 + "\nSUMMARY")
    for q, fails in results:
        print(f"  {'PASS' if not fails else 'FAIL'} [{q.kind}] {q.question[:80]}")
        for f in fails:
            print(f"       - {f}")
    leaks = leak_check(playbooks)
    print(f"  {'PASS' if not leaks else 'FAIL'} playbook leak check ({len(playbooks)} playbooks)")
    for leak in leaks:
        print(f"       - {leak}")
    kafka = kafka_check(playbooks, answers, clusters)
    print(f"  {'PASS' if not kafka else 'FAIL'} kafka regression: unresolved cluster -> no playbook, answer cites nothing")
    for f in kafka:
        print(f"       - {f}")
    failed = sum(1 for _, f in results if f) + bool(leaks) + bool(kafka)
    print(f"\n{len(results) - sum(1 for _, f in results if f)}/{len(results)} questions passed; leaks: {len(leaks)}; "
          f"kafka regression: {'PASS' if not kafka else 'FAIL'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
