"""Skills: reusable procedures ONE employee's agent learned in ONE conversation.

Not to be confused with playbooks (org-wide fixes distilled from the same problem across 2+ employees,
app/distiller.py) or RAG docs (uploaded source material, app/rag_docs.py).

extract: one Gemini call per session -> 0..2 drafts, post-checked (evidence, leaks, secret patterns), replacing the
session's previous drafts. A candidate that repeats a skill the owner already has from the same session (not
archived) is skipped and reported as already learned. retrieve: active skills visible to the employee, cosine on
embeddings (as playbooks), at most one per owner and skill name.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from app.config import get_settings
from app.llm import cosine, get_llm
from app.models import Session, Skill, SkillDraftOut, SkillsOut, now
from app.prompts import SKILL_EXTRACT_SYSTEM, format_numbered_transcript, is_cited_answer
from app.store import get_store
from app.tracing import set_attrs, span

log = logging.getLogger("orgmind")

MIN_MESSAGES = 4
MAX_SKILLS = 2
DEDUP_SIMILARITY = 0.88   # cosine of name + trigger embeddings at or above this = the same skill

SECRET_PATTERNS = {
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "bearer_token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "long_hex": re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    "base64_blob": re.compile(r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Z])[A-Za-z0-9+/]{40,}={0,2}"),
    "api_token": re.compile(r"\b(ghp|gho|github_pat|xox[abp]|sk)[-_][A-Za-z0-9_]{16,}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


class SkillError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class ExtractStats:
    proposed: int = 0
    saved: int = 0
    rejected: int = 0
    redacted: int = 0
    replaced_drafts: int = 0
    skipped: int = 0
    already_learned: list[Skill] = field(default_factory=list)   # existing skills that candidates repeated
    latency_ms: float = 0.0
    rejections: list[str] = field(default_factory=list)


def skill_text(sk) -> str:
    params = " ".join(f"{p.name} {p.description}" for p in sk.parameters)
    return "\n".join([sk.name, sk.description, sk.trigger, *sk.steps, params])


def skill_doc_text(sk: Skill) -> str:
    """What gets embedded for retrieval (mirrors playbook_doc_text)."""
    return f"{sk.name}\n{sk.description}\n{sk.trigger}\n" + "\n".join(sk.steps)


def norm_name(name: str) -> str:
    return " ".join(name.lower().split())


def _dedup_text(sk) -> str:
    return f"{sk.name}\n{sk.trigger}"


def dedup(candidates: list[Skill], existing: list[Skill]) -> tuple[list[Skill], list[Skill]]:
    """Drop candidates that repeat an existing skill or an earlier candidate: same normalized name, or name+trigger
    embeddings with cosine >= DEDUP_SIMILARITY. Returns (kept candidates, existing skills that were repeated)."""
    if not candidates:
        return [], []
    vecs = get_llm().embed([_dedup_text(x) for x in [*existing, *candidates]], "SEMANTIC_SIMILARITY")
    seen = list(zip(existing, vecs[:len(existing)]))
    kept, repeated = [], []
    for c, v in zip(candidates, vecs[len(existing):]):
        match = next((e for e, ev in seen if norm_name(e.name) == norm_name(c.name)
                      or cosine(v, ev) >= DEDUP_SIMILARITY), None)
        if match is None:
            kept.append(c)
            seen.append((c, v))
        elif match in existing and match not in repeated:
            repeated.append(match)
    return kept, repeated


def secret_hits(text: str) -> list[str]:
    return [name for name, rx in SECRET_PATTERNS.items() if rx.search(text)]


def untraceable_code(steps: list[str], transcript: str) -> list[str]:
    """`code` spans in steps that don't occur in the transcript ({param} placeholders match anything)."""
    flat = re.sub(r"\s+", " ", transcript)
    missing = []
    text = "\n".join(steps).replace("\\`", "\x00")  # escaped backticks inside a span, e.g. FROM \`dataset.table\`
    for code in re.findall(r"`([^`]+)`", text):
        code = code.replace("\x00", "`")
        parts = re.split(r"(\{[A-Za-z_][A-Za-z0-9_]*\})", re.sub(r"\s+", " ", code.strip()))
        rx = "".join(".+?" if re.fullmatch(r"\{[A-Za-z_][A-Za-z0-9_]*\}", p) else re.escape(p) for p in parts)
        if not re.search(rx, flat):
            missing.append(code)
    return missing


