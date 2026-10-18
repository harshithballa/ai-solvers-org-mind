"""Put the live demo into the state for recording the video, or (--finish) into the judge-ready state afterwards.

    python -m scripts.record_prep [--base-url URL] [--admin-token-file PATH] [--live-docs | --finish] [--dry-run]

Recording (default): a full knowledge reset for ALL employees, then one private document staged.
  a. reset the demo (POST /api/demo/reset): wipes skills and playbooks, re-seeds the 15 closed sessions
  b. delete share_events and today's usage counters (Firestore, as demo_prep --clear-share-events --reset-usage)
  c. delete EVERY document: rag_docs row + Cloud Storage object + Vertex AI Search document
     (scripts.purge_test_docs --all, then --orphans for anything left in the bucket or index; dry runs first)
  d. check: 0 documents, 0 skills, 0 playbooks, 0 share events, 15 sessions, ses_seed_14 closed
  e. upload acme-bq-onboarding as Dana (private, NOT shared) and wait until Ready (--index-timeout, default 180s)
     --live-docs skips this, so there are no documents at all
  f. probe: Liam asks the Dev agent a BigQuery question with only Documents on (expect no citation while the doc
     is private to Dana), then reset again so the probe chat is gone (documents survive a reset)
  g. scripts.clear_traces (dry run, then --yes): Admin > Traces and today's usage counters
  h. state report
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
from pathlib import Path

from scripts.demo_prep import (DEFAULT_TOKEN_FILE, DEFAULT_URL, DOC_TEXT, DOC_TITLE, OWNER, SESSION, clear_share_events,
                               reset_usage)

RAG_ENV = {"ORGMIND_RAG_PROVIDER": "vertex", "ORGMIND_RAG_DATASTORE": "orgmind-rag-datastore_1792251536297",
           "ORGMIND_RAG_BUCKET": "orgmind-rag-docs-arched-jetty", "ORGMIND_RAG_LOCATION": "global",
           "GOOGLE_CLOUD_PROJECT": "arched-jetty-502906-q6"}
SEEDED_SESSIONS = 15
PROBE_EMPLOYEE, PROBE_AGENT = "e_liam", "a_dev"
PROBE_QUESTION = "how do I get started with bq at acme?"


def demo_prep_flags() -> list[str]:
    return ["--yes", "--clear-share-events", "--reset-usage"]


def actions(finish: bool, live_docs: bool = False) -> list[str]:
    traces = "scripts.clear_traces (dry run, then --yes): Admin > Traces and today's usage counters"
    if finish:
        return ["purge test- documents (scripts.purge_test_docs dry run, then --yes)",
                f"scripts.demo_prep {' '.join(demo_prep_flags())}",
                f"assert 2 playbooks, exactly 1 skill for Dana (no duplicates), {DOC_TITLE} shared and Ready, "
                "no test- documents",
                traces, "print the state report and GET the main URL"]
    return ["reset the demo (skills and playbooks wiped, 15 seeded sessions re-created)",
            "delete every share_events document and today's usage counters",
            "delete EVERY document for all employees: row, Cloud Storage object and Vertex AI Search document "
            "(purge_test_docs --all, then --orphans; dry runs first)",
            f"check 0 documents, 0 skills, 0 playbooks, 0 share events, {SEEDED_SESSIONS} sessions, {SESSION} closed",
            "no document staged (--live-docs)" if live_docs
            else f"upload {DOC_TITLE} as Dana (private, not shared) and wait until Ready",
            f"probe: Liam asks {PROBE_QUESTION!r} with only Documents on, then reset to drop the probe chat",
            traces, "print the state report"]


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
    ap.add_argument("--live-docs", action="store_true", help="recording: stage no document (upload it live)")
    ap.add_argument("--dry-run", action="store_true", help="print the actions without changing anything")
    ap.add_argument("--database", default="(default)", help="Firestore database (purge, share events, traces)")
    ap.add_argument("--index-timeout", type=int, default=180)
    args = ap.parse_args()
    if args.finish and args.live_docs:
        sys.exit("--live-docs applies to the recording mode only")
    base = args.base_url.rstrip("/")
    mode = "finish (judge-ready)" if args.finish else "recording" + (" --live-docs" if args.live_docs else "")
    print(f"target: {base}  mode: {mode}  database: {args.database!r}")
    for letter, a in zip("abcdefgh", actions(args.finish, args.live_docs)):
        print(f"  {letter}. {a}")
    if args.dry_run:
        print("dry run: nothing was changed")
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

    purge_env = {**RAG_ENV, "ORGMIND_STORE": "firestore", "ORGMIND_FIRESTORE_DATABASE": args.database}
    db = ["--database", args.database]
    t0 = time.time()
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(f"   {'OK  ' if ok else 'FAIL'} {what}")
        if not ok:
            failures.append(what)

    employees = [e["id"] for e in call("GET", "/api/org")["employees"]]

    def all_docs():
        return list({d["id"]: d for e in employees for d in call("GET", f"/api/rag/docs?employee_id={e}")}.values())

    def all_skills():
        return list({s["id"]: s for e in employees for s in call("GET", f"/api/skills?employee_id={e}")}.values())

    def all_sessions():
        return [s for e in employees for s in call("GET", f"/api/sessions?employee_id={e}")]

    if args.finish:
        print("\na. purge test- documents")
        run("scripts.purge_test_docs", env=purge_env)
        run("scripts.purge_test_docs", "--yes", env=purge_env)
        print("\nb. demo_prep")
        run("scripts.demo_prep", "--base-url", base, "--admin-token-file", args.admin_token_file, *demo_prep_flags(), *db)
        print("\nc. assert the judge-ready state")
        pbs = call("GET", "/api/playbooks")
        check(len(pbs) == 2, f"2 playbooks: {[(p['title'], p['version']) for p in pbs]}")
        live = [s for s in all_skills() if s["owner_employee_id"] == OWNER and s["status"] != "archived"]
        names = [s["name"].strip().lower() for s in live]
        check(len(live) == 1 and len(set(names)) == len(names), f"exactly 1 skill for Dana: {[s['name'] for s in live]}")
        docs = all_docs()
        bq = [d for d in docs if d["title"] == DOC_TITLE]
        check(len(bq) == 1 and bq[0]["scope"] == "org" and bq[0]["status"] == "ready",
              f"{DOC_TITLE} shared and Ready: {[(d['status'], d['scope']) for d in bq]}")
        tests = [d["title"] for d in docs if d["title"].lower().startswith("test-")]
        check(not tests, f"no test- documents: {tests}")
    else:
        print("\na. reset"); call("POST", "/api/demo/reset")
        print("\nb. share events and usage counters")
        print(f"   share_events deleted: {clear_share_events(args.database)}")
        print(f"   usage_counters deleted: {reset_usage(args.database)}")
        print("\nc. delete every document (row, Cloud Storage object, Vertex AI Search document)")
        run("scripts.purge_test_docs", "--all", env=purge_env)
        run("scripts.purge_test_docs", "--all", "--yes", env=purge_env)
        run("scripts.purge_test_docs", "--orphans", *db, "--min-age", "0", env=purge_env)
        run("scripts.purge_test_docs", "--orphans", *db, "--min-age", "0", "--yes", env=purge_env)
        print("\nd. check the clean state")
        check(not all_docs(), "0 documents")
        check(not all_skills(), "0 skills (all employees)")
        check(not call("GET", "/api/playbooks"), "0 playbooks")
        check(not call("GET", "/api/guardrail/summary")["events"], "0 share events")
        sessions = all_sessions()
        check(len(sessions) == SEEDED_SESSIONS, f"{SEEDED_SESSIONS} sessions: {len(sessions)}")
        check(any(s["id"] == SESSION and s["status"] == "closed" for s in sessions), f"{SESSION} present and closed")

        if args.live_docs:
            print("\ne. no document staged (--live-docs)")
        else:
            print(f"\ne. upload {DOC_TITLE} as Dana")
            start = time.time()
            doc = call("POST", "/api/rag/docs", {"employee_id": OWNER, "title": DOC_TITLE, "text": DOC_TEXT})
            print(f"   uploaded {doc['id']} ({doc['status']}, scope={doc['scope']})")
            while doc["status"] not in ("ready", "failed"):
                if time.time() - start > args.index_timeout:
                    sys.exit(f"   not Ready after {args.index_timeout}s (still {doc['status']})")
                time.sleep(5)
                doc = next(d for d in call("GET", f"/api/rag/docs?employee_id={OWNER}") if d["id"] == doc["id"])
            if doc["status"] != "ready":
                sys.exit(f"   document failed: {doc.get('error')}")
            print(f"   Ready after {time.time() - start:.1f}s")

        print("\nf. probe: Liam, Documents only")
        ses = call("POST", "/api/sessions", {"employee_id": PROBE_EMPLOYEE, "agent_id": PROBE_AGENT})
        res = call("POST", f"/api/sessions/{ses['id']}/messages",
                   {"content": PROBE_QUESTION, "use_playbooks": False, "use_skills": False, "use_rag": True})
        probe = {"cited_docs": res["cited_docs"], "rag_retrieved": [h.get("title") for h in res["rag"]["retrieved"]],
                 "rag_error": res["rag"]["error"], "answer": " ".join(res["answer"].split())[:300]}
        call("POST", "/api/demo/reset")   # drops the probe chat; documents survive a reset
        print(f"   probe chat removed (reset); sessions now {len(all_sessions())}")

    print("\ng. clear traces" if not args.finish else "\nd. clear traces")
    project = {"GOOGLE_CLOUD_PROJECT": RAG_ENV["GOOGLE_CLOUD_PROJECT"]}
    run("scripts.clear_traces", *db, env=project)
    run("scripts.clear_traces", *db, "--yes", env=project)

    print("\nh. state report" if not args.finish else "\ne. state report")
    docs = all_docs()
    for d in docs:
        print(f"   document {d['title']!r} owner={d['owner_employee_id']} scope={d['scope']} status={d['status']}")
    print(f"   documents: {len(docs)}")
    skills, pbs = all_skills(), call("GET", "/api/playbooks")
    events = call("GET", "/api/guardrail/summary")["events"]
    sessions = all_sessions()
    print(f"   skills: {len(skills)}  playbooks: {len(pbs)}  share_events: {len(events)}  sessions: {len(sessions)}")
    if not args.finish:
        print(f"   probe (Liam, Documents only, {PROBE_QUESTION!r}): cited_docs={probe['cited_docs']} "
              f"rag_retrieved={probe['rag_retrieved']} rag_error={probe['rag_error']}")
        print(f"   probe answer: {probe['answer']}")
        want = [] if args.live_docs else [(DOC_TITLE, OWNER, "private", "ready")]
        check(sorted((d["title"], d["owner_employee_id"], d["scope"], d["status"]) for d in docs) == want,
              "documents: none" if args.live_docs else f"documents: only {DOC_TITLE} (Dana, private, Ready)")
        check(not skills and not pbs and not events, "0 skills, 0 playbooks, 0 share events")
        check(len(sessions) == SEEDED_SESSIONS and any(s["id"] == SESSION and s["status"] == "closed" for s in sessions),
              f"{SEEDED_SESSIONS} seeded sessions, {SESSION} closed")
    print(f"   traces: {len(call('GET', '/api/traces?limit=200')['calls'])}")
    print(f"   usage: {json.dumps(call('GET', '/api/usage')['buckets'])}")
    health = call("GET", "/api/health")
    print(f"   health: ok={health['ok']} model={health['chat_model']} store={health['store']} limits={health['limits']}")
    if args.finish:
        with urllib.request.urlopen(base + "/", timeout=60) as r:
            check(r.status == 200, f"GET {base}/ -> {r.status}")
    print(f"\n{'all checks passed' if not failures else f'{len(failures)} check(s) FAILED'} in {time.time() - t0:.0f}s")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
