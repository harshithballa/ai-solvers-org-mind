"""Answer pipeline: retrieve playbooks -> build prompt -> Gemini -> validated citations.

Only the current session's history goes into context, never other employees' sessions.
"""
from dataclasses import dataclass

from app.llm import get_llm
from app.models import Agent, AnswerOut, Message, Playbook
from app.prompts import NO_PLAYBOOKS_NOTE, PLAYBOOK_CONTEXT, format_playbooks
from app.retrieval import retrieve_playbooks

HISTORY_TURNS = 10


@dataclass
class AnswerResult:
    answer: str
    cited: list[Playbook]
    retrieved: list[tuple[Playbook, float]]


def answer(agent: Agent, history: list[Message], question: str, use_playbooks: bool = True) -> AnswerResult:
    prior_user = [m.content for m in history if m.role == "user"][-1:]
    retrieved = retrieve_playbooks(agent.org_id, agent.kind, "\n".join(prior_user + [question])) if use_playbooks else []
    playbooks = [p for p, _ in retrieved]

    system = agent.system_prompt
    system += PLAYBOOK_CONTEXT.format(playbooks=format_playbooks(playbooks)) if playbooks else NO_PLAYBOOKS_NOTE

    contents = [("user" if m.role == "user" else "model", m.content) for m in history[-HISTORY_TURNS:]]
    contents.append(("user", question))

    def fake() -> AnswerOut:
        if not playbooks:
            return AnswerOut(answer="I don't know the Acme-specific fix for this. General guidance: check logs and credentials.",
                             used_playbook_ids=[])
        pb = playbooks[0]
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(pb.steps, 1))
        return AnswerOut(answer=f"Per the playbook *{pb.title}*:\n{steps}", used_playbook_ids=[pb.id])

    out = get_llm().generate_json(system, contents, AnswerOut, fake=fake)
    by_id = {p.id: p for p in playbooks}
    cited = [by_id[i] for i in dict.fromkeys(out.used_playbook_ids) if i in by_id]
    return AnswerResult(answer=out.answer, cited=cited, retrieved=retrieved)
