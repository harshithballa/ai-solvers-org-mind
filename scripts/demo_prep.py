"""Prepare the demo state on a running OrgMind server. Dry run by default (prints the steps).

    python scripts/demo_prep.py --base-url http://localhost:8080 --admin-token-file ~/.orgmind_admin_token [--yes]

With --yes, idempotently: reset demo, distill, upload "acme-bq-onboarding.txt" as Dana (reused if it already
exists and isn't failed), wait for Ready, learn skills from Dana's BigQuery chat (ses_seed_14), approve the skill,
then share the doc and the skill with the org (both go through the guardrail). Prints the final state.
All requests carry the admin token, so they are not counted against the demo cost caps.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DOC_TITLE = "acme-bq-onboarding"   # uploaded as acme-bq-onboarding.txt
DOC_TEXT = """Acme analytics onboarding: querying BigQuery from your laptop (synthetic).

1. Authenticate once with Application Default Credentials: gcloud auth application-default login
   (service-account key files are not allowed on laptops).
2. You can read the shared dataset acme-analytics.events.orders, but query jobs must run in a project you may
   bill. Pass your own sandbox project: bq query --project_id=<your sandbox project> ...
3. bq query defaults to legacy SQL. Add --use_legacy_sql=false and wrap table names in backticks.
4. Questions go to #data-platform.
"""
OWNER, SESSION = "e_dana", "ses_seed_14"
STEPS = [
    "reset the demo (POST /api/demo/reset)",
    "distill sessions into playbooks (POST /api/distill)",
    f"as Dana, upload {DOC_TITLE}.txt unless a non-failed copy exists, and wait until it is Ready",
    f"as Dana, learn skills from {SESSION} and approve the BigQuery skill (reuses an active one if present)",
    "share the document and the skill with the org (guardrail scan; stops if blocked)",
    "print the final state",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--admin-token-file", required=True)
    ap.add_argument("--yes", action="store_true", help="actually run (default: dry run)")
    ap.add_argument("--index-timeout", type=int, default=900)
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    print(f"target: {base}")
    for i, s in enumerate(STEPS, 1):
        print(f"  {i}. {s}")
    if not args.yes:
        print("dry run: pass --yes to execute")
        return 0
    token = Path(args.admin_token_file).expanduser().read_text().strip()
    if not token:
        sys.exit("admin token file is empty")

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-Admin-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            sys.exit(f"{method} {path} failed: {e.code} {e.read().decode()[:300]}")

    t0 = time.time()
    print("\n1. reset"); call("POST", "/api/demo/reset")
    print("2. distill"); rep = call("POST", "/api/distill", {})
    print(f"   playbooks created={rep['playbooks_created']} updated={rep['playbooks_updated']}")

    print("3. document")
    docs = [d for d in call("GET", f"/api/rag/docs?employee_id={OWNER}")
            if d["owner_employee_id"] == OWNER and d["title"] == DOC_TITLE and d["status"] != "failed"]
    doc = docs[0] if docs else call("POST", "/api/rag/docs", {"employee_id": OWNER, "title": DOC_TITLE, "text": DOC_TEXT})
    print(f"   {'reusing' if docs else 'uploaded'} {doc['id']} ({doc['status']})")
    start = time.time()
    while doc["status"] not in ("ready", "failed"):
        if time.time() - start > args.index_timeout:
            sys.exit("   timed out waiting for Ready")
        time.sleep(10)
        doc = next(d for d in call("GET", f"/api/rag/docs?employee_id={OWNER}") if d["id"] == doc["id"])
        print(f"   {doc['status']} after {time.time() - start:.0f}s")
    if doc["status"] != "ready":
        sys.exit(f"   document failed: {doc.get('error')}")

    print("4. skill")
    mine = [s for s in call("GET", f"/api/skills?employee_id={OWNER}")
            if s["owner_employee_id"] == OWNER and s["source_session_id"] == SESSION and s["status"] == "active"]
    if mine:
        skill = mine[0]
        print(f"   reusing active skill {skill['id']} {skill['name']!r}")
    else:
        found = call("POST", "/api/skills/extract", {"session_id": SESSION, "employee_id": OWNER})["skills"]
        if not found:
            sys.exit("   no skill extracted from the BigQuery chat")
        skill = call("POST", f"/api/skills/{found[0]['id']}/approve", {"employee_id": OWNER})
        print(f"   extracted and approved {skill['id']} {skill['name']!r}")

    print("5. share")
    for kind, item, path in (("doc", doc, f"/api/rag/docs/{doc['id']}"), ("skill", skill, f"/api/skills/{skill['id']}")):
        if item.get("scope") == "org":
            print(f"   {kind} already shared")
            continue
        res = call("POST", f"{path}/share", {"employee_id": OWNER})
        print(f"   {kind}: {res['verdict']} {[(f['category'], f['preview']) for f in res['findings']]}")
        if res["verdict"] != "clean":
            sys.exit(f"   {kind} was not shared: {res.get('message')}")

    print("6. final state")
    pbs = call("GET", "/api/playbooks")
    print(f"   playbooks: {[p['title'] for p in pbs]}")
    for d in call("GET", f"/api/rag/docs?employee_id={OWNER}"):
        print(f"   doc   {d['title']!r} {d['status']} scope={d['scope']} shared_by={d.get('shared_by_name')}")
    for s in call("GET", f"/api/skills?employee_id={OWNER}"):
        print(f"   skill {s['name']!r} {s['status']} scope={s['scope']} shared_by={s.get('shared_by_name')}")
    print(f"done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
