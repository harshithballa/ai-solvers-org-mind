"""Wipe EVERY document in a scratch Firestore database. Refuses "(default)". Dry run unless --yes.

    python -m scripts.firestore_wipe --database orgmind-test [--yes]

The database id is re-checked on the client object before every destructive call.
"""
import argparse
import os
import sys

PROTECTED = {"(default)", "default", ""}


def guard(client, expected: str) -> None:
    actual = getattr(client, "_database", None)
    if actual in PROTECTED or actual != expected:
        raise SystemExit(f"refusing: client database is {actual!r}, expected scratch database {expected!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", required=True)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    if args.database in PROTECTED:
        sys.exit("refusing to touch the (default) database")
    from google.cloud import firestore

    client = firestore.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT", "arched-jetty-502906-q6"), database=args.database)
    guard(client, args.database)
    cols = list(client.collections())
    counts = {c.id: c.count().get()[0][0].value for c in cols}
    print(f"database {client._database!r}: {counts or 'empty'}")
    if not args.yes:
        print("dry run: pass --yes to delete everything listed")
        return 0
    for c in cols:
        guard(client, args.database)
        client.recursive_delete(c)
        print(f"  wiped {c.id}")
    guard(client, args.database)
    left = {c.id: c.count().get()[0][0].value for c in client.collections()}
    print(f"after wipe: {left or 'empty'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
