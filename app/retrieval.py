from app.config import get_settings
from app.llm import cosine, get_llm
from app.models import Playbook
from app.store import get_store


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
    scored = score_playbooks(org_id, agent_kind, query)
    return [(p, sc) for p, sc in scored[: s.retrieval_top_k] if sc >= s.retrieval_threshold]


def playbook_doc_text(pb: Playbook) -> str:
    return f"{pb.title}\n{pb.trigger}\n" + "\n".join(pb.symptoms)
