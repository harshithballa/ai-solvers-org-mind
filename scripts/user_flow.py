"""Browser flow for the User workspace against a running server with REAL Gemini + Vertex AI Search:

    python scripts/user_flow.py --url http://localhost:8092 --shots <dir>

Run the server with ORGMIND_STORE=memory (never production data), ORGMIND_LLM=vertex, ORGMIND_RAG_PROVIDER=vertex.
Uploads one "test-" document per run (purge with `python -m scripts.purge_test_docs --yes`).
Desktop pass: default route, Viewing-as switcher, chat with each toggle, upload -> Indexing -> Ready, RAG citation,
learn skills from Dana's BigQuery chat, approve, skill citation, Priya can't see or use it, edit. Phone pass: layout +
skill citation.
"""
import argparse
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8092")
ap.add_argument("--shots", default=tempfile.mkdtemp(prefix="orgmind-user-flow-"))
ap.add_argument("--index-timeout", type=int, default=900)
args = ap.parse_args()
BASE, SHOTS = args.url.rstrip("/"), args.shots
os.makedirs(SHOTS, exist_ok=True)
results, errors, created_docs = [], [], []
CODE = secrets.token_hex(3)
DOC_NAME = f"test-ui-orion-{CODE}.txt"
DOC_TEXT = (f"Acme Platform note (synthetic). The Orion-7 nightly export bucket is acme-orion-exports-{CODE}. "
            "Exports run at 02:30 UTC and are kept for 21 days. Ask in #orion-data for access.")
RAG_Q = "Which bucket does the Orion-7 nightly export go to, and how long are exports kept?"
SKILL_Q = "How do I run a query against the acme-analytics orders table in BigQuery from my laptop?"
VAULT_Q = "My staging deploy is failing with ACME_VAULT_TOKEN expired. How do I fix it?"


def step(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)


