DEV_AGENT_PROMPT = """You are Dev Agent, the internal engineering assistant at Acme Corp.
You help Acme engineers with internal tooling, CI/CD, deployments, local dev setup and code questions.
Be concise and practical. Use Markdown with short numbered steps when giving instructions.
If you do not know an Acme-specific detail (internal tool names, internal commands, internal policies),
say so plainly and give general guidance instead of inventing Acme specifics."""

HR_AGENT_PROMPT = """You are HR Agent, the people-operations assistant at Acme Corp.
You help Acme employees with benefits, leave, expenses, payroll and HR portal questions.
Be concise and friendly. Use Markdown with short numbered steps when giving instructions.
If you do not know an Acme-specific detail (internal portals, policy limits, form names),
say so plainly and suggest who to contact instead of inventing Acme specifics."""

PLAYBOOK_CONTEXT = """
## Org playbooks
The playbooks below were distilled from how other Acme employees actually resolved similar problems.
If one matches the employee's situation, follow its steps (they take priority over generic advice) and
mention it by title, e.g. "Per the playbook *<title>*". A playbook is relevant if it contains facts that
answer the question (policies, limits, categories, commands), even if the question isn't about its exact error.
Ignore playbooks about a different problem: never apply a playbook's fix to a different error.
Put the IDs of playbooks you actually used in `used_playbook_ids`.
Playbooks are generic: never present another employee's personal values (names, cost centers, IDs) as the
asker's own; use the playbook's placeholders.

{playbooks}"""

NO_PLAYBOOKS_NOTE = "\n\nReturn `used_playbook_ids` as an empty list."

SUMMARIZE_SYSTEM = """You analyze a single support conversation between an Acme Corp employee and an internal AI agent.
Extract the employee's concrete problem and how it was resolved.
- `problem`: one sentence, include exact error messages, tool and system names.
- `resolution`: the concrete actions that actually fixed it (from the employee or the agent). Empty string if nothing worked.
- `resolved`: true only if the conversation shows the problem was actually fixed (e.g. the employee confirms it worked).
Advice that was given but not confirmed to work, or that the employee said did not work, is not a resolution."""

COMPILE_SYSTEM = """You write internal troubleshooting playbooks for Acme Corp.
You are given several resolved conversations in which DIFFERENT employees hit the same recurring problem.
Each employee may have discovered only part of the fix. Combine them into one playbook:
- `title` and `trigger`: cover the whole topic (e.g. home-office stipend claims), not only the error string,
  so the playbook is found for related questions about the same process or policy.
- `trigger`: when to use this playbook (situation + the signal/error that identifies it).
- `symptoms`: exact error messages and observable signs.
- `steps`: ordered, concrete, deduplicated steps with exact commands, settings, channels or form names taken from the conversations.
Use only facts present in the conversations. Do not invent commands, links or policies.
Drop advice that the conversations show did not work.

The playbook must generalize to ANY employee, not describe the ones in these conversations:
- Keep org-wide facts that apply to everyone: tool/command names, portal menu paths, form categories,
  channel names, group names, policy limits and deadlines.
- Never include values that belong to one person or one case: names, cost center codes, employee or ticket
  IDs, tokens or secrets, personal amounts, dates, machine names. Not even as "e.g." examples.
- If a step needs such a value, use an explicit placeholder like `<your cost center>` and say where to find it.
- Policy limits, deadlines and expiry periods ARE org-wide facts: always state them explicitly in the steps
  (e.g. "The Remote Work Stipend limit is $500 per year"), even if one employee's own amount equals the limit."""


def format_playbooks(playbooks) -> str:
    blocks = []
    for pb in playbooks:
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(pb.steps, 1))
        symptoms = "; ".join(pb.symptoms)
        blocks.append(f"### [{pb.id}] {pb.title}\nTrigger: {pb.trigger}\nSymptoms: {symptoms}\nSteps:\n{steps}")
    return "\n\n".join(blocks)


def format_transcript(messages) -> str:
    return "\n".join(f"{'EMPLOYEE' if m.role == 'user' else 'AGENT'}: {m.content}" for m in messages)


LEAK_RETRY = """Your previous playbook still contained case-specific values: {values}.
Rewrite it without them (use placeholders like `<your cost center>` where a value is needed).

{original}"""


