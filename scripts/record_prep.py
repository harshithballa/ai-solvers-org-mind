"""Put the live demo into the state for recording the video, or (--finish) into the judge-ready state afterwards.

    python -m scripts.record_prep [--base-url URL] [--admin-token-file PATH] [--finish] [--dry-run]

Recording (default):
  a. purge "test-" documents (prints scripts.purge_test_docs' dry run, then deletes)
  b. scripts.demo_prep --yes --no-skill --no-distill --clear-share-events --reset-usage
  c. write ~/test-contact.txt (synthetic email + phone), upload it as Dana the way the UI does (multipart file),
     wait until it is Ready and print how long that took
  d. scripts.clear_traces (dry run, then --yes): empties Admin > Traces and today's usage counters
  e. state report, checked against: ses_seed_14 present, Dana has 0 skills, Dana's docs = acme-bq-onboarding
     (shared) + test-contact (Ready, private), 0 playbooks, 0 share events
--finish (after recording; judge-ready):
  a. purge "test-" documents   b. scripts.demo_prep --yes --clear-share-events --reset-usage (default state)
  c. assert 2 playbooks, exactly 1 skill for Dana (no duplicate names), acme-bq-onboarding shared and Ready,
     no "test-" documents   d. scripts.clear_traces   e. state report plus one GET of the main URL

--dry-run prints the actions and changes nothing (no network calls, the token file is not read). Firestore steps use
--database (default "(default)"); the admin token is never printed. Exits 1 if a check fails.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from scripts.demo_prep import DEFAULT_TOKEN_FILE, DEFAULT_URL, DOC_TITLE, OWNER, SESSION

CONTACT_FILE = "~/test-contact.txt"
CONTACT_TITLE = "test-contact"
EMAIL, PHONE = "jordan.reyes.home@gmail.com", "+1 415 555 0142"   # synthetic, so the guardrail has PII to block
CONTACT_TEXT = (f"Laptop help contacts (synthetic): email {EMAIL} or call {PHONE} for hardware swaps. "
                "Swaps happen on Fridays at the Acme front desk.")
RAG_ENV = {"ORGMIND_RAG_PROVIDER": "vertex", "ORGMIND_RAG_DATASTORE": "orgmind-rag-datastore_1792251536297",
           "ORGMIND_RAG_BUCKET": "orgmind-rag-docs-arched-jetty", "ORGMIND_RAG_LOCATION": "global",
           "GOOGLE_CLOUD_PROJECT": "arched-jetty-502906-q6"}


def demo_prep_flags(finish: bool) -> list[str]:
    return ["--yes", "--clear-share-events", "--reset-usage"] + ([] if finish else ["--no-skill", "--no-distill"])


def actions(finish: bool) -> list[str]:
    purge = "purge test- documents (scripts.purge_test_docs dry run, then --yes)"
    prep = f"scripts.demo_prep {' '.join(demo_prep_flags(finish))}"
    traces = "scripts.clear_traces (dry run, then --yes): Admin > Traces and today's usage counters"
    if finish:
        return [purge, prep,
                f"assert 2 playbooks, exactly 1 skill for Dana (no duplicates), {DOC_TITLE} shared and Ready, "
                "no test- documents",
                traces, "print the state report and GET the main URL"]
    return [purge, prep,
            f"write {CONTACT_FILE} (synthetic email + phone), upload it as Dana (multipart, like the UI), wait until Ready",
            traces,
            f"print the state report (expect {SESSION}, 0 Dana skills, {DOC_TITLE} shared + {CONTACT_TITLE} Ready "
            "private, 0 playbooks, 0 share events)"]


def run(module: str, *argv: str, env: dict | None = None) -> None:
    print(f"   $ python -m {module} {' '.join(argv)}", flush=True)
    r = subprocess.run([sys.executable, "-m", module, *argv], env={**os.environ, **(env or {})})
    if r.returncode:
        sys.exit(f"   failed with exit code {r.returncode}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=DEFAULT_URL)
    ap.add_argument("--admin-token-file", default=DEFAULT_TOKEN_FILE)
    ap.add_argument("--finish", action="store_true", help="judge-ready state (run after recording)")
    ap.add_argument("--dry-run", action="store_true", help="print the actions without changing anything")
    ap.add_argument("--database", default="(default)", help="Firestore database (purge, share events, traces)")
    ap.add_argument("--index-timeout", type=int, default=900)
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    print(f"target: {base}  mode: {'finish (judge-ready)' if args.finish else 'recording'}  database: {args.database!r}")
    for letter, a in zip("abcde", actions(args.finish)):
        print(f"  {letter}. {a}")
    if args.dry_run:
        print("dry run: nothing was changed")
        return 0
    token = Path(args.admin_token_file).expanduser().read_text().strip()
    if not token:
        sys.exit("admin token file is empty")

    def call(method, path, body=None, data=None, content_type="application/json"):
        payload = data if data is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(base + path, method=method, data=payload,
                                     headers={"Content-Type": content_type, "X-Admin-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            sys.exit(f"{method} {path} failed: {e.code} {e.read().decode()[:300]}")

    purge_env = {**RAG_ENV, "ORGMIND_STORE": "firestore", "ORGMIND_FIRESTORE_DATABASE": args.database}
    db = ["--database", args.database]
    t0 = time.time()

    print("\na. purge test- documents")
    run("scripts.purge_test_docs", env=purge_env)
    run("scripts.purge_test_docs", "--yes", env=purge_env)

    print("\nb. demo_prep")
    run("scripts.demo_prep", "--base-url", base, "--admin-token-file", args.admin_token_file,
        *demo_prep_flags(args.finish), *db)

    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(f"   {'OK  ' if ok else 'FAIL'} {what}")
        if not ok:
            failures.append(what)

    def dana_docs():
        return [d for d in call("GET", f"/api/rag/docs?employee_id={OWNER}") if d["owner_employee_id"] == OWNER]

    def dana_skills():
        return [s for s in call("GET", f"/api/skills?employee_id={OWNER}") if s["owner_employee_id"] == OWNER]

    if args.finish:
        print("\nc. assert the judge-ready state")
        pbs = call("GET", "/api/playbooks")
        check(len(pbs) == 2, f"2 playbooks: {[(p['title'], p['version']) for p in pbs]}")
        live = [s for s in dana_skills() if s["status"] != "archived"]
        names = [s["name"].strip().lower() for s in live]
        check(len(live) == 1 and len(set(names)) == len(names), f"exactly 1 skill for Dana: {[s['name'] for s in live]}")
        docs = dana_docs()
        bq = [d for d in docs if d["title"] == DOC_TITLE]
        check(len(bq) == 1 and bq[0]["scope"] == "org" and bq[0]["status"] == "ready",
              f"{DOC_TITLE} shared and Ready: {[(d['status'], d['scope']) for d in bq]}")
        tests = [d["title"] for d in docs if d["title"].lower().startswith("test-")]
        check(not tests, f"no test- documents: {tests}")
    else:
        print(f"\nc. upload {CONTACT_TITLE}.txt as Dana")
        path = Path(CONTACT_FILE).expanduser()
        path.write_text(CONTACT_TEXT)
        print(f"   wrote {path}:\n   {CONTACT_TEXT}")
        boundary = uuid.uuid4().hex
        form = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"employee_id\"\r\n\r\n{OWNER}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
                f"Content-Type: text/plain\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        start = time.time()
        doc = call("POST", "/api/rag/docs", data=form, content_type=f"multipart/form-data; boundary={boundary}")
        print(f"   uploaded {doc['id']} ({doc['status']})")
        while doc["status"] not in ("ready", "failed"):
            if time.time() - start > args.index_timeout:
                sys.exit("   timed out waiting for Ready")
            time.sleep(5)
            doc = next(d for d in dana_docs() if d["id"] == doc["id"])
        if doc["status"] != "ready":
            sys.exit(f"   document failed: {doc.get('error')}")
        print(f"   Ready after {time.time() - start:.1f}s")

    print("\nd. clear traces")
    project = {"GOOGLE_CLOUD_PROJECT": RAG_ENV["GOOGLE_CLOUD_PROJECT"]}
    run("scripts.clear_traces", *db, env=project)
    run("scripts.clear_traces", *db, "--yes", env=project)

    print("\ne. state report")
    chats = call("GET", f"/api/sessions?employee_id={OWNER}")
    print(f"   Dana's chats ({len(chats)}): {[(c['id'], c['title'], c['status']) for c in chats]}")
    check(any(c["id"] == SESSION for c in chats), f"{SESSION} present")
    skills = dana_skills()
    print(f"   Dana's skills ({len(skills)}): {[(s['name'], s['status'], s['scope']) for s in skills]}")
    docs = dana_docs()
    print(f"   Dana's docs: {[(d['title'], d['status'], d['scope']) for d in docs]}")
    pbs = call("GET", "/api/playbooks")
    print(f"   playbooks ({len(pbs)}): {[(p['title'], p['version']) for p in pbs]}")
    events = call("GET", "/api/guardrail/summary")["events"]
    print(f"   share events: {len(events)}")
    if not args.finish:
        check(not skills, "Dana has 0 skills")
        got = sorted((d["title"], d["status"], d["scope"]) for d in docs)
        check(got == [(DOC_TITLE, "ready", "org"), (CONTACT_TITLE, "ready", "private")],
              f"Dana's docs = {DOC_TITLE} shared + {CONTACT_TITLE} Ready and private")
        check(not pbs, "0 playbooks")
        check(not events, "0 share events")
    traces = call("GET", "/api/traces?limit=200")["calls"]
    print(f"   traces: {len(traces)}")
    print(f"   usage: {json.dumps(call('GET', '/api/usage'))}")
    health = call("GET", "/api/health")
    print(f"   health: ok={health['ok']} model={health['chat_model']} store={health['store']} limits={health['limits']}")
    if args.finish:
        with urllib.request.urlopen(base + "/", timeout=60) as r:
            check(r.status == 200, f"GET {base}/ -> {r.status}")
    print(f"\n{'all checks passed' if not failures else f'{len(failures)} check(s) FAILED'} in {time.time() - t0:.0f}s")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
