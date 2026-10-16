from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
QUESTION = "acme deploy staging fails with vault permission denied ACME_VAULT_TOKEN expired"


def test_reset_requires_token():
    assert client.post("/api/demo/reset").status_code == 401
    assert client.post("/api/demo/reset", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.post("/api/demo/reset", headers={"X-Admin-Token": "test-token"}).status_code == 200


def test_chat_cites_playbook_after_distill():
    sid = client.post("/api/sessions", json={"employee_id": "e_dana", "agent_id": "a_dev"}).json()["id"]
    before = client.post(f"/api/sessions/{sid}/messages", json={"content": QUESTION}).json()
    assert before["cited"] == []

    assert client.post("/api/distill", json={}).json()["playbooks_created"] == 2

    after = client.post(f"/api/sessions/{sid}/messages", json={"content": QUESTION}).json()
    assert len(after["cited"]) == 1
    assert after["message"]["cited_playbook_ids"] == [after["cited"][0]["id"]]
    assert len(client.get(f"/api/sessions/{sid}").json()["messages"]) == 4


def test_compare_side_by_side():
    client.post("/api/distill", json={})
    res = client.post("/api/compare", json={"agent_id": "a_dev", "question": QUESTION}).json()
    assert res["baseline"]["cited"] == []
    assert len(res["with_playbooks"]["cited"]) == 1