def api(path, body=None):
    req = urllib.request.Request(BASE + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def shot(page, name, full=True):
    page.screenshot(path=f"{SHOTS}/{name}.png", full_page=full)


def answered(page):
    return page.locator(".msg.assistant .bubble:not(.typing)").count()


def ask(page, text, toggles):
    for t in ("use-playbooks", "use-skills", "use-rag"):
        page.set_checked(f"#{t}", t in toggles)
    n = answered(page)
    page.fill("#message-input", text)
    page.click("#send-btn")
    page.wait_for_function(f"document.querySelectorAll('.msg.assistant .bubble:not(.typing)').length > {n}", timeout=180000)
    page.wait_for_function("!document.querySelector('#message-input').disabled", timeout=30000)
    return page.locator(".msg.assistant").last


def new_chat(page):
    page.click("#new-session-btn")
    page.wait_for_function("!document.querySelector('#message-input').disabled", timeout=30000)


def switch(page, emp):
    page.select_option("#employee-select", emp)
    page.wait_for_function(f"document.querySelector('#profile-role').textContent.length > 0 && "
                           f"document.querySelector('#employee-select').value === '{emp}'")
    page.wait_for_timeout(1200)


def close_modal(page):
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal.hidden", state="attached")


def overflow(page):
    return page.evaluate("document.documentElement.scrollWidth - window.innerWidth")


with sync_playwright() as p:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    b = p.chromium.launch(executable_path=chromium, args=["--no-sandbox"]) if chromium else p.chromium.launch()

    # ================= desktop =================
    page = b.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
    page.goto(BASE)
    page.wait_for_selector("#agent-cards .agent-card", timeout=120000)
    page.wait_for_selector("#session-list li[data-session]", timeout=30000)
    health = api("/api/health")
    step("default route is the User workspace", page.url.endswith("#/user") and page.is_visible("#page-user-home")
         and not page.is_visible("#subnav"), page.url)
    step("defaults to Dana Kim + Dev Agent, toggles off",
         page.input_value("#employee-select") == "e_dana" and page.is_visible("[data-agent=a_dev].selected")
         and not any(page.is_checked(f"#{t}") for t in ("use-playbooks", "use-skills", "use-rag")))
    agents = page.locator(".agent-card").all_text_contents()
    step("agent cards: only seeded Dev + HR, Documents chip only when RAG is on",
         len(agents) == 2 and all(("Documents" in a) == (health["rag"] != "off") for a in agents), f"rag={health['rag']}")
    badge = page.text_content("#mode-badge")
    step("model pill shows the configured model", health["chat_model"] in badge, badge)
    shot(page, "d01_landing_dana")

    # switcher
    dana_sessions = page.locator("#session-list li[data-session]").all_text_contents()
    switch(page, "e_priya")
    priya_sessions = page.locator("#session-list li[data-session]").all_text_contents()
    step("switcher changes employee, profile and chats",
         "Backend Engineer" in page.text_content("#profile-role") and page.text_content("#profile-avatar") == "PR"
         and priya_sessions and priya_sessions != dana_sessions, f"priya chats={len(priya_sessions)}")
    shot(page, "d02_viewing_as_priya")
    switch(page, "e_dana")

    # chat: no toggles, then playbooks (after distill)
    new_chat(page)
    m = ask(page, VAULT_Q, set())
    step("chat with all toggles off: answer, no citations", m.locator(".chip.cite").count() == 0)
    t0 = time.time()
    rep = api("/api/distill", {})
    step("distill (via API) for the playbook toggle", rep["playbooks_created"] == 2, f"{time.time() - t0:.0f}s")
    page.reload(); page.wait_for_selector("#session-list li[data-session]")
    page.click("#session-list li[data-session] >> nth=0")
    page.wait_for_selector(".msg.assistant")
    m = ask(page, VAULT_Q, {"use-playbooks"})
    step("Playbooks on: cites a playbook chip", m.locator(".chip.cite.pb").count() == 1)
    m.locator(".chip.cite.pb").first.click()
    page.wait_for_selector("#modal-body h2")
    step("playbook chip opens its detail", "Distilled from" in page.text_content("#modal-body"))
    shot(page, "d03_playbook_citation", full=False)
    close_modal(page)
    page.click("#learn-btn")
    page.wait_for_selector("#learn-result:not([hidden])", timeout=120000)
    lr = page.text_content("#learn-result")
    step("learn skills on a Q&A chat: friendly result, not an error",
         "No reusable skill found in this chat" in lr or "Learned" in lr, lr)
    # Any draft from that chat would muddy the skill checks below: archive it.
    for _ in range(3):
        btns = page.locator(".skill-card:has(.st.draft) [data-skill-act=archive]")
        if not btns.count():
            break
        btns.first.click()
        page.wait_for_timeout(1000)

    # upload a test doc -> Indexing -> Ready
    path = os.path.join(tempfile.mkdtemp(), DOC_NAME)
    with open(path, "w") as f:
        f.write(DOC_TEXT)
    page.click("#doc-add-btn")
    page.set_input_files("#doc-file", path)
    t0 = time.time()
    page.click("#doc-submit")
    item = page.locator(".doc-item", has_text=DOC_NAME[:-4])
    item.wait_for(timeout=60000)
    created_docs.append(DOC_NAME)
    st = item.locator(".st").text_content()
    page.wait_for_timeout(2500)
    st2 = item.locator(".st").text_content()
    step("upload shows Indexing with a running elapsed timer, scope Private",
         st.startswith("Indexing") and st2 != st and "Private" in item.text_content(), f"{st!r} -> {st2!r}")
    shot(page, "d04_doc_indexing")
    item.locator(".st.ready, .st.failed").wait_for(timeout=args.index_timeout * 1000)
    ready_s = time.time() - t0
    step("document reaches Ready (auto-poll, real Vertex)", item.locator(".st.ready").count() == 1, f"{ready_s:.0f}s")
    step("profile counts my documents", page.text_content("#stat-docs") == "1")
    shot(page, "d05_doc_ready")

    # RAG question
    new_chat(page)
    m = ask(page, RAG_Q, {"use-rag"})
    txt = m.text_content()
    step("My documents on: answer cites the doc and uses its fact",
         m.locator(".chip.cite.doc").count() == 1 and f"acme-orion-exports-{CODE}" in txt, txt[:140].replace("\n", " "))
    m.locator(".chip.cite.doc").first.click()
    page.wait_for_selector("#modal-body h2")
    step("doc chip opens detail with the excerpt used", "Excerpt the agent used" in page.text_content("#modal-body"))
    shot(page, "d06_doc_citation", full=False)
    close_modal(page)

    # learn skills from the seeded BigQuery chat, approve
    page.click("#session-list li[data-session=ses_seed_14]")
    page.wait_for_selector(".msg.user")
    page.click("#learn-btn")
    page.wait_for_selector("#learn-result:not([hidden])", timeout=120000)
    lr = page.text_content("#learn-result")
    step("learn skills from Dana's BigQuery chat -> 1 draft", lr.startswith("Learned 1 skill"), lr)
    card = page.locator(".skill-card", has=page.locator(".st.draft")).first
    card.wait_for()
    ctext = card.text_content()
    step("skill card: name, steps, parameter names, evidence, no personal value",
         "Based on" in ctext and card.locator(".sk-steps li").count() >= 1 and card.locator("code").count() >= 1
         and "acme-sbx-dkim-4821" not in ctext, ctext[:120].replace("\n", " "))
    if card.locator("[data-skill-act=more]").count():
        card.locator("[data-skill-act=more]").click()
        step("long trigger expands with show more", card.locator(".trig-full").is_visible())
    shot(page, "d07_skill_draft")
    card.locator("[data-skill-act=approve]").click()
    page.wait_for_selector(".skill-card .st.active")
    skill_name = page.locator(".skill-card:has(.st.active) .sk-name").first.text_content()
    step("approve: draft -> Active", True, skill_name)

    # skill citation
    new_chat(page)
    m = ask(page, SKILL_Q, {"use-skills"})
    step("Skills on: answer cites Dana's skill", m.locator(".chip.cite.sk").count() == 1,
         m.text_content()[:140].replace("\n", " "))
    m.locator(".chip.cite.sk").first.click()
    page.wait_for_selector("#modal-body h2")
    step("skill chip opens skill detail", skill_name in page.text_content("#modal-body h2"))
    shot(page, "d08_skill_citation", full=False)
    close_modal(page)

    # Priya can't see or use it
    switch(page, "e_priya")
    step("Priya: Dana's skill and doc are not listed",
         skill_name not in page.text_content("#skill-list") and DOC_NAME[:-4] not in page.text_content("#doc-list"))
    new_chat(page)
    m = ask(page, SKILL_Q, {"use-skills"})
    step("Priya: same question with Skills on cites no skill", m.locator(".chip.cite.sk").count() == 0)
    shot(page, "d09_priya_no_skill")

    # back to Dana: edit
    switch(page, "e_dana")
    card = page.locator(".skill-card:has(.st.active)").first
    card.locator("[data-skill-act=edit]").click()
    page.fill(".skill-card.editing .ed-desc", "Run a BigQuery query on the Acme analytics dataset from the CLI.")
    page.click(".skill-card.editing [data-skill-act=save]")
    page.wait_for_selector(".skill-card:has(.st.active) >> text=v2")
    step("edit skill: saved, version bumped", True)
    step("desktop: no horizontal overflow", overflow(page) <= 0, f"{overflow(page)}px")
    page.close()

    # ================= phone =================
    page = b.new_page(viewport={"width": 400, "height": 860})
    page.on("pageerror", lambda e: errors.append(f"pageerror (phone): {e}"))
    page.goto(BASE)
    page.wait_for_selector("#agent-cards .agent-card", timeout=120000)
    page.wait_for_function("document.querySelector('#session-select').options.length > 1")
    step("phone: rail is a dropdown, My knowledge collapsed",
         page.is_visible("#session-select") and not page.is_visible("#session-list") and not page.is_visible("#doc-list"))
    shot(page, "p01_landing")
    page.select_option("#session-select", "ses_seed_14")
    page.wait_for_selector(".msg.user")
    page.wait_for_selector("#learn-btn:not([hidden])")
    step("phone: picking a chat from the dropdown opens it, learn action visible", True)
    new_chat(page)
    m = ask(page, SKILL_Q, {"use-skills"})
    step("phone: Skills on cites the skill", m.locator(".chip.cite.sk").count() == 1)
    shot(page, "p02_chat_skill")
    page.click("#knowledge-toggle")
    step("phone: My knowledge expands with the doc and the skill",
         page.is_visible("#doc-list") and DOC_NAME[:-4] in page.text_content("#doc-list")
         and page.locator(".skill-card .st.active").count() >= 1)
    shot(page, "p03_knowledge")
    step("phone: no horizontal overflow", overflow(page) <= 0, f"{overflow(page)}px")
    page.close()
    b.close()

step("no browser errors", not errors, "; ".join(errors)[:300])
with open(f"{SHOTS}/test_docs_created.txt", "w") as f:
    f.write("\n".join(created_docs) + "\n")
print(f"\ntest docs created: {created_docs}")
print(f"{sum(ok for _, ok, _ in results)}/{len(results)} steps passed (screenshots: {SHOTS})")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
