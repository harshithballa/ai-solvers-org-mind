"""Browser flow for the User workspace against a running server with REAL Gemini + Vertex AI Search:

    python scripts/user_flow.py --url http://localhost:8092 --shots <dir>

Run a FRESHLY STARTED server with ORGMIND_STORE=memory (never production data), ORGMIND_LLM=vertex,
ORGMIND_RAG_PROVIDER=vertex, ORGMIND_LIMITS=off (the flow refuses to run on a server with leftover state).
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
ap.add_argument("--admin-token", default="", help="optional: also screenshot Admin > Guardrails")
ap.add_argument("--limit-check", action="store_true",
                help="only check that a 429 renders inline (run the server with ORGMIND_LIMITS=on ORGMIND_LIMIT_CHAT_MESSAGES=1)")
args = ap.parse_args()
BASE, SHOTS = args.url.rstrip("/"), args.shots
os.makedirs(SHOTS, exist_ok=True)
results, errors, created_docs = [], [], []
CODE = secrets.token_hex(3)
DOC_NAME = f"test-ui-orion-{CODE}.txt"
PII_TITLE = f"test-ui-contacts-{CODE}"
PII_TEXT = ("Laptop help contacts (synthetic): reach Priyanka Rao at priyanka.rao.home@gmail.com or on +91 98450 12345. "
            "Hardware swaps happen on Fridays.")
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


def open_row(page, row):
    """Rows in My knowledge are compact; details and the full actions open in the drawer."""
    row.click()
    page.wait_for_selector("#modal:not(.hidden) #modal-body h2")


def overflow(page):
    return page.evaluate("document.documentElement.scrollWidth - window.innerWidth")


def limit_check(b):
    """With a chat limit of 1/day: the second message shows the server's friendly 429 sentence inline."""
    for width, label in ((1440, "desktop"), (400, "phone")):
        page = b.new_page(viewport={"width": width, "height": 900})
        page.goto(BASE)
        page.wait_for_selector("#agent-cards .agent-card", timeout=120000)
        new_chat(page)
        page.fill("#message-input", "hello")
        page.click("#send-btn")
        page.wait_for_function("document.querySelector('.msg.notice') || "
                               "document.querySelectorAll('.msg.assistant .bubble:not(.typing)').length > 0", timeout=60000)
        if not page.locator(".msg.notice").count():
            page.fill("#message-input", "and again")
            page.click("#send-btn")
            page.wait_for_selector(".msg.notice", timeout=60000)
        note = page.text_content(".msg.notice")
        step(f"[{label}] over the limit: friendly inline notice, no raw error",
             "limit" in note and "429" not in note and page.input_value("#message-input") != "", note)
        shot(page, f"limit_{label}_429_notice", full=False)
        page.close()


