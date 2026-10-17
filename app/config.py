import os
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    project: str
    location: str
    store: str
    firestore_database: str
    llm: str
    chat_model: str
    embed_model: str
    embed_dim: int
    cluster_threshold: float
    retrieval_threshold: float
    retrieval_top_k: int
    admin_token: str
    tracing: str
    trace_cap: int
    rag_provider: str = "off"
    rag_datastore: str = ""
    rag_bucket: str = ""
    rag_location: str = "global"
    rag_min_score: float = 0.25
    rag_top_k: int = 3
    skill_min_score: float = 0.6
    skill_top_k: int = 2
    limits: str = "on"                                          # ORGMIND_LIMITS=off disables all cost caps
    daily_limits: dict = field(default_factory=dict)            # bucket -> max per UTC day (global)
    ip_limits: dict = field(default_factory=dict)               # bucket -> max per hour per client IP
    org_id: str = "acme"


# Per UTC day, global. Sized for an INR 1,000 judging budget (see scripts/cost_estimate.py) while leaving
# plenty of room for a normal judge session or a recording. Override with ORGMIND_LIMIT_<BUCKET>.
DAILY_LIMITS = {"gemini_calls": 600, "chat_messages": 250, "distill": 15, "skills_extract": 30, "share_scans": 40,
                "compare": 120, "rag_uploads": 20, "sessions_created": 80}
# Best-effort per client IP, per hour (in-memory, per instance). Override with ORGMIND_IP_LIMIT_<BUCKET>.
IP_LIMITS = {"chat_messages": 40, "distill": 3}


def _limits(prefix: str, defaults: dict) -> dict:
    return {b: int(os.getenv(f"{prefix}{b.upper()}", str(v))) for b, v in defaults.items()}


@lru_cache
def get_settings() -> Settings:
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    return Settings(
        project=project,
        location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        store=os.getenv("ORGMIND_STORE", "memory"),
        firestore_database=os.getenv("ORGMIND_FIRESTORE_DATABASE", "(default)"),
        llm=os.getenv("ORGMIND_LLM", "vertex" if project else "fake"),
        chat_model=os.getenv("ORGMIND_CHAT_MODEL", "gemini-2.5-flash"),
        embed_model=os.getenv("ORGMIND_EMBED_MODEL", "gemini-embedding-001"),
        embed_dim=int(os.getenv("ORGMIND_EMBED_DIM", "768")),
        cluster_threshold=float(os.getenv("ORGMIND_CLUSTER_THRESHOLD", "0.88")),
        retrieval_threshold=float(os.getenv("ORGMIND_RETRIEVAL_THRESHOLD", "0.62")),
        retrieval_top_k=int(os.getenv("ORGMIND_RETRIEVAL_TOP_K", "3")),
        admin_token=os.getenv("ORGMIND_ADMIN_TOKEN", ""),
        tracing=os.getenv("ORGMIND_TRACING", "off"),
        trace_cap=int(os.getenv("ORGMIND_TRACE_CAP", "500")),
        rag_provider=os.getenv("ORGMIND_RAG_PROVIDER", "off"),
        rag_datastore=os.getenv("ORGMIND_RAG_DATASTORE", ""),
        rag_bucket=os.getenv("ORGMIND_RAG_BUCKET", ""),
        rag_location=os.getenv("ORGMIND_RAG_LOCATION", "global"),
        rag_min_score=float(os.getenv("ORGMIND_RAG_MIN_SCORE", "0.25")),
        rag_top_k=int(os.getenv("ORGMIND_RAG_TOP_K", "3")),
        skill_min_score=float(os.getenv("ORGMIND_SKILL_MIN_SCORE", "0.6")),
        skill_top_k=int(os.getenv("ORGMIND_SKILL_TOP_K", "2")),
        limits=os.getenv("ORGMIND_LIMITS", "on"),
        daily_limits=_limits("ORGMIND_LIMIT_", DAILY_LIMITS),
        ip_limits=_limits("ORGMIND_IP_LIMIT_", IP_LIMITS),
    )