# ---------------- extraction ----------------

_RESOLVED_RE = re.compile(r"\b(worked|fixed|resolved|works now|went through|that did it)\b", re.I)
_NEGATED_RE = re.compile(r"\b(didn'?t|did not|doesn'?t|not|still)\b[^.]{0,20}\b(work|fix)", re.I)


def _fake_extract(msgs) -> SkillsOut:
    """Deterministic stand-in: a confirmed conversation containing `code` yields one skill made of that code."""
    users = [m.content for m in msgs if m.role == "user"]
    last = users[-1] if users else ""
    if len(users) < 2 or not _RESOLVED_RE.search(last) or _NEGATED_RE.search(last):
        return SkillsOut(skills=[])
    codes, evidence = [], []
    for i, m in enumerate(msgs):
        for c in re.findall(r"`([^`]+)`", m.content):
            if c not in codes:
                codes.append(c)
                evidence.append(i)
    if not codes:
        return SkillsOut(skills=[])
    topic = re.sub(r"[`\s]+", " ", users[0]).strip()[:60]
    return SkillsOut(skills=[SkillDraftOut(
        excluded_specifics=[], name=f"Repeat the procedure for: {topic}", description=f"Steps that worked for: {topic}",
        trigger=topic, steps=[f"Run `{c}`" for c in codes], parameters=[], evidence=sorted(set(evidence)))])


def _check(draft: SkillDraftOut, msgs, stats: ExtractStats) -> SkillDraftOut | None:
    """Deterministic post-checks. Returns the (possibly redacted) draft, or None if rejected."""
    evidence = sorted({i for i in draft.evidence if 0 <= i < len(msgs)})
    if not evidence:
        stats.rejections.append(f"{draft.name!r}: no valid evidence indexes")
        return None
    if all(is_cited_answer(msgs[i]) for i in evidence):
        # Only backed by agent turns that repeated playbooks/documents: nothing this employee worked out.
        stats.rejections.append(f"{draft.name!r}: evidence is only agent answers citing playbooks/documents")
        return None
    hits = secret_hits(skill_text(draft))
    if hits:
        stats.rejections.append(f"{draft.name!r}: secret patterns {hits}")
        return None
    # Values the model itself flagged as personal must not survive (same enforcement idea as playbooks).
    leaked = [v.strip() for v in draft.excluded_specifics
              if len(v.strip()) >= 3 and v.strip().lower() in skill_text(draft).lower()]
    if leaked:
        def scrub(t: str) -> str:
            for v in leaked:
                t = re.sub(re.escape(v), "{redacted}", t, flags=re.I)
            return t
        draft = draft.model_copy(update={"name": scrub(draft.name), "description": scrub(draft.description),
                                         "trigger": scrub(draft.trigger), "steps": [scrub(s) for s in draft.steps]})
        stats.redacted += 1
        log.warning("skill %r: redacted personal values %s", draft.name, leaked)
    return draft.model_copy(update={"evidence": evidence})


