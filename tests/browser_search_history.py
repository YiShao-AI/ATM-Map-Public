#!/usr/bin/env python3
"""Local browser verification for Search History; never calls a paid API.

Run with a Python environment that has Playwright installed:
  python tests/browser_search_history.py [screenshot-path]
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GOOGLE_MAPS_API_KEY", "browser-test-key-not-used")

import proxy  # noqa: E402


def fake_place(segment: str) -> dict:
    offsets = {"gas": 0.000, "liquor": 0.004, "smoke": -0.004,
               "convenience": 0.006, "laundromat": -0.006}
    offset = offsets.get(segment, 0)
    primary = {"gas": "gas_station", "liquor": "liquor_store",
               "laundromat": "laundry", "convenience": "convenience_store",
               "smoke": "tobacco_shop"}.get(segment, "store")
    return {
        "id": f"browser-{segment}",
        # A leading formula character exercises spreadsheet-injection escaping
        # in the downloaded CSV without calling an external provider.
        "displayName": {
            "text": "=1+1" if segment == "gas" else f"Browser Test {segment.title()}"
        },
        "formattedAddress": f"{100 + len(segment)} Main St, Los Angeles, CA",
        "location": {"latitude": 34.05 + offset, "longitude": -118.25 + offset},
        "primaryType": primary, "types": [primary],
        "rating": 4.4,
        "userRatingCount": 81,
        "regularOpeningHours": {"periods": [{
            "open": {"day": 1, "hour": 8},
            "close": {"day": 1, "hour": 23},
        }]},
    }


def main() -> None:
    isolated = pathlib.Path(tempfile.mkdtemp(prefix="atm-history-browser-"))
    proxy.store.DB_PATH = isolated / "sites.db"
    proxy.store.LEGACY_JSON = isolated / "missing.json"
    proxy.store._local = threading.local()
    proxy.CACHE_DIR = isolated / "cache"
    proxy.USAGE_FILE = isolated / "usage.json"
    proxy.KILL_FILE = isolated / "killswitch"
    proxy.VISIT_LOG = str(isolated / "visits.log")
    proxy.AUTH_ON = False
    proxy.store.init()

    state_params = {"mode": "pin", "zip": "", "zip_shape": "radius",
                    "label": "Recovery check", "lat": 34.05, "lng": -118.25,
                    "radius_m": 1000, "segments": ["gas"],
                    "include_hidden": False, "polygon": []}
    stopped = proxy.store.create_search_run(state_params)["search_code"]
    proxy.store.checkpoint_search_run(
        stopped, {"results": [], "api_calls": 3}, run_status="stopped_budget",
        error_detail="Configured call budget reached.")
    interrupted = proxy.store.create_search_run(
        {**state_params, "label": "Interrupted check", "lat": 34.06})["search_code"]
    proxy.store.checkpoint_search_run(
        interrupted, {"results": [{"place_id": "partial", "name": "Recovered result",
                                    "lat": 34.06, "lng": -118.25}], "api_calls": 1})
    proxy.store.recover_interrupted_search_runs()

    calls: dict[str, int] = {}

    def fake_adaptive(segment, rect, budget, depth=0):
        calls[segment] = calls.get(segment, 0) + 1
        warm = calls[segment] > 1
        if not warm:
            budget["calls"] += 1
        return {"places": [fake_place(segment)], "calls": 0 if warm else 1,
                "cache_hits": 1 if warm else 0, "tiles": 1,
                "saturated": False, "max_depth": 0}

    proxy._adaptive = fake_adaptive
    server = proxy.ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}/"
    shot = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else isolated / "history.png"

    params = {"mode": "pin", "zip": "", "zip_shape": "radius",
              "label": "Downtown LA", "lat": 34.05, "lng": -118.25,
              "radius_m": 2500, "segments": ["gas", "liquor"],
              "include_hidden": False, "polygon": []}
    page_errors = []
    try:
        with sync_playwright() as play:
            browser = play.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 900},
                                          accept_downloads=True)
            page = context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_selector("#map")

            page.evaluate("p => runSearch(p)", params)
            first = page.evaluate("currentSearchId")
            assert first.startswith("S-")
            assert "Found 2 sites" in page.locator("#status").inner_text()
            row_count = page.locator("#rbody tr[data-id]").count()
            assert row_count == 2, (row_count, page.evaluate("results"),
                                    page.locator("#rbody").inner_html())

            page.locator("#vHistory").click()
            page.wait_for_selector(f'[data-open-run="{first}"]')
            assert "STOPPED BY BUDGET" in page.locator(f'tr[data-run="{stopped}"]').inner_text()
            assert "INTERRUPTED" in page.locator(f'tr[data-run="{interrupted}"]').inner_text()
            row = page.locator(f'tr[data-run="{first}"]')
            row_text = row.inner_text()
            assert "COMPLETE" in row_text, row_text
            assert "2" in row_text, row_text

            # The visible ID itself is an action, not merely accent-coloured text.
            row.locator('.run-link').click()
            page.wait_for_function("code => currentSearchId === code && view === 'results'", arg=first)
            page.wait_for_selector("#rbody tr[data-id]")
            assert page.locator("#rbody tr[data-id]").count() == 2

            with page.expect_download() as download_info:
                page.locator("#csvBtn").click()
            download = download_info.value
            assert first in download.suggested_filename
            csv_text = pathlib.Path(download.path()).read_text(encoding="utf-8")
            assert '"\'=1+1"' in csv_text
            assert '"=1+1"' not in csv_text

            page.locator("#vHistory").click()
            page.locator(f'[data-repeat-run="{first}"]').click()
            page.wait_for_function("code => currentSearchId && currentSearchId !== code", arg=first)
            second = page.evaluate("currentSearchId")
            assert page.locator("#progTxt").inner_text().endswith("2 cached")

            # A provider failure after one completed category must render the
            # checkpointed rows immediately, without requiring a page refresh.
            failure_calls = 0
            def partial_then_broken(segment, rect, budget, depth=0):
                nonlocal failure_calls
                failure_calls += 1
                if failure_calls == 2:
                    raise RuntimeError("provider unavailable")
                budget["calls"] += 1
                return {"places": [fake_place(segment)], "calls": 1,
                        "cache_hits": 0, "tiles": 1, "saturated": False,
                        "max_depth": 0}
            proxy._adaptive = partial_then_broken
            page.evaluate("p => runSearch(p)", params)
            assert page.locator("#rbody tr[data-id]").count() == 1
            assert "checkpointed partial results" in page.locator("#status").inner_text()
            assert "not completed" in page.locator("#status").inner_text()

            direct = context.new_page()
            direct.on("pageerror", lambda error: page_errors.append(str(error)))
            direct.goto(base + "#search=" + second, wait_until="domcontentloaded")
            direct.wait_for_function("code => currentSearchId === code", arg=second)
            assert direct.locator("#rbody tr[data-id]").count() == 2

            direct.locator("#vHistory").click()
            direct.locator("#historyQuery").fill(first)
            direct.locator("#historyFind").click()
            direct.wait_for_selector(f'tr[data-run="{first}"]')
            assert direct.locator("tr[data-run]").count() == 1
            direct.screenshot(path=str(shot), full_page=True)

            browser.close()
        if page_errors:
            raise AssertionError("browser page errors: " + "; ".join(page_errors))
        print(f"PASS search history browser flow: {first} -> {second}")
        print(f"Screenshot: {shot}")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
