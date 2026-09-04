"""Store logic tests. No network, no paid API, no production data.

Each test gets a throwaway database in a temp directory: store.py is loaded with
its ROOT rewritten, so sites.db here is never touched.

Run:  python3 -m unittest discover -s tests -v
"""
import importlib.util, pathlib, tempfile, unittest
from datetime import date, timedelta

SRC = pathlib.Path(__file__).resolve().parent.parent / "store.py"


def fresh_store():
    tmp = pathlib.Path(tempfile.mkdtemp())
    src = SRC.read_text().replace(
        "ROOT = Path(__file__).resolve().parent", f'ROOT = Path("{tmp}")')
    mod_path = tmp / "store_under_test.py"
    mod_path.write_text(src)
    spec = importlib.util.spec_from_file_location("store_under_test", mod_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.init()
    return m


class MonotonicFunnel(unittest.TestCase):
    """The rule that stops a mailing partner re-uploading a file and knocking
    shops in `negotiating` back to `postcard_mailed`."""

    def setUp(self):
        self.s = fresh_store()

    def test_stage_advances(self):
        self.s.upsert("A", {"name": "A", "stage": "responded"})
        self.s.upsert("A", {"stage": "negotiating"})
        self.assertEqual(self.s.get("A")["stage"], "negotiating")

    def test_stage_never_goes_backwards(self):
        self.s.upsert("A", {"name": "A", "stage": "negotiating"})
        out = self.s.upsert("A", {"stage": "postcard_mailed"})
        self.assertEqual(self.s.get("A")["stage"], "negotiating")
        self.assertEqual(out.get("stage_blocked"), "postcard_mailed")

    def test_blocked_stage_is_still_recorded_as_evidence(self):
        self.s.upsert("A", {"name": "A", "stage": "negotiating"})
        self.s.upsert("A", {"stage": "postcard_mailed"})
        kinds = [e["kind"] for e in self.s.events("A")]
        self.assertIn("stage_blocked", kinds)

    def test_parallel_entry_points_share_a_rank(self):
        # a shop reached by phone is as far along as one that answered a postcard
        self.assertEqual(self.s.STAGE_RANK["contacted"], self.s.STAGE_RANK["responded"])


class RevisitMaths(unittest.TestCase):
    """A reject reason's half-life is the basis of the whole revisit queue."""

    def setUp(self):
        self.s = fresh_store()

    def test_every_reason_code_resolves(self):
        for code, spec in self.s.REASON_CODES.items():
            after, perm = self.s.compute_revisit(code)
            if spec["permanent"] or not spec["months"]:
                self.assertIsNone(after, code)
                self.assertEqual(perm, 1, code)
            else:
                self.assertIsNotNone(after, code)
                self.assertEqual(perm, 0, code)

    def test_interval_is_roughly_the_stated_months(self):
        after, _ = self.s.compute_revisit("competitor_atm", when=date(2026, 1, 1))
        delta = (date.fromisoformat(after) - date(2026, 1, 1)).days
        self.assertAlmostEqual(delta, 24 * 30.44, delta=5)

    def test_unknown_code_is_not_treated_as_permanent(self):
        self.assertEqual(self.s.compute_revisit("nonsense"), (None, 0))

    def test_rejecting_sets_the_expiry(self):
        r = self.s.upsert("A", {"name": "A", "status": "rejected",
                                "reason_code": "owner_declined"})
        self.assertIsNotNone(r["revisit_after"])
        self.assertEqual(r["permanent"], 0)

    def test_permanent_reason_never_revisits(self):
        r = self.s.upsert("A", {"name": "A", "status": "rejected",
                                "reason_code": "no_indoor_space"})
        self.assertIsNone(r["revisit_after"])
        self.assertEqual(r["permanent"], 1)

    def test_requalifying_clears_permanent(self):
        """Regression: `permanent` used to stick after a site left `rejected`."""
        self.s.upsert("A", {"name": "A", "status": "rejected",
                            "reason_code": "no_indoor_space"})
        r = self.s.upsert("A", {"status": "prospect"})
        self.assertEqual(r["permanent"], 0)
        self.assertIsNone(r["revisit_after"])
        self.assertIsNone(r["reason_code"])

    def test_due_queue_excludes_permanent_and_future(self):
        past = (date.today() - timedelta(days=1)).isoformat()
        self.s.upsert("DUE", {"name": "Due", "status": "rejected",
                              "reason_code": "owner_declined"})
        self.s.conn().execute("UPDATE sites SET revisit_after=? WHERE place_id='DUE'", (past,))
        self.s.conn().commit()
        self.s.upsert("PERM", {"name": "Perm", "status": "rejected",
                               "reason_code": "closed"})
        self.s.upsert("FUTURE", {"name": "Later", "status": "rejected",
                                 "reason_code": "safety"})
        ids = {r["place_id"] for r in self.s.revisit_due()}
        self.assertEqual(ids, {"DUE"})


class CampaignTransition(unittest.TestCase):
    """Bulk transition is the headline feature; it used to invent site rows."""

    def setUp(self):
        self.s = fresh_store()

    def test_unknown_members_are_reported_not_created(self):
        """Regression: a stale id in a campaign silently became a nameless site."""
        self.s.upsert("REAL", {"name": "Real shop"})
        self.s.create_campaign("c", ["REAL", "GHOST"])
        cid = self.s.campaigns()[0]["campaign_id"]
        out = self.s.transition_campaign(cid, "postcard_mailed")
        self.assertEqual(out["advanced"], 1)
        self.assertEqual(out["unknown"], ["GHOST"])
        self.assertIsNone(self.s.get("GHOST"))

    def test_members_further_along_are_skipped(self):
        self.s.upsert("AHEAD", {"name": "Ahead", "stage": "negotiating"})
        self.s.upsert("BEHIND", {"name": "Behind"})
        self.s.create_campaign("c", ["AHEAD", "BEHIND"])
        cid = self.s.campaigns()[0]["campaign_id"]
        out = self.s.transition_campaign(cid, "postcard_mailed")
        self.assertEqual(out["advanced"], 1)
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(self.s.get("AHEAD")["stage"], "negotiating")

    def test_transition_writes_history_for_advanced_members(self):
        self.s.upsert("A", {"name": "A"})
        self.s.create_campaign("c", ["A"])
        cid = self.s.campaigns()[0]["campaign_id"]
        self.s.transition_campaign(cid, "postcard_mailed")
        live = [d for d in self.s.history("A") if not d["superseded"]]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["stage"], "postcard_mailed")

    def test_bulk_transition_scales(self):
        """400 members must not fall foul of SQLite's variable limit."""
        ids = [f"S{i}" for i in range(400)]
        for i in ids:
            self.s.upsert(i, {"name": i})
        self.s.create_campaign("big", ids)
        cid = self.s.campaigns()[0]["campaign_id"]
        out = self.s.transition_campaign(cid, "postcard_mailed")
        self.assertEqual(out["advanced"], 400)


