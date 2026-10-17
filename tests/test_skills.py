import pytest
from fastapi.testclient import TestClient

from app import skills
from app.main import app
from app.models import Message, Session, Skill
from app.seed import BIGQUERY_SESSION_ID, ORG_ID

client = TestClient(app)
BQ_QUESTION = "How do I run a bq query against acme-analytics.events.orders with --use_legacy_sql=false?"


@pytest.fixture(autouse=True)
def _clear_llm_calls(fresh_store):
    yield
    fresh_store._llm_calls.clear()  # llm_calls survives reset; keep test_tracing's view clean


def _extract(session_id=BIGQUERY_SESSION_ID, employee_id="e_dana"):
    return client.post("/api/skills/extract", json={"session_id": session_id, "employee_id": employee_id})


def _make_session(store, sid, employee_id, turns, agent_id="a_dev"):
    store.put(Session(id=sid, org_id=ORG_ID, employee_id=employee_id, agent_id=agent_id, agent_kind="dev"))
    for i, t in enumerate(turns):
        store.add_message(Message(session_id=sid, role="user" if i % 2 == 0 else "assistant", content=t))


def _approved_skill():
    [sk] = _extract().json()["skills"]
    assert client.post(f"/api/skills/{sk['id']}/approve", json={"employee_id": "e_dana"}).status_code == 200
    return sk


# ---------- extraction ----------

def test_extract_happy_path():
    res = _extract()
    assert res.status_code == 200, res.text
    [sk] = res.json()["skills"]
    assert sk["status"] == "draft" and sk["scope"] == "private" and sk["owner_employee_id"] == "e_dana"
    assert sk["source_session_id"] == BIGQUERY_SESSION_ID and sk["agent_kind"] == "dev" and sk["version"] == 1
    assert sk["evidence"] and all(0 <= i < 8 for i in sk["evidence"])
    assert any("--use_legacy_sql=false" in s for s in sk["steps"])
    assert "embedding" not in sk


def test_small_talk_yields_no_skill(fresh_store):
    _make_session(fresh_store, "ses_chat", "e_dana", ["hi there", "Hello! How can I help?",
                                                      "just saying thanks for yesterday", "You're welcome!"])
    res = _extract("ses_chat")
    assert res.status_code == 200 and res.json()["skills"] == []
    assert fresh_store.list(Skill, source_session_id="ses_chat") == []


def test_only_owner_can_extract_and_session_needs_messages(fresh_store):
    assert _extract(employee_id="e_noah").status_code == 403
    _make_session(fresh_store, "ses_short", "e_dana", ["hi", "hello"])
    assert _extract("ses_short").status_code == 400
    assert _extract("ses_missing").status_code == 404


def test_reextract_replaces_drafts_but_keeps_active(fresh_store):
    first = _extract().json()["skills"][0]["id"]
    second = _extract().json()
    assert second["replaced_drafts"] == 1
    ids = {s.id for s in fresh_store.list(Skill, source_session_id=BIGQUERY_SESSION_ID)}
    assert first not in ids and ids == {second["skills"][0]["id"]}

    client.post(f"/api/skills/{second['skills'][0]['id']}/approve", json={"employee_id": "e_dana"})
    third = _extract().json()
    assert third["replaced_drafts"] == 0
    rows = fresh_store.list(Skill, source_session_id=BIGQUERY_SESSION_ID)
    assert sorted(s.status for s in rows) == ["active", "draft"]


def test_secret_patterns_are_rejected(fresh_store):
    _make_session(fresh_store, "ses_secret", "e_dana", [
        "How do I call the reports API?",
        "Send the header with your token.",
        "Ran `curl -H 'Authorization: Bearer abcd1234efgh5678ijkl' https://reports.acme.internal/v1` and it worked.",
        "Great!"])
    res = _extract("ses_secret").json()
    assert res["proposed"] == 1 and res["rejected"] == 1 and res["skills"] == []