with sync_playwright() as p:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    b = p.chromium.launch(executable_path=chromium, args=["--no-sandbox"]) if chromium else p.chromium.launch()
    if args.limit_check:
        limit_check(b)
        b.close()
        print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} steps passed (screenshots: {SHOTS})")
        sys.exit(0 if all(ok for _, ok, _ in results) else 1)

    # ================= desktop =================
    page = b.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
    page.goto(BASE)
    page.wait_for_selector("#agent-cards .agent-card", timeout=120000)
    page.wait_for_selector("#session-list li[data-session]", timeout=30000)
    health = api("/api/health")
    leftovers = (api("/api/rag/docs?employee_id=e_dana") if health["rag"] != "off" else []) + api("/api/skills?employee_id=e_dana")
    if leftovers or api("/api/playbooks"):
        sys.exit("user_flow needs a freshly started server (memory store): this one has docs/skills/playbooks from a "
                 "previous run. Restart it and run again.")
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
    page.click("#doc-add-btn")
    page.click("[data-docmode=text]")
    page.fill("#doc-title", PII_TITLE)
    page.fill("#doc-text", PII_TEXT)
    page.click("#doc-submit")
    pii_item = page.locator(".doc-item").filter(has_text=PII_TITLE).filter(has_not_text="(redacted)")
    pii_item.wait_for(timeout=60000)
    created_docs.append(PII_TITLE + ".txt (pasted)")
    st = item.locator(".st").text_content()
    page.wait_for_timeout(2500)
    st2 = item.locator(".st").text_content()
    step("upload shows Indexing with a running elapsed timer, scope Private",
         st.startswith("Indexing") and st2 != st and "Private" in item.text_content(), f"{st!r} -> {st2!r}")
    shot(page, "d04_doc_indexing")
    item.locator(".st.ready, .st.failed").wait_for(timeout=args.index_timeout * 1000)
    ready_s = time.time() - t0
    step("document reaches Ready (auto-poll, real Vertex)", item.locator(".st.ready").count() == 1, f"{ready_s:.0f}s")
    pii_item.locator(".st.ready, .st.failed").wait_for(timeout=args.index_timeout * 1000)
    step("profile counts my documents", page.text_content("#stat-docs") == "2")
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
    open_row(page, card.locator(".sk-name"))
    detail = page.locator("#modal-body .skill-detail")
    ctext = detail.text_content()
    step("skill drawer: name, steps, parameter names, evidence, no personal value",
         "Based on" in ctext and detail.locator(".sk-steps li").count() >= 1 and detail.locator("code").count() >= 1
         and "acme-sbx-dkim-4821" not in ctext + card.text_content(), ctext[:120].replace("\n", " "))
    shot(page, "d07_skill_draft", full=False)
    close_modal(page)
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
    page.wait_for_selector("#modal-body .skill-detail .tag >> text=v2")
    step("edit skill: saved, version bumped", True)
    close_modal(page)

    # ---- share to org (guardrail) ----
    def share_result(btn):
        btn.click()
        page.wait_for_selector("#modal:not(.hidden) .share-result", timeout=120000)
        return page.get_attribute(".share-result", "data-verdict"), page.text_content("#modal-body")

    doc_item = page.locator(".doc-item", has_text=DOC_NAME[:-4])
    open_row(page, doc_item)
    verdict, text = share_result(page.locator("#modal-body [data-share-act=share]"))
    step("share clean doc: 'Shared with your org'", verdict == "clean" and "Shared with your org" in text, verdict)
    shot(page, "d10_share_doc_clean", full=False)
    close_modal(page)
    page.wait_for_selector(f".doc-item:has-text('{DOC_NAME[:-4]}') .scope.org")
    open_row(page, doc_item)
    step("doc shows 'Shared with org' chip and Unshare", page.locator("#modal-body [data-share-act=unshare]").count() == 1)
    close_modal(page)
    card = page.locator(".skill-card:has(.st.active)").first
    verdict, text = share_result(card.locator("[data-share-act=share]"))
    step("share active skill: clean", verdict == "clean", text[:80])
    close_modal(page)

    pii = page.locator(".doc-item").filter(has_text=PII_TITLE).filter(has_not_text="(redacted)")
    open_row(page, pii)
    verdict, text = share_result(page.locator("#modal-body [data-share-act=share]"))
    step("share doc with email + phone: Blocked, masked previews only",
         verdict == "blocked" and "EMAIL" in text and "PHONE" in text and "priyanka.rao.home" not in text
         and "98450" not in text and "p***@g***.com" in text, text[:160].replace("\n", " "))
    shot(page, "d11_share_blocked", full=False)
    page.click("#modal-body [data-share-act=redacted]")
    page.wait_for_selector(".share-result[data-verdict=clean]", timeout=120000)
    step("'Share a redacted copy' publishes a new doc (original stays private)",
         "redacted" in page.text_content("#modal-body") and pii.locator(".scope.org").count() == 0)
    created_docs.append(f"{PII_TITLE} (redacted) - created by the share-redacted flow")
    shot(page, "d12_redacted_copy", full=False)
    close_modal(page)

    card = page.locator(".skill-card:has(.st.active)").first
    card.locator("[data-skill-act=edit]").click()
    page.fill(".skill-card.editing .ed-name", "Query BigQuery from the CLI")
    page.click(".skill-card.editing [data-skill-act=save]")
    page.wait_for_selector(".toast:not(.hidden)")
    toast_text = page.text_content("#toast")
    close_modal(page)
    card = page.locator(".skill-card:has(.st.active)").first
    step("editing a shared skill makes it private again (and says so)",
         "private again" in toast_text and card.locator(".scope.org").count() == 0, toast_text)
    verdict, _ = share_result(card.locator("[data-share-act=share]"))
    close_modal(page)
    step("re-share after edit: re-scanned clean", verdict == "clean")
    skill_name = page.locator(".skill-card:has(.st.active) .sk-name").first.text_content()

    # ---- Priya sees and cites Dana's shared items ----
    switch(page, "e_priya")
    shared = page.text_content("#shared-list")
    step("Priya: 'Shared by colleagues' lists Dana's doc and skill with attribution",
         DOC_NAME[:-4] in shared and skill_name in shared and "Shared by Dana Kim" in shared
         and PII_TITLE not in shared.replace(PII_TITLE + " (redacted)", ""), shared[:160])
    shot(page, "d13_priya_shared_list")
    new_chat(page)
    m = ask(page, SKILL_Q, {"use-skills"})
    step("Priya (Skills on): cites Dana's shared skill", m.locator(".chip.cite.sk").count() == 1, m.text_content()[:120].replace("\n", " "))
    m.locator(".chip.cite.sk").first.click()
    page.wait_for_selector("#modal-body h2")
    step("skill drawer shows 'Shared by Dana Kim'", "Shared by Dana Kim" in page.text_content("#modal-body"))
    shot(page, "d14_priya_skill_attribution", full=False)
    close_modal(page)
    m = ask(page, RAG_Q, {"use-rag"})
    step("Priya (Documents on): cites Dana's shared doc", m.locator(".chip.cite.doc").count() == 1
         and f"acme-orion-exports-{CODE}" in m.text_content(), m.text_content()[:120].replace("\n", " "))
    m.locator(".chip.cite.doc").first.click()
    page.wait_for_selector("#modal-body h2")
    step("doc drawer shows 'Shared by Dana Kim' (read-only)", "Shared by Dana Kim" in page.text_content("#modal-body"))
    close_modal(page)
    step("Priya has no share/edit controls on colleagues' items",
         page.locator("#shared-list [data-share-act], #shared-list [data-skill-act]").count() == 0)
    switch(page, "e_dana")

    if args.admin_token:
        page.evaluate(f"localStorage.setItem('orgmind_admin_token', {json.dumps(args.admin_token)})")
        page.evaluate("location.hash = '#/admin/guardrails'")
        page.wait_for_selector("#guardrail-view table", timeout=30000)
        g = page.text_content("#guardrail-view")
        step("Admin > Guardrails: counts + events with categories only",
             "blocked" in g and "EMAIL" in g and "priyanka" not in g.lower(), g[:120].replace("\n", " "))
        shot(page, "d15_admin_guardrails")
        page.evaluate("localStorage.removeItem('orgmind_admin_token')")
        page.evaluate("location.hash = '#/user'")
        page.wait_for_selector("#page-user-home:not([hidden])")

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
    card = page.locator(".skill-card:has(.st.active)").first
    card.locator("[data-share-act=unshare]").click()
    page.wait_for_selector(".skill-card:has(.st.active) [data-share-act=share]")
    page.locator(".skill-card:has(.st.active) [data-share-act=share]").first.click()
    page.wait_for_selector("#modal:not(.hidden) .share-result", timeout=120000)
    step("phone: unshare then share skill -> 'Shared with your org'",
         page.get_attribute(".share-result", "data-verdict") == "clean")
    shot(page, "p04_share_clean", full=False)
    close_modal(page)
    open_row(page, page.locator(".doc-item").filter(has_text=PII_TITLE).filter(has_not_text="(redacted)"))
    page.locator("#modal-body [data-share-act=share]").click()
    page.wait_for_selector("#modal:not(.hidden) .share-result", timeout=120000)
    txt = page.text_content("#modal-body")
    step("phone: blocked dialog with masked findings and redacted-copy button",
         page.get_attribute(".share-result", "data-verdict") == "blocked" and "priyanka.rao.home" not in txt
         and page.locator("#modal-body [data-share-act=redacted]").count() == 1)
    shot(page, "p05_share_blocked", full=False)
    close_modal(page)
    step("phone: no horizontal overflow", overflow(page) <= 0, f"{overflow(page)}px")
    page.close()
    b.close()

step("no browser errors", not errors, "; ".join(errors)[:300])
with open(f"{SHOTS}/test_docs_created.txt", "w") as f:
    f.write("\n".join(created_docs) + "\n")
print(f"\ntest docs created: {created_docs}")
print(f"{sum(ok for _, ok, _ in results)}/{len(results)} steps passed (screenshots: {SHOTS})")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