class DecisionHistory(unittest.TestCase):
    def setUp(self):
        self.s = fresh_store()

    def test_repeat_writes_do_not_churn_decisions(self):
        for _ in range(3):
            self.s.upsert("A", {"name": "A", "status": "rejected",
                                "reason_code": "owner_declined"})
        rows = self.s.history("A")
        self.assertEqual(len(rows), 1)

    def test_status_change_supersedes_the_previous_decision(self):
        self.s.upsert("A", {"name": "A", "status": "rejected",
                            "reason_code": "owner_declined"})
        self.s.upsert("A", {"status": "pipeline"})
        rows = self.s.history("A")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len([r for r in rows if not r["superseded"]]), 1)

    def test_delete_removes_children(self):
        self.s.upsert("A", {"name": "A", "status": "rejected",
                            "reason_code": "closed"})
        self.s.create_campaign("c", ["A"])
        self.assertTrue(self.s.delete("A"))
        c = self.s.conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM decisions WHERE place_id='A'").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM campaign_members WHERE place_id='A'").fetchone()[0], 0)
        # the audit trail is deliberately preserved
        self.assertTrue(any(e["kind"] == "deleted" for e in self.s.events("A")))


class ManualVerification(unittest.TestCase):
    def setUp(self):
        self.s = fresh_store()

    def test_manual_verification_round_trip_is_reversible(self):
        marked = self.s.upsert("A", {"name": "Verified shop", "shortlist": 1})
        self.assertEqual(marked["shortlist"], 1)
        self.s.upsert("A", {"notes": "keep the approval while editing"})
        self.assertEqual(self.s.get("A")["shortlist"], 1)
        cleared = self.s.upsert("A", {"shortlist": 0})
        self.assertEqual(cleared["shortlist"], 0)

    def test_rejection_removes_manual_approval(self):
        self.s.upsert("A", {"name": "Verified shop", "shortlist": 1})
        rejected = self.s.upsert("A", {"status": "rejected",
                                        "reason_code": "owner_declined"})
        self.assertEqual(rejected["shortlist"], 0)