def extract(session: Session, employee_id: str) -> tuple[list[Skill], ExtractStats]:
    if session.employee_id != employee_id:
        raise SkillError(403, "only the session's owner can extract skills from it")
    store = get_store()
    msgs = store.list_messages(session.id)
    if len(msgs) < MIN_MESSAGES:
        raise SkillError(400, f"session needs at least {MIN_MESSAGES} messages to extract skills (has {len(msgs)})")
    stats = ExtractStats()
    t0 = time.perf_counter()
    with span("skills.extract", **{"orgmind.session_id": session.id, "orgmind.messages": len(msgs)}) as sp:
        out = get_llm().generate_json(SKILL_EXTRACT_SYSTEM, f"Conversation:\n{format_numbered_transcript(msgs)}",
                                      SkillsOut, fake=lambda: _fake_extract(msgs))
        stats.proposed = len(out.skills)
        drafts = [d for d in (_check(d, msgs, stats) for d in out.skills[:MAX_SKILLS]) if d is not None]
        stats.rejected = stats.proposed - len(drafts)
        for r in stats.rejections:
            log.warning("skill rejected (session %s): %s", session.id, r)

        transcript = "\n".join(m.content for m in msgs)
        skills = []
        for d in drafts:
            if missing := untraceable_code(d.steps, transcript):
                log.warning("skill %r has code not found verbatim in the transcript: %s", d.name, missing)
            skills.append(Skill(org_id=session.org_id, owner_employee_id=employee_id, agent_kind=session.agent_kind,
                                name=d.name, description=d.description, trigger=d.trigger, steps=d.steps,
                                parameters=d.parameters, evidence=d.evidence, source_session_id=session.id))
        # Don't learn the same skill twice: compare with the owner's non-archived skills from this session.
        existing = [x for x in store.list(Skill, source_session_id=session.id)
                    if x.owner_employee_id == employee_id and x.status != "archived"]
        proposed = len(skills)
        skills, stats.already_learned = dedup(skills, existing)
        stats.skipped = proposed - len(skills)
        if skills:
            vecs = get_llm().embed([skill_doc_text(s) for s in skills], "RETRIEVAL_DOCUMENT")
            for s, v in zip(skills, vecs):
                s.embedding = v

        # Idempotent per session: replace previous drafts (except ones a candidate just repeated, which are kept
        # as they are), never touch active/archived skills.
        kept_ids = {x.id for x in stats.already_learned}
        for old in existing:
            if old.status == "draft" and old.id not in kept_ids:
                store.delete(Skill, old.id)
                stats.replaced_drafts += 1
        for s in skills:
            store.put(s)
        stats.saved = len(skills)
        stats.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        set_attrs(sp, **{"orgmind.skills.proposed": stats.proposed, "orgmind.skills.saved": stats.saved,
                         "orgmind.skills.rejected": stats.rejected, "orgmind.skills.redacted": stats.redacted,
                         "orgmind.skills.replaced_drafts": stats.replaced_drafts,
                         "orgmind.skills.skipped_duplicates": stats.skipped,
                         "orgmind.latency_ms": stats.latency_ms})
    log.info("skills extract %s: %d proposed, %d saved, %d rejected, %d redacted, %d already learned, %.0fms",
             session.id, stats.proposed, stats.saved, stats.rejected, stats.redacted, stats.skipped, stats.latency_ms)
    return skills, stats


# ---------------- visibility / lifecycle ----------------

def visible(org_id: str, employee_id: str) -> list[Skill]:
    """Own skills (any status) plus other employees' scope=org skills that are active. Drafts stay private."""
    store = get_store()
    own = store.list(Skill, org_id=org_id, owner_employee_id=employee_id)
    shared = [s for s in store.list(Skill, org_id=org_id, scope="org")
              if s.status == "active" and s.owner_employee_id != employee_id]
    return sorted(own + shared, key=lambda s: s.created_at, reverse=True)


def _owned(skill_id: str, employee_id: str) -> Skill:
    sk = get_store().get(Skill, skill_id)
    if sk is None:
        raise SkillError(404, f"Skill {skill_id} not found")
    if sk.owner_employee_id != employee_id:
        # Don't reveal other employees' private skills.
        raise SkillError(404 if sk.scope == "private" else 403, f"Skill {skill_id} not found" if sk.scope == "private"
                         else "only the skill's owner can change it")
    return sk


