"""Share an owner's ACTIVE skill or READY document with the whole org, gated by the PII/secret guardrail.

Publishing happens in the same request as a clean scan; anything else leaves the item private. Every attempt is
written to share_events with finding CATEGORIES only. Retrieval needs no change: org-scoped active skills and
org-scoped documents are already visible to every employee (app/skills.py, app/rag_docs.py).
There is no human review step yet (planned).
"""
from __future__ import annotations

import logging

from app import guardrail
from app.models import RagDoc, ShareEvent, Skill, now
from app.rag import RagProvider
from app.rag_docs import MAX_TEXT_BYTES, upload
from app.store import get_store
from app.tracing import span

log = logging.getLogger("orgmind")

TEXT_MIME_TYPES = {"text/plain", "text/markdown"}
MAX_REDACTION_ROUNDS = 3


class ShareError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def record(org_id: str, actor: str, item_type: str, item_id: str, action: str, verdict: str,
           categories: list[str] | None = None) -> None:
    try:
        get_store().put(ShareEvent(org_id=org_id, actor_employee_id=actor, item_type=item_type, item_id=item_id,
                                   action=action, verdict=verdict, finding_categories=sorted(set(categories or []))))
    except Exception as e:  # the audit log must not break sharing
        log.warning("share_event write failed: %s", e)
    log.info("share %s %s %s: %s %s", item_type, item_id, action, verdict, sorted(set(categories or [])))


def _owned(obj, kind: str, employee_id: str):
    if obj is None or (obj.owner_employee_id != employee_id and obj.scope != "org"):
        raise ShareError(404, f"{kind} not found")
    if obj.owner_employee_id != employee_id:
        raise ShareError(403, f"only the {kind}'s owner can share or unshare it")
    return obj


def skill_text(sk: Skill) -> str:
    params = "\n".join(f"{p.name}: {p.description}" for p in sk.parameters)
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(sk.steps, 1))
    return f"Name: {sk.name}\nDescription: {sk.description}\nWhen to use: {sk.trigger}\nSteps:\n{steps}\nParameters:\n{params}"


def _publish(obj, employee_id: str) -> None:
    obj.scope, obj.shared_by, obj.shared_at, obj.updated_at = "org", employee_id, now(), now()
    get_store().put(obj)


def _make_private(obj) -> None:
    obj.scope, obj.shared_by, obj.shared_at, obj.updated_at = "private", None, None, now()
    get_store().put(obj)


# ---------------- skills ----------------

def share_skill(skill_id: str, employee_id: str) -> tuple[guardrail.ScanResult, Skill]:
    sk = _owned(get_store().get(Skill, skill_id), "skill", employee_id)
    if sk.status != "active":
        raise ShareError(409, "Only active (approved) skills can be shared.")
    if sk.scope == "org":
        raise ShareError(409, "This skill is already shared with your org.")
    with span("share.skill", **{"orgmind.skill_id": sk.id}):
        result = guardrail.scan(skill_text(sk), "skill")
        if result.verdict == "clean":
            _publish(sk, employee_id)
            record(sk.org_id, employee_id, "skill", sk.id, "share", "clean")
        else:
            record(sk.org_id, employee_id, "skill", sk.id, "blocked", result.verdict, result.categories)
    return result, sk


def unshare_skill(skill_id: str, employee_id: str, verdict: str = "owner") -> Skill:
    sk = _owned(get_store().get(Skill, skill_id), "skill", employee_id)
    if sk.scope != "private":
        _make_private(sk)
        record(sk.org_id, employee_id, "skill", sk.id, "unshare", verdict)
    return sk


# ---------------- documents ----------------

def _doc_text(provider: RagProvider, doc: RagDoc) -> str:
    if doc.status != "ready":
        raise ShareError(409, "Only documents that finished indexing (Ready) can be shared.")
    if doc.mime_type not in TEXT_MIME_TYPES:
        raise ShareError(400, "PDFs can't be scanned yet, so they can't be shared.")
    if doc.size > MAX_TEXT_BYTES:
        raise ShareError(400, f"This document is too large to scan (the limit is {MAX_TEXT_BYTES // 1024} KB).")
    text = provider.fetch_text(doc.gcs_uri, MAX_TEXT_BYTES)
    if text is None:
        raise ShareError(502, "Couldn't read the document back to scan it. Please try again.")
    return text


def share_doc(provider: RagProvider, doc_id: str, employee_id: str) -> tuple[guardrail.ScanResult, RagDoc]:
    doc = _owned(get_store().get(RagDoc, doc_id), "document", employee_id)
    if doc.scope == "org":
        raise ShareError(409, "This document is already shared with your org.")
    with span("share.doc", **{"orgmind.doc_id": doc.id}):
        result = guardrail.scan(_doc_text(provider, doc), "document")
        if result.verdict == "clean":
            _publish(doc, employee_id)   # retrieval filters by our own scope: instant, no re-import
            record(doc.org_id, employee_id, "doc", doc.id, "share", "clean")
        else:
            record(doc.org_id, employee_id, "doc", doc.id, "blocked", result.verdict, result.categories)
    return result, doc


def unshare_doc(doc_id: str, employee_id: str) -> RagDoc:
    doc = _owned(get_store().get(RagDoc, doc_id), "document", employee_id)
    if doc.scope != "private":
        _make_private(doc)
        record(doc.org_id, employee_id, "doc", doc.id, "unshare", "owner")
    return doc


def share_redacted_copy(provider: RagProvider, doc_id: str, employee_id: str
                        ) -> tuple[guardrail.ScanResult, guardrail.ScanResult | None, RagDoc | None]:
    """Scan, replace every finding with [REDACTED:<CATEGORY>], re-scan, and publish a NEW document only if the
    redacted text is clean. The original stays private. Returns (first scan, re-scan, new doc)."""
    doc = _owned(get_store().get(RagDoc, doc_id), "document", employee_id)
    with span("share.redacted_copy", **{"orgmind.doc_id": doc.id}):
        text = _doc_text(provider, doc)
        first = guardrail.scan(text, "document")
        if first.verdict == "error":
            record(doc.org_id, employee_id, "doc", doc.id, "blocked", "error", first.categories)
            return first, None, None
        if first.verdict == "clean":
            raise ShareError(409, "Nothing to redact: share the document itself.")
        # Redact, re-scan; a re-scan can surface values that only stand out once their neighbours are gone
        # (e.g. a name next to a removed email), so redact again, up to MAX_REDACTION_ROUNDS scans of the copy.
        redacted = guardrail.redact(text, first.findings)
        second = guardrail.scan(redacted, "document")
        rounds = 1
        while second.verdict == "blocked" and rounds < MAX_REDACTION_ROUNDS:
            redacted = guardrail.redact(redacted, second.findings)
            second = guardrail.scan(redacted, "document")
            rounds += 1
        if second.verdict != "clean":
            record(doc.org_id, employee_id, "doc", doc.id, "blocked", second.verdict, second.categories)
            return first, second, None
        base = doc.title.rsplit(".", 1)[0] if doc.title.lower().endswith((".txt", ".md")) else doc.title
        copy = upload(provider, doc.org_id, employee_id, f"{base} (redacted)", f"{base}-redacted.txt",
                      redacted.encode("utf-8"))
        if copy.status != "failed":
            _publish(copy, employee_id)   # visible to the org as soon as indexing finishes
        record(doc.org_id, employee_id, "doc", copy.id, "redacted_copy", "clean" if copy.status != "failed" else "error",
               first.categories)
    return first, second, copy
