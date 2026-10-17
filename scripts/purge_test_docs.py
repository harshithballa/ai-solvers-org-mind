"""Delete test documents: rag_docs rows, GCS objects AND Vertex AI Search index documents whose title (or
uploaded file name) starts with "test-". Dry run by default.

    python -m scripts.purge_test_docs          # list what would be deleted
    python -m scripts.purge_test_docs --yes    # delete

Uses the same env as the app (GOOGLE_CLOUD_PROJECT, ORGMIND_RAG_BUCKET, ORGMIND_RAG_DATASTORE,
ORGMIND_RAG_LOCATION, ORGMIND_STORE). Test docs uploaded to an in-memory server have no surviving row, so GCS
objects are matched by name too: private/<employee>/<doc_id>-test-...
"""
import argparse
import os
import re
import sys

DEFAULTS = {
    "GOOGLE_CLOUD_PROJECT": "arched-jetty-502906-q6",
    "ORGMIND_RAG_DATASTORE": "orgmind-rag-datastore_1792251536297",
    "ORGMIND_RAG_BUCKET": "orgmind-rag-docs-arched-jetty",
    "ORGMIND_RAG_LOCATION": "global",
}
PREFIX = "test-"
OBJECT_RX = re.compile(r"^private/[^/]+/doc_[0-9a-f]+-(?P<name>.+)$")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually delete (default is a dry run)")
    args = ap.parse_args()
    for k, v in DEFAULTS.items():
        os.environ.setdefault(k, v)

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

    targets: dict[str, list[RagDoc]] = {}   # gs:// uri -> rows
    for row in get_store().list(RagDoc):
        if row.title.lower().startswith(PREFIX) and row.gcs_uri.startswith("gs://"):
            targets.setdefault(row.gcs_uri, []).append(row)
    for blob in bucket.list_blobs(prefix="private/"):
        m = OBJECT_RX.match(blob.name)
        if m and m.group("name").lower().startswith(PREFIX):
            targets.setdefault(f"gs://{s.rag_bucket}/{blob.name}", [])

    print(f"store={s.store} bucket={s.rag_bucket} datastore={s.rag_datastore}")
    print(f"{len(targets)} test document(s) {'to delete' if args.yes else 'found (dry run; pass --yes to delete)'}:")
    for uri, rows in sorted(targets.items()):
        print(f"  {uri}  index_id={content_doc_id(uri)}  rows={[r.id for r in rows]}")
    if not args.yes:
        return 0

    failures = 0
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
