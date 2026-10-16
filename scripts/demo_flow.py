"""Drive the full demo flow in a headless browser and report each step.

    ORGMIND_ADMIN_TOKEN=... python scripts/demo_flow.py --url https://<service-url> [--leave-distilled]

Resets the demo first (needs the admin token from the environment; it is never printed).
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8080")
ap.add_argument("--shots", default=tempfile.mkdtemp(prefix="orgmind-shots-"))
ap.add_argument("--leave-distilled", action="store_true", help="finish with a clean reset + distill")
args = ap.parse_args()
BASE, SHOTS = args.url.rstrip("/"), args.shots
TOKEN = os.environ.get("ORGMIND_ADMIN_TOKEN", "")
if not TOKEN:
    sys.exit("ORGMIND_ADMIN_TOKEN must be set in the environment")
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")
os.makedirs(SHOTS, exist_ok=True)
results, errors = [], []

def step(name, ok, detail=""):
    results.append((name, ok, detail)); print(f"{'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"]) if CHROMIUM else p.chromium.launch()
    page = b.new_page(viewport={"width": 1440, "height": 900})
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text} @ {m.location.get('url')}") if m.type == "error" and "401" not in m.text else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto(BASE); page.wait_for_selector("#compare-presets .chip", timeout=120000)
    step("load: lands on Admin > Knowledge (compare)", page.url.endswith("#/admin/knowledge")
         and page.is_visible("#page-admin-knowledge"), f"url={page.url} badge={page.text_content('#mode-badge')!r}")

    # 0. reset: wrong token rejected, right token resets to seeded state
    page.click("#reset-btn"); page.fill("#token-input", "wrong"); page.click("#token-confirm")
    page.wait_for_function("document.querySelector('#token-error').textContent.length > 0")
    step("reset rejects wrong token", "Invalid" in page.text_content("#token-error"))
    page.fill("#token-input", TOKEN); page.click("#token-confirm")
    page.wait_for_selector("#token-modal.hidden", state="attached", timeout=120000)
    page.wait_for_function("document.querySelector('#playbook-count').textContent === '0'")
    step("reset with token -> seeded, no playbooks", True)

    # 1. compare before distill
    page.click("#compare-presets .chip >> nth=0")
    page.wait_for_function("document.querySelector('#compare-btn').textContent === 'Ask both'", timeout=120000)
    cites = page.locator("#playbook-citations .chip.cite").count()
    step("compare before distill: no citation", cites == 0, page.text_content("#playbook-citations")[:90])
    page.screenshot(path=f"{SHOTS}/1_compare_before.png")

    # 2. distill
    t = time.time(); page.click("#distill-btn")
    page.wait_for_selector("#distill-report .phase-line", timeout=5000)
    page.wait_for_function("/Distilling… [1-9]\\d*s/.test(document.querySelector('#distill-btn').textContent)", timeout=10000)
    step("distill shows phase line + elapsed timer while running",
         "Summarizing sessions → clustering → writing playbooks" in page.text_content("#distill-report"),
         page.text_content("#distill-btn"))
    page.wait_for_function("document.querySelector('#distill-btn').textContent === 'Distill sessions'", timeout=180000)
    n = page.text_content("#playbook-count")
    step("distill", n == "2", f"playbooks={n} in {time.time()-t:.1f}s; report={page.text_content('#distill-report')[:120]!r}")
    page.screenshot(path=f"{SHOTS}/2_distilled.png")

    # 3. compare after distill
    page.click("#compare-presets .chip >> nth=0")
    page.wait_for_function("document.querySelector('#compare-btn').textContent === 'Ask both'", timeout=120000)
    cites = page.locator("#playbook-citations .chip.cite").count()
    step("compare after distill: cites playbook", cites == 1, page.text_content("#playbook-citations")[:120])
    base_txt, pb_txt = page.text_content("#baseline-answer"), page.text_content("#playbook-answer")
    step("playbook answer has Acme specifics", "acme auth refresh" in pb_txt and "acme auth refresh" not in base_txt)
    page.screenshot(path=f"{SHOTS}/3_compare_after.png", full_page=True)

    # 4. citation chip -> playbook modal with sources
    page.click("#playbook-citations .chip.cite")
    page.wait_for_selector("#modal-body h2")
    srcs = page.locator("#modal-body .source").count(); unres = page.locator("#modal-body .source.unresolved").count()
    step("playbook modal shows sources", srcs == 4 and unres == 1, f"sources={srcs} unresolved={unres}")
    page.screenshot(path=f"{SHOTS}/4_playbook_modal.png")
    page.keyboard.press("Escape")

    # 5. adversarial compare: similar wording, different issue
    page.click("#compare-presets .chip >> nth=2")
    page.wait_for_function("document.querySelector('#compare-btn').textContent === 'Ask both'", timeout=120000)
    step("adversarial compare: no citation", page.locator("#playbook-citations .chip.cite").count() == 0,
         page.text_content("#playbook-citations")[:120])

    # 6. chat as Dana with Dev agent
    page.click(".mode-switch a[data-mode=user]"); page.wait_for_selector("#page-user-chat:not([hidden])")
    step("chat defaults to Dana", page.input_value("#employee-select") == "e_dana")
    page.click("#new-session-btn"); page.wait_for_function("!document.querySelector('#message-input').disabled")
    page.fill("#message-input", "Hi, I'm new. acme deploy staging fails with ACME_VAULT_TOKEN expired, what do I do?")
    page.click("#send-btn")
    page.wait_for_selector(".msg.assistant .bubble:not(.typing)", timeout=120000)
    step("chat reply cites playbook", page.locator(".msg.assistant .chip.cite").count() == 1)
    page.fill("#message-input", "I ran acme auth refresh --vault on the VPN, but acme deploy staging still says vault permission denied. "
                 "Which group do I need and how do I request access?")
    page.click("#send-btn")
    page.wait_for_function("document.querySelectorAll('.msg.assistant .bubble:not(.typing)').length === 2", timeout=120000)
    last = page.locator(".msg.assistant .bubble").nth(1).text_content()
    step("follow-up uses session context", "staging-deployers" in last, last[:120].replace("\n", " "))
    step("session appears in list", page.locator("#session-list li[data-session]").count() == 1)
    page.screenshot(path=f"{SHOTS}/5_chat_dana.png")

    # 7. HR chat as Noah
    page.select_option("#employee-select", "e_noah"); page.select_option("#agent-select", "a_hr")
    page.click("#new-session-btn"); page.wait_for_function("!document.querySelector('#message-input').disabled")
    page.fill("#message-input", "My home-office stipend claim got rejected with Missing cost center. Help?")
    page.click("#send-btn")
    page.wait_for_selector(".msg.assistant .bubble:not(.typing)", timeout=120000)
    reply = page.locator(".msg.assistant .bubble").first.text_content()
    step("HR reply cites playbook, no ENG-200", page.locator(".msg.assistant .chip.cite").count() == 1 and "ENG-200" not in reply)
    page.screenshot(path=f"{SHOTS}/6_chat_noah_hr.png")

    # 8. open a seeded session (read history)
    page.select_option("#employee-select", "e_priya"); page.select_option("#agent-select", "a_dev")
    page.wait_for_selector("#session-list li[data-session]")
    page.click("#session-list li[data-session] >> nth=0")
    page.wait_for_selector(".msg.user")
    step("seeded session opens", page.locator(".msg").count() >= 4)

    # 9. traces: Admin > Traces shows the requests made above; drawer reachable from the User page
    page.click("#trace-btn"); page.wait_for_selector("#trace-drawer:not([hidden]) .trace-group", timeout=60000)
    step("trace drawer opens from User page with calls", page.locator("#drawer-body .trace-group").count() > 0)
    page.keyboard.press("Escape")
    page.evaluate("location.hash = '#/admin/traces'")
    page.wait_for_selector("#traces-view .trace-group", timeout=60000)
    heads = " ".join(page.locator("#traces-view .trace-head").all_text_contents())
    rows = page.locator("#traces-view tbody tr").count()
    step("traces tab lists chat, compare and distill requests", all(k in heads for k in ("chat", "compare", "distill")),
         f"groups={page.locator('#traces-view .trace-group').count()} calls={rows}")
    health = page.evaluate("fetch('/api/health').then(r => r.json())")
    if health.get("tracing") == "cloudtrace":
        n_links = page.locator("#traces-view .trace-head a[href*='console.cloud.google.com/traces']").count()
        step("Cloud Trace links per request", n_links > 0 and page.is_visible("#cloud-trace-link"), f"links={n_links}")
    page.screenshot(path=f"{SHOTS}/8_traces.png", full_page=True)

    # 10. narrow viewport sanity
    page.set_viewport_size({"width": 400, "height": 900}); page.click(".mode-switch a[data-mode=admin]")
    page.wait_for_selector("#page-admin-knowledge:not([hidden])")
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    step("mobile width: no horizontal overflow", overflow <= 0, f"overflow={overflow}px")
    page.screenshot(path=f"{SHOTS}/7_mobile.png", full_page=True)
    b.close()

if args.leave_distilled:
    def post(path, headers=None):
        req = urllib.request.Request(BASE + path, method="POST", data=b"{}",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    post("/api/demo/reset", {"X-Admin-Token": TOKEN})
    r = post("/api/distill")
    step("final state: reset + distilled", r["playbooks_created"] == 2,
         f"created={r['playbooks_created']} timings={r['timings_s']}")

step("no browser console errors", not errors, "; ".join(errors)[:300])
print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} steps passed (screenshots: {SHOTS})")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
