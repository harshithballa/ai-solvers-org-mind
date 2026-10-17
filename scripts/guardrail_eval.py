"""Guardrail eval against REAL Gemini (in-process, memory store, limits off). Document retrieval in the end-to-end case
uses the in-memory RAG provider (keyword search), so runs don't wait ~90s for Vertex indexing; scope, sharing and
the guardrail are the real code. The browser flow (scripts/user_flow.py) covers sharing with real Vertex.

    GOOGLE_CLOUD_PROJECT=<project> python -m scripts.guardrail_eval [--runs 3]
"""
import argparse
import json
import os
import sys
from collections import defaultdict

CLEAN = ("Acme internal note: the staging deploy freeze window is Thursdays 4pm-6pm IST.\n"
         "To request an exception, post in #release-ops with the ticket ID and tag @rohan-release.")
PII = ("Onboarding contacts (synthetic). For laptop issues reach Priyanka Rao at priyanka.rao.home@gmail.com or on "
       "+91 98450 12345. The staging deploy freeze window is Thursdays 4pm-6pm IST.")
SECRETS = ("CI setup notes. export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE before running the build. "
           "The license was paid with card 4111 1111 1111 1111.")
ORG_FACTS = ("Release process (synthetic). Post in #release-ops and tag @rohan-release. File a ticket such as REL-1234 or "
             "INC-20261017. Check the numbers in acme-analytics.events.orders, deploy with `acme deploy staging`, and request "
             "the staging-deployers group in #platform-access. The Remote Work Stipend limit is $500 per year and receipts "
             "must be uploaded within 60 days. Exports run at 02:30 UTC in the org project acme-analytics.")
SANDBOX = ("How I run the weekly export: I use my sandbox project acme-sbx-dkim-4821 for the query jobs and read from the "
           "shared dataset acme-analytics.events.orders.")
