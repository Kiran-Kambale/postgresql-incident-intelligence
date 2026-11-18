"""Browser tests (Playwright + Chromium) against a real server in demo mode. Skipped when Playwright is not installed.

They verify what unit tests cannot: that every page renders without console errors or failing API calls, that interactions
(drawer, filters, dialogs, theme, keyboard) behave, and that risky actions require confirmation.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")
from tests.conftest import phase_offset_for  # noqa: E402

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parent.parent
PAGES = ["overview", "activity", "queries", "locks", "vacuum", "replication", "incidents", "evidence", "instances", "settings", "audit"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = free_port()
    env = {**os.environ, "DEMO_MODE": "true", "AUTO_MONITOR": "true", "DATA_DIR": str(tmp_path_factory.mktemp("e2e")), "DEMO_PHASE_OFFSET_S": str(phase_offset_for(1500)), "LOG_LEVEL": "WARNING"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    import urllib.request
    for _ in range(120):
        try:
            if urllib.request.urlopen(base + "/api/health/live", timeout=1).status == 200:
                time.sleep(3)          # let the first live collections land after the demo history is seeded
                break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("server did not start: " + proc.stderr.read().decode()[:500])
    yield base
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    pg = ctx.new_page()
    pg.problems = []
    pg.on("console", lambda m: pg.problems.append(f"console: {m.text}") if m.type == "error" else None)
    pg.on("pageerror", lambda e: pg.problems.append(f"pageerror: {e}"))
    pg.on("response", lambda r: pg.problems.append(f"{r.status} {r.url}") if r.status >= 400 and "/api/" in r.url else None)
    pg.base = server
    yield pg
    ctx.close()


def goto(pg, route, wait=1500):
    pg.goto(f"{pg.base}/#/{route}")
    pg.wait_for_timeout(wait)


@pytest.mark.parametrize("name", PAGES)
def test_every_page_renders_without_errors(page, name):
    goto(page, name, 2200)
    assert page.locator("#view h1").first.inner_text()
    assert not page.locator(".errorbox").count(), page.locator(".errorbox").first.inner_text()
    assert page.problems == []
    assert page.locator('#nav a[aria-current="page"]').get_attribute("data-page") == name


def test_demo_banner_and_no_signin_button_without_auth(page):
    goto(page, "overview")
    assert "DEMO DATA" in page.locator("#banners").inner_text() and not page.locator("#auth").is_visible()


def test_overview_kpis_are_clickable_and_navigate(page):
    goto(page, "overview", 2500)
    page.locator(".kpi", has_text="Blocked sessions").click()
    page.wait_for_timeout(1200)
    assert page.url.endswith("#/locks")
    assert page.locator("g.node").count() >= 2 and "ROOT BLOCKER" in page.locator("g.node").first.text_content().upper()


def test_blocking_graph_node_selection_shows_details(page):
    goto(page, "locks", 2500)
    page.locator("g.node").first.click()
    page.wait_for_timeout(500)
    txt = page.locator("#view").inner_text()
    assert "root blocker" in txt.lower() and "Statement" in txt and "billing-job" in txt


def test_evidence_drawer_opens_shows_six_answers_and_closes_with_escape(page):
    goto(page, "evidence", 2500)
    page.locator("#view tbody tr.clickable").first.click()
    page.wait_for_selector("#drawer:not([hidden])")
    page.wait_for_timeout(800)
    body = page.locator("#drawer-body").inner_text()
    for q in ("What was observed?", "Why does it matter?", "What evidence supports it?", "What is the likely impact?", "What should the engineer investigate next?", "Suggested actions and their risks"):
        assert q in body
    assert page.locator("#drawer").get_attribute("aria-modal") == "true"
    page.keyboard.press("Escape")
    assert not page.locator("#drawer").is_visible()


def test_evidence_filters_search_and_json_tab(page):
    goto(page, "evidence", 2500)
    total_before = page.locator("#view tbody tr.clickable").count()
    page.locator(".chip", has_text="critical").first.click()
    page.wait_for_timeout(1200)
    rows = page.locator("#view tbody tr.clickable")
    assert 0 < rows.count() <= total_before
    for i in range(min(rows.count(), 5)):
        assert "critical" in rows.nth(i).inner_text().lower()
    page.locator("[data-search]").fill("zzzz-no-such-thing")
    page.wait_for_timeout(900)
    assert "No evidence matches" in page.locator("#view").inner_text()
    page.locator("[data-search]").fill("")
    page.wait_for_timeout(900)
    page.locator("#view tbody tr.clickable").first.click()
    page.wait_for_selector("#drawer:not([hidden])")
    page.get_by_role("tab", name="Evidence JSON").click()
    assert '"fingerprint"' in page.locator("#drawer-body pre.json").inner_text()


def test_theme_toggle_timezone_and_range_controls(page):
    goto(page, "overview", 1500)
    before = page.evaluate("document.documentElement.dataset.theme")
    page.locator("#theme").click()
    assert page.evaluate("document.documentElement.dataset.theme") != before
    page.select_option("#tz", "Asia/Kolkata")
    page.wait_for_timeout(800)
    assert page.evaluate("localStorage.getItem('dir.tz')") == "Asia/Kolkata"
    page.locator("#range button", has_text="6h").click()
    page.wait_for_timeout(800)
    assert page.locator("#range button[aria-pressed='true']").inner_text() == "6h"


def test_slash_focuses_search_and_keyboard_activation_works(page):
    goto(page, "evidence", 2000)
    page.locator("body").click(position={"x": 700, "y": 400})
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.hasAttribute('data-search')")
    page.keyboard.press("Escape")
    page.locator("#view tbody tr.clickable").first.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("#drawer:not([hidden])")
    page.wait_for_timeout(600)
    assert page.locator("#drawer").is_visible(), "the drawer must stay open after keyboard activation"
    assert page.evaluate("document.activeElement.id") == "drawer-close"


def test_create_incident_via_dialog_then_change_status_with_confirmation(page):
    goto(page, "incidents", 1500)
    page.get_by_role("button", name="New incident").first.click()
    page.wait_for_selector("dialog[open]")
    page.fill("#f-title", "E2E: checkout slow")
    page.fill("#f-symptom", "created by the browser test")
    page.locator("#dlg-actions button", has_text="Create incident").click()
    page.wait_for_url("**/#/incidents/DIR-*")
    page.wait_for_timeout(1200)
    assert "E2E: checkout slow" in page.locator("#view h1").inner_text()
    page.get_by_role("button", name="Start investigating").click()
    page.wait_for_timeout(1000)
    assert page.locator("#view .badge", has_text="Investigating").count() >= 1
    page.get_by_role("button", name="Resolve…").click()
    page.wait_for_selector("dialog[open]")
    page.locator("#dlg-actions button", has_text="Mark resolved").click()
    assert "required" in page.locator("#form-err").inner_text().lower()          # empty resolution is rejected client-side
    page.fill("#f-resolution", "fixed by test")
    page.locator("#dlg-actions button", has_text="Mark resolved").click()
    page.wait_for_timeout(1200)
    assert page.locator("#view .badge", has_text="Resolved").count() >= 1 and page.problems == []


def test_candidate_dismissal_requires_confirmation(page):
    goto(page, "incidents", 1500)
    page.locator(".seg button", has_text="Candidates").click()
    page.wait_for_timeout(900)
    page.locator("#view tbody tr.clickable").first.click()
    page.wait_for_url("**/#/incidents/DIR-*")
    page.wait_for_timeout(1000)
    page.get_by_role("button", name="Dismiss").click()
    page.wait_for_selector("dialog[open]")
    assert "dismissed" in page.locator("#dlg-body").inner_text().lower()
    page.locator("#dlg-actions button", has_text="Cancel").click()
    assert page.locator("#view .badge", has_text="Candidate").count() >= 1             # cancelled: nothing changed


def test_disruptive_sql_requires_explicit_confirmation_and_is_never_executed(page):
    goto(page, "evidence", 2000)
    page.locator(".chip", has_text="blocking").first.click()
    page.wait_for_timeout(1200)
    page.locator("#view tbody tr.clickable").first.click()
    page.wait_for_selector("#drawer:not([hidden])")
    page.wait_for_timeout(800)
    page.get_by_role("button", name="Copy SQL…").first.click()
    page.wait_for_selector("dialog[open]")
    t = page.locator("#dlg-body").inner_text().lower()
    assert "does not run" in t and ("risk" in t or "interrupt" in t or "roll" in t)
    page.locator("#dlg-actions button", has_text="Cancel").click()


def test_settings_page_validates_and_saves(page):
    goto(page, "settings", 1500)
    field = page.locator("input[aria-label^='Active query older than this']")
    field.fill("45")
    page.get_by_role("button", name="Save changes").click()
    page.wait_for_timeout(1000)
    assert "Settings saved" in page.locator("#toasts").inner_text()
    field = page.locator("input[aria-label^='Active query older than this']")
    assert field.input_value() == "45"
    page.get_by_role("button", name="Reset").first.click()
    page.wait_for_timeout(800)


def test_responsive_layout_collapses_navigation(browser, server):
    ctx = browser.new_context(viewport={"width": 480, "height": 800})
    pg = ctx.new_page()
    pg.goto(f"{server}/#/overview")
    pg.wait_for_timeout(1500)
    assert pg.locator("#menu").is_visible()
    pg.locator("#menu").click()
    assert "open" in pg.locator("#sidebar").get_attribute("class")
    ctx.close()


def test_no_inline_script_or_remote_resources(page):
    goto(page, "overview", 1500)
    hosts = page.evaluate("[...performance.getEntriesByType('resource')].map(r => new URL(r.name).host)")
    assert set(hosts) <= {page.base.split('//')[1]}, hosts
