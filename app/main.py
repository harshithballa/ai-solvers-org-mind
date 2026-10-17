import hmac
import re
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent import AnswerResult, answer
from app.config import get_settings
from app.distiller import DistillReport, distill
from app.llm import get_llm
from app.models import Agent, Employee, Message, Org, Playbook, RagDoc, Session, Skill, now
from app import rag_docs, skills
from app.rag import get_rag_provider
from app.seed import ORG_ID, seed
from app.store import get_store
from app import tracing

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tracing.init()
    if get_store().is_empty():
        seed(get_store())
    yield


app = FastAPI(title="OrgMind", lifespan=lifespan)

# One trace per chat / distill / compare request.
TRACED = [("POST", re.compile(r"^/api/sessions/[^/]+/messages$"), "chat"),
          ("POST", re.compile(r"^/api/distill$"), "distill"),
          ("POST", re.compile(r"^/api/compare$"), "compare"),
          ("POST", re.compile(r"^/api/rag/docs$"), "rag_upload"),
          ("GET", re.compile(r"^/api/rag/docs$"), "rag_docs"),
          ("POST", re.compile(r"^/api/rag/search$"), "rag_search"),
          ("POST", re.compile(r"^/api/skills/extract$"), "skills_extract")]


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    kind = next((k for m, rx, k in TRACED if request.method == m and rx.match(request.url.path)), None)
    if kind is None:
        return await call_next(request)
    t0 = time.perf_counter()
    status = 500
    with tracing.request_scope(kind, {k.lower(): v for k, v in request.headers.items()}) as rt:
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            # Flush before the response goes out: Cloud Run throttles CPU after that.
            await run_in_threadpool(tracing.finish, rt, status, (time.perf_counter() - t0) * 1000)


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
        "cited_docs": [{"id": h.doc.id, "title": h.doc.title} for h in r.cited_docs],
        "cited_skills": [{"id": sk.id, "name": sk.name, "version": sk.version} for sk in r.cited_skills],
        "skills": {"error": r.skills_error,
                   "retrieved": [{"id": sk.id, "name": sk.name, "score": round(sc, 3)} for sk, sc in r.retrieved_skills]},
        "rag": {"used": r.rag_used, "error": r.rag_error,
                "retrieved": [_hit_payload(h) for h in r.retrieved_docs]},
    }


def _hit_payload(h: rag_docs.DocHit) -> dict:
    return {"doc_id": h.doc.id, "title": h.doc.title, "snippet": h.snippet,
            "score": None if h.score is None else round(h.score, 3), "source": h.source}


# ---------- org / demo ----------

@app.get("/api/health")
def health():
    s = get_settings()
    return {"ok": True, "store": s.store, "llm": get_llm().mode, "chat_model": s.chat_model, "rag": s.rag_provider,
            "cluster_threshold": s.cluster_threshold, "retrieval_threshold": s.retrieval_threshold,
            "tracing": s.tracing, "project": s.project or None}


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
    use_rag: bool = False
    use_skills: bool = False


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

    result = answer(agent, history, body.content, use_playbooks=body.use_playbooks,
                    use_rag=body.use_rag, employee_id=session.employee_id, use_skills=body.use_skills)
    store.add_message(Message(session_id=session_id, role="user", content=body.content))
    reply = Message(session_id=session_id, role="assistant", content=result.answer,
                    cited_playbook_ids=[p.id for p in result.cited],
                    cited_doc_ids=[h.doc.id for h in result.cited_docs],
                    cited_skill_ids=[sk.id for sk in result.cited_skills])
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
        base = pool.submit(tracing.wrap(answer), agent, [], body.question, False)
        withpb = pool.submit(tracing.wrap(answer), agent, [], body.question, True)
        return {"question": body.question, "baseline": _result_payload(base.result()),
                "with_playbooks": _result_payload(withpb.result())}


# ---------- document RAG ----------

def _rag_provider_or_503():
    provider = get_rag_provider()
    if provider is None:
        raise HTTPException(503, "document RAG is disabled (ORGMIND_RAG_PROVIDER=off)")
    return provider


class TextDoc(BaseModel):
    employee_id: str
    title: str
    text: str


