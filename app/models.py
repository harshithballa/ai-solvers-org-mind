from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


AgentKind = Literal["dev", "hr"]


# ---- stored entities ----

class Org(BaseModel):
    id: str
    name: str
    created_at: datetime = Field(default_factory=now)


class Employee(BaseModel):
    id: str
    org_id: str
    name: str
    role: str
    team: str
    avatar_color: str = "#888888"


class Agent(BaseModel):
    id: str
    org_id: str
    kind: AgentKind
    name: str
    team: str
    description: str
    system_prompt: str


class SessionSummary(BaseModel):
    problem: str
    context: str
    resolution: str
    resolved: bool
    embedding: list[float] = []


class Session(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ses"))
    org_id: str
    employee_id: str
    agent_id: str
    agent_kind: AgentKind
    title: str = "New conversation"
    status: Literal["open", "closed"] = "open"
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    summary: SessionSummary | None = None
    summarized_at: datetime | None = None


class Message(BaseModel):
    id: str = Field(default_factory=lambda: new_id("msg"))
    session_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime = Field(default_factory=now)
    cited_playbook_ids: list[str] = []
    cited_doc_ids: list[str] = []


class Playbook(BaseModel):
    id: str = Field(default_factory=lambda: new_id("pb"))
    org_id: str
    agent_kind: AgentKind
    title: str
    trigger: str
    symptoms: list[str]
    steps: list[str]
    source_session_ids: list[str]
    source_employee_ids: list[str]
    related_unresolved_session_ids: list[str] = []
    version: int = 1
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    embedding: list[float] = []          # retrieval (RETRIEVAL_DOCUMENT)
    centroid: list[float] = []           # mean of source summary embeddings, for cluster matching


RagScope = Literal["private", "org"]
RagStatus = Literal["uploading", "indexing", "ready", "failed"]


class RagDoc(BaseModel):
    """An uploaded document. Ownership and scope live here, never in the search index."""
    id: str = Field(default_factory=lambda: new_id("doc"))
    org_id: str
    owner_employee_id: str
    title: str
    scope: RagScope = "private"
    gcs_uri: str = ""                    # provider URI (gs://... for Vertex); search hits map back via this
    mime_type: str
    size: int
    status: RagStatus = "uploading"
    import_operation: str = ""
    error: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class LLMCall(BaseModel):
    """One recorded Gemini call (in-app trace view). Capped collection, not part of demo data."""
    id: str
    ts: datetime
    request: str                      # chat | distill | compare | other
    trace_id: str = ""
    span_id: str = ""
    operation: str                    # answer | summarize | compile_playbook | embed:<task>
    model: str
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    playbook_ids: list[str] = []
    error: str | None = None
    prompt_excerpt: str = ""
    response_excerpt: str = ""


# ---- Gemini structured-output schemas (no defaults: keep schemas plain) ----

class SummaryOut(BaseModel):
    problem: str = Field(description="One sentence: the concrete problem the employee had, including exact error text or system names.")
    context: str = Field(description="Relevant environment details: tools, systems, what they were doing.")
    resolution: str = Field(description="What actually fixed it, as concrete actions. Empty string if not fixed.")
    resolved: bool = Field(description="True only if the conversation shows the problem was actually fixed.")


class PlaybookOut(BaseModel):
    org_wide_facts: list[str] = Field(description="Fill this FIRST: facts that apply to every employee and must be kept: tool/command names, portal menu paths, categories, channels, groups, policy limits, deadlines and expiry periods (e.g. 'stipend limit $500 per year', 'receipts within 60 days').")
    excluded_specifics: list[str] = Field(description="Values that belong to one person or one case and must NOT appear in the playbook: names, cost center codes, IDs, tokens, the amount one person paid, dates, the item one person bought. A policy limit is an org-wide fact even if someone's claim amount equals it.")
    title: str = Field(description="Short imperative title naming the topic and the error, e.g. 'Fix expired ACME_VAULT_TOKEN on staging deploy'.")
    trigger: str = Field(description="When this playbook applies: the situation and the error/symptom that signals it.")
    symptoms: list[str] = Field(description="Observable symptoms or exact error messages.")
    steps: list[str] = Field(description="Ordered, concrete resolution steps with exact commands/settings/links.")


class AnswerOut(BaseModel):
    answer: str = Field(description="The reply to the employee, in Markdown.")
    used_playbook_ids: list[str] = Field(description="IDs of the provided playbooks actually used in the answer. Empty if none.")


class AnswerWithDocsOut(BaseModel):
    """Used only when the request asked for document RAG, so the playbook-only schema stays unchanged."""
    answer: str = Field(description="The reply to the employee, in Markdown.")
    used_playbook_ids: list[str] = Field(description="IDs of the provided playbooks actually used in the answer. Empty if none.")
    used_doc_ids: list[str] = Field(description="IDs of the provided document excerpts actually used in the answer. Empty if none.")
