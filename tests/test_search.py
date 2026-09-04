"""Search-logic and access-control tests.

No paid API is called: `_post_places` is stubbed throughout, so tiling and
pagination are exercised for free. The HTTP tests only run when a proxy is
already listening on 127.0.0.1:8093, and only issue GETs that must be refused.

Run:  python3 -m unittest discover -s tests -v
"""
import json, os, pathlib, shutil, sys, tempfile, threading, unittest, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-key-not-used")
import proxy  # noqa: E402

# Isolate the module from production state BEFORE any test runs. Without this the
# stubbed calls increment the real .usage.json — which drives the killswitch and
# the spend estimate — and write into the real response cache.
_TMP = pathlib.Path(tempfile.mkdtemp())
proxy.CACHE_DIR = _TMP / "cache"
proxy.USAGE_FILE = _TMP / "usage.json"
proxy.KILL_FILE = _TMP / "killswitch"

BASE = "http://127.0.0.1:8093"


def _server_up() -> bool:
    try:
        urllib.request.urlopen(BASE + "/api/status", timeout=2).read()
        return True
    except Exception:
        return False


class CoLocationMerging(unittest.TestCase):
    """Google lists a fuel station and its attached shop as separate places with
    different ids at the same address (verified: ARCO + ampm, 27 m apart)."""

    @staticmethod
    def rec(pid, name, seg, lat, lng, addr, reviews=10, o=None, c=None, h24=False):
        return {"place_id": pid, "name": name, "seg": seg, "lat": lat, "lng": lng,
                "address": addr, "reviews": reviews, "rating": 4.0,
                "open_min": o, "close_min": c, "is_24h": h24,
                "hours": "24 hours" if h24 else "09:00–22:00",
                "hours_ok": (o is not None and o <= 600 and c >= 1260),
                "phone": "", "website": "", "photo_ref": None, "type": ""}

    def test_same_address_and_close_are_merged(self):
        a = self.rec("p1", "ARCO", "gas", 34.0500, -118.2500, "1 Main St, LA, CA", 200)
        b = self.rec("p2", "ampm", "convenience", 34.05015, -118.25010, "1 Main St, LA, CA", 50)
        out = proxy.cluster_sites([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual([o["name"] for o in out[0]["also_here"]], ["ampm"])

    def test_same_address_but_far_apart_is_not_merged(self):
        a = self.rec("p1", "ARCO", "gas", 34.0500, -118.2500, "1 Main St, LA, CA")
        b = self.rec("p2", "Other", "smoke", 34.0600, -118.2600, "1 Main St, LA, CA")
        self.assertEqual(len(proxy.cluster_sites([a, b])), 2)

    def test_close_but_different_address_is_not_merged(self):
        """Strip-mall neighbours must survive as separate candidates."""
        a = self.rec("p1", "Shop A", "smoke", 34.0500, -118.2500, "1 Main St, LA, CA")
        b = self.rec("p2", "Shop B", "liquor", 34.05010, -118.25005, "3 Main St, LA, CA")
        self.assertEqual(len(proxy.cluster_sites([a, b])), 2)

    def test_merged_site_takes_the_most_restrictive_hours(self):
        """The machine is inside the shop: a 24 h pump with a shop closing at
        23:00 is a 23:00 site, not a 24 h one."""
        pump = self.rec("p1", "ARCO", "gas", 34.05, -118.25, "1 Main St, LA, CA", 200, h24=True)
        pump.update(open_min=0, close_min=1440)
        shop = self.rec("p2", "ampm", "convenience", 34.05005, -118.25002,
                        "1 Main St, LA, CA", 50, o=6 * 60, c=23 * 60)
        out = proxy.cluster_sites([pump, shop])[0]
        self.assertFalse(out["is_24h"])
        self.assertEqual(out["close_min"], 23 * 60)
        self.assertTrue(out["hours_ok"])          # closes after 21:00

    def test_merge_can_fail_the_hours_rule(self):
        pump = self.rec("p1", "Shell", "gas", 34.05, -118.25, "1 Main St, LA, CA", 300, h24=True)
        pump.update(open_min=0, close_min=1440)
        shop = self.rec("p2", "Food Mart", "convenience", 34.05005, -118.25002,
                        "1 Main St, LA, CA", 40, o=7 * 60, c=20 * 60)
        out = proxy.cluster_sites([pump, shop])[0]
        self.assertFalse(out["hours_ok"])         # closes at 20:00 — not viable


class ShopAndAtmDetection(unittest.TestCase):
    """Google's `types` under-reports, so absence must never read as 'no'."""

    def test_shop_type_is_confident(self):
        self.assertEqual(
            proxy.shop_presence({"types": ["gas_station", "convenience_store"]}, "Chevron"),
            "yes")

    def test_brand_name_is_only_likely(self):
        self.assertEqual(proxy.shop_presence({"types": ["gas_station"]}, "ARCO ampm"),
                         "likely")

    def test_absence_is_unknown_never_no(self):
        self.assertEqual(proxy.shop_presence({"types": ["gas_station"]}, "USA Gasoline"),
                         "unknown")


class WeeklyOpeningHours(unittest.TestCase):
    """Qualification uses the most restrictive reported day."""

    @staticmethod
    def place(*periods):
        return {"regularOpeningHours": {"periods": list(periods)}}

    def test_latest_open_and_earliest_close_define_weekly_window(self):
        place = self.place(
            {"open": {"day": 1, "hour": 8}, "close": {"day": 1, "hour": 23}},
            {"open": {"day": 2, "hour": 10}, "close": {"day": 2, "hour": 21}},
        )
        label, opened, closed, is_24h, meets = proxy._hours(place)
        self.assertEqual((label, opened, closed), ("10:00–21:00", 600, 1260))
        self.assertFalse(is_24h)
        self.assertTrue(meets)

    def test_one_early_closing_day_fails_the_open_late_rule(self):
        place = self.place(
            {"open": {"day": 1, "hour": 8}, "close": {"day": 1, "hour": 23}},
            {"open": {"day": 2, "hour": 9}, "close": {"day": 2, "hour": 20}},
        )
        self.assertFalse(proxy._hours(place)[4])


class AdaptiveTiling(unittest.TestCase):
    """Pagination and quadrant-splitting, with the network stubbed out."""

    def setUp(self):
        self.real = proxy._post_places
        self.calls = []
        # Tests share a query and a rect, so they also share a cache key. Start
        # every one cold or a later test scores a hit and spends zero calls.
        shutil.rmtree(proxy.CACHE_DIR, ignore_errors=True)

    def tearDown(self):
        proxy._post_places = self.real

    def _stub(self, per_call):
        def fake(body):
            self.calls.append(body)
            n = per_call(len(self.calls))
            places = [{"id": f"p{len(self.calls)}_{i}",
                       "displayName": {"text": f"S{i}"},
                       "location": {"latitude": 34.05, "longitude": -118.25}}
                      for i in range(n)]
            out = {"places": places}
            if n == proxy.PAGE_SIZE:
                out["nextPageToken"] = "tok"
            return out
        proxy._post_places = fake

    def test_sparse_tile_costs_one_call_and_never_splits(self):
        self._stub(lambda i: 3)
        rect = proxy.circle_to_rect(34.05, -118.25, 1000)
        out = proxy._adaptive("smoke", rect, {"calls": 0, "max": 40})
        self.assertEqual(out["calls"], 1)
        self.assertEqual(out["tiles"], 1)
        self.assertFalse(out["saturated"])

    def test_pagination_runs_to_the_api_ceiling(self):
        self._stub(lambda i: proxy.PAGE_SIZE)          # always full → always a token
        rect = proxy.circle_to_rect(34.05, -118.25, 1000)
        out = proxy._adaptive("smoke", rect, {"calls": 0, "max": 3})
        self.assertLessEqual(out["calls"], 3)          # budget respected
        self.assertEqual(proxy.MAX_PAGES, 3)

    def test_budget_is_never_exceeded(self):
        self._stub(lambda i: proxy.PAGE_SIZE)
        rect = proxy.circle_to_rect(34.05, -118.25, 4000)
        budget = {"calls": 0, "max": 5}
        proxy._adaptive("smoke", rect, budget)
        self.assertLessEqual(budget["calls"], 5)

    def test_warm_saturated_search_reassembles_cached_children(self):
        """A cached saturated parent still needs its cached child tiles."""
        self._stub(lambda i: proxy.PAGE_SIZE if i <= proxy.MAX_PAGES else 2)
        rect = proxy.circle_to_rect(34.05, -118.25, 1000)

        cold = proxy._adaptive("smoke", rect, {"calls": 0, "max": 40})
        warm = proxy._adaptive("smoke", rect, {"calls": 0, "max": 40})

        self.assertEqual(cold["calls"], 7)
        self.assertEqual(warm["calls"], 0)
        self.assertEqual(warm["cache_hits"], 5)
        self.assertEqual(warm["tiles"], 5)
        self.assertEqual(warm["places"], cold["places"])

    def test_children_tile_the_parent_without_gaps(self):
        rect = {"lo_lat": 0.0, "hi_lat": 2.0, "lo_lng": 0.0, "hi_lng": 2.0}
        kids = proxy._children(rect)
        self.assertEqual(len(kids), 4)
        area = sum((k["hi_lat"] - k["lo_lat"]) * (k["hi_lng"] - k["lo_lng"]) for k in kids)
        self.assertAlmostEqual(area, 4.0, places=9)    # exactly covers, no overlap


class AtmResolution(unittest.TestCase):
    """Existing ATMs arrive with no place_id, so each costs one lookup. The
    query shape matters: verified 2026-08-31 that appending city/state/zip
    returns 0 results where the street head alone matches exactly."""

    def setUp(self):
        self.real = proxy._post_places
        self.sent = []

    def tearDown(self):
        proxy._post_places = self.real

    def _stub(self, places):
        def fake(body):
            self.sent.append(body)
            return {"places": places}
        proxy._post_places = fake

    ATM = {"sn": "SN1", "name": "Almond Smoke Shop",
           "address": "30145 Antelope Rd, Menifee, CA 92584",
           "lat": 33.6836642, "lng": -117.1673222}

    @staticmethod
    def place(addr):
        return {"id": "P1", "displayName": {"text": "Almond Smoke Shop + CIGAR"},
                "formattedAddress": addr, "location": {"latitude": 33.6836,
                                                       "longitude": -117.1673},
                "types": ["store"]}

    def test_query_uses_the_street_head_not_the_full_address(self):
        self._stub([self.place("30145 Antelope Rd, Menifee, CA 92584, USA")])
        proxy.resolve_place(self.ATM, {"calls": 0, "max": 5})
        q = self.sent[0]["textQuery"]
        self.assertIn("30145 Antelope Rd", q)
        self.assertNotIn("Menifee", q)      # city/state/zip over-constrain
        self.assertNotIn("92584", q)

    def test_search_is_restricted_not_biased(self):
        """An unrestricted name search matches the same brand in another town."""
        self._stub([self.place("30145 Antelope Rd, Menifee, CA 92584, USA")])
        proxy.resolve_place(self.ATM, {"calls": 0, "max": 5})
        self.assertIn("locationRestriction", self.sent[0])
        self.assertNotIn("locationBias", self.sent[0])

    def test_matching_address_resolves_as_exact(self):
        self._stub([self.place("30145 Antelope Rd, Menifee, CA 92584, USA")])
        pid, meta, conf = proxy.resolve_place(self.ATM, {"calls": 0, "max": 5})
        self.assertEqual((pid, conf), ("P1", "exact"))
        self.assertEqual(meta["name"], "Almond Smoke Shop + CIGAR")

    def test_different_address_in_the_box_is_only_near(self):
        self._stub([self.place("30201 Some Other Rd, Menifee, CA 92584, USA")])
        _, _, conf = proxy.resolve_place(self.ATM, {"calls": 0, "max": 5})
        self.assertEqual(conf, "near")

    def test_no_results_is_a_clean_miss(self):
        self._stub([])
        pid, meta, conf = proxy.resolve_place(self.ATM, {"calls": 0, "max": 5})
        self.assertEqual((pid, meta, conf), (None, {}, "none"))

    def test_a_miss_costs_two_calls_because_of_the_address_retry(self):
        """Nothing found on name+street triggers one address-only retry, so a
        true miss costs exactly two calls -- never more."""
        self._stub([])
        budget = {"calls": 0, "max": 5}
        proxy.resolve_place(self.ATM, budget)
        self.assertEqual(budget["calls"], 2)

    def test_the_retry_drops_the_name_and_widens_the_box(self):
        self._stub([])
        proxy.resolve_place(self.ATM, {"calls": 0, "max": 5})
        first, retry = self.sent[0]["textQuery"], self.sent[1]["textQuery"]
        self.assertIn("Almond", first)
        self.assertNotIn("Almond", retry)          # name dropped: it can be stale
        self.assertEqual(retry, "30145 Antelope Rd")
        span = lambda b: (b["locationRestriction"]["rectangle"]["high"]["latitude"]
                          - b["locationRestriction"]["rectangle"]["low"]["latitude"])
        self.assertGreater(span(self.sent[1]), span(self.sent[0]))

    def test_the_retry_accepts_only_an_exact_address_match(self):
        """A wide retry box is safe only because a near miss is rejected --
        verified against a real case that returned a neighbourhood, not a shop."""
        calls = {"n": 0}
        def fake(body):
            calls["n"] += 1
            self.sent.append(body)
            if calls["n"] == 1:
                return {"places": []}
            return {"places": [self.place("999 Somewhere Else Rd, Menifee, CA")]}
        proxy._post_places = fake
        pid, _, conf = proxy.resolve_place(self.ATM, {"calls": 0, "max": 5})
        self.assertIsNone(pid)
        self.assertEqual(conf, "none")

    def test_the_retry_accepts_a_new_tenant_at_the_same_address(self):
        """A different business at the exact address is a tenant change, which is
        signal worth keeping -- not a bad match."""
        calls = {"n": 0}
        def fake(body):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"places": []}
            p = self.place("30145 Antelope Rd, Menifee, CA 92584, USA")
            p["displayName"] = {"text": "AIRPORT MARKET"}
            return {"places": [p]}
        proxy._post_places = fake
        pid, meta, conf = proxy.resolve_place(self.ATM, {"calls": 0, "max": 5})
        self.assertEqual(conf, "exact")
        self.assertEqual(meta["name"], "AIRPORT MARKET")

    def test_a_record_without_coordinates_is_never_looked_up(self):
        self._stub([])
        budget = {"calls": 0, "max": 5}
        pid, _, _ = proxy.resolve_place({"name": "X", "address": "Y"}, budget)
        self.assertIsNone(pid)
        self.assertEqual(budget["calls"], 0)     # no coords, no spend


class DurableSearchRuns(unittest.TestCase):
    """Search execution is recorded without touching the production database."""

    def setUp(self):
        self.old_store = (proxy.store.DB_PATH, proxy.store.LEGACY_JSON,
                          proxy.store._local)
        self.old_adaptive = proxy._adaptive
        root = pathlib.Path(tempfile.mkdtemp())
        proxy.store.DB_PATH = root / "sites.db"
        proxy.store.LEGACY_JSON = root / "missing.json"
        proxy.store._local = threading.local()
        proxy.store.init()
        shutil.rmtree(proxy.CACHE_DIR, ignore_errors=True)

    def tearDown(self):
        live = getattr(proxy.store._local, "c", None)
        if live:
            live.close()
        proxy.store.DB_PATH, proxy.store.LEGACY_JSON, proxy.store._local = self.old_store
        proxy._adaptive = self.old_adaptive

    @staticmethod
    def params(**overrides):
        base = {"mode": "pin", "zip": "", "zip_shape": "radius",
                "label": "Downtown LA", "lat": 34.05, "lng": -118.25,
                "radius_m": 2500, "segments": ["gas"],
                "include_hidden": False, "polygon": []}
        base.update(overrides)
        return base

    @staticmethod
    def place(pid="P1", lat=34.05, lng=-118.25):
        return {"id": pid, "displayName": {"text": "Test Market"},
                "formattedAddress": "1 Main St, Los Angeles, CA",
                "location": {"latitude": lat, "longitude": lng},
                "primaryType": "convenience_store",
                "regularOpeningHours": {"periods": [{
                    "open": {"day": 1, "hour": 8},
                    "close": {"day": 1, "hour": 23}}]}}

    def test_success_is_checkpointed_and_reopenable(self):
        def fake(seg, rect, budget, depth=0):
            budget["calls"] += 1
            return {"places": [self.place()], "calls": 1, "cache_hits": 0,
                    "tiles": 1, "saturated": False, "max_depth": 0}
        proxy._adaptive = fake
        out = proxy.execute_search_run(self.params())
        saved = proxy.store.get_search_run(out["search_id"])
        self.assertEqual(out["run_status"], "complete")
        self.assertEqual((saved["result_count"], saved["api_calls"]), (1, 1))
        self.assertEqual(saved["results"][0]["name"], "Test Market")
        self.assertEqual(saved["estimated_cost_usd"], .035)

    def test_repeat_receives_a_new_id_linked_to_the_original(self):
        proxy._adaptive = lambda *a, **k: {
            "places": [], "calls": 0, "cache_hits": 1, "tiles": 1,
            "saturated": False, "max_depth": 0}
        first = proxy.execute_search_run(self.params())
        second = proxy.execute_search_run(self.params())
        self.assertNotEqual(first["search_id"], second["search_id"])
        self.assertEqual(second["repeated_from"], first["search_id"])

    def test_polygon_snapshot_contains_only_visible_results(self):
        def fake(seg, rect, budget, depth=0):
            return {"places": [self.place("IN", 34.05, -118.25),
                               self.place("OUT", 34.08, -118.25)],
                    "calls": 1, "cache_hits": 0, "tiles": 1,
                    "saturated": False, "max_depth": 0}
        proxy._adaptive = fake
        polygon = [[34.04, -118.26], [34.06, -118.26],
                   [34.06, -118.24], [34.04, -118.24]]
        out = proxy.execute_search_run(self.params(radius_m=10000, polygon=polygon))
        self.assertEqual([r["place_id"] for r in out["results"]], ["IN"])
        saved = proxy.store.get_search_run(out["search_id"])
        self.assertEqual([r["place_id"] for r in saved["results"]], ["IN"])

    def test_provider_failure_is_visible_in_history(self):
        def broken(*args, **kwargs):
            raise RuntimeError("provider unavailable")
        proxy._adaptive = broken
        out = proxy.execute_search_run(self.params())
        saved = proxy.store.get_search_run(out["search_id"])
        self.assertEqual(out["run_status"], "failed")
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error_code"], "places_unreachable")

    def test_provider_failure_returns_checkpointed_partial_results(self):
        calls = 0

        def partial_then_broken(seg, rect, budget, depth=0):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("provider unavailable")
            budget["calls"] += 1
            return {"places": [self.place()], "calls": 1, "cache_hits": 0,
                    "tiles": 1, "saturated": False, "max_depth": 0}

        proxy._adaptive = partial_then_broken
        out = proxy.execute_search_run(self.params(segments=["gas", "liquor"]))
        saved = proxy.store.get_search_run(out["search_id"])
        self.assertEqual(out["run_status"], "failed")
        self.assertEqual(out["failed_segment"], "liquor")
        self.assertEqual([r["place_id"] for r in out["results"]], ["P1"])
        self.assertEqual([r["place_id"] for r in saved["results"]], ["P1"])

    def test_budget_stop_has_a_distinct_terminal_state(self):
        proxy._adaptive = lambda *a, **k: {
            "places": [], "calls": 0, "cache_hits": 0, "tiles": 1,
            "saturated": True, "max_depth": 0, "budget_hit": True}
        out = proxy.execute_search_run(self.params())
        self.assertEqual(out["run_status"], "stopped_budget")
        self.assertIn("per-search call budget", out["refused"])

    def test_request_validation_rejects_invalid_geometry(self):
        with self.assertRaisesRegex(ValueError, "bad_coords"):
            proxy.parse_search_params({**self.params(), "lat": 100})
        with self.assertRaisesRegex(ValueError, "bad_polygon"):
            proxy.parse_search_params({**self.params(), "polygon": [[34, -118]]})


class PricingAccounting(unittest.TestCase):
    """The field mask—not the endpoint name—determines the Places SKU."""

    def test_enterprise_fields_are_priced_as_text_search_enterprise(self):
        enterprise_fields = {
            "places.regularOpeningHours", "places.nationalPhoneNumber",
            "places.rating", "places.userRatingCount", "places.websiteUri",
        }
        self.assertTrue(enterprise_fields.issubset(set(proxy.FIELD_MASK.split(","))))
        self.assertEqual(proxy.SKU["search"]["label"],
                         "Places Text Search (Enterprise)")
        self.assertEqual(proxy.SKU["search"]["per_1k"], 35.00)
        self.assertEqual(proxy.SKU["search"]["free_month"], 1000)

    def test_local_estimate_applies_the_published_free_usage_cap(self):
        usage = {"month_by_sku": {"search": 1000, "geocode": 0, "photo": 0}}
        self.assertEqual(proxy.estimate_spend(usage), 0.0)
        usage["month_by_sku"]["search"] = 1001
        self.assertEqual(proxy.estimate_spend(usage), 0.035)

    def test_pricing_provenance_is_exposed_without_credentials(self):
        self.assertRegex(proxy.PRICING_AS_OF, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(proxy.PRICING_SOURCE,
                         "https://developers.google.com/maps/billing-and-pricing/pricing")


class HardLifetimeCap(unittest.TestCase):
    """The ceiling for an exposed deployment. It must not be recoverable by
    waiting for midnight or by deleting the kill file, or it is not a hard cap."""

    def setUp(self):
        self.cap, self.usage = proxy.HARD_CALL_CAP, proxy.USAGE_FILE
        proxy.HARD_CALL_CAP = 3
        proxy.USAGE_FILE = _TMP / f"usage_{id(self)}.json"
        proxy.clear_kill()
        proxy._burst.clear()

    def tearDown(self):
        proxy.HARD_CALL_CAP, proxy.USAGE_FILE = self.cap, self.usage
        proxy.clear_kill()

    def _spend(self, n):
        for _ in range(n):
            proxy.record_call("search")

    def test_calls_are_allowed_up_to_the_cap(self):
        self._spend(2)
        self.assertIsNone(proxy.guard())

    def test_the_cap_refuses_further_calls(self):
        self._spend(3)
        self.assertIn("hard cap", proxy.guard() or "")

    def test_clearing_the_killswitch_does_not_restore_capacity(self):
        self._spend(3)
        proxy.guard()                      # trips the kill file
        proxy.clear_kill()
        self.assertIn("hard cap", proxy.guard() or "")

    def test_a_new_day_does_not_restore_capacity(self):
        """The daily counter resets at midnight; the lifetime counter must not."""
        self._spend(3)
        u = json.loads(proxy.USAGE_FILE.read_text())
        u["date"] = "2020-01-01"           # force the daily rollover
        proxy.USAGE_FILE.write_text(json.dumps(u))
        proxy.clear_kill()
        self.assertEqual(proxy._usage()["calls"], 0)        # daily did reset
        self.assertEqual(proxy._usage()["lifetime"], 3)     # lifetime did not
        self.assertIn("hard cap", proxy.guard() or "")

    def test_status_reports_what_is_left(self):
        self._spend(1)
        s = proxy.status()
        self.assertEqual(s["hard_cap"], 3)
        self.assertEqual(s["lifetime_calls"], 1)
        self.assertEqual(s["remaining_lifetime"], 2)


class StaticFileAccess(unittest.TestCase):
    """The handler serves the project directory, so this must be an allowlist."""

    @classmethod
    def setUpClass(cls):
        if not _server_up():
            raise unittest.SkipTest("proxy not running on 127.0.0.1:8093")

    def _status(self, path):
        try:
            return urllib.request.urlopen(BASE + path, timeout=5).getcode()
        except urllib.error.HTTPError as e:
            return e.code

    def test_data_and_source_are_refused(self):
        for path in ("/sites.db", "/sites.db-wal", "/store.py", "/crm_adapter.py",
                     "/proxy.py", "/.env", "/sites.json", "/build_atms.py",
                     "/api/../.env", "/api/%2e%2e/.env", "/%2e%2e/.env"):
            self.assertEqual(self._status(path), 403, path)

    def test_app_assets_are_served(self):
        for path in ("/", "/index.html", "/atms.js", "/pipeline.js"):
            self.assertEqual(self._status(path), 200, path)

    def test_traversal_is_refused(self):
        self.assertEqual(self._status("/../.env"), 403)


if __name__ == "__main__":
    unittest.main()
