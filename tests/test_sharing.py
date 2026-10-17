import json

import pytest
from fastapi.testclient import TestClient

from app import rag
from app.main import app
from app.models import RagDoc, ShareEvent
from app.seed import BIGQUERY_SESSION_ID

client = TestClient(app)
CLEAN_TEXT = ("Acme internal note: the staging deploy freeze window is Thursdays 4pm-6pm IST. "
              "To request an exception, post in #release-ops with the ticket ID and tag @rohan-release.")
PII_TEXT = "Questions? Mail dana.kim@gmail.com or call +91 98765 43210. Freeze is Thursdays 4pm-6pm IST."
BQ_Q = "How do I run a bq query against acme-analytics.events.orders with --use_legacy_sql=false?"


@pytest.fixture(autouse=True)
def fake_rag(fresh_store):
    provider = rag.FakeRagProvider()
    rag.set_rag_provider(provider)
    fresh_store._data["rag_docs"].clear()
    fresh_store._data["share_events"].clear()
    yield provider
    rag.set_rag_provider(None)
    fresh_store._llm_calls.clear()


def _doc(employee_id, title, text, ready=True):
    d = client.post("/api/rag/docs", json={"employee_id": employee_id, "title": title, "text": text}).json()
    if ready:
        client.get(f"/api/rag/docs?employee_id={employee_id}")   # status refresh -> ready
    return d


CLEAN_STEPS = ["Run `gcloud auth application-default login`.",
               "Run `bq query --project_id={project_id} --use_legacy_sql=false` with the table in backticks."]


def _active_skill(clean=True):
    """The fake extractor copies Dana's sandbox id into the steps (the guardrail would block that), so tests edit
    the steps into their parameterised form first, as a user would."""
    [sk] = client.post("/api/skills/extract", json={"session_id": BIGQUERY_SESSION_ID, "employee_id": "e_dana"}).json()["skills"]
    if clean:
        client.patch(f"/api/skills/{sk['id']}", json={"employee_id": "e_dana", "steps": CLEAN_STEPS})
    client.post(f"/api/skills/{sk['id']}/approve", json={"employee_id": "e_dana"})
    return sk


def _share(kind, item_id, employee_id="e_dana"):
    path = f"/api/rag/docs/{item_id}/share" if kind == "doc" else f"/api/skills/{item_id}/share"
    return client.post(path, json={"employee_id": employee_id})


def _docs_for(emp):
    return {d["id"]: d for d in client.get(f"/api/rag/docs?employee_id={emp}").json()}


def test_clean_doc_shared_is_visible_and_attributed_for_others(fake_rag):
    d = _doc("e_dana", "Release freeze", CLEAN_TEXT)
    assert d["id"] not in _docs_for("e_priya")
    res = _share("doc", d["id"])
    assert res.status_code == 200 and res.json()["verdict"] == "clean"
    seen = _docs_for("e_priya")[d["id"]]
    assert seen["scope"] == "org" and seen["shared_by_name"] == "Dana Kim"
    sid = client.post("/api/sessions", json={"employee_id": "e_priya", "agent_id": "a_dev"}).json()["id"]
    ans = client.post(f"/api/sessions/{sid}/messages",
                      json={"content": "When is the staging deploy freeze?", "use_rag": True}).json()
    assert ans["message"]["cited_doc_ids"] == [d["id"]]
    # unshare -> gone for Priya, and no longer cited
    assert client.post(f"/api/rag/docs/{d['id']}/unshare", json={"employee_id": "e_dana"}).json()["scope"] == "private"
    assert d["id"] not in _docs_for("e_priya")
    ans = client.post(f"/api/sessions/{sid}/messages",
                      json={"content": "When is the staging deploy freeze?", "use_rag": True}).json()
    assert ans["message"]["cited_doc_ids"] == []


def test_pii_doc_blocked_with_masked_findings_only(fresh_store):
    d = _doc("e_dana", "Contact sheet", PII_TEXT)
    res = _share("doc", d["id"]).json()
    blob = json.dumps(res)
    assert res["verdict"] == "blocked" and {f["category"] for f in res["findings"]} == {"EMAIL", "PHONE"}
    assert "dana.kim@gmail.com" not in blob and "98765" not in blob
    assert fresh_store.get(RagDoc, d["id"]).scope == "private"
    events = fresh_store.list(ShareEvent, item_id=d["id"])
    assert [e.action for e in events] == ["blocked"] and events[0].finding_categories == ["EMAIL", "PHONE"]
    assert "dana.kim" not in json.dumps([e.model_dump(mode="json") for e in events])


