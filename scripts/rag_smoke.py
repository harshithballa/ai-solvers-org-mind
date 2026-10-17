"""Smoke test against the REAL Vertex AI Search data store (your ADC), with an in-memory store
(never touches Firestore):

    python -m scripts.rag_smoke [--timeout 900] [--gemini]

Uploads a doc with a made-up fact, polls until status=ready (prints elapsed), polls search until the doc is
actually returned, prints the top snippet, checks the owner filter, and prints relevance scores for a
related and an unrelated query (to set ORGMIND_RAG_MIN_SCORE from data). --gemini also asks the real
Dev Agent with use_rag on (needs Vertex AI Gemini access).
"""
import argparse
import os
import secrets
import sys
import time

DEFAULTS = {
    "GOOGLE_CLOUD_PROJECT": "arched-jetty-502906-q6",
    "ORGMIND_RAG_DATASTORE": "orgmind-rag-datastore_1792251536297",
    "ORGMIND_RAG_BUCKET": "orgmind-rag-docs-arched-jetty",
    "ORGMIND_RAG_LOCATION": "global",
}
OWNER, OTHER = "e_dana", "e_noah"
UNRELATED = "how do I reset my gym membership"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=900, help="seconds to wait for ready + searchable")
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--gemini", action="store_true", help="also run a grounded chat with real Gemini")
    args = ap.parse_args()

    for k, v in DEFAULTS.items():
        os.environ.setdefault(k, v)
    os.environ.update({"ORGMIND_STORE": "memory", "ORGMIND_RAG_PROVIDER": "vertex", "ORGMIND_TRACING": "off",
                       "ORGMIND_LLM": "vertex" if args.gemini else "fake"})

    from fastapi.testclient import TestClient

    from app import rag_docs
    from app.main import app
    from app.rag import get_rag_provider
    from app.seed import ORG_ID

    code = secrets.token_hex(3)
    fact_q = "What is the Zephyr-9 cold-storage rotation codeword?"
    text = (f"Acme Platform runbook (synthetic). The Zephyr-9 cold-storage rotation codeword for Q4 is "
            f"'marmalade-otter-{code}'. Rotate the Zephyr-9 keys every 45 days by running "
            f"`acme zephyr rotate --cold`. Escalations go to #zephyr-oncall; page the duty lead via PagerDuty "
            f"service 'zephyr-cold'. Never paste the codeword in public channels.")
    ok = True

    with TestClient(app) as client:
        print(f"provider={get_rag_provider().name} datastore={os.environ['ORGMIND_RAG_DATASTORE']} "
              f"min_score={os.environ.get('ORGMIND_RAG_MIN_SCORE', '0.5')}")
        t0 = time.monotonic()
        res = client.post("/api/rag/docs", json={"employee_id": OWNER, "title": f"Zephyr-9 runbook {code}", "text": text})
        if res.status_code != 200:
            print("upload failed:", res.status_code, res.text)
            return 1
        doc = res.json()
        print(f"uploaded {doc['id']} -> {doc['gcs_uri']} status={doc['status']} ({time.monotonic() - t0:.1f}s)")
        print(f"  import operation: {doc['import_operation']}")

        status, t_ready = doc["status"], None
        while time.monotonic() - t0 < args.timeout:
            rows = {d["id"]: d for d in client.get(f"/api/rag/docs?employee_id={OWNER}").json()}
            row = rows[doc["id"]]
            if row["status"] != status:
                status = row["status"]
                print(f"  [{time.monotonic() - t0:6.1f}s] status -> {status} {row.get('error') or ''}")
            if status in ("ready", "failed"):
                t_ready = time.monotonic() - t0
                break
            time.sleep(args.interval)
        if status != "ready":
            print(f"FAIL: status={status} after {time.monotonic() - t0:.0f}s")
            return 1
        print(f"READY after {t_ready:.1f}s (import operation done)")

        # Ready means the import finished; check how long until search actually returns the doc.
        hit, t_search = None, None
        while time.monotonic() - t0 < args.timeout:
            results = client.post("/api/rag/search", json={"employee_id": OWNER, "query": fact_q}).json()["results"]
            hit = next((r for r in results if r["doc_id"] == doc["id"]), None)
            if hit:
                t_search = time.monotonic() - t0
                break
            time.sleep(args.interval)
        if not hit:
            print(f"FAIL: doc never appeared in search within {args.timeout}s")
            return 1
        print(f"SEARCHABLE after {t_search:.1f}s (ready +{t_search - t_ready:.1f}s)")
        print(f"top hit: score={hit['score']} source={hit['source']} title={hit['title']!r}")
        print("snippet:\n  " + hit["snippet"].replace("\n", "\n  "))
        if f"marmalade-otter-{code}" not in hit["snippet"]:
            print("WARN: the distinctive fact is not in the snippet")
            ok = False

        provider = get_rag_provider()
        for label, q in (("related", fact_q), ("unrelated", UNRELATED)):
            t1 = time.monotonic()
            raw = provider.search(q)
            print(f"raw scores [{label}] {q!r} ({(time.monotonic() - t1) * 1000:.0f}ms, {len(raw)} results):")
            for h in raw:
                print(f"  {h.score if h.score is None else round(h.score, 3)}  {h.source:<18} {h.uri}")
        unrelated = client.post("/api/rag/search", json={"employee_id": OWNER, "query": UNRELATED}).json()["results"]
        print(f"unrelated query via API (after scope + min-score gate): {len(unrelated)} results "
              f"{[(r['title'], r['score']) for r in unrelated]}")

        other = client.post("/api/rag/search", json={"employee_id": OTHER, "query": fact_q}).json()["results"]
        _, stats = rag_docs.search(provider, ORG_ID, OTHER, fact_q)
        leaked = any(r["doc_id"] == doc["id"] for r in other)
        print(f"owner filter: {OTHER} got {len(other)} results; raw={stats.raw} scope_dropped={stats.scope_dropped} "
              f"unmapped={stats.unmapped} -> {'LEAK' if leaked else 'OK'}")
        if leaked or stats.scope_dropped < 1:
            ok = False

        if args.gemini:
            for emp, q in ((OWNER, fact_q), (OWNER, UNRELATED), (OTHER, fact_q)):
                sid = client.post("/api/sessions", json={"employee_id": emp, "agent_id": "a_dev"}).json()["id"]
                r = client.post(f"/api/sessions/{sid}/messages", json={"content": q, "use_rag": True}).json()
                print(f"\n[gemini] {emp}: {q}\n  cited_doc_ids={r['message']['cited_doc_ids']} rag_error={r['rag']['error']}")
                print("  " + r["answer"].replace("\n", "\n  "))

    print("\nSMOKE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