class PlaceMetadata(unittest.TestCase):
    """Rich fields are shared by search results, pipeline sites and existing
    ATMs, and reach the store by two routes — a free capture at mark() time and
    a paid lookup — which must not overwrite each other with blanks."""

    def setUp(self):
        self.s = fresh_store()

    def test_capture_stores_the_rich_fields(self):
        self.s.upsert_meta("P1", {"name": "Shop", "hours": "09:00–21:00",
                                  "phone": "555", "rating": 4.5, "reviews": 294,
                                  "has_shop": "yes"})
        m = self.s.get_meta("P1")
        self.assertEqual(m["hours"], "09:00–21:00")
        self.assertEqual(m["reviews"], 294)
        self.assertEqual(m["source"], "search")

    def test_blank_values_never_overwrite_known_ones(self):
        """A later capture missing `phone` must not erase a phone we paid for."""
        self.s.upsert_meta("P1", {"name": "Shop", "phone": "555-0100"}, source="lookup")
        self.s.upsert_meta("P1", {"name": "Shop", "phone": "", "rating": 4.1})
        m = self.s.get_meta("P1")
        self.assertEqual(m["phone"], "555-0100")
        self.assertEqual(m["rating"], 4.1)

    def test_booleans_survive_the_round_trip(self):
        self.s.upsert_meta("P1", {"is_24h": True, "has_atm": False, "hours_ok": True})
        m = self.s.get_meta("P1")
        self.assertEqual((m["is_24h"], m["has_atm"], m["hours_ok"]), (1, 0, 1))

    def test_meta_many_chunks_past_the_variable_limit(self):
        ids = [f"P{i}" for i in range(450)]
        for i in ids:
            self.s.upsert_meta(i, {"name": i})
        self.assertEqual(len(self.s.meta_many(ids)), 450)

    def test_a_failed_lookup_is_recorded_so_it_is_not_paid_for_twice(self):
        self.s.link_atm("SN1", None, "none")
        self.assertIn("SN1", self.s.atm_links())
        self.assertIsNone(self.s.atm_links()["SN1"]["place_id"])

    def test_resolving_an_atm_links_it_to_a_place(self):
        self.s.link_atm("SN1", None, "none")          # earlier miss
        self.s.link_atm("SN1", "P1", "exact")         # later success replaces it
        self.assertEqual(self.s.atm_links()["SN1"]["place_id"], "P1")
        self.assertEqual(self.s.atm_links()["SN1"]["confidence"], "exact")

    def test_metadata_does_not_create_a_pipeline_site(self):
        """An existing ATM is not a prospect — enriching it must not add a row
        to `sites` or it would show up as a candidate."""
        self.s.upsert_meta("P1", {"name": "Existing ATM host"}, source="lookup")
        self.assertIsNone(self.s.get("P1"))
        self.assertEqual(len(self.s.all_sites()), 0)


