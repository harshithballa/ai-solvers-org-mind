from app.config import get_settings
from app.llm import cosine, get_llm
from app.models import Playbook
from app.store import get_store
from app.tracing import set_attrs, span


def score_playbooks(org_id: str, agent_kind: str, query: str) -> list[tuple[Playbook, float]]:
    """All playbooks for this org + agent kind, scored against the query, best first."""
    playbooks = [p for p in get_store().list(Playbook, org_id=org_id, agent_kind=agent_kind) if p.embedding]
    if not playbooks:
        return []
    [q] = get_llm().embed([query], "RETRIEVAL_QUERY")
    return sorted(((p, cosine(q, p.embedding)) for p in playbooks), key=lambda x: x[1], reverse=True)


def retrieve_playbooks(org_id: str, agent_kind: str, query: str) -> list[tuple[Playbook, float]]:
    """Top-k playbooks whose similarity clears the retrieval threshold."""
    s = get_settings()
    with span("orgmind.retrieval", **{"orgmind.agent_kind": agent_kind, "orgmind.threshold": s.retrieval_threshold}) as sp:
        scored = score_playbooks(org_id, agent_kind, query)
        hits = [(p, sc) for p, sc in scored[: s.retrieval_top_k] if sc >= s.retrieval_threshold]
        set_attrs(sp, **{"orgmind.playbooks_scored": len(scored),
                         "orgmind.top_score": round(scored[0][1], 3) if scored else None,
                         "orgmind.retrieved_ids": ",".join(p.id for p, _ in hits) or None})
        return hits


def playbook_doc_text(pb: Playbook) -> str:
    return f"{pb.title}\n{pb.trigger}\n" + "\n".join(pb.symptoms) + "\n" + "\n".join(pb.steps)
