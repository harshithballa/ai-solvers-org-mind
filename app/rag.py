"""Document RAG providers: store a file, index it, search it.

Providers know nothing about owners or scope. They take an object path plus bytes, return a URI, and
search returns hits keyed by that URI. app/rag_docs.py maps hits back to `rag_docs` rows and applies scope.

ORGMIND_RAG_PROVIDER = vertex (GCS + Vertex AI Search) | fake (in-memory, deterministic) | off (default).
"""
from __future__ import annotations

import hashlib
import html
import re
import threading
from dataclasses import dataclass
from typing import Literal, Protocol

from app.config import get_settings
from app.tracing import set_attrs, span

EXCERPT_CHARS = 6000     # per document, whether from extractive segments or the GCS fallback
EXTRACTIVE_SEGMENTS = 3


@dataclass
class IngestResult:
    uri: str              # where the provider stored the file; search hits carry the same URI
    operation: str        # opaque handle for check_status ("" if indexing is synchronous)


@dataclass
class StatusResult:
    status: Literal["indexing", "ready", "failed"]
    error: str | None = None


ExcerptSource = Literal["extractive_segment", "extractive_answer", "snippet", "full_text", "none"]


@dataclass
class SearchHit:
    uri: str
    snippet: str
    score: float | None = None         # Vertex: retrievalSignals.semanticRelevanceScore (0..1); fake: overlap ratio
    source: ExcerptSource = "none"     # where `snippet` came from; "snippet" means short/truncated text


class RagProvider(Protocol):
    name: str

    def ingest(self, object_path: str, data: bytes, mime_type: str) -> IngestResult: ...
    def check_status(self, operation: str, uri: str) -> StatusResult: ...
    def search(self, query: str, page_size: int = 10) -> list[SearchHit]: ...
    def fetch_text(self, uri: str, max_chars: int = EXCERPT_CHARS) -> str | None: ...


TEXT_MIME_TYPES = {"text/plain", "text/markdown"}


def _clip(text: str, limit: int = EXCERPT_CHARS) -> str:
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def clean_html(text: str) -> str:
    """Search snippets come HTML-escaped with <b> highlights (and &nbsp;): strip tags, decode entities."""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")


# ---------------- Vertex AI Search (Discovery Engine) ----------------