@pytest.mark.parametrize("text,kind", [
    ("export AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP", "aws_access_key"),
    ("mail dana.kim@acme.example for access", "email"),
    ("token 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b", "long_hex"),
    ("key c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3JldDEyMw==", "base64_blob"),
])
def test_secret_pattern_detector(text, kind):
    assert kind in skills.secret_hits(text)
    assert skills.secret_hits("bq query --project_id={project_id} --use_legacy_sql=false") == []


def test_personal_values_flagged_by_model_are_redacted():
    from app.models import SkillDraftOut
    draft = SkillDraftOut(excluded_specifics=["acme-sbx-dkim-4821"], name="Run a query", description="d", trigger="t",
                          steps=["bq query --project_id=acme-sbx-dkim-4821"], parameters=[], evidence=[1])
    stats = skills.ExtractStats()
    out = skills._check(draft, 4, stats)
    assert out.steps == ["bq query --project_id={redacted}"] and stats.redacted == 1
    assert skills._check(draft.model_copy(update={"evidence": [99]}), 4, stats) is None


def test_untraceable_code_detector():
    transcript = "run `bq query --project_id=acme-sbx-1 --use_legacy_sql=false 'SELECT 1'` it worked"
    assert skills.untraceable_code(["Run `bq query --project_id={project_id} --use_legacy_sql=false`"], transcript) == []
    assert skills.untraceable_code(["Run `bq mk --dataset foo`"], transcript) == ["bq mk --dataset foo"]
    nested = "FROM `acme-analytics.events.orders` worked: bq query --use_legacy_sql=false 'SELECT 1 FROM `t.x`'"
    assert skills.untraceable_code(["e.g. `bq query --use_legacy_sql=false 'SELECT 1 FROM \\`t.x\\`'`"], nested) == []


# ---------- lifecycle / visibility ----------

def test_draft_approve_archive_transitions():
    sk = _extract().json()["skills"][0]
    url = f"/api/skills/{sk['id']}"
    assert client.post(f"{url}/approve", json={"employee_id": "e_noah"}).status_code == 404   # private: not revealed
    res = client.post(f"{url}/approve", json={"employee_id": "e_dana"})
    assert res.status_code == 200 and res.json()["status"] == "active"
    assert client.post(f"{url}/approve", json={"employee_id": "e_dana"}).status_code == 409
    res = client.post(f"{url}/archive", json={"employee_id": "e_dana"})
    assert res.status_code == 200 and res.json()["status"] == "archived"
    assert client.post(f"{url}/archive", json={"employee_id": "e_dana"}).status_code == 409


def test_patch_bumps_version_and_is_owner_only():
    sk = _extract().json()["skills"][0]
    url = f"/api/skills/{sk['id']}"
    assert client.patch(url, json={"employee_id": "e_noah", "name": "x"}).status_code == 404
    res = client.patch(url, json={"employee_id": "e_dana", "name": "Run a BigQuery query from the CLI",
                                  "steps": ["Run `gcloud auth application-default login`"]})
    assert res.status_code == 200
    body = res.json()
    assert body["version"] == 2 and body["name"] == "Run a BigQuery query from the CLI" and len(body["steps"]) == 1
    assert client.patch(url, json={"employee_id": "e_dana", "description": "mail me at dana@acme.example"}).status_code == 400
    assert client.patch(url, json={"employee_id": "e_dana", "steps": [" "]}).status_code == 400


def test_drafts_never_visible_to_other_employees(fresh_store):
    sk = _extract().json()["skills"][0]
    assert [s["id"] for s in client.get("/api/skills?employee_id=e_dana").json()] == [sk["id"]]
    assert client.get("/api/skills?employee_id=e_noah").json() == []
    # Even an org-scoped draft stays with its owner; once active it is shared.
    row = fresh_store.get(Skill, sk["id"])
    fresh_store.put(row.model_copy(update={"scope": "org"}))
    assert client.get("/api/skills?employee_id=e_noah").json() == []
    client.post(f"/api/skills/{sk['id']}/approve", json={"employee_id": "e_dana"})
    assert [s["id"] for s in client.get("/api/skills?employee_id=e_noah").json()] == [sk["id"]]


