import hmac
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent import AnswerResult, answer
from app.config import get_settings
from app.distiller import DistillReport, distill
from app.llm import get_llm
from app.models import Agent, Employee, Message, Org, Playbook, Session, now
from app.seed import ORG_ID, seed
from app.store import get_store

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if get_store().is_empty():
        seed(get_store())
    yield


app = FastAPI(title="OrgMind", lifespan=lifespan)


def _get_or_404(cls, id: str):
    obj = get_store().get(cls, id)
    if obj is None:
        raise HTTPException(404, f"{cls.__name__} {id} not found")
    return obj


def _result_payload(r: AnswerResult) -> dict:
    return {
        "answer": r.answer,
        "cited": [{"id": p.id, "title": p.title, "version": p.version} for p in r.cited],
        "retrieved": [{"id": p.id, "title": p.title, "score": round(sc, 3)} for p, sc in r.retrieved],
    }


# ---------- org / demo ----------

@app.get("/api/health")
def health():
    s = get_settings()
    return {"ok": True, "store": s.store, "llm": get_llm().mode, "chat_model": s.chat_model,
            "cluster_threshold": s.cluster_threshold, "retrieval_threshold": s.retrieval_threshold}


@app.get("/api/org")
def org():
    store = get_store()
    return {
        "org": _get_or_404(Org, ORG_ID),
        "employees": store.list(Employee, org_id=ORG_ID),
        "agents": [a.model_dump(exclude={"system_prompt"}) for a in store.list(Agent, org_id=ORG_ID)],
    }


@app.post("/api/demo/reset")
def demo_reset(x_admin_token: str = Header(default="")):
    token = get_settings().admin_token
    if not token:
        raise HTTPException(403, "reset is disabled (ORGMIND_ADMIN_TOKEN not set)")
    if not hmac.compare_digest(x_admin_token, token):
        raise HTTPException(401, "invalid admin token")
    seed(get_store())
    return {"ok": True}


# ---------- sessions ----------

class NewSession(BaseModel):
    employee_id: str
    agent_id: str


class NewMessage(BaseModel):
    content: str
    use_playbooks: bool = True


@app.get("/api/sessions")
def list_sessions(employee_id: str | None = None, agent_id: str | None = None) -> list[Session]:
    filters = {k: v for k, v in {"employee_id": employee_id, "agent_id": agent_id}.items() if v}
    rows = get_store().list(Session, org_id=ORG_ID, **filters)
    for r in rows:
        r.summary = None if r.summary is None else r.summary.model_copy(update={"embedding": []})
    return sorted(rows, key=lambda s: s.updated_at, reverse=True)


@app.post("/api/sessions")
def create_session(body: NewSession) -> Session:
    _get_or_404(Employee, body.employee_id)
    agent = _get_or_404(Agent, body.agent_id)
    session = Session(org_id=ORG_ID, employee_id=body.employee_id, agent_id=agent.id, agent_kind=agent.kind)
    get_store().put(session)
    return session


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    session = _get_or_404(Session, session_id)
    if session.summary:
        session.summary.embedding = []
    return {"session": session, "messages": get_store().list_messages(session_id)}


@app.post("/api/sessions/{session_id}/messages")
def post_message(session_id: str, body: NewMessage):
    store = get_store()
    session = _get_or_404(Session, session_id)
    agent = _get_or_404(Agent, session.agent_id)
    history = store.list_messages(session_id)

    result = answer(agent, history, body.content, use_playbooks=body.use_playbooks)
    store.add_message(Message(session_id=session_id, role="user", content=body.content))
    reply = Message(session_id=session_id, role="assistant", content=result.answer,
                    cited_playbook_ids=[p.id for p in result.cited])
    store.add_message(reply)

    if not history:
        session.title = body.content.replace("`", "")[:70]
    session.updated_at = now()
    store.put(session)
    return {"message": reply, **_result_payload(result)}


# ---------- distill / playbooks / compare ----------

class DistillRequest(BaseModel):
    agent_kind: str | None = None


@app.post("/api/distill")
def run_distill(body: DistillRequest | None = None) -> DistillReport:
    return distill(ORG_ID, body.agent_kind if body else None)


@app.get("/api/playbooks")
def list_playbooks(agent_kind: str | None = None):
    filters = {"agent_kind": agent_kind} if agent_kind else {}
    rows = get_store().list(Playbook, org_id=ORG_ID, **filters)
    return [p.model_dump(exclude={"embedding", "centroid"}) for p in sorted(rows, key=lambda p: p.updated_at, reverse=True)]


@app.get("/api/playbooks/{playbook_id}")
def get_playbook(playbook_id: str):
    store = get_store()
    pb = _get_or_404(Playbook, playbook_id)
    employees = {e.id: e for e in store.list(Employee, org_id=ORG_ID)}
    sources = []
    for sid in pb.source_session_ids + pb.related_unresolved_session_ids:
        s = store.get(Session, sid)
        if s:
            emp = employees.get(s.employee_id)
            sources.append({"session_id": s.id, "title": s.title, "employee": emp.name if emp else s.employee_id,
                            "resolved": sid in pb.source_session_ids, "created_at": s.created_at})
    return {"playbook": pb.model_dump(exclude={"embedding", "centroid"}), "sources": sources}


class CompareRequest(BaseModel):
    agent_id: str
    question: str


@app.post("/api/compare")
def compare(body: CompareRequest):
    agent = _get_or_404(Agent, body.agent_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        base = pool.submit(answer, agent, [], body.question, False)
        withpb = pool.submit(answer, agent, [], body.question, True)
        return {"question": body.question, "baseline": _result_payload(base.result()),
                "with_playbooks": _result_payload(withpb.result())}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
