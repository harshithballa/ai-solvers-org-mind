"""Delete RAG documents from GCS AND the Vertex AI Search index. Dry run by default.

    python -m scripts.purge_test_docs          # list "test-" documents that would be deleted
    python -m scripts.purge_test_docs --yes    # delete them (GCS object, index document, rag_docs row)

    python -m scripts.purge_test_docs --all [--yes]    # EVERY document (all employees), same delete path

    python -m scripts.purge_test_docs --orphans --database <firestore db>          # list orphans
    python -m scripts.purge_test_docs --orphans --database <firestore db> --yes    # delete them

"test-" mode: rows, GCS objects and index documents whose title (or uploaded file name) starts with "test-". Test
docs uploaded to an in-memory server have no surviving row, so GCS objects are matched by name too:
private/<employee>/<doc_id>-test-...

--orphans mode: every GCS object in the bucket (root and private/) and every index document that has NO rag_docs row
in the named Firestore database. Firestore is only read. An object is spared if a row points at it, if its name
carries a row's doc id (an upload in flight writes the row before gcs_uri), or if it is younger than --min-age
minutes. Index document id = sha256(gcs_uri)[:32].

Uses the same env as the app (GOOGLE_CLOUD_PROJECT, ORGMIND_RAG_BUCKET, ORGMIND_RAG_DATASTORE, ORGMIND_RAG_LOCATION,
ORGMIND_STORE).
"""
import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

DEFAULTS = {
    "GOOGLE_CLOUD_PROJECT": "arched-jetty-502906-q6",
    "ORGMIND_RAG_DATASTORE": "orgmind-rag-datastore_1792251536297",
    "ORGMIND_RAG_BUCKET": "orgmind-rag-docs-arched-jetty",
    "ORGMIND_RAG_LOCATION": "global",
}
PREFIX = "test-"
OBJECT_RX = re.compile(r"^private/[^/]+/doc_[0-9a-f]+-(?P<name>.+)$")
DOC_ID_RX = re.compile(r"(?:^|/)(?P<id>doc_[0-9a-f]+)-")


