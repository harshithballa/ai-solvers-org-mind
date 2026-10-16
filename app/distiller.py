"""Distiller: summarize sessions -> cluster across employees -> compile/update playbooks.

Rules (see CLAUDE.md): a playbook needs resolved sessions from >= 2 distinct employees; only resolved
sessions contribute steps; a cluster matching an existing playbook recompiles it and bumps the version.
"""
import re
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel

from app.config import get_settings
from app.llm import cosine, get_llm, normalize
from app.models import Playbook, PlaybookOut, Session, SessionSummary, SummaryOut, now
from app.prompts import COMPILE_SYSTEM, SUMMARIZE_SYSTEM, format_transcript
from app.retrieval import playbook_doc_text
from app.store import get_store

MIN_EMPLOYEES = 2


class ClusterReport(BaseModel):
    agent_kind: str
    session_ids: list[str]
    employee_ids: list[str]
    resolved_session_ids: list[str]
    outcome: str  # created | updated | unchanged | skipped
    reason: str = ""
    playbook_id: str | None = None
    title: str | None = None


class DistillReport(BaseModel):
    sessions_scanned: int
    sessions_summarized: int
    clusters: list[ClusterReport]
    playbooks_created: int
    playbooks_updated: int


# ---------- 1. summarize ----------

_RESOLVED_RE = re.compile(r"\b(worked|fixed|resolved|works now|went through|that did it|approved)\b", re.I)
_NEGATED_RE = re.compile(r"\b(didn'?t|did not|doesn'?t|not|still)\b[^.]{0,20}\b(work|fix)", re.I)


def _fake_summary(transcript_msgs) -> SummaryOut:
    users = [m.content for m in transcript_msgs if m.role == "user"]
    last = users[-1]
    resolved = len(users) > 1 and bool(_RESOLVED_RE.search(last)) and not _NEGATED_RE.search(last)
    return SummaryOut(problem=users[0], context="", resolution=last if resolved else "", resolved=resolved)


def summarize_session(session: Session) -> Session:
    msgs = get_store().list_messages(session.id)
    out = get_llm().generate_json(
        SUMMARIZE_SYSTEM, f"Conversation:\n{format_transcript(msgs)}", SummaryOut, fake=lambda: _fake_summary(msgs))
    [emb] = get_llm().embed([f"{out.problem}\n{out.context}"], "SEMANTIC_SIMILARITY")
    session.summary = SessionSummary(**out.model_dump(), embedding=emb)
    session.summarized_at = now()
    get_store().put(session)
    return session


def _needs_summary(s: Session) -> bool:
    return s.summary is None or s.summarized_at is None or s.summarized_at < s.updated_at


# ---------- 2. cluster ----------

def cluster_sessions(sessions: list[Session], threshold: float) -> list[list[Session]]:
    """Complete-linkage clustering: every pair inside a cluster must clear the threshold.

    Gemini embeddings of unrelated support chats still score ~0.8, so single linkage chains
    unrelated sessions together; complete linkage doesn't.
    """
    n = len(sessions)
    sim = [[cosine(sessions[i].summary.embedding, sessions[j].summary.embedding) for j in range(n)] for i in range(n)]
    label = list(range(n))
    pairs = sorted(((sim[i][j], i, j) for i in range(n) for j in range(i + 1, n) if sim[i][j] >= threshold), reverse=True)
    for _, i, j in pairs:
        a, b = label[i], label[j]
        if a == b:
            continue
        members_a = [k for k in range(n) if label[k] == a]
        members_b = [k for k in range(n) if label[k] == b]
        if all(sim[x][y] >= threshold for x in members_a for y in members_b):
            for k in members_b:
                label[k] = a
    groups: dict[int, list[Session]] = {}
    for i, s in enumerate(sessions):
        groups.setdefault(label[i], []).append(s)
    return [g for g in groups.values() if len(g) > 1]


# ---------- 3. compile ----------

def _centroid(sessions: list[Session]) -> list[float]:
    vecs = [s.summary.embedding for s in sessions]
    return normalize([sum(xs) / len(vecs) for xs in zip(*vecs)])


