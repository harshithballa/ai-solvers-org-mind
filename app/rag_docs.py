"""Uploaded documents: ownership + scope (in our store) on top of a RagProvider (storage + search index).

The search index has no notion of owners. Every hit is mapped back to a `rag_docs` row by URI and dropped
unless the asking employee owns it or it is scope=org. Unmapped hits are dropped too.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from app.config import get_settings
from app.models import RagDoc, now
from app.rag import EXCERPT_CHARS, RagProvider, SearchHit
from app.store import get_store
from app.tracing import set_attrs, span

log = logging.getLogger("orgmind")

ALLOWED = {".txt": "text/plain", ".md": "text/markdown", ".pdf": "application/pdf"}
MAX_BYTES = 5 * 1024 * 1024
SEARCH_PAGE_SIZE = 10


class UploadError(ValueError):
    """Bad input: maps to HTTP 400."""


@dataclass
class DocHit:
    doc: RagDoc
    snippet: str
    score: float | None
    source: str


@dataclass
class SearchStats:
    raw: int = 0               # results from the provider
    scope_dropped: int = 0     # not owned by the asker and not scope=org
    unmapped: int = 0          # no rag_docs row for the URI (e.g. files imported outside the app)
    below_score: int = 0       # under ORGMIND_RAG_MIN_SCORE: treated as "not covered"
    fallback_reads: int = 0    # full-text reads because search gave only a short snippet or answer
    latency_ms: float = 0.0
    scores: list[float | None] = field(default_factory=list)


def safe_filename(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]).strip("-.")
    return base[:80] or "document"


def validate(filename: str, data: bytes) -> str:
    """Return the mime type, or raise UploadError."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in ALLOWED:
        raise UploadError(f"unsupported file type '{ext or filename}': use .txt, .md or .pdf")
    if not data:
        raise UploadError("file is empty")
    if len(data) > MAX_BYTES:
        raise UploadError(f"file is {len(data)} bytes; the limit is {MAX_BYTES} (5 MB)")
    mime = ALLOWED[ext]
    if mime == "application/pdf":
        if not data.startswith(b"%PDF"):
            raise UploadError("file does not look like a PDF")
    else:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise UploadError("text files must be UTF-8") from None
    return mime


def upload(provider: RagProvider, org_id: str, employee_id: str, title: str, filename: str, data: bytes) -> RagDoc:
    """Store + start indexing. Returns the doc with status=indexing (or failed, with `error`)."""
    filename = safe_filename(filename)
    mime = validate(filename, data)
    store = get_store()
    doc = RagDoc(org_id=org_id, owner_employee_id=employee_id, title=title.strip() or filename,
                 mime_type=mime, size=len(data))
    store.put(doc)  # status=uploading
    try:
        res = provider.ingest(f"private/{employee_id}/{doc.id}-{filename}", data, mime)
        doc.gcs_uri, doc.import_operation, doc.status = res.uri, res.operation, "indexing"
    except Exception as e:
        log.warning("rag upload failed for %s: %s", doc.id, e)
        doc.status, doc.error = "failed", f"{type(e).__name__}: {str(e)[:300]}"
    doc.updated_at = now()
    store.put(doc)
    return doc


def refresh(provider: RagProvider | None, doc: RagDoc) -> RagDoc:
    """Update an indexing doc from its import operation (called on read; no background workers)."""
    if provider is None or doc.status != "indexing" or not doc.import_operation:
        return doc
    with span("rag.import.status", **{"orgmind.rag.doc_id": doc.id}) as sp:
        try:
            st = provider.check_status(doc.import_operation, doc.gcs_uri)
        except Exception as e:  # leave it indexing; the next read retries
            log.warning("rag status check failed for %s: %s", doc.id, e)
            return doc
        set_attrs(sp, **{"orgmind.rag.status": st.status})
    if st.status != doc.status:
        doc.status, doc.error, doc.updated_at = st.status, st.error, now()
        get_store().put(doc)
    return doc


def visible_docs(org_id: str, employee_id: str) -> list[RagDoc]:
    """The employee's own docs plus every scope=org doc in the org."""
    store = get_store()
    own = store.list(RagDoc, org_id=org_id, owner_employee_id=employee_id)
    shared = store.list(RagDoc, org_id=org_id, scope="org")
    by_id = {d.id: d for d in own + shared}
    return sorted(by_id.values(), key=lambda d: d.created_at, reverse=True)


def search(provider: RagProvider, org_id: str, employee_id: str, query: str,
           top_k: int | None = None) -> tuple[list[DocHit], SearchStats]:
    s = get_settings()
    top_k = top_k or s.rag_top_k
    stats = SearchStats()
    t0 = time.perf_counter()
    with span("rag.search", **{"orgmind.rag.provider": provider.name, "orgmind.rag.min_score": s.rag_min_score}) as sp:
        hits: list[SearchHit] = provider.search(query, page_size=SEARCH_PAGE_SIZE)
        stats.raw = len(hits)
        stats.scores = [h.score for h in hits]
        allowed = {d.gcs_uri: d for d in visible_docs(org_id, employee_id) if d.gcs_uri and d.status != "failed"}
        known = None  # lazily: is a dropped URI some other employee's doc, or not ours at all?
        out: list[DocHit] = []
        for h in hits:
            doc = allowed.get(h.uri)
            if doc is None:
                if known is None:
                    known = {d.gcs_uri for d in get_store().list(RagDoc, org_id=org_id)}
                if h.uri in known:
                    stats.scope_dropped += 1
                else:
                    stats.unmapped += 1
                continue
            if h.score is not None and h.score < s.rag_min_score:
                stats.below_score += 1
                continue
            if len(out) < top_k and all(o.doc.id != doc.id for o in out):
                out.append(DocHit(doc=doc, snippet=h.snippet, score=h.score, source=h.source))
        # Fallback, only for docs the asker may see: search gave a short snippet (or nothing).
        for hit in out:
            if hit.source in ("snippet", "extractive_answer", "none"):
                try:
                    text = provider.fetch_text(hit.doc.gcs_uri, EXCERPT_CHARS)
                except Exception as e:
                    log.warning("rag full-text fallback failed for %s: %s", hit.doc.id, e)
                    text = None
                if text:
                    hit.snippet, hit.source = text, "full_text"
                    stats.fallback_reads += 1
        stats.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        set_attrs(sp, **{"orgmind.rag.result_count": stats.raw, "orgmind.rag.returned": len(out),
                         "orgmind.rag.scope_dropped": stats.scope_dropped, "orgmind.rag.unmapped": stats.unmapped,
                         "orgmind.rag.below_score": stats.below_score, "orgmind.rag.fallback_reads": stats.fallback_reads,
                         "orgmind.rag.latency_ms": stats.latency_ms,
                         "orgmind.rag.doc_ids": ",".join(h.doc.id for h in out) or None})
    log.info("rag search %d raw -> %d returned (%d scope-dropped, %d unmapped, %d below score) %.0fms",
             stats.raw, len(out), stats.scope_dropped, stats.unmapped, stats.below_score, stats.latency_ms)
    return out, stats
