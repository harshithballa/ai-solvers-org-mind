import os

os.environ.update({
    "ORGMIND_LLM": "fake",
    "ORGMIND_STORE": "memory",
    "ORGMIND_CLUSTER_THRESHOLD": "0.35",
    "ORGMIND_RETRIEVAL_THRESHOLD": "0.2",
    "ORGMIND_SKILL_MIN_SCORE": "0.2",
    "ORGMIND_ADMIN_TOKEN": "test-token",
    "ORGMIND_TRACING": "off",  # never export spans from unit tests
})

import pytest  # noqa: E402

from app.seed import seed  # noqa: E402
from app.store import get_store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_store():
    seed(get_store())
    yield get_store()