def find_orphans(rows, objects: dict[str, datetime], index_docs: dict[str, str], cutoff: datetime
                 ) -> tuple[list[str], list[str], dict[str, str]]:
    """rows: RagDoc rows; objects: gs:// uri -> created time; index_docs: index id -> uri ("" if unknown).
    Returns (orphan object uris, orphan index ids, spared uri -> reason)."""
    from app.rag import content_doc_id

    row_uris = {r.gcs_uri for r in rows if r.gcs_uri}
    row_ids = {r.id for r in rows}
    spared: dict[str, str] = {}
    orphan_objects = []
    for uri, created in sorted(objects.items()):
        m = DOC_ID_RX.search(uri)
        if uri in row_uris:
            spared[uri] = "has row"
        elif m and m.group("id") in row_ids:
            spared[uri] = f"row {m.group('id')} (upload in flight)"
        elif created > cutoff:
            spared[uri] = "too new"
        else:
            orphan_objects.append(uri)
    keep_ids = {content_doc_id(u) for u in row_uris | set(spared)}
    orphan_index = sorted(i for i in index_docs if i not in keep_ids)
    return orphan_objects, orphan_index, spared


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually delete (default is a dry run)")
    ap.add_argument("--all", action="store_true", help="every document, not only test- ones (full knowledge reset)")
    ap.add_argument("--orphans", action="store_true", help="delete GCS objects and index documents with no rag_docs row")
    ap.add_argument("--database", help="--orphans: the Firestore database whose rag_docs rows are kept (read only)")
    ap.add_argument("--min-age", type=int, default=10, help="--orphans: spare objects younger than this many minutes")
    args = ap.parse_args()
    for k, v in DEFAULTS.items():
        os.environ.setdefault(k, v)
    if args.orphans:
        if not args.database:
            sys.exit("--orphans needs --database: without the real rag_docs rows every document looks orphaned")
        os.environ.update({"ORGMIND_STORE": "firestore", "ORGMIND_FIRESTORE_DATABASE": args.database})

    from google.api_core import exceptions
    from google.api_core.client_options import ClientOptions
    from google.cloud import discoveryengine_v1 as de
    from google.cloud import storage

    from app.config import get_settings
    from app.models import RagDoc
    from app.rag import content_doc_id
    from app.store import get_store

    s = get_settings()
    bucket = storage.Client(project=s.project).bucket(s.rag_bucket)
    opts = None if s.rag_location == "global" else ClientOptions(api_endpoint=f"{s.rag_location}-discoveryengine.googleapis.com")
    docs = de.DocumentServiceClient(client_options=opts)
    branch = (f"projects/{s.project}/locations/{s.rag_location}/collections/default_collection/"
              f"dataStores/{s.rag_datastore}/branches/default_branch")

    if args.orphans:
        store = get_store()
        actual = getattr(getattr(store, "_db", None), "_database", None)
        if actual != args.database:
            sys.exit(f"refusing: store database is {actual!r}, expected {args.database!r}")
        rows = store.list(RagDoc)   # read only: orphan mode never writes Firestore
        objects = {f"gs://{s.rag_bucket}/{b.name}": b.time_created for b in bucket.list_blobs()}
        index_docs = {d.id: d.content.uri for d in docs.list_documents(parent=branch)}
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=args.min_age)
        orphan_objects, orphan_index, spared = find_orphans(rows, objects, index_docs, cutoff)
        print(f"firestore database={actual!r} rag_docs rows={len(rows)} bucket={s.rag_bucket} "
              f"objects={len(objects)} datastore={s.rag_datastore} index documents={len(index_docs)}")
        for uri, why in spared.items():
            print(f"  spared  {uri}  ({why})")
        verb = "to delete" if args.yes else "found (dry run; pass --yes to delete)"
        print(f"{len(orphan_objects)} orphan GCS object(s) {verb}:")
        for uri in orphan_objects:
            print(f"  {uri}  index_id={content_doc_id(uri)}")
        print(f"{len(orphan_index)} orphan index document(s) {verb}:")
        for i in orphan_index:
            print(f"  {i}  {index_docs[i] or '(no uri)'}")
        if not args.yes:
            return 0
        failures = 0
        for i in orphan_index:
            try:
                docs.delete_document(name=f"{branch}/documents/{i}")
                print(f"  index {i}: deleted")
            except exceptions.NotFound:
                print(f"  index {i}: already gone")
            except Exception as e:
                failures += 1
                print(f"  index {i}: FAILED {e}")
        for uri in orphan_objects:
            try:
                bucket.blob(uri.split("/", 3)[3]).delete()
                print(f"  object {uri}: deleted")
            except exceptions.NotFound:
                print(f"  object {uri}: already gone")
            except Exception as e:
                failures += 1
                print(f"  object {uri}: FAILED {e}")
        print("done" if not failures else f"done with {failures} failure(s)")
        return 1 if failures else 0

    targets: dict[str, list[RagDoc]] = {}   # gs:// uri -> rows
    for row in get_store().list(RagDoc):
        if (args.all or row.title.lower().startswith(PREFIX)) and row.gcs_uri.startswith("gs://"):
            targets.setdefault(row.gcs_uri, []).append(row)
    for blob in bucket.list_blobs(prefix="private/"):
        m = OBJECT_RX.match(blob.name)
        if args.all or (m and m.group("name").lower().startswith(PREFIX)):
            targets.setdefault(f"gs://{s.rag_bucket}/{blob.name}", [])

    print(f"store={s.store} bucket={s.rag_bucket} datastore={s.rag_datastore}")
    rowless = [r for r in get_store().list(RagDoc) if args.all and not r.gcs_uri.startswith("gs://")]
    print(f"{len(targets)} {'' if args.all else 'test '}document(s) {'to delete' if args.yes else 'found (dry run; pass --yes to delete)'}:")
    for uri, rows in sorted(targets.items()):
        print(f"  {uri}  index_id={content_doc_id(uri)}  rows={[r.id for r in rows]}")
    for r in rowless:
        print(f"  row {r.id} ({r.title!r}, {r.status}) has no stored file")
    if not args.yes:
        return 0

    failures = 0
    for r in rowless:
        get_store().delete(RagDoc, r.id)
    for uri, rows in sorted(targets.items()):
        try:
            docs.delete_document(name=f"{branch}/documents/{content_doc_id(uri)}")
            index = "deleted"
        except exceptions.NotFound:
            index = "not in index"
        except Exception as e:
            index, failures = f"FAILED {e}", failures + 1
        try:
            bucket.blob(uri.split("/", 3)[3]).delete()
            obj = "deleted"
        except exceptions.NotFound:
            obj = "already gone"
        except Exception as e:
            obj, failures = f"FAILED {e}", failures + 1
        for r in rows:
            get_store().delete(RagDoc, r.id)
        print(f"  {uri}: index {index}; object {obj}; rows deleted {len(rows)}")
    print("done" if not failures else f"done with {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