class VertexSearchProvider:
    """GCS bucket + an unstructured Discovery Engine data store (data_schema=content).

    With the content schema each file becomes one document whose ID is the first 128 bits of
    SHA256(gcs_uri) as hex, and search results carry the URI in derived_struct_data["link"].
    """
    name = "vertex"

    def __init__(self, project: str, location: str, datastore: str, bucket: str):
        from google.api_core.client_options import ClientOptions
        from google.cloud import discoveryengine_v1 as de
        from google.cloud import storage

        if not (project and datastore and bucket):
            raise ValueError("vertex RAG needs GOOGLE_CLOUD_PROJECT, ORGMIND_RAG_DATASTORE and ORGMIND_RAG_BUCKET")
        self._de = de
        opts = None if location == "global" else ClientOptions(api_endpoint=f"{location}-discoveryengine.googleapis.com")
        self._docs = de.DocumentServiceClient(client_options=opts)
        self._search = de.SearchServiceClient(client_options=opts)
        self._bucket = storage.Client(project=project).bucket(bucket)
        self._bucket_name = bucket
        ds = f"projects/{project}/locations/{location}/collections/default_collection/dataStores/{datastore}"
        self._branch = f"{ds}/branches/default_branch"
        self._serving_config = f"{ds}/servingConfigs/default_search"
        self._extractive = True  # flipped off if this edition rejects extractive content

    def ingest(self, object_path, data, mime_type):
        de = self._de
        with span("rag.upload", **{"orgmind.rag.provider": self.name, "orgmind.rag.bytes": len(data),
                                   "orgmind.rag.mime_type": mime_type}):
            blob = self._bucket.blob(object_path)
            blob.upload_from_string(data, content_type=mime_type, if_generation_match=0)
        uri = f"gs://{self._bucket_name}/{object_path}"
        with span("rag.import", **{"orgmind.rag.provider": self.name, "orgmind.rag.uri": uri}) as sp:
            op = self._docs.import_documents(request=de.ImportDocumentsRequest(
                parent=self._branch,
                gcs_source=de.GcsSource(input_uris=[uri], data_schema="content"),
                reconciliation_mode=de.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL))
            name = op.operation.name
            set_attrs(sp, **{"orgmind.rag.operation": name})
        return IngestResult(uri=uri, operation=name)

    def check_status(self, operation, uri):
        from google.api_core import exceptions
        from google.longrunning import operations_pb2

        de = self._de
        op = self._docs.get_operation(request=operations_pb2.GetOperationRequest(name=operation))
        if not op.done:
            return StatusResult("indexing")
        if op.HasField("error") and op.error.code:
            return StatusResult("failed", f"import failed: {op.error.message}"[:500])
        meta = de.ImportDocumentsMetadata.deserialize(op.metadata.value) if op.metadata.value else None
        if meta is not None and meta.failure_count:
            resp = de.ImportDocumentsResponse.deserialize(op.response.value) if op.response.value else None
            sample = resp.error_samples[0].message if resp and resp.error_samples else "unknown error"
            return StatusResult("failed", f"import rejected the file: {sample}"[:500])
        # Import finished. The document's index_status.pending_message can linger long after the
        # document is searchable (and index_time may never be set), so only explicit errors count.
        try:
            doc = self._docs.get_document(name=f"{self._branch}/documents/{content_doc_id(uri)}")
        except exceptions.NotFound:
            return StatusResult("indexing")
        if doc.index_status.error_samples:
            return StatusResult("failed", f"indexing failed: {doc.index_status.error_samples[0].message}"[:500])
        return StatusResult("ready")

    def search(self, query, page_size=10):
        from google.api_core import exceptions

        de = self._de
        C = de.SearchRequest.ContentSearchSpec

        def run(extractive: bool):
            spec = C(snippet_spec=C.SnippetSpec(return_snippet=True))
            if extractive:
                spec.extractive_content_spec = C.ExtractiveContentSpec(
                    max_extractive_answer_count=1, max_extractive_segment_count=EXTRACTIVE_SEGMENTS)
            req = de.SearchRequest(serving_config=self._serving_config, query=query, page_size=page_size,
                                   content_search_spec=spec)
            return next(iter(self._search.search(request=req).pages))  # first page only: one RPC

        try:
            page = run(self._extractive)
        except exceptions.InvalidArgument:
            if not self._extractive:
                raise
            self._extractive = False
            page = run(False)
        hits = []
        for r in page.results:
            d = r.document.derived_struct_data
            uri = d.get("link") or r.document.content.uri
            if uri:
                score = r.retrieval_signals.semantic_relevance_score if "retrieval_signals" in r else None
                text, source = _best_excerpt(d)
                hits.append(SearchHit(uri=uri, snippet=text, score=score, source=source))
        return hits

    def fetch_text(self, uri, max_chars=EXCERPT_CHARS):
        """Fallback when search gave only a short snippet: we wrote the object, so read it back (text only)."""
        prefix = f"gs://{self._bucket_name}/"
        if not uri.startswith(prefix):
            return None
        blob = self._bucket.blob(uri[len(prefix):])
        blob.reload()
        if (blob.content_type or "").split(";")[0] not in TEXT_MIME_TYPES:
            return None  # PDFs etc.: no local parser, keep the search snippet
        # Read a little extra so the cut lands on a word boundary.
        data = blob.download_as_bytes(start=0, end=max_chars * 4)
        return _clip(data.decode("utf-8", errors="ignore"), max_chars)


