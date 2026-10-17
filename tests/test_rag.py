import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import rag, rag_docs
from app.config import get_settings
from app.main import app
from app.models import Agent, RagDoc
from app.rag import FakeRagProvider, SearchHit
from app.seed import ORG_ID

client = TestClient(app)
FREEZE = ("Acme internal note: the staging deploy freeze window is Thursdays 4pm-6pm IST.\n"
          "To request an exception, post in #release-ops with the ticket ID and tag @rohan-release.")


@pytest.fixture
def fake(fresh_store):
    provider = FakeRagProvider()
    rag.set_rag_provider(provider)
    fresh_store._data["rag_docs"].clear()  # rag_docs deliberately survives demo reset
    yield provider
    rag.set_rag_provider(None)
    fresh_store._llm_calls.clear()  # llm_calls survives reset; don't leak our distill into test_tracing


def _upload(employee_id, title, text):
    res = client.post("/api/rag/docs", json={"employee_id": employee_id, "title": title, "text": text})
    assert res.status_code == 200, res.text
    return res.json()


def _search(employee_id, query):
    res = client.post("/api/rag/search", json={"employee_id": employee_id, "query": query})
    assert res.status_code == 200, res.text
    return res.json()["results"]


# ---------- provider ----------

def test_fake_provider_is_deterministic_keyword_overlap():
    p = FakeRagProvider()
    a = p.ingest("private/e1/a.txt", FREEZE.encode(), "text/plain")
    p.ingest("private/e1/b.txt", b"Gym membership resets happen at the front desk.", "text/plain")
    assert p.check_status(a.operation, a.uri).status == "ready"
    hits = p.search("when is the staging deploy freeze window")
    assert [h.uri for h in hits] == [a.uri]
    assert "Thursdays 4pm-6pm IST" in hits[0].snippet
    assert 0 < hits[0].score <= 1
    assert p.search("when is the staging deploy freeze window") == hits
    assert p.search("quantum chromodynamics") == []


def test_snippet_html_is_stripped():
    d = {"snippets": [{"snippet": "the <b>staging deploy freeze</b> window&nbsp;is Thursdays &amp; tag&nbsp;...",
                       "snippet_status": "SUCCESS"}]}
    text, source = rag._best_excerpt(d)
    assert source == "snippet"
    assert text == "the staging deploy freeze window is Thursdays & tag ..."
    seg = {"extractive_segments": [{"content": "Segment one."}, {"content": "Segment two."}], **d}
    assert rag._best_excerpt(seg) == ("Segment one.\n…\nSegment two.", "extractive_segment")


def test_content_doc_id_matches_discovery_engine():
    # ID Discovery Engine assigned to gs://orgmind-rag-docs-arched-jetty/acme-test.txt (observed live)
    assert rag.content_doc_id("gs://orgmind-rag-docs-arched-jetty/acme-test.txt") == "ecfce8380e10d75254a38b0337c70a83"


# ---------- endpoints ----------

def test_upload_index_and_search_flow(fake):
    doc = _upload("e_dana", "Release freeze", FREEZE)
    assert doc["status"] == "indexing" and doc["scope"] == "private"
    assert doc["gcs_uri"] == f"fake://private/e_dana/{doc['id']}-Release-freeze.txt"
    [listed] = client.get("/api/rag/docs?employee_id=e_dana").json()
    assert listed["id"] == doc["id"] and listed["status"] == "ready"   # status refreshed on read
    [hit] = _search("e_dana", "staging deploy freeze window exception")
    assert hit["doc_id"] == doc["id"] and hit["title"] == "Release freeze"
    assert "#release-ops" in hit["snippet"]


def test_multipart_upload_and_validation(fake):
    res = client.post("/api/rag/docs", data={"employee_id": "e_dana"},
                      files={"file": ("runbook.md", b"# Runbook\nRotate the pager key monthly.", "text/markdown")})
    assert res.status_code == 200, res.text
    assert res.json()["title"] == "runbook" and res.json()["mime_type"] == "text/markdown"

    bad = [("notes.docx", b"hi"), ("fake.pdf", b"not a pdf"), ("big.txt", b"x" * (rag_docs.MAX_BYTES + 1)),
           ("empty.txt", b"")]
    for name, data in bad:
        r = client.post("/api/rag/docs", data={"employee_id": "e_dana"}, files={"file": (name, data)})
        assert r.status_code == 400, (name, r.text)
    assert client.post("/api/rag/docs", json={"employee_id": "nobody", "title": "t", "text": "x"}).status_code == 404


def test_scope_private_never_leaks_org_shared_for_all(fake, fresh_store):
    private = _upload("e_dana", "Dana private freeze note", FREEZE)
    shared = _upload("e_noah", "Org freeze policy", FREEZE.replace("Acme internal note", "Acme org policy"))
    doc = fresh_store.get(RagDoc, shared["id"])
    fresh_store.put(doc.model_copy(update={"scope": "org"}))   # sharing UI arrives in Phase 4

    q = "staging deploy freeze window"
    dana = {h["doc_id"] for h in _search("e_dana", q)}
    noah = {h["doc_id"] for h in _search("e_noah", q)}
    priya = {h["doc_id"] for h in _search("e_priya", q)}
    assert dana == {private["id"], shared["id"]}
    assert noah == {shared["id"]}
    assert priya == {shared["id"]}

    noah_list = {d["id"] for d in client.get("/api/rag/docs?employee_id=e_noah").json()}
    assert noah_list == {shared["id"]}

    _, stats = rag_docs.search(fake, ORG_ID, "e_noah", q)
    assert stats.raw == 2 and stats.scope_dropped == 1


