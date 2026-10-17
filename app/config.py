import os
from dataclasses import dataclass
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
    org_id: str = "acme"


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
    )
