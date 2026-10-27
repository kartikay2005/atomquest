"""Browser end-to-end test: the console page really updates and its controls work.
Skipped automatically when Playwright (and its Chromium) is not installed."""
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

pw = pytest.importorskip("playwright.sync_api")

from engine import Engine, Settings  # noqa: E402
from server import make_handler  # noqa: E402


@pytest.fixture(scope="module")
def url():
    eng = Engine(Settings(month=5, nights=1, seed=1))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(eng))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    eng.stop()


def test_console_updates_and_controls_work(url):
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium not available: {e}")
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_function("document.querySelector('#connText').textContent.startsWith('Live')", timeout=20000)
        page.wait_for_function("!document.querySelector('#notice').textContent", timeout=30000)
        c1 = page.inner_text("#clock")
        page.click("#speeds button[data-s='3600']")
        time.sleep(2.5)
        assert page.inner_text("#clock") != c1, "clock did not advance"
        assert page.inner_text("#kViol") == "0"
        page.click("button[data-f='sensor_crc']")
        page.wait_for_function("document.querySelector('#log').textContent.includes('sensor_crc')", timeout=10000)
        page.click("#tabs button[data-tab='connect']")
        assert page.locator("#flow .node").count() == 8   # weather, room, pod, fan, firmware, governor, IR, AC
        page.click("#tabs button[data-tab='live']")
        page.click("#scenario summary")
        page.click("#months button[data-m='8']")
        page.click("#apply")
        page.wait_for_function("document.querySelector('#scenSummary').textContent.includes('August')", timeout=30000)
        assert not errors, errors
        browser.close()
