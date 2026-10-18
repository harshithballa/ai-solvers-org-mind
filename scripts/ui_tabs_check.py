"""Visit every User/Admin tab at desktop and phone width: screenshot each, check layout and preview rules.

    python scripts/ui_tabs_check.py --url http://localhost:8080 --shots <dir>

Preview tabs must show the "Preview · planned" badge, every control must be disabled with a
"Planned for …" tooltip, sample rows/cards must be labelled "Sample", and the tab must make no network calls.
"""
import argparse
import os
import shutil
import sys
import tempfile

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8080")
ap.add_argument("--shots", default=tempfile.mkdtemp(prefix="orgmind-tabs-"))
args = ap.parse_args()
BASE = args.url.rstrip("/")
os.makedirs(args.shots, exist_ok=True)

ROUTES = {  # route -> is preview
    "user": False,
    "admin/knowledge": False, "admin/traces": False, "admin/agents": True, "admin/users": True,
    "admin/mcps": True, "admin/skills": True, "admin/guardrails": False, "admin/roadmap": False,
}
MUST_SAY_SAMPLE = {"admin/mcps"}
SECTION = {"user": "#page-user-home"}
results = []


def step(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)


CONTROLS_JS = """(sel) => [...document.querySelectorAll(sel + ' input, ' + sel + ' button, ' + sel + ' select, ' + sel + ' textarea')]
  .map(el => ({tag: el.tagName, disabled: el.disabled,
              tip: (el.closest('[title^="Planned for"]') || {}).title || ''}))"""

with sync_playwright() as p:
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    b = p.chromium.launch(executable_path=chromium, args=["--no-sandbox"]) if chromium else p.chromium.launch()
    for width, label in ((1440, "desktop"), (400, "phone")):
        page = b.new_page(viewport={"width": width, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/")
        page.wait_for_selector("#agent-cards .agent-card", timeout=120000)
        step(f"[{label}] default landing is #/user", page.url.endswith("#/user"), page.url)
        step(f"[{label}] no user preview tabs, admin tabs hidden on user page",
             page.locator("#page-user-connections, #page-user-upload").count() == 0 and not page.is_visible("#subnav"))
        for route, preview in ROUTES.items():
            requests = []
            handler = lambda r: requests.append(r.url) if "/api/" in r.url else None
            page.on("request", handler)
            page.evaluate(f"location.hash = '#/{route}'")
            sec = SECTION.get(route, f"#page-{route.replace('/', '-')}")
            page.wait_for_selector(f"{sec}:not([hidden])")
            page.wait_for_timeout(400)
            page.remove_listener("request", handler)
            visible = page.eval_on_selector_all(".page", "ps => ps.filter(p => !p.hidden).map(p => p.id)")
            active = page.evaluate("(document.querySelector('.subnav a.active') || {}).textContent || ''")
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            ok = visible == [sec[1:]] and overflow <= 0
            step(f"[{label}] {route} renders", ok, f"tab={active.strip()!r} overflow={overflow}px")
            page.screenshot(path=f"{args.shots}/{label}_{route.replace('/', '_')}.png", full_page=True)
            if preview:
                badge = page.is_visible(f"{sec} .preview-badge") and "Preview · planned" in page.text_content(f"{sec} .preview-badge")
                ctrls = page.evaluate(CONTROLS_JS, sec)
                bad = [c for c in ctrls if not c["disabled"] or not c["tip"].startswith("Planned for")]
                step(f"[{label}] {route} preview badge + all {len(ctrls)} controls disabled with tooltip",
                     bool(badge and ctrls and not bad), f"bad={bad[:3]}")
                step(f"[{label}] {route} makes no network calls", not requests, f"requests={requests}")
                if route in MUST_SAY_SAMPLE:
                    n_sample = page.locator(f"{sec} .sample-tag").count()
                    step(f"[{label}] {route} sample rows labelled", n_sample >= 4, f"sample tags={n_sample}")
        if label == "phone":
            page.evaluate("location.hash = '#/user'"); page.wait_for_selector("#page-user-home:not([hidden])")
            step("[phone] chat rail becomes a dropdown, My knowledge collapsible",
                 page.is_visible("#session-select") and not page.is_visible("#session-list")
                 and page.is_visible("#knowledge-toggle") and not page.is_visible("#doc-list"))
        # collapsible panels (desktop only: phone stacks them)
        if label == "desktop":
            page.evaluate("location.hash = '#/admin/knowledge'"); page.wait_for_selector("#page-admin-knowledge:not([hidden])")
            w_open = page.eval_on_selector(".split-main", "e => e.getBoundingClientRect().width")
            page.click("#knowledge-panel .collapse-btn")
            w_closed = page.eval_on_selector(".split-main", "e => e.getBoundingClientRect().width")
            page.screenshot(path=f"{args.shots}/desktop_admin_knowledge_collapsed.png")
            page.click("#knowledge-panel .rail")
            step("[desktop] knowledge panel collapses -> compare widens", w_closed > w_open + 200, f"{w_open:.0f}px -> {w_closed:.0f}px")
            page.evaluate("location.hash = '#/user'"); page.wait_for_selector("#page-user-home:not([hidden])")
            boxes = [page.eval_on_selector(sel, "e => e.getBoundingClientRect().left") for sel in (".rail-panel", ".chat", "#knowledge")]
            step("[desktop] user page: rail | chat | My knowledge side by side", boxes == sorted(boxes) and len(set(boxes)) == 3, f"{boxes}")
            page.goto(BASE + "/#/user/chat"); page.wait_for_selector("#page-user-home:not([hidden])")
            step("[desktop] old #/user/chat link lands on the workspace", page.url.endswith("#/user"))
            # deep link straight into a tab
            page.goto(BASE + "/#/admin/roadmap"); page.wait_for_selector("#page-admin-roadmap:not([hidden]) .legend")
            legend = page.locator("#page-admin-roadmap .legend > span").all_text_contents()
            step("[desktop] deep link #/admin/roadmap + legend has exactly Working now / December round",
                 legend == ["Working now", "December round"], str(legend))
            kinds = page.eval_on_selector_all("#page-admin-roadmap .arch li", "els => els.map(e => e.className)")
            step("[desktop] roadmap: every item is green (now) or amber (December), no V2/V3 left",
                 kinds and set(kinds) <= {"st-now", "st-dec"} and "V2" not in page.text_content("#page-admin-roadmap")
                 and "V3" not in page.text_content("#page-admin-roadmap"), f"{len(kinds)} items: {sorted(set(kinds))}")
            road = page.text_content("#page-admin-roadmap")
            step("[desktop] roadmap: docs + skills real, sharing planned", all(x in road for x in
                 ("Document upload + RAG", "Skills learned from one employee", "Share docs and skills with the org")))
            page.click("#trace-btn"); page.wait_for_selector("#trace-drawer:not([hidden])")
            step("[desktop] Trace drawer opens from top bar", True)
            page.keyboard.press("Escape")
        step(f"[{label}] no page errors", not errors, "; ".join(errors)[:200])
        page.close()
    b.close()

print(f"\n{sum(results)}/{len(results)} checks passed (screenshots: {args.shots})")
sys.exit(0 if all(results) else 1)
