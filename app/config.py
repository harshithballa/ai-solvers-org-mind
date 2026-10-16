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
    )