class LocationIdentity(unittest.TestCase):
    def setUp(self):
        self.s = fresh_store()

    def test_same_address_different_tenant_shares_a_location(self):
        a = self.s.location_id_for("100 Main St, Springfield, IL", 34.05, -118.25)
        b = self.s.location_id_for("100 Main St Ste 4, Springfield, IL", 34.05, -118.25)
        self.assertEqual(a, b)

    def test_different_addresses_differ(self):
        a = self.s.location_id_for("100 Main St", 34.05, -118.25)
        b = self.s.location_id_for("200 Main St", 34.06, -118.26)
        self.assertNotEqual(a, b)


class SearchRunHistory(unittest.TestCase):
    def setUp(self):
        self.s = fresh_store()
        self.params = {
            "mode": "pin", "zip": "", "zip_shape": "radius",
            "label": "Downtown LA", "lat": 34.0522, "lng": -118.2437,
            "radius_m": 2500, "segments": ["gas", "liquor"],
            "include_hidden": False, "polygon": [],
        }

    def test_id_exists_before_provider_work_and_has_a_human_format(self):
        run = self.s.create_search_run(self.params, source_version="abc123")
        self.assertRegex(run["search_code"], r"^S-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["api_calls"], 0)
        self.assertEqual(run["params"]["label"], "Downtown LA")

    def test_checkpoint_preserves_a_recoverable_result_snapshot(self):
        code = self.s.create_search_run(self.params)["search_code"]
        result = {"place_id": "P1", "name": "First shop", "lat": 34.05,
                  "lng": -118.24}
        self.s.checkpoint_search_run(code, {"results": [result], "api_calls": 2,
                                                   "tiles": 1})
        run = self.s.get_search_run(code)
        self.assertEqual(run["result_count"], 1)
        self.assertEqual(run["results"][0]["name"], "First shop")
        self.assertIsNone(run["completed_at"])

    def test_completed_run_round_trips_metrics_and_results(self):
        code = self.s.create_search_run(self.params)["search_code"]
        self.s.checkpoint_search_run(code, {
            "results": [{"place_id": "P1", "name": "Shop"}],
            "api_calls": 9, "cache_hits": 3, "tiles": 4,
            "estimated_cost_usd": .315, "truncated_segments": [],
            "coverage": {"gas": {"found": 10}}}, run_status="complete")
        run = self.s.get_search_run(code)
        self.assertEqual(run["status"], "complete")
        self.assertEqual((run["api_calls"], run["cache_hits"], run["tile_count"]),
                         (9, 3, 4))
        self.assertEqual(run["estimated_cost_usd"], .315)
        self.assertEqual(run["coverage"]["gas"]["found"], 10)
        self.assertIsNotNone(run["completed_at"])

    def test_an_identical_repeat_links_to_the_previous_run(self):
        first = self.s.create_search_run(self.params)["search_code"]
        self.s.checkpoint_search_run(first, {"results": []}, run_status="complete")
        repeat = self.s.create_search_run(dict(self.params))
        self.assertEqual(repeat["repeated_from"], first)

    def test_restart_marks_running_work_interrupted_without_losing_results(self):
        code = self.s.create_search_run(self.params)["search_code"]
        self.s.checkpoint_search_run(code, {
            "results": [{"place_id": "P1", "name": "Recovered"}],
            "api_calls": 1})
        self.assertEqual(self.s.recover_interrupted_search_runs(), 1)
        run = self.s.get_search_run(code)
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(run["results"][0]["name"], "Recovered")
        self.assertEqual(run["error_code"], "process_restart")

    def test_history_can_be_filtered_by_id_or_area(self):
        code = self.s.create_search_run(self.params)["search_code"]
        self.assertEqual(len(self.s.search_runs(query=code)), 1)
        self.assertEqual(len(self.s.search_runs(query="Downtown")), 1)
        self.assertEqual(self.s.search_runs(query="Nowhere"), [])


if __name__ == "__main__":
    unittest.main()
