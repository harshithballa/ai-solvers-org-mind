"""Static checks on the frontend: lazy chat creation and the two-status roadmap. Browser checks live in
scripts/user_flow.py and scripts/ui_tabs_check.py."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
STATIC = Path(__file__).resolve().parent.parent / "static"


def test_page_load_reads_create_no_session():
    before = len(client.get("/api/sessions").json())
    # what the User page fetches on load and on agent / viewer switches
    for path in ("/", "/api/health", "/api/org", "/api/playbooks",
                 "/api/sessions?employee_id=e_dana&agent_id=a_dev", "/api/sessions?employee_id=e_priya&agent_id=a_hr",
                 "/api/skills?employee_id=e_dana"):
        assert client.get(path).status_code == 200, path
    assert len(client.get("/api/sessions").json()) == before


def test_sessions_are_created_only_from_one_place_in_the_frontend():
    js = (STATIC / "app.js").read_text()
    assert len(re.findall(r'api\("/api/sessions", \{ method: "POST"', js)) == 1
    assert "const createSession = () =>" in js
    html = (STATIC / "index.html").read_text()
    assert '<label for="message-input"' in html


def test_roadmap_has_exactly_two_statuses():
    js = (STATIC / "previews.js").read_text()
    assert 'const ST = { now: "Working now", dec: "December round" };' in js
    roadmap = js[js.index("function roadmap()"):js.index("const PAGES")]
    assert set(re.findall(r'item\("(\w+)"', roadmap)) == {"now", "dec"}
    assert "V2" not in roadmap and "V3" not in roadmap
    css = (STATIC / "styles.css").read_text()
    assert ".st-v2" not in css and ".st-v3" not in css