def compile_playbook(sources: list[Session]) -> PlaybookOut:
    store = get_store()
    parts = []
    for i, s in enumerate(sources, 1):
        transcript = format_transcript(store.list_messages(s.id))
        parts.append(f"--- Conversation {i} (employee {s.employee_id}) ---\n"
                     f"Problem: {s.summary.problem}\nResolution: {s.summary.resolution}\n\n{transcript}")

    def fake() -> PlaybookOut:
        return PlaybookOut(title=f"Fix: {sources[0].summary.problem[:60]}", trigger=sources[0].summary.problem,
                           symptoms=[s.summary.problem for s in sources],
                           steps=[s.summary.resolution for s in sources])

    return get_llm().generate_json(COMPILE_SYSTEM, "\n\n".join(parts), PlaybookOut, fake=fake, think=True)


def _find_existing(cluster: list[Session], centroid: list[float], playbooks: list[Playbook],
                   threshold: float) -> Playbook | None:
    ids = {s.id for s in cluster}
    for pb in playbooks:
        if ids & (set(pb.source_session_ids) | set(pb.related_unresolved_session_ids)):
            return pb
    best = max(playbooks, key=lambda pb: cosine(centroid, pb.centroid), default=None)
    if best and cosine(centroid, best.centroid) >= threshold:
        return best
    return None


# ---------- pipeline ----------

def distill(org_id: str, agent_kind: str | None = None) -> DistillReport:
    s = get_settings()
    store = get_store()
    sessions = [x for x in store.list(Session, org_id=org_id) if agent_kind in (None, x.agent_kind)]

    stale = [x for x in sessions if _needs_summary(x) and len(store.list_messages(x.id)) >= 2]
    with ThreadPoolExecutor(max_workers=8) as pool:
        summarized = {x.id: x for x in pool.map(summarize_session, stale)}
    sessions = [summarized.get(x.id, x) for x in sessions if x.summary or x.id in summarized]

    reports: list[ClusterReport] = []
    created = updated = 0
    for kind in sorted({x.agent_kind for x in sessions}):
        playbooks = store.list(Playbook, org_id=org_id, agent_kind=kind)
        for cluster in cluster_sessions([x for x in sessions if x.agent_kind == kind], s.cluster_threshold):
            resolved = [x for x in cluster if x.summary.resolved]
            unresolved_ids = [x.id for x in cluster if not x.summary.resolved]
            rep = ClusterReport(agent_kind=kind, session_ids=[x.id for x in cluster],
                                employee_ids=sorted({x.employee_id for x in cluster}),
                                resolved_session_ids=[x.id for x in resolved], outcome="skipped")
            reports.append(rep)

            if len({x.employee_id for x in resolved}) < MIN_EMPLOYEES:
                rep.reason = f"needs resolved sessions from >= {MIN_EMPLOYEES} distinct employees"
                continue

            centroid = _centroid(resolved)
            existing = _find_existing(cluster, centroid, playbooks, s.cluster_threshold)
            if existing:
                source_ids = list(dict.fromkeys(existing.source_session_ids + rep.resolved_session_ids))
                related = list(dict.fromkeys(existing.related_unresolved_session_ids + unresolved_ids))
                rep.playbook_id, rep.title = existing.id, existing.title
                if set(source_ids) == set(existing.source_session_ids) and set(related) == set(existing.related_unresolved_session_ids):
                    rep.outcome = "unchanged"
                    continue
                sources = [x for x in (store.get(Session, i) for i in source_ids) if x and x.summary and x.summary.resolved]
            else:
                sources, related = resolved, unresolved_ids

            out = compile_playbook(sources)
            pb = existing or Playbook(org_id=org_id, agent_kind=kind, title="", trigger="", symptoms=[], steps=[],
                                      source_session_ids=[], source_employee_ids=[])
            pb.title, pb.trigger, pb.symptoms, pb.steps = out.title, out.trigger, out.symptoms, out.steps
            pb.source_session_ids = [x.id for x in sources]
            pb.source_employee_ids = sorted({x.employee_id for x in sources})
            pb.related_unresolved_session_ids = related
            pb.centroid = _centroid(sources)
            [pb.embedding] = get_llm().embed([playbook_doc_text(pb)], "RETRIEVAL_DOCUMENT")
            if existing:
                pb.version += 1
                pb.updated_at = now()
                rep.outcome = "updated"
                updated += 1
            else:
                rep.outcome = "created"
                created += 1
                playbooks.append(pb)
            store.put(pb)
            rep.playbook_id, rep.title = pb.id, pb.title

    return DistillReport(sessions_scanned=len(sessions), sessions_summarized=len(summarized),
                         clusters=reports, playbooks_created=created, playbooks_updated=updated)