@app.post("/api/rag/docs")
async def upload_rag_doc(request: Request):
    """Multipart (employee_id, file, optional title) for .txt/.md/.pdf up to 5 MB, or JSON {employee_id, title, text}."""
    provider = _rag_provider_or_503()
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        upload, employee_id = form.get("file"), str(form.get("employee_id") or "")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(400, "multipart upload needs a 'file' field")
        data = await upload.read(rag_docs.MAX_BYTES + 1)
        filename = upload.filename or "document"
        title = str(form.get("title") or "") or filename.rsplit(".", 1)[0]
    else:
        try:
            body = TextDoc.model_validate(await request.json())
        except Exception as e:
            raise HTTPException(422, f"expected multipart or JSON {{employee_id, title, text}}: {e}") from None
        employee_id, title, data = body.employee_id, body.title, body.text.encode("utf-8")
        filename = f"{rag_docs.safe_filename(body.title)[:60] or 'note'}.txt"
    _get_or_404(Employee, employee_id)
    try:
        doc = await run_in_threadpool(rag_docs.upload, provider, ORG_ID, employee_id, title, filename, data)
    except rag_docs.UploadError as e:
        raise HTTPException(400, str(e)) from None
    if doc.status == "failed":
        raise HTTPException(502, {"message": "upload or import failed", "doc": doc.model_dump(mode="json")})
    return doc


@app.get("/api/rag/docs")
def list_rag_docs(employee_id: str) -> list[RagDoc]:
    provider = _rag_provider_or_503()
    _get_or_404(Employee, employee_id)
    return [rag_docs.refresh(provider, d) for d in rag_docs.visible_docs(ORG_ID, employee_id)]


class RagSearch(BaseModel):
    employee_id: str
    query: str


@app.post("/api/rag/search")
def rag_search(body: RagSearch):
    provider = _rag_provider_or_503()
    _get_or_404(Employee, body.employee_id)
    try:
        hits, _ = rag_docs.search(provider, ORG_ID, body.employee_id, body.query)
    except Exception as e:
        tracing.log.warning("rag search failed: %s", e)
        raise HTTPException(502, f"search failed: {type(e).__name__}") from None
    return {"query": body.query, "results": [_hit_payload(h) for h in hits]}


# ---------- skills (one employee's learned procedures; not playbooks) ----------

def _skill_payload(sk: Skill) -> dict:
    return sk.model_dump(mode="json", exclude={"embedding"})


def _skill_call(fn, *args):
    try:
        return fn(*args)
    except skills.SkillError as e:
        raise HTTPException(e.status, str(e)) from None


class ExtractSkills(BaseModel):
    session_id: str
    employee_id: str


@app.post("/api/skills/extract")
def extract_skills(body: ExtractSkills):
    session = _get_or_404(Session, body.session_id)
    _get_or_404(Employee, body.employee_id)
    found, stats = _skill_call(skills.extract, session, body.employee_id)
    return {"session_id": session.id, "skills": [_skill_payload(sk) for sk in found],
            "proposed": stats.proposed, "rejected": stats.rejected, "redacted": stats.redacted,
            "replaced_drafts": stats.replaced_drafts}


@app.get("/api/skills")
def list_skills(employee_id: str):
    _get_or_404(Employee, employee_id)
    return [_skill_payload(sk) for sk in skills.visible(ORG_ID, employee_id)]


class SkillAction(BaseModel):
    employee_id: str


class SkillEdit(BaseModel):
    employee_id: str
    name: str | None = None
    description: str | None = None
    steps: list[str] | None = None


@app.post("/api/skills/{skill_id}/approve")
def approve_skill(skill_id: str, body: SkillAction):
    return _skill_payload(_skill_call(skills.approve, skill_id, body.employee_id))


@app.post("/api/skills/{skill_id}/archive")
def archive_skill(skill_id: str, body: SkillAction):
    return _skill_payload(_skill_call(skills.archive, skill_id, body.employee_id))


@app.patch("/api/skills/{skill_id}")
def edit_skill(skill_id: str, body: SkillEdit):
    return _skill_payload(_skill_call(skills.edit, skill_id, body.employee_id, body.name, body.description, body.steps))


@app.get("/api/traces")
def list_traces(limit: int = Query(50, ge=1, le=200)):
    s = get_settings()
    return {"tracing": s.tracing, "project": s.project or None,
            "calls": [c.model_dump(mode="json") for c in get_store().list_llm_calls(limit)]}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
