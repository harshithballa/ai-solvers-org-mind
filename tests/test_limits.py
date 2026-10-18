import dataclasses
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import limits
from app.config import DAILY_LIMITS, IP_LIMITS, get_settings
from app.main import app
from app.models import AnswerOut, Message, Session

client = TestClient(app)


@pytest.fixture
def limited(monkeypatch, fresh_store):
    """Limits ON with small, test-chosen values (conftest runs everything else with ORGMIND_LIMITS=off)."""
    def configure(**daily):
        s = dataclasses.replace(get_settings(), limits="on", daily_limits={**DAILY_LIMITS, **daily},
                                ip_limits=dict(IP_LIMITS))
        monkeypatch.setattr(limits, "get_settings", lambda: s)
        return s
    fresh_store._counters.clear()
    limits.reset_ip_limits()
    configure()
    yield configure
    fresh_store._counters.clear()
    limits.reset_ip_limits()
    fresh_store._llm_calls.clear()


def _session(headers=None):
    res = client.post("/api/sessions", json={"employee_id": "e_dana", "agent_id": "a_dev"}, headers=headers or {})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _say(sid, text="hello", headers=None):
    return client.post(f"/api/sessions/{sid}/messages", json={"content": text}, headers=headers or {})


def test_counter_increments_and_rolls_over_by_utc_day(limited, fresh_store, monkeypatch):
    monkeypatch.setattr(limits, "utcnow", lambda: datetime(2026, 10, 17, 23, 59, tzinfo=timezone.utc))
    limits.hit("compare")
    limits.hit("compare")
    assert fresh_store.get_counter("2026-10-17-compare") == 2
    monkeypatch.setattr(limits, "utcnow", lambda: datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc))
    limits.hit("compare")
    assert fresh_store.get_counter("2026-10-18-compare") == 1
    assert fresh_store.get_counter("2026-10-17-compare") == 2


def test_429_at_the_limit_with_payload(limited):
    limited(chat_messages=1)
    sid = _session()
    assert _say(sid).status_code == 200
    res = _say(sid)
    assert res.status_code == 429
    body = res.json()
    assert body["limit"] == "chat_messages" and body["resets"] == "00:00 UTC"
    assert "today's chat limit" in body["detail"]


def test_admin_token_bypasses_and_is_not_counted(limited, fresh_store):
    limited(chat_messages=1)
    admin = {"X-Admin-Token": "test-token"}
    sid = _session(admin)
    for _ in range(3):
        assert _say(sid, headers=admin).status_code == 200
    assert fresh_store.get_counter(limits.day_key("chat_messages")) == 0
    assert _say(sid, headers={"X-Admin-Token": "wrong"}).status_code == 200   # counted: 1 of 1
    assert _say(sid, headers={"X-Admin-Token": "wrong"}).status_code == 429


def test_fail_open_when_counter_store_raises(limited, fresh_store, monkeypatch):
    limited(chat_messages=0, sessions_created=0)

    def boom(*a, **k):
        raise RuntimeError("firestore unavailable")
    monkeypatch.setattr(fresh_store, "incr_counter", boom)
    monkeypatch.setattr(fresh_store, "get_counter", boom)
    sid = _session()
    assert _say(sid).status_code == 200
    assert client.post("/api/distill", json={}).status_code == 200


def test_limits_off_changes_nothing(fresh_store):
    assert get_settings().limits == "off"
    sid = _session()
    for _ in range(5):
        assert _say(sid).status_code == 200
    assert fresh_store._counters == {}


def test_message_length_and_session_size_caps(fresh_store):
    sid = _session()
    res = _say(sid, "x" * 2001)
    assert res.status_code == 413 and "2000 characters" in res.json()["detail"]
    for i in range(60):
        fresh_store.add_message(Message(session_id=sid, role="user" if i % 2 == 0 else "assistant", content="m"))
    res = _say(sid)
    assert res.status_code == 409 and "start a new chat" in res.json()["detail"]


def test_gemini_ceiling_blocks_distill_up_front(limited, fresh_store):
    limited(gemini_calls=10)   # one distill of the 15 seeded sessions needs up to 2*15+12 = 42 calls
    res = client.post("/api/distill", json={})
    assert res.status_code == 429 and res.json()["limit"] == "gemini_calls"
    assert fresh_store.get_counter(limits.day_key("distill")) == 0                 # not consumed
    assert all(s.summary is None for s in fresh_store.list(Session, org_id="acme"))  # nothing ran half way


def test_every_real_gemini_call_counts_and_ceiling_stops_the_call(limited, fresh_store):
    from app.llm import VertexLLM
    calls = []
    llm = VertexLLM.__new__(VertexLLM)
    llm._s = get_settings()
    llm._client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kw: calls.append(kw) or SimpleNamespace(
            parsed=AnswerOut(answer="hi", used_playbook_ids=[]), usage_metadata=None, text="")))
    limited(gemini_calls=2)
    for _ in range(2):
        llm.generate_json("sys", "q", AnswerOut, fake=lambda: None)
    assert fresh_store.get_counter(limits.day_key("gemini_calls")) == 2
    assert calls[0]["config"].max_output_tokens == 2048
    with pytest.raises(limits.LimitExceeded):
        llm.generate_json("sys", "q", AnswerOut, fake=lambda: None)
    assert len(calls) == 2   # refused before reaching Gemini


def test_per_ip_chat_limit_uses_rightmost_forwarded_for(limited):
    s = limited()
    s.ip_limits["chat_messages"] = 2
    sid = _session()
    ip_a = {"X-Forwarded-For": "6.6.6.6, 1.1.1.1"}
    assert _say(sid, headers=ip_a).status_code == 200
    assert _say(sid, headers={"X-Forwarded-For": "7.7.7.7, 1.1.1.1"}).status_code == 200  # same right-most IP
    res = _say(sid, headers=ip_a)
    assert res.status_code == 429 and res.json()["resets"] == "within the hour"
    assert _say(sid, headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200


def test_usage_endpoint_requires_admin(limited):
    _session()
    assert client.get("/api/usage").status_code == 401
    res = client.get("/api/usage", headers={"X-Admin-Token": "test-token"})
    assert res.status_code == 200
    rows = {r["bucket"]: r for r in res.json()["buckets"]}
    assert rows["sessions_created"]["count"] == 1 and rows["gemini_calls"]["limit"] == 600


def test_clear_traces_only_selects_todays_counters():
    from scripts.clear_traces import todays_counter_ids

    ids = ["2026-10-18-chat_messages", "2026-10-18-gemini_calls", "2026-10-17-gemini_calls", "2026-10-180-x"]
    assert todays_counter_ids(ids, "2026-10-18") == ["2026-10-18-chat_messages", "2026-10-18-gemini_calls"]