def test_share_redacted_copy_publishes_clean_new_doc(fresh_store):
    d = _doc("e_dana", "Contact sheet", PII_TEXT)
    res = client.post(f"/api/rag/docs/{d['id']}/share-redacted", json={"employee_id": "e_dana"}).json()
    assert res["verdict"] == "clean" and res["doc"]["title"] == "Contact sheet (redacted)"
    assert res["doc"]["scope"] == "org" and res["doc"]["status"] == "indexing"
    assert fresh_store.get(RagDoc, d["id"]).scope == "private"
    text = client.app and rag.get_rag_provider().fetch_text(res["doc"]["gcs_uri"])
    assert "[REDACTED:EMAIL]" in text and "[REDACTED:PHONE]" in text and "gmail" not in text


def test_owner_only_and_doc_rules(fake_rag, fresh_store):
    d = _doc("e_dana", "Release freeze", CLEAN_TEXT)
    assert _share("doc", d["id"], "e_priya").status_code == 404           # private: not revealed
    _share("doc", d["id"])
    assert _share("doc", d["id"], "e_priya").status_code == 403           # visible, but not hers
    assert client.post(f"/api/rag/docs/{d['id']}/unshare", json={"employee_id": "e_priya"}).status_code == 403
    pdf = client.post("/api/rag/docs", data={"employee_id": "e_dana"},
                      files={"file": ("policy.pdf", b"%PDF-1.4 fake", "application/pdf")}).json()
    client.get("/api/rag/docs?employee_id=e_dana")
    res = _share("doc", pdf["id"])
    assert res.status_code == 400 and "PDFs can't be scanned yet" in res.json()["detail"]
    pending = _doc("e_dana", "Pending", CLEAN_TEXT, ready=False)
    assert _share("doc", pending["id"]).status_code == 409
    big = fresh_store.get(RagDoc, pending["id"]).model_copy(update={"status": "ready", "size": 300 * 1024})
    fresh_store.put(big)
    res = _share("doc", big.id)
    assert res.status_code == 400 and "too large to scan" in res.json()["detail"]


def test_skill_must_be_active_and_owner_only():
    [sk] = client.post("/api/skills/extract", json={"session_id": BIGQUERY_SESSION_ID, "employee_id": "e_dana"}).json()["skills"]
    assert _share("skill", sk["id"]).status_code == 409                    # draft
    client.patch(f"/api/skills/{sk['id']}", json={"employee_id": "e_dana", "steps": CLEAN_STEPS})
    client.post(f"/api/skills/{sk['id']}/approve", json={"employee_id": "e_dana"})
    assert _share("skill", sk["id"], "e_priya").status_code == 404
    res = _share("skill", sk["id"]).json()
    assert res["verdict"] == "clean" and res["skill"]["scope"] == "org" and res["skill"]["shared_by_name"] == "Dana Kim"
    for act in ("approve", "archive", "unshare"):
        assert client.post(f"/api/skills/{sk['id']}/{act}", json={"employee_id": "e_priya"}).status_code == 403
    assert client.patch(f"/api/skills/{sk['id']}", json={"employee_id": "e_priya", "name": "x"}).status_code == 403


def test_org_skill_retrieved_by_colleague_with_attribution(monkeypatch):
    sk = _active_skill()
    _share("skill", sk["id"])
    listed = {s["id"]: s for s in client.get("/api/skills?employee_id=e_priya").json()}
    assert listed[sk["id"]]["shared_by_name"] == "Dana Kim"
    from app.llm import get_llm
    seen = {}
    real = get_llm().generate_json
    monkeypatch.setattr(get_llm(), "generate_json", lambda system, contents, schema, fake, think=False:
                        seen.setdefault("system", system) and real(system, contents, schema, fake, think))
    sid = client.post("/api/sessions", json={"employee_id": "e_priya", "agent_id": "a_dev"}).json()["id"]
    ans = client.post(f"/api/sessions/{sid}/messages", json={"content": BQ_Q, "use_skills": True}).json()
    assert ans["message"]["cited_skill_ids"] == [sk["id"]]
    assert "Shared with the org by Dana Kim" in seen["system"]