SKILL_Q = "How do I run a query against the acme-analytics orders table in BigQuery from my laptop?"
DOC_Q = "When is the staging deploy freeze window and how do I ask for an exception?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    os.environ.update({"ORGMIND_STORE": "memory", "ORGMIND_LLM": "vertex", "ORGMIND_RAG_PROVIDER": "fake",
                       "ORGMIND_LIMITS": "off", "ORGMIND_TRACING": "off"})
    if not os.getenv("GOOGLE_CLOUD_PROJECT"):
        sys.exit("set GOOGLE_CLOUD_PROJECT")

    from fastapi.testclient import TestClient

    from app import guardrail, rag
    from app.llm import get_llm
    from app.main import app
    from app.seed import BIGQUERY_SESSION_ID, seed
    from app.sharing import skill_text
    from app.store import get_store
    from app.models import Skill

    table: dict[str, list[str]] = defaultdict(list)
    failures: list[str] = []
    false_positives: list[str] = []

    def check(run, name, ok, detail):
        table[name].append(detail)
        print(f"  {'PASS' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failures.append(f"run {run}: {name}: {detail}")

    def show(res):
        return f"{res.verdict} {[(f.category, f.preview) for f in res.findings]}"

    with TestClient(app) as c:
        for run in range(1, args.runs + 1):
            print(f"\n=== run {run}")
            seed(get_store())
            get_store()._data["rag_docs"].clear()
            rag.set_rag_provider(rag.FakeRagProvider())

            r = guardrail.scan(CLEAN, "document")
            check(run, "1 clean doc", r.verdict == "clean", show(r))
            r = guardrail.scan(PII, "document")
            check(run, "2 personal email + phone", r.verdict == "blocked" and {"EMAIL", "PHONE"} <= set(r.categories), show(r))
            pii_findings = r.findings
            r = guardrail.scan(SECRETS, "document")
            check(run, "3 AKIA key + card", r.verdict == "blocked" and {"AWS_ACCESS_KEY", "CARD_NUMBER"} <= set(r.categories), show(r))
            r = guardrail.scan(ORG_FACTS, "document")
            fps = [f"{f.category} {f.raw!r} ({f.source})" for f in r.findings]   # org facts only: safe to print
            false_positives += [f"run {run}: {x}" for x in fps]
            check(run, "4 org facts only", r.verdict == "clean", "clean" if not fps else f"FALSE POSITIVES {fps}")
            r = guardrail.scan(SANDBOX, "document")
            check(run, "5 personal sandbox id in prose (recorded, not required)", True,
                  "flagged " + str(r.categories) if r.verdict == "blocked" else "NOT flagged")

            # 6. the BigQuery skill
            [sk] = c.post("/api/skills/extract", json={"session_id": BIGQUERY_SESSION_ID, "employee_id": "e_dana"}).json()["skills"]
            c.post(f"/api/skills/{sk['id']}/approve", json={"employee_id": "e_dana"})
            res = c.post(f"/api/skills/{sk['id']}/share", json={"employee_id": "e_dana"}).json()
            check(run, "6a BigQuery skill shares clean", res["verdict"] == "clean",
                  f"{res['verdict']} {[(f['category'], f['preview']) for f in res['findings']]}")
            patched = c.patch(f"/api/skills/{sk['id']}", json={"employee_id": "e_dana",
                              "description": sk["description"] + " Questions: dana.kim@gmail.com"})
            obj = Skill.model_validate({**sk, "description": sk["description"] + " Questions: dana.kim@gmail.com"})
            r = guardrail.scan(skill_text(obj), "skill")
            check(run, "6b skill + personal email blocked", patched.status_code == 400 and r.verdict == "blocked"
                  and "EMAIL" in r.categories, f"PATCH {patched.status_code} (edit check); share scan {show(r)}")

            # 7. redacted copy of case 2
            redacted = guardrail.redact(PII, pii_findings)
            r = guardrail.scan(redacted, "document")
            check(run, "7 redacted copy re-scans clean", r.verdict == "clean" and "gmail" not in redacted, show(r))

            # 8. end to end: Dana shares skill + doc; Priya cites both; unshare; edit reverts
            doc = c.post("/api/rag/docs", json={"employee_id": "e_dana", "title": "Staging freeze", "text": CLEAN}).json()
            c.get("/api/rag/docs?employee_id=e_dana")
            shared_doc = c.post(f"/api/rag/docs/{doc['id']}/share", json={"employee_id": "e_dana"}).json()
            sk_id = sk["id"]
            # re-share the skill (6b's PATCH was rejected, so it is still shared; make sure)
            if c.get("/api/skills?employee_id=e_dana").json() and \
               next(s for s in c.get("/api/skills?employee_id=e_dana").json() if s["id"] == sk_id)["scope"] != "org":
                c.post(f"/api/skills/{sk_id}/share", json={"employee_id": "e_dana"})

            def priya(q, **flags):
                sid = c.post("/api/sessions", json={"employee_id": "e_priya", "agent_id": "a_dev"}).json()["id"]
                return c.post(f"/api/sessions/{sid}/messages", json={"content": q, **flags}).json()

            a1 = priya(SKILL_Q, use_skills=True)
            a2 = priya(DOC_Q, use_rag=True)
            attributed = "Dana" in a1["answer"]
            check(run, "8a Priya cites Dana's shared skill and doc",
                  shared_doc["verdict"] == "clean" and a1["message"]["cited_skill_ids"] == [sk_id]
                  and a2["message"]["cited_doc_ids"] == [doc["id"]],
                  f"skill cited={a1['message']['cited_skill_ids'] == [sk_id]} (answer names Dana: {attributed}); "
                  f"doc cited={a2['message']['cited_doc_ids'] == [doc['id']]}")
            c.post(f"/api/rag/docs/{doc['id']}/unshare", json={"employee_id": "e_dana"})
            c.post(f"/api/skills/{sk_id}/unshare", json={"employee_id": "e_dana"})
            b1, b2 = priya(SKILL_Q, use_skills=True), priya(DOC_Q, use_rag=True)
            check(run, "8b after unshare Priya cites neither",
                  b1["message"]["cited_skill_ids"] == [] and b2["message"]["cited_doc_ids"] == [],
                  f"skill={b1['message']['cited_skill_ids']} doc={b2['message']['cited_doc_ids']}")
            c.post(f"/api/skills/{sk_id}/share", json={"employee_id": "e_dana"})
            e = c.patch(f"/api/skills/{sk_id}", json={"employee_id": "e_dana", "name": "Run a BigQuery query from the CLI"}).json()
            check(run, "8c editing a shared skill reverts it to private", e.get("scope") == "private" and e.get("unshared") is True,
                  f"scope={e.get('scope')} message={e.get('message')!r}")

            # 9. simulated Gemini failure
            llm = get_llm()
            real = llm.generate_json

            def boom(*a, **k):
                raise RuntimeError("simulated Vertex outage")
            llm.generate_json = boom
            try:
                r = guardrail.scan(CLEAN, "document")
            finally:
                llm.generate_json = real
            check(run, "9 Gemini failure fails closed", r.verdict == "error" and "Couldn't verify" in r.message, r.verdict)

    print("\nVARIANCE (per case, one entry per run)")
    for name, details in table.items():
        print(f"  {name}: {'identical' if len(set(details)) == 1 else 'VARIES'}")
        if len(set(details)) > 1:
            for d in details:
                print(f"      {d}")
    print("\nFALSE POSITIVES on the org-facts doc:", json.dumps(false_positives) if false_positives else "none")
    print("GUARDRAIL EVAL", "PASS" if not failures else "FAIL")
    for f in failures:
        print("  -", f)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