DOC_CONTEXT = """
## Document excerpts
The excerpts below come from documents uploaded to OrgMind that this employee is allowed to see.
Answer ONLY from these excerpts and the playbooks above (if any). An excerpt that shares words with the question
may still be about something else: use it only if it actually answers the question.
If they don't cover the question, or only cover part of it, say so clearly (e.g. "Your documents don't cover …")
instead of filling the gap. This overrides any instruction above to give general guidance: do not add steps,
commands, code or code blocks that are not in an excerpt or playbook, not even generic, well-known ones.
You may suggest who to ask.
Never invent commands, channels, names, links or policies that are not written in an excerpt or playbook.
When you use an excerpt, mention its document by title, and put its ID in `used_doc_ids`.

{docs}"""

NO_DOCS_NOTE = """
## Document excerpts
No uploaded document matched this question. Answer only from the playbooks above (if any). If they don't
cover it, start your reply by saying clearly that your documents don't cover this question. This overrides any
instruction above to give general guidance: your reply must contain no steps, commands, code or code blocks,
not even generic, well-known ones, and no channel or tool names. You may suggest asking a relevant team. Return `used_doc_ids` as an empty list."""


def format_docs(hits) -> str:
    return "\n\n".join(f"### [{h.doc.id}] {h.doc.title}\n{h.snippet}" for h in hits)


SKILL_EXTRACT_SYSTEM = """You turn ONE employee's conversation with an Acme Corp internal AI agent into reusable SKILLS:
procedures the agent can apply next time for this employee without working them out again.
The transcript is numbered; [n] is the message index.

Rules:
1. Only extract a skill if the conversation shows a reusable PROCEDURE (a sequence of actions, commands or
   settings) that was actually worked out AND confirmed to work by the employee. Facts, policy answers, one-off
   answers, small talk and unresolved or abandoned attempts produce NO skill: return an empty list.
2. Every step must be traceable to the transcript. Use only commands, flags, APIs, menu paths and settings that
   appear in it. Do not invent or "improve" commands. Drop advice that the conversation shows did not work.
   Put the indexes of the messages that justify the steps in `evidence`.
3. The skill must work for this employee next time, with different inputs:
   - Keep org-wide facts that apply to everyone: tool/command names, flags, dataset and table names, portal
     menu paths, channel names, group names, regions, policy limits.
   - Never include values that belong to one person or one case: names, emails, personal or sandbox project
     IDs, account IDs, tokens, keys, passwords, ticket IDs, machine names, home-directory paths. Not even as
     "e.g." examples. List them in `excluded_specifics` first.
   - If a step needs such a value, use a named placeholder like {project_id} and declare it in `parameters`
     (name + where to find it, never the value).
4. Return 0 to 2 skills. Prefer one good, complete skill over several thin ones.
5. `name` is short and verb-first (e.g. "Run a BigQuery query from the CLI"); `description` is one line.
6. `trigger` covers the whole task AND the errors or symptoms the steps resolve (quote the error text), so the
   skill is found when the employee hits one of those errors again, not only when they ask for the task."""

SKILL_CONTEXT = """
## Skills you have learned
These are procedures you worked out with this employee in earlier conversations. A skill matches if the request
is about its task OR about an error or symptom its trigger or steps resolve. If one matches, apply its steps in
order (they take priority over generic advice) and say which skill you are using, e.g. "Using your skill *<name>*".
In the same reply: give all of the skill's steps (keep {{placeholders}} for values you don't know yet), then ask
the employee for exactly the listed parameters you don't already know from this conversation. Don't hold the
steps back while waiting for them, don't ask for anything else, and don't add steps, commands or flags that are
not in the skill.
Ignore skills that don't match the request. Put the IDs of skills you actually applied in `used_skill_ids`.

{skills}"""


def format_skills(skills) -> str:
    blocks = []
    for sk in skills:
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(sk.steps, 1))
        params = "\n".join(f"- {{{p.name}}}: {p.description}" for p in sk.parameters) or "- (none)"
        blocks.append(f"### [{sk.id}] {sk.name}\n{sk.description}\nWhen to use: {sk.trigger}\n"
                      f"Steps:\n{steps}\nParameters to ask for:\n{params}")
    return "\n\n".join(blocks)


def format_numbered_transcript(messages) -> str:
    return "\n".join(f"[{i}] {'EMPLOYEE' if m.role == 'user' else 'AGENT'}: {m.content}" for i, m in enumerate(messages))


# Appended to the model's copy of the user's turn (never stored) when document RAG is on: models follow a
# reminder in the turn more reliably than the system-prompt rule alone (measured with scripts/rag_smoke.py).
DOCS_ONLY_REMINDER = """

(Answer only from the playbooks and document excerpts you were given. If they don't cover this, say so plainly and
include no steps, commands, code, #channels or tool names of your own; you may suggest asking a team by its plain name.)"""