def test_unmapped_and_low_score_hits_are_dropped(fake, monkeypatch):
    doc = _upload("e_dana", "Freeze", FREEZE)
    hits = [SearchHit(uri="gs://other/imported-elsewhere.txt", snippet="x", score=0.9, source="snippet"),
            SearchHit(uri=doc["gcs_uri"], snippet="weak", score=0.3, source="extractive_segment")]
    monkeypatch.setattr(fake, "search", lambda q, page_size=10: hits)
    out, stats = rag_docs.search(fake, ORG_ID, "e_dana", "anything")
    assert out == [] and stats.unmapped == 1 and stats.below_score == 1


def test_short_snippet_falls_back_to_full_text(fake, monkeypatch):
    doc = _upload("e_dana", "Freeze", FREEZE)
    monkeypatch.setattr(fake, "search", lambda q, page_size=10: [
        SearchHit(uri=doc["gcs_uri"], snippet="the staging deploy freeze window is Thursdays ...", score=0.8,
                  source="snippet")])
    [hit], stats = rag_docs.search(fake, ORG_ID, "e_dana", "freeze")
    assert hit.source == "full_text" and "@rohan-release" in hit.snippet and stats.fallback_reads == 1


def test_rag_endpoints_disabled_when_off():
    assert client.get("/api/health").json()["rag"] == "off"
    assert client.post("/api/rag/search", json={"employee_id": "e_dana", "query": "x"}).status_code == 503
    assert client.get("/api/rag/docs?employee_id=e_dana").status_code == 503


# ---------- chat integration ----------

def _session(employee_id="e_dana"):
    return client.post("/api/sessions", json={"employee_id": employee_id, "agent_id": "a_dev"}).json()["id"]


def test_chat_with_rag_cites_doc(fake):
    doc = _upload("e_dana", "Release freeze", FREEZE)
    sid = _session()
    res = client.post(f"/api/sessions/{sid}/messages",
                      json={"content": "When is the staging deploy freeze?", "use_rag": True}).json()
    assert res["message"]["cited_doc_ids"] == [doc["id"]]
    assert res["cited_docs"] == [{"id": doc["id"], "title": "Release freeze"}]
    assert res["rag"]["used"] and res["rag"]["error"] is None
    assert "Thursdays 4pm-6pm IST" in res["answer"]
    stored = client.get(f"/api/sessions/{sid}").json()["messages"][-1]
    assert stored["cited_doc_ids"] == [doc["id"]]


def test_chat_rag_respects_scope(fake):
    _upload("e_dana", "Release freeze", FREEZE)
    sid = _session("e_noah")
    res = client.post(f"/api/sessions/{sid}/messages",
                      json={"content": "When is the staging deploy freeze?", "use_rag": True}).json()
    assert res["message"]["cited_doc_ids"] == [] and res["rag"]["retrieved"] == []
    assert "don't cover" in res["answer"]


def test_playbooks_come_before_docs_in_prompt(fake, fresh_store, monkeypatch):
    from app import agent as agent_mod
    from app.llm import get_llm

    client.post("/api/distill", json={})
    _upload("e_dana", "Vault notes", "ACME_VAULT_TOKEN expired on staging deploy: also check the vault status page.")
    seen = {}
    real = get_llm().generate_json

    def spy(system, contents, schema, fake, think=False):
        seen["system"], seen["schema"] = system, schema.__name__
        return real(system, contents, schema, fake, think)

    monkeypatch.setattr(get_llm(), "generate_json", spy)
    dev = fresh_store.get(Agent, "a_dev")
    res = agent_mod.answer(dev, [], "staging deploy fails ACME_VAULT_TOKEN expired vault",
                           use_playbooks=True, use_rag=True, employee_id="e_dana")
    assert res.cited and res.cited_docs
    assert seen["schema"] == "AnswerWithDocsOut"
    assert seen["system"].index("## Org playbooks") < seen["system"].index("## Document excerpts")


def test_rag_failure_degrades_gracefully(fake, monkeypatch):
    _upload("e_dana", "Release freeze", FREEZE)

    def boom(*a, **k):
        raise RuntimeError("discovery engine unavailable")
    monkeypatch.setattr(fake, "search", boom)
    sid = _session()
    res = client.post(f"/api/sessions/{sid}/messages",
                      json={"content": "When is the staging deploy freeze?", "use_rag": True})
    assert res.status_code == 200
    body = res.json()
    assert body["message"]["role"] == "assistant" and body["message"]["cited_doc_ids"] == []
    assert body["rag"]["used"] is False and "discovery engine unavailable" in body["rag"]["error"]


def test_use_rag_is_a_noop_when_provider_off(monkeypatch):
    seen = {}
    from app.llm import get_llm
    real = get_llm().generate_json

    def spy(system, contents, schema, fake, think=False):
        seen["system"], seen["schema"] = system, schema.__name__
        return real(system, contents, schema, fake, think)

    monkeypatch.setattr(get_llm(), "generate_json", spy)
    sid = _session()
    res = client.post(f"/api/sessions/{sid}/messages", json={"content": "hello", "use_rag": True})
    assert res.status_code == 200
    assert seen["schema"] == "AnswerOut" and "Document excerpts" not in seen["system"]
    assert res.json()["rag"] == {"used": False, "error": None, "retrieved": []}


def test_min_score_threshold_is_configurable(fake, monkeypatch):
    doc = _upload("e_dana", "Freeze", FREEZE)
    monkeypatch.setattr(rag_docs, "get_settings", lambda: dataclasses.replace(get_settings(), rag_min_score=0.99))
    out, stats = rag_docs.search(fake, ORG_ID, "e_dana", "staging freeze window gym banana")
    assert out == [] and stats.below_score == 1 and doc["id"]