def test_patch_on_shared_skill_reverts_to_private(fresh_store):
    sk = _active_skill()
    _share("skill", sk["id"])
    res = client.patch(f"/api/skills/{sk['id']}", json={"employee_id": "e_dana", "description": "Query BigQuery from the CLI."}).json()
    assert res["scope"] == "private" and res["unshared"] is True and "private again" in res["message"]
    assert sk["id"] not in {s["id"] for s in client.get("/api/skills?employee_id=e_priya").json()}
    assert [e.verdict for e in fresh_store.list(ShareEvent, item_id=sk["id"]) if e.action == "unshare"] == ["edited"]


def test_blocked_skill_and_fail_closed(monkeypatch, fresh_store):
    sk = _active_skill()
    from app.llm import get_llm

    def boom(*a, **k):
        raise RuntimeError("vertex unavailable")
    monkeypatch.setattr(get_llm(), "generate_json", boom)
    res = _share("skill", sk["id"]).json()
    assert res["verdict"] == "error" and "Couldn't verify" in res["message"] and res["skill"]["scope"] == "private"
    assert [e.verdict for e in fresh_store.list(ShareEvent, item_id=sk["id"])] == ["error"]


def test_share_events_never_hold_values(fresh_store):
    d = _doc("e_dana", "Keys", "aws AKIAIOSFODNN7EXAMPLE card 4111 1111 1111 1111 sandbox acme-sbx-dkim-4821")
    _share("doc", d["id"])
    client.post(f"/api/rag/docs/{d['id']}/share-redacted", json={"employee_id": "e_dana"})
    blob = json.dumps([e.model_dump(mode="json") for e in fresh_store.list(ShareEvent)])
    for raw in ("AKIAIOSFODNN7EXAMPLE", "4111", "acme-sbx-dkim-4821"):
        assert raw not in blob


def test_guardrail_summary_admin_only():
    d = _doc("e_dana", "Contact sheet", PII_TEXT)
    _share("doc", d["id"])
    assert client.get("/api/guardrail/summary").status_code == 401
    res = client.get("/api/guardrail/summary", headers={"X-Admin-Token": "test-token"}).json()
    assert res["counts"]["blocked"] >= 1 and res["human_review"] == "planned"
    assert res["events"][0]["finding_categories"] == ["EMAIL", "PHONE"] and "gmail" not in json.dumps(res)


def test_skill_with_personal_sandbox_id_is_blocked():
    sk = _active_skill(clean=False)
    res = _share("skill", sk["id"]).json()
    assert res["verdict"] == "blocked" and res["findings"][0]["category"] == "PERSONAL_PROJECT"
    assert "acme-sbx-dkim-4821" not in json.dumps(res["findings"])   # the owner's own skill text is returned as-is


def test_redacted_copy_redacts_again_when_rescan_finds_more(monkeypatch, fresh_store):
    """First scan finds the email; the re-scan of the copy then flags the name next to it; a second redaction
    round removes it and the copy publishes."""
    from app.llm import get_llm
    from app.models import GuardrailFindingOut, GuardrailOut
    d = _doc("e_dana", "Contacts", "Reach Priyanka Rao at priyanka.rao.home@gmail.com for laptops.")
    real = get_llm().generate_json

    def staged(system, contents, schema, fake, think=False):
        if schema is GuardrailOut and "[REDACTED:EMAIL]" in contents and "Priyanka" in contents:
            out = GuardrailOut(findings=[GuardrailFindingOut(category="PERSONAL_CONTACT", quote="Priyanka Rao", reason="a person")])
            return real(system, contents, schema, lambda: out, think)
        return real(system, contents, schema, fake, think)
    monkeypatch.setattr(get_llm(), "generate_json", staged)
    res = client.post(f"/api/rag/docs/{d['id']}/share-redacted", json={"employee_id": "e_dana"}).json()
    assert res["verdict"] == "clean" and res["doc"]["scope"] == "org"
    text = rag.get_rag_provider().fetch_text(res["doc"]["gcs_uri"])
    assert "Priyanka" not in text and "[REDACTED:PERSONAL_CONTACT]" in text and "[REDACTED:EMAIL]" in text
