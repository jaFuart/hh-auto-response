"""No retry after uncertain delivery; safe errors have a durable finite budget."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hh_auto.core.delivery import ApplicationDelivery, DeliveryError, delivery_key


class ApplicationClaims(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "data" / "application-delivery.json"
        self.queue = ApplicationDelivery(self.path)
        self.now = datetime(2026, 11, 14, 7, tzinfo=timezone.utc)

    def test_claim_survives_restart_and_cannot_be_blindly_reclaimed(self):
        self.assertTrue(self.queue.claim("resume", "101", vacancy={"title": "Python"}, now=self.now))
        second = ApplicationDelivery(self.path)
        self.assertFalse(second.claim("resume", "101", now=self.now + timedelta(days=1)))
        self.assertEqual(second.get("resume", "101")["attempts"], 1)
        self.assertEqual(second.due(now=self.now + timedelta(minutes=1))[0]["status"], "in_flight")
        second.reconcile("resume", "101", "unknown", now=self.now)
        self.assertFalse(second.claim("resume", "101", now=self.now + timedelta(days=1)))

    def test_possible_outbound_requires_positive_read_only_probe_before_retry(self):
        self.queue.claim("resume", "101", now=self.now)
        result = self.queue.record_result("resume", "101", confirmed=False, outbound_attempted=True, now=self.now)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(self.queue.claim("resume", "101", now=self.now + timedelta(minutes=1)))
        result = self.queue.reconcile("resume", "101", "not_applied", now=self.now + timedelta(minutes=1))
        self.assertEqual(result["status"], "pending")
        self.assertTrue(self.queue.claim("resume", "101", now=self.now + timedelta(minutes=2)))

    def test_safe_generation_failures_retry_with_backoff_and_stop_after_three(self):
        for attempt in range(1, 4):
            self.assertTrue(self.queue.claim("resume", "101", now=self.now))
            row = self.queue.record_result("resume", "101", confirmed=False, outbound_attempted=False, now=self.now)
            self.assertEqual(row["attempts"], attempt)
            self.assertFalse(self.queue.claim("resume", "101", now=self.now))
            self.now += timedelta(seconds=(30, 120, 300)[attempt - 1])
        self.assertEqual(row["status"], "exhausted")
        self.assertEqual(self.queue.due(now=self.now), [])
        self.assertFalse(ApplicationDelivery(self.path).claim("resume", "101", now=self.now))

    def test_confirmed_delivery_is_terminal_even_after_resume_change(self):
        self.queue.claim("resume", "101", now=self.now)
        self.queue.reconcile("resume", "101", "applied", now=self.now)
        row = self.queue.record_result("resume", "101", confirmed=False, outbound_attempted=False, now=self.now)
        self.assertEqual(row["status"], "confirmed")
        self.assertFalse(self.queue.claim("other-resume", "101", now=self.now))
        self.assertEqual(self.queue.due(now=self.now + timedelta(days=1)), [])

    def test_two_instances_reload_under_lock_before_claiming(self):
        second = ApplicationDelivery(self.path)
        self.assertTrue(self.queue.claim("resume", "101", now=self.now))
        self.assertFalse(second.claim("resume", "101", now=self.now))

    def test_malformed_state_fails_closed_and_unknown_never_means_not_applied(self):
        self.path.parent.mkdir()
        self.path.write_text('{"version":1,"applications":{"bad":{"status":"pending"}}}')
        with self.assertRaises(DeliveryError):
            ApplicationDelivery(self.path)
        self.path.write_text("{truncated")
        with self.assertRaises(DeliveryError):
            ApplicationDelivery(self.path)

    def test_stable_key_and_no_result_without_prior_claim(self):
        self.assertEqual(delivery_key("resume", "101"), delivery_key("resume", "101"))
        with self.assertRaises(DeliveryError):
            self.queue.record_result("resume", "101", confirmed=True, outbound_attempted=True, now=self.now)

    def test_permanent_skip_cancels_without_consuming_more_attempts_or_retry(self):
        self.queue.claim("resume", "101", now=self.now)
        self.queue.record_result("resume", "101", confirmed=False, outbound_attempted=False, now=self.now)
        row = self.queue.cancel("resume", "101", "Не проходит текущий фильтр", now=self.now)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(ApplicationDelivery(self.path).due(now=self.now + timedelta(days=1)), [])
        self.assertFalse(self.queue.claim("resume", "101", now=self.now + timedelta(days=1)))

    def test_cancel_does_not_downgrade_confirmed_application(self):
        self.queue.claim("resume", "101", now=self.now)
        self.queue.record_result("resume", "101", confirmed=True, outbound_attempted=True, now=self.now)
        self.assertEqual(self.queue.cancel("resume", "101", "Filter changed", now=self.now)["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
