"""Prepare the demo state on a running OrgMind server. Dry run by default (prints the steps).

    python -m scripts.demo_prep [--base-url URL] [--admin-token-file PATH] [--yes]

--base-url defaults to the live service (DEFAULT_URL), --admin-token-file to ~/.config/orgmind/admin_token.

With --yes, idempotently: reset demo, distill, upload "acme-bq-onboarding.txt" as Dana (reused if it already
exists and isn't failed), wait for Ready, learn skills from Dana's BigQuery chat (ses_seed_14), approve the skill,
then share the doc and the skill with the org (both go through the guardrail). Prints the final state.
All requests carry the admin token, so they are not counted against the demo cost caps.

For a live recording, skip the parts you want to show live (the reset already removes skills and playbooks):
    --no-skill      no skill is extracted/approved/shared; checks that no employee has a skill. Dana's closed
                    BigQuery chat (ses_seed_14) stays, so "Learn skills from this chat" can be run live.
    --no-distill    no distill; checks that there are no playbooks (Admin > Knowledge shows its empty state).
    --clear-share-events  delete every share_events document (the audit log a reset keeps), so Admin > Guardrails
                    starts empty. share_events has no API, so this talks to Firestore directly (database
                    --database, default "(default)"; the id is re-checked before every delete, as in clear_traces).
The shared document acme-bq-onboarding is prepared either way.
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
DEFAULT_URL = "https://orgmind-207900876373.us-central1.run.app"
DEFAULT_TOKEN_FILE = "~/.config/orgmind/admin_token"
BATCH = 400


def steps(no_skill=False, no_distill=False, clear_share_events=False):
    return [
        "reset the demo (POST /api/demo/reset)",
        *(["delete every share_events document (Firestore; Guardrails starts empty)"] if clear_share_events else []),
        "skip distill; check there are no playbooks" if no_distill else "distill sessions into playbooks (POST /api/distill)",
        f"as Dana, upload {DOC_TITLE}.txt unless a non-failed copy exists, and wait until it is Ready",
        f"skip the skill; check no employee has a skill ({SESSION} stays for a live 'Learn skills')" if no_skill
        else f"as Dana, learn skills from {SESSION} and approve the BigQuery skill (reuses an active one if present)",
        "share the document with the org (guardrail scan; stops if blocked)" if no_skill
        else "share the document and the skill with the org (guardrail scan; stops if blocked)",
        "print the final state",
    ]


def clear_share_events(database: str) -> int:
    """Delete all share_events documents in the named Firestore database. Returns how many were deleted."""
    import os

    from google.cloud import firestore

    from scripts.clear_traces import guard
    client = firestore.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT", "arched-jetty-502906-q6"), database=database)
    guard(client, database)
    refs = [d.reference for d in client.collection("share_events").select([]).stream()]
    for start in range(0, len(refs), BATCH):
        guard(client, database)
        batch = client.batch()
        for ref in refs[start:start + BATCH]:
            batch.delete(ref)
        batch.commit()
    return len(refs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=DEFAULT_URL)
    ap.add_argument("--admin-token-file", default=DEFAULT_TOKEN_FILE)
    ap.add_argument("--yes", action="store_true", help="actually run (default: dry run)")
    ap.add_argument("--index-timeout", type=int, default=900)
    ap.add_argument("--no-skill", action="store_true", help="leave no skills (learn the BigQuery skill live)")
    ap.add_argument("--no-distill", action="store_true", help="leave no playbooks (run Distill live)")
    ap.add_argument("--clear-share-events", action="store_true", help="delete the share audit log (Guardrails starts empty)")
    ap.add_argument("--database", default="(default)", help="Firestore database for --clear-share-events")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    print(f"target: {base}")
    for i, s in enumerate(steps(args.no_skill, args.no_distill, args.clear_share_events), 1):
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
    if args.clear_share_events:
        print(f"   share_events deleted: {clear_share_events(args.database)} (database {args.database!r})")
    if args.no_distill:
        pbs = call("GET", "/api/playbooks")
        print(f"2. distill skipped: {len(pbs)} playbooks")
        if pbs:
            sys.exit("   expected no playbooks after the reset")
    else:
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

    employees = [e["id"] for e in call("GET", "/api/org")["employees"]]
    skill = None
    if args.no_skill:
        left = {e: len(call("GET", f"/api/skills?employee_id={e}")) for e in employees}
        print(f"4. skill skipped: skills per employee {left}")
        if any(left.values()):
            sys.exit("   expected no skills after the reset")
        session = call("GET", f"/api/sessions/{SESSION}")
        print(f"   {SESSION} kept ({session['session']['status']}, {len(session['messages'])} messages)")
    else:
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
    items = [("doc", doc, f"/api/rag/docs/{doc['id']}")] + ([("skill", skill, f"/api/skills/{skill['id']}")] if skill else [])
    for kind, item, path in items:
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
    print(f"   skills (all employees): {sum(len(call('GET', f'/api/skills?employee_id={e}')) for e in employees)}")
    print(f"   chats: {sum(len(call('GET', f'/api/sessions?employee_id={e}')) for e in employees)}")
    print(f"done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