# ---------- chat integration ----------

def _session(employee_id="e_dana"):
    return client.post("/api/sessions", json={"employee_id": employee_id, "agent_id": "a_dev"}).json()["id"]


def _ask(sid, **flags):
    res = client.post(f"/api/sessions/{sid}/messages", json={"content": BQ_QUESTION, **flags})
    assert res.status_code == 200, res.text
    return res.json()


def test_chat_applies_active_skill_and_cites_it():
    sk = _approved_skill()
    sid = _session()
    res = _ask(sid, use_skills=True)
    assert res["message"]["cited_skill_ids"] == [sk["id"]]
    assert res["cited_skills"][0]["id"] == sk["id"]
    assert client.get(f"/api/sessions/{sid}").json()["messages"][-1]["cited_skill_ids"] == [sk["id"]]


def test_drafts_and_other_employees_skills_are_not_used():
    _extract()  # draft only
    assert _ask(_session(), use_skills=True)["message"]["cited_skill_ids"] == []
    _approved_skill()
    res = _ask(_session("e_noah"), use_skills=True)
    assert res["message"]["cited_skill_ids"] == [] and res["skills"]["retrieved"] == []


def test_prompt_order_playbooks_skills_docs(monkeypatch, fresh_store):
    from app import agent as agent_mod
    from app import rag
    from app.llm import get_llm
    from app.models import Agent

    provider = rag.FakeRagProvider()
    rag.set_rag_provider(provider)
    fresh_store._data["rag_docs"].clear()
    try:
        client.post("/api/distill", json={})
        _approved_skill()
        client.post("/api/rag/docs", json={"employee_id": "e_dana", "title": "BQ notes",
                                           "text": "bq query needs --use_legacy_sql=false for acme-analytics; vault token expired on staging deploy"})
        seen = {}
        real = get_llm().generate_json

        def spy(system, contents, schema, fake, think=False):
            seen["system"], seen["schema"] = system, schema.__name__
            return real(system, contents, schema, fake, think)

        monkeypatch.setattr(get_llm(), "generate_json", spy)
        agent_mod.answer(fresh_store.get(Agent, "a_dev"), [],
                         BQ_QUESTION + " also staging deploy fails ACME_VAULT_TOKEN expired vault",
                         use_playbooks=True, use_rag=True, employee_id="e_dana", use_skills=True)
        sys_ = seen["system"]
        assert seen["schema"] == "AnswerWithSkillsAndDocsOut"
        assert sys_.index("## Org playbooks") < sys_.index("## Skills you have learned") < sys_.index("## Document excerpts")
    finally:
        rag.set_rag_provider(None)
        fresh_store._data["rag_docs"].clear()


def test_use_skills_off_is_unchanged(monkeypatch):
    from app.llm import get_llm
    _approved_skill()
    seen = {}
    real = get_llm().generate_json

    def spy(system, contents, schema, fake, think=False):
        seen["system"], seen["schema"] = system, schema.__name__
        return real(system, contents, schema, fake, think)

    monkeypatch.setattr(get_llm(), "generate_json", spy)
    res = _ask(_session())
    assert seen["schema"] == "AnswerOut" and "Skills you have learned" not in seen["system"]
    assert res["message"]["cited_skill_ids"] == [] and res["skills"]["retrieved"] == []


def test_skill_failure_degrades_gracefully(monkeypatch):
    from app import agent as agent_mod
    _approved_skill()

    def boom(*a, **k):
        raise RuntimeError("embedding service down")
    monkeypatch.setattr(agent_mod, "retrieve_skills", boom)
    res = _ask(_session(), use_skills=True)
    assert res["message"]["role"] == "assistant" and res["message"]["cited_skill_ids"] == []
    assert "embedding service down" in res["skills"]["error"]


def test_bigquery_session_does_not_change_distill(fresh_store):
    report = client.post("/api/distill", json={}).json()
    assert report["playbooks_created"] == 2
    assert not any(BIGQUERY_SESSION_ID in c["session_ids"] for c in report["clusters"] if c["outcome"] != "skipped")
