"""Answer pipeline: retrieve playbooks (+ optional skills and document excerpts) -> build prompt -> Gemini ->
validated citations.

Only the current session's history goes into context, never other employees' sessions.
"""
import logging
from dataclasses import dataclass, field

from app.llm import get_llm
from app.models import (Agent, AnswerOut, AnswerWithDocsOut, AnswerWithSkillsAndDocsOut, AnswerWithSkillsOut, Message,
                        Playbook, Skill)
from app.prompts import (DOC_CONTEXT, DOCS_ONLY_REMINDER, NO_DOCS_NOTE, NO_PLAYBOOKS_NOTE, PLAYBOOK_CONTEXT, SKILL_CONTEXT, format_docs,
                         format_playbooks, format_skills)
from app.rag import get_rag_provider
from app.rag_docs import DocHit
from app.rag_docs import search as search_docs
from app.retrieval import retrieve_playbooks
from app.skills import retrieve as retrieve_skills
from app.tracing import set_attrs, span

HISTORY_TURNS = 10
log = logging.getLogger("orgmind")


@dataclass
class AnswerResult:
    answer: str
    cited: list[Playbook]
    retrieved: list[tuple[Playbook, float]]
    cited_docs: list[DocHit] = field(default_factory=list)
    retrieved_docs: list[DocHit] = field(default_factory=list)
    rag_used: bool = False
    rag_error: str | None = None
    cited_skills: list[Skill] = field(default_factory=list)
    retrieved_skills: list[tuple[Skill, float]] = field(default_factory=list)
    skills_error: str | None = None


def _retrieve_skills(agent: Agent, employee_id: str | None, query: str) -> tuple[list[tuple[Skill, float]], str | None]:
    """Active skills for this employee. Never raises: on failure the answer goes ahead without skills."""
    if not employee_id:
        return [], None
    try:
        return retrieve_skills(agent.org_id, agent.kind, employee_id, query), None
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:300]}"
        log.warning("skill retrieval failed, answering without skills: %s", err)
        return [], err


def _schema(skills_on: bool, docs_on: bool):
    return {(False, False): AnswerOut, (False, True): AnswerWithDocsOut,
            (True, False): AnswerWithSkillsOut, (True, True): AnswerWithSkillsAndDocsOut}[(skills_on, docs_on)]


def _retrieve_docs(agent: Agent, employee_id: str | None, query: str) -> tuple[list[DocHit], str | None]:
    """Document excerpts for this employee. Never raises: on any failure the answer goes ahead without RAG."""
    provider = get_rag_provider()
    if provider is None or not employee_id:
        return [], None
    try:
        hits, _ = search_docs(provider, agent.org_id, employee_id, query)
        return hits, None
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:300]}"
        log.warning("rag retrieval failed, answering without documents: %s", err)
        with span("rag.degraded", **{"orgmind.rag.error": err}) as sp:
            set_attrs(sp, **{"orgmind.rag.degraded": True})
        return [], err


def answer(agent: Agent, history: list[Message], question: str, use_playbooks: bool = True,
           use_rag: bool = False, employee_id: str | None = None, use_skills: bool = False) -> AnswerResult:
    prior_user = [m.content for m in history if m.role == "user"][-1:]
    query = "\n".join(prior_user + [question])
    retrieved = retrieve_playbooks(agent.org_id, agent.kind, query) if use_playbooks else []
    playbooks = [p for p, _ in retrieved]

    retrieved_skills, skills_error = _retrieve_skills(agent, employee_id, query) if use_skills else ([], None)
    skills = [sk for sk, _ in retrieved_skills]

    rag_on = use_rag and get_rag_provider() is not None
    docs, rag_error = _retrieve_docs(agent, employee_id, query) if rag_on else ([], None)
    rag_used = rag_on and rag_error is None  # a failed retrieval falls back to the plain prompt + schema

    # Playbooks, then skills, then documents.
    system = agent.system_prompt
    system += PLAYBOOK_CONTEXT.format(playbooks=format_playbooks(playbooks)) if playbooks else NO_PLAYBOOKS_NOTE
    if skills:
        system += SKILL_CONTEXT.format(skills=format_skills(skills))
    if rag_used:
        system += DOC_CONTEXT.format(docs=format_docs(docs)) if docs else NO_DOCS_NOTE

    contents = [("user" if m.role == "user" else "model", m.content) for m in history[-HISTORY_TURNS:]]
    contents.append(("user", question + DOCS_ONLY_REMINDER if rag_used else question))

    schema = _schema(bool(skills), rag_used)

    def fake():
        parts, ids = [], {"used_playbook_ids": [], "used_skill_ids": [], "used_doc_ids": []}
        if playbooks:
            pb = playbooks[0]
            steps = "\n".join(f"{i}. {s}" for i, s in enumerate(pb.steps, 1))
            parts.append(f"Per the playbook *{pb.title}*:\n{steps}")
            ids["used_playbook_ids"] = [pb.id]
        if skills:
            sk = skills[0]
            steps = "\n".join(f"{i}. {s}" for i, s in enumerate(sk.steps, 1))
            ask = ", ".join(p.name for p in sk.parameters)
            parts.append(f"Using your skill *{sk.name}*:\n{steps}" + (f"\nPlease tell me: {ask}." if ask else ""))
            ids["used_skill_ids"] = [sk.id]
        if docs:
            d = docs[0]
            parts.append(f"Per the document *{d.doc.title}*: {d.snippet}")
            ids["used_doc_ids"] = [d.doc.id]
        if not parts:
            parts.append("Your documents don't cover this question." if rag_used else
                         "I don't know the Acme-specific fix for this. General guidance: check logs and credentials.")
        return schema(answer="\n\n".join(parts), **{k: v for k, v in ids.items() if k in schema.model_fields})

    out = get_llm().generate_json(system, contents, schema, fake=fake)
    by_id = {p.id: p for p in playbooks}
    cited = [by_id[i] for i in dict.fromkeys(out.used_playbook_ids) if i in by_id]
    docs_by_id = {h.doc.id: h for h in docs}
    cited_docs = [docs_by_id[i] for i in dict.fromkeys(getattr(out, "used_doc_ids", [])) if i in docs_by_id]
    skills_by_id = {sk.id: sk for sk in skills}
    cited_skills = [skills_by_id[i] for i in dict.fromkeys(getattr(out, "used_skill_ids", [])) if i in skills_by_id]
    return AnswerResult(answer=out.answer, cited=cited, retrieved=retrieved, cited_docs=cited_docs,
                        retrieved_docs=docs, rag_used=rag_used, rag_error=rag_error, cited_skills=cited_skills,
                        retrieved_skills=retrieved_skills, skills_error=skills_error)
