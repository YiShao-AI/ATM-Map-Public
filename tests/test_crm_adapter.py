import json
import os
import threading
import unittest
from unittest.mock import patch

import crm_adapter as crm


class CrmAuthentication(unittest.TestCase):
    def test_shared_token_requires_an_exact_nonempty_match(self):
        with patch.dict(os.environ, {"CRM_WEBHOOK_TOKEN": "portfolio-secret"}, clear=False):
            self.assertTrue(crm.check_token("portfolio-secret"))
            self.assertFalse(crm.check_token("portfolio-secrex"))
            self.assertFalse(crm.check_token("portfolio-secret-extra"))
            self.assertFalse(crm.check_token(None))

    def test_missing_configuration_disables_inbound_authentication(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CRM_WEBHOOK_TOKEN", None)
            self.assertFalse(crm.is_enabled())
            self.assertFalse(crm.check_token("anything"))


class CrmVocabulary(unittest.TestCase):
    def test_stage_aliases_map_to_the_canonical_funnel(self):
        self.assertEqual(crm.map_stage("Closed Won"), "contract_signed")
        self.assertEqual(crm.map_stage("install-scheduled"), "shipment")
        self.assertIsNone(crm.map_stage("unmapped stage"))

    def test_inbound_payload_preserves_identity_and_explicit_rejection(self):
        match, mapped = crm.parse_inbound({
            "id": "place-123",
            "crm_id": "crm-456",
            "deal_stage": "qualified",
            "deal_status": "do not contact",
            "owner": "West team",
        })
        self.assertEqual(match, "place-123")
        self.assertEqual(mapped["crm_id"], "crm-456")
        self.assertEqual(mapped["stage"], "consultation")
        self.assertEqual(mapped["status"], "do_not_contact")
        self.assertEqual(mapped["owner"], "West team")


class CrmFailureBoundary(unittest.TestCase):
    def test_outbound_failure_is_contained_and_request_is_bounded(self):
        attempted = threading.Event()
        captured = {}

        def fail(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data)
            attempted.set()
            raise OSError("simulated CRM outage")

        env = {
            "CRM_WEBHOOK_URL": "https://crm.example.test/events",
            "CRM_WEBHOOK_TOKEN": "outbound-secret",
            "CRM_TIMEOUT_S": "0.25",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "crm_adapter.urllib.request.urlopen", side_effect=fail
        ):
            crm.notify("site.updated", {"id": "place-123", "stage": "responded"})
            self.assertTrue(attempted.wait(1), "outbound CRM call was not attempted")

        self.assertEqual(captured["url"], env["CRM_WEBHOOK_URL"])
        self.assertEqual(captured["timeout"], 0.25)
        self.assertEqual(captured["payload"]["site_id"], "place-123")
        self.assertEqual(captured["payload"]["stage"], "responded")


if __name__ == "__main__":
    unittest.main()
