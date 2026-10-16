"""Persistence check: FirestoreStore data must survive an app restart.

Runs two separate uvicorn processes against a *test* Firestore database (never "(default)"),
using real Gemini, then wipes the test database.

    GOOGLE_CLOUD_PROJECT=... python -m scripts.firestore_persistence_check --database orgmind-test
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

PORT = 8091
BASE = f"http://127.0.0.1:{PORT}"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def start_app(env: dict) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for _ in range(120):
        try:
            call("GET", "/api/health")
            return proc
        except (urllib.error.URLError, ConnectionError):
            if proc.poll() is not None:
                raise RuntimeError(f"app exited during startup:\n{proc.stderr.read()[-2000:]}")
            time.sleep(0.5)
    raise RuntimeError("app did not become healthy")


def stop_app(proc: subprocess.Popen):
    proc.terminate()
    proc.wait(timeout=30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", default="orgmind-test")
    ap.add_argument("--keep", action="store_true", help="don't wipe the test database afterwards")
    args = ap.parse_args()
    if args.database == "(default)":
        sys.exit("refusing to run against the (default) demo database")

    token = secrets.token_urlsafe(24)  # throwaway, never printed
    env = {**os.environ, "ORGMIND_STORE": "firestore", "ORGMIND_FIRESTORE_DATABASE": args.database,
           "ORGMIND_LLM": "vertex", "ORGMIND_ADMIN_TOKEN": token}
    admin = {"X-Admin-Token": token}

    # ---- process 1: seed, distill, chat
    proc = start_app(env)
    try:
        call("POST", "/api/demo/reset", {}, admin)
        r1 = call("POST", "/api/distill", {})
        check("process 1: distill creates playbooks", r1["playbooks_created"] == 2,
              f"created={r1['playbooks_created']} summarized={r1['sessions_summarized']} {r1['timings_s']}")
        pbs1 = {p["id"]: (p["title"], p["version"]) for p in call("GET", "/api/playbooks")}
        sid = call("POST", "/api/sessions", {"employee_id": "e_noah", "agent_id": "a_hr"})["id"]
        reply = call("POST", f"/api/sessions/{sid}/messages",
                     {"content": "My home-office stipend claim was rejected with 'Missing cost center'. What now?"})
        check("process 1: chat cites playbook", len(reply["cited"]) == 1)
        n_sessions1 = len(call("GET", "/api/sessions"))
    finally:
        stop_app(proc)

    # ---- direct store read between restarts: embeddings and summaries are really in Firestore
    from app.models import Playbook, Session
    from app.store import FirestoreStore

    store = FirestoreStore(os.environ["GOOGLE_CLOUD_PROJECT"], args.database)
    pbs = store.list(Playbook, org_id="acme")
    check("firestore: playbook embeddings + centroids stored",
          len(pbs) == 2 and all(len(p.embedding) == 768 and len(p.centroid) == 768 for p in pbs),
          f"dims={[(len(p.embedding), len(p.centroid)) for p in pbs]}")
    summarized = [s for s in store.list(Session, org_id="acme") if s.summary and len(s.summary.embedding) == 768]
    check("firestore: session summaries + embeddings stored", len(summarized) == 14, f"summarized={len(summarized)}")

    # ---- process 2: everything still there after restart
    proc = start_app(env)
    try:
        pbs2 = {p["id"]: (p["title"], p["version"]) for p in call("GET", "/api/playbooks")}
        check("process 2: playbooks survived restart", pbs2 == pbs1, f"{len(pbs2)} playbooks")
        s2 = call("GET", f"/api/sessions/{sid}")
        msgs = s2["messages"]
        check("process 2: session + messages survived restart",
              len(msgs) == 2 and msgs[1]["cited_playbook_ids"] == [reply["cited"][0]["id"]], f"messages={len(msgs)}")
        check("process 2: session list survived", len(call("GET", "/api/sessions")) == n_sessions1, f"{n_sessions1} sessions")
        cmp = call("POST", "/api/compare", {"agent_id": "a_dev",
                                            "question": "My staging deploy fails with ACME_VAULT_TOKEN expired. Fix?"})
        check("process 2: retrieval works from stored embeddings", len(cmp["with_playbooks"]["cited"]) == 1,
              f"retrieved={[(r['title'][:40], r['score']) for r in cmp['with_playbooks']['retrieved']]}")
        r2 = call("POST", "/api/distill", {})
        check("process 2: cached summaries reused (only the new chat summarized)", r2["sessions_summarized"] == 1,
              f"summarized={r2['sessions_summarized']} created={r2['playbooks_created']} "
              f"updated={r2['playbooks_updated']} {r2['timings_s']}")
        check("process 2: no duplicate playbooks", len(call("GET", "/api/playbooks")) == 2)
    finally:
        stop_app(proc)

    # ---- cleanup
    if not args.keep:
        store.reset()
        left = {c: len(list(store._db.collection(c).limit(1).stream()))
                for c in ("orgs", "employees", "agents", "sessions", "playbooks")}
        msgs_left = len(list(store._db.collection_group("messages").limit(1).stream()))
        check("cleanup: test database empty", not any(left.values()) and msgs_left == 0, f"{left} messages={msgs_left}")

    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