def content_doc_id(uri: str) -> str:
    """Document ID Discovery Engine assigns to a file imported with data_schema=content."""
    return hashlib.sha256(uri.encode()).hexdigest()[:32]


def _best_excerpt(d) -> tuple[str, ExcerptSource]:
    """Prefer extractive segments (verbatim, longest; all of them joined), then the extractive answer,
    then the (truncated) snippet."""
    for key, field, source in (("extractive_segments", "content", "extractive_segment"),
                               ("extractive_answers", "content", "extractive_answer"),
                               ("snippets", "snippet", "snippet")):
        parts = []
        for item in d.get(key) or []:
            if key == "snippets" and item.get("snippet_status", "SUCCESS") != "SUCCESS":
                continue
            text = clean_html(item.get(field) or "").strip()
            if text and text not in parts:
                parts.append(text)
        if parts:
            return _clip("\n…\n".join(parts)), source
    return "", "none"


# ---------------- Fake (tests / offline) ----------------

_TOKEN = re.compile(r"[a-z0-9_@#-]+")
_STOP = set("""a an the and or but if to of in on at for with from by is are was were be been it its this that
i me my we our you your do does did not no so can how what when where which who why any some there here have
has had about into as than then too very please""".split())


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1}


class FakeRagProvider:
    """In-memory, deterministic keyword overlap, no network. Ready on the first status check."""
    name = "fake"

    def __init__(self):
        self._lock = threading.Lock()
        self._docs: dict[str, str] = {}

    def ingest(self, object_path, data, mime_type):
        uri = f"fake://{object_path}"
        with span("rag.upload", **{"orgmind.rag.provider": self.name, "orgmind.rag.bytes": len(data),
                                   "orgmind.rag.mime_type": mime_type}):
            text = data.decode("utf-8", errors="ignore")
        with span("rag.import", **{"orgmind.rag.provider": self.name, "orgmind.rag.uri": uri}):
            with self._lock:
                self._docs[uri] = text
        return IngestResult(uri=uri, operation=f"fake-op/{object_path}")

    def check_status(self, operation, uri):
        return StatusResult("ready" if uri in self._docs else "failed", None if uri in self._docs else "unknown uri")

    def search(self, query, page_size=10):
        q = _tokens(query)
        scored = []
        for uri, text in list(self._docs.items()):
            overlap = len(q & _tokens(text))
            if overlap:
                scored.append((overlap / len(q), uri, text))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [SearchHit(uri=uri, snippet=self._snippet(q, text), score=round(sc, 3), source="extractive_segment")
                for sc, uri, text in scored[:page_size]]

    def fetch_text(self, uri, max_chars=EXCERPT_CHARS):
        text = self._docs.get(uri)
        return None if text is None else _clip(text, max_chars)

    @staticmethod
    def _snippet(q: set[str], text: str) -> str:
        lines = [ln for ln in re.split(r"(?<=[.!?])\s+|\n+", text) if ln.strip()]
        best = max(range(len(lines)), key=lambda i: (len(q & _tokens(lines[i])), -i)) if lines else 0
        return _clip(" ".join(lines[max(0, best - 1): best + 2]))

    def clear(self) -> None:
        with self._lock:
            self._docs.clear()


# ---------------- factory ----------------

_UNSET = object()
_provider: RagProvider | None | object = _UNSET


def get_rag_provider() -> RagProvider | None:
    """None when ORGMIND_RAG_PROVIDER=off: callers then behave exactly as before RAG existed."""
    global _provider
    if _provider is _UNSET:
        s = get_settings()
        if s.rag_provider == "vertex":
            _provider = VertexSearchProvider(s.project, s.rag_location, s.rag_datastore, s.rag_bucket)
        elif s.rag_provider == "fake":
            _provider = FakeRagProvider()
        else:
            _provider = None
    return _provider  # type: ignore[return-value]


def set_rag_provider(provider: RagProvider | None) -> None:
    """Override the provider (tests, smoke script)."""
    global _provider
    _provider = provider