def approve(skill_id: str, employee_id: str) -> Skill:
    sk = _owned(skill_id, employee_id)
    if sk.status != "draft":
        raise SkillError(409, f"only draft skills can be approved (status is {sk.status})")
    sk.status, sk.updated_at = "active", now()
    get_store().put(sk)
    return sk


def archive(skill_id: str, employee_id: str) -> Skill:
    sk = _owned(skill_id, employee_id)
    if sk.status == "archived":
        raise SkillError(409, "skill is already archived")
    was_shared = sk.scope == "org"
    sk.status, sk.updated_at = "archived", now()
    if was_shared:   # archived skills are never retrieved; take it off the org list too
        sk.scope, sk.shared_by, sk.shared_at = "private", None, None
    get_store().put(sk)
    if was_shared:
        from app.sharing import record
        record(sk.org_id, employee_id, "skill", sk.id, "unshare", "archived")
    return sk


def edit(skill_id: str, employee_id: str, name: str | None, description: str | None,
         steps: list[str] | None) -> tuple[Skill, bool]:
    """Returns (skill, unshared). Editing an org-shared skill makes it private again: the new content has not
    been through the guardrail, so it must be re-scanned (shared again) before colleagues can use it."""
    sk = _owned(skill_id, employee_id)
    if sk.status == "archived":
        raise SkillError(409, "archived skills can't be edited")
    changes = {k: v for k, v in {"name": name, "description": description, "steps": steps}.items() if v is not None}
    if not changes:
        return sk, False
    if "steps" in changes and not [s for s in changes["steps"] if s.strip()]:
        raise SkillError(400, "a skill needs at least one step")
    candidate = sk.model_copy(update=changes)
    if hits := secret_hits(skill_text(candidate)):
        raise SkillError(400, f"edit rejected: looks like it contains secrets or personal values ({', '.join(hits)})")
    candidate.version += 1
    candidate.updated_at = now()
    unshared = candidate.scope == "org"
    if unshared:
        candidate.scope, candidate.shared_by, candidate.shared_at = "private", None, None
    [candidate.embedding] = get_llm().embed([skill_doc_text(candidate)], "RETRIEVAL_DOCUMENT")
    get_store().put(candidate)
    if unshared:
        from app.sharing import record
        record(candidate.org_id, employee_id, "skill", candidate.id, "unshare", "edited")
    return candidate, unshared


# ---------------- retrieval ----------------

def score_skills(org_id: str, agent_kind: str, employee_id: str, query: str) -> list[tuple[Skill, float]]:
    """Active skills for this agent kind visible to the employee, scored against the query, best first."""
    candidates = [s for s in visible(org_id, employee_id)
                  if s.status == "active" and s.agent_kind == agent_kind and s.embedding]
    if not candidates:
        return []
    [q] = get_llm().embed([query], "RETRIEVAL_QUERY")
    scored = sorted(((s, cosine(q, s.embedding)) for s in candidates), key=lambda x: x[1], reverse=True)
    out, seen = [], set()
    for sk, sc in scored:   # one skill per owner and name, even if older data holds duplicates
        key = (sk.owner_employee_id, norm_name(sk.name))
        if key not in seen:
            seen.add(key)
            out.append((sk, sc))
    return out


def retrieve(org_id: str, agent_kind: str, employee_id: str, query: str) -> list[tuple[Skill, float]]:
    s = get_settings()
    with span("skills.retrieve", **{"orgmind.agent_kind": agent_kind, "orgmind.threshold": s.skill_min_score}) as sp:
        scored = score_skills(org_id, agent_kind, employee_id, query)
        kept = [(sk, sc) for sk, sc in scored[: s.skill_top_k] if sc >= s.skill_min_score]
        set_attrs(sp, **{"orgmind.skills.candidates": len(scored), "orgmind.skills.kept": len(kept),
                         "orgmind.top_score": round(scored[0][1], 3) if scored else None,
                         "orgmind.skill_ids": ",".join(sk.id for sk, _ in kept) or None})
        return kept
