from fastapi.testclient import TestClient

from app import tracing
from app.main import app
from app.models import LLMCall, now
from app.store import MemoryStore

client = TestClient(app)
QUESTION = "acme deploy staging fails with vault permission denied ACME_VAULT_TOKEN expired"


def _calls(limit=50):
    res = client.get(f"/api/traces?limit={limit}")
    assert res.status_code == 200
    return res.json()["calls"]


def test_tracing_is_off_in_unit_tests():
    assert client.get("/api/health").json()["tracing"] == "off"
    assert client.get("/api/health").json()["limits"] == "off"   # conftest sets ORGMIND_LIMITS=off
    assert tracing._provider is None
    assert client.get("/api/traces").json()["tracing"] == "off"


def test_compare_records_one_grouped_request_with_playbook_ids():
    client.post("/api/distill", json={})
    distill_calls = [c for c in _calls(200) if c["request"] == "distill"]
    ops = {c["operation"] for c in distill_calls}
    assert {"summarize", "compile_playbook", "embed:semantic_similarity", "embed:retrieval_document"} <= ops
    assert len({c["trace_id"] for c in distill_calls}) == 1

    client.post("/api/compare", json={"agent_id": "a_dev", "question": QUESTION})
    calls = [c for c in _calls() if c["request"] == "compare"]
    assert len({c["trace_id"] for c in calls}) == 1          # both answers share the request trace
    answers = [c for c in calls if c["operation"] == "answer"]
    assert len(answers) == 2                                   # baseline + with playbooks
    assert sorted(len(c["playbook_ids"]) for c in answers) == [0, 1]
    assert any(c["operation"] == "embed:retrieval_query" for c in calls)
    for c in calls:
        assert c["model"] == "fake" and c["latency_ms"] >= 0 and c["error"] is None
        assert len(c["prompt_excerpt"]) <= tracing.EXCERPT_CHARS + 1


def test_chat_request_is_recorded():
    sid = client.post("/api/sessions", json={"employee_id": "e_dana", "agent_id": "a_dev"}).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"content": QUESTION})
    newest = _calls(5)
    assert newest[0]["request"] == "chat"
    assert newest[0]["operation"] == "answer"


def test_recorder_failure_never_fails_the_request(monkeypatch, fresh_store):
    def boom(*a, **k):
        raise RuntimeError("firestore down")
    monkeypatch.setattr(fresh_store, "add_llm_calls", boom)
    sid = client.post("/api/sessions", json={"employee_id": "e_dana", "agent_id": "a_dev"}).json()["id"]
    res = client.post(f"/api/sessions/{sid}/messages", json={"content": "hello"})
    assert res.status_code == 200
    assert res.json()["message"]["role"] == "assistant"


def test_llm_error_is_recorded_and_reraised():
    with tracing.request_scope("other", {}) as rt:
        try:
            with tracing.llm_call("answer", "fake", "prompt"):
                raise ValueError("model exploded")
        except ValueError:
            pass
        else:
            raise AssertionError("error was swallowed")
    assert rt.calls[0].error.startswith("ValueError: model exploded")


def test_excerpts_truncated_and_cap_enforced():
    store = MemoryStore()
    long = "x" * 5000
    for i in range(8):
        store.add_llm_calls([LLMCall(id=f"c{i}", ts=now(), request="chat", operation="answer", model="fake",
                                     latency_ms=1.0, prompt_excerpt=tracing._excerpt(long))], cap=5)
    rows = store.list_llm_calls(50)
    assert [r.id for r in rows] == ["c7", "c6", "c5", "c4", "c3"]
    assert len(rows[0].prompt_excerpt) == tracing.EXCERPT_CHARS + 1


def test_traces_limit_validated():
    assert client.get("/api/traces?limit=0").status_code == 422
    assert client.get("/api/traces?limit=500").status_code == 422


def test_json_log_lines_carry_trace_correlation(monkeypatch):
    import dataclasses
    import json
    import logging

    from app.config import get_settings

    monkeypatch.setattr(tracing, "get_settings", lambda: dataclasses.replace(get_settings(), project="demo-project"))
    rec = logging.LogRecord("orgmind", logging.INFO, __file__, 1, "llm call %s", ("answer",), None)
    rec.json_fields = {"operation": "answer", "latency_ms": 12.5}
    rec.trace_id, rec.span_id = "a" * 32, "b" * 16
    line = json.loads(tracing._CloudJsonFormatter().format(rec))
    assert line["severity"] == "INFO" and line["message"] == "llm call answer"
    assert line["logging.googleapis.com/trace"] == "projects/demo-project/traces/" + "a" * 32
    assert line["logging.googleapis.com/spanId"] == "b" * 16
    assert line["operation"] == "answer"


def test_flush_never_holds_a_response_longer_than_its_timeout(monkeypatch):
    """A slow span export (the SDK's force_flush ignores its timeout) must not delay the response."""
    import threading
    import time as _time

    from app import tracing

    release = threading.Event()

    class SlowProvider:
        calls = 0

        def force_flush(self, timeout_millis=None):
            SlowProvider.calls += 1
            release.wait(5)

    monkeypatch.setattr(tracing, "_provider", SlowProvider())
    monkeypatch.setattr(tracing, "_flush_thread", None)
    try:
        t0 = _time.monotonic()
        tracing.flush(timeout_ms=200)
        tracing.flush(timeout_ms=200)   # export still running: waits again, but starts no second export
        assert _time.monotonic() - t0 < 1.5
        assert SlowProvider.calls == 1
    finally:
        release.set()
        tracing._flush_thread.join(2)
