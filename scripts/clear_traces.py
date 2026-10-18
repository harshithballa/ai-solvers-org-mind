"""Clear the in-app trace log and today's cost-cap counters in a Firestore database. Dry run unless --yes.

    python -m scripts.clear_traces --database "(default)"          # list what would be deleted
    python -m scripts.clear_traces --database "(default)" --yes    # delete

Deletes ONLY: every document in `llm_calls` (Admin > Traces) and the `usage_counters` documents for today's UTC
date ("{YYYY-MM-DD}-{bucket}"). Earlier days' counters and all demo data are kept. The database id is re-checked
on the client object before every destructive call. Like scripts/firestore_wipe.py, this is an ops script and
talks to Firestore directly.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

BATCH = 400


def todays_counter_ids(ids, today: str) -> list[str]:
    return sorted(i for i in ids if i.startswith(f"{today}-"))


def guard(client, expected: str) -> None:
    actual = getattr(client, "_database", None)
    if actual != expected:
        raise SystemExit(f"refusing: client database is {actual!r}, expected {expected!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", required=True, help='Firestore database id, e.g. "(default)" for the live demo')
    ap.add_argument("--yes", action="store_true", help="actually delete (default is a dry run)")
    args = ap.parse_args()
    from google.cloud import firestore

    client = firestore.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT", "arched-jetty-502906-q6"), database=args.database)
    guard(client, args.database)
    today = f"{datetime.now(timezone.utc):%Y-%m-%d}"
    calls = [d.reference for d in client.collection("llm_calls").select([]).stream()]
    counters = todays_counter_ids((d.id for d in client.collection("usage_counters").select([]).stream()), today)
    print(f"database {args.database!r}: {len(calls)} llm_calls document(s); today's ({today}) usage_counters: "
          f"{counters or 'none'}")
    if not args.yes:
        print("dry run: pass --yes to delete them")
        return 0
    refs = calls + [client.collection("usage_counters").document(i) for i in counters]
    for start in range(0, len(refs), BATCH):
        guard(client, args.database)
        batch = client.batch()
        for ref in refs[start:start + BATCH]:
            batch.delete(ref)
        batch.commit()
    print(f"deleted {len(calls)} llm_calls and {len(counters)} usage_counters document(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
