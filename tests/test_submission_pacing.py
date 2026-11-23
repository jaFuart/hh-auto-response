"""Persistent pre-submit pacing; simulated clocks, no browser, network or real sleeps."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from hh_auto.core.control import StopRequested
from hh_auto.hh import submission_pacing as pacing
from hh_auto.hh.submission_pacing import PacingBlocked, PacingStateError, SubmissionPacer


class Clock:
    def __init__(self):
        self.now = 10_000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class SubmissionPacingTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / pacing.FILE
        self.clock = Clock()

    def pacer(self, **overrides):
        policy = {"submit_interval_seconds": 180, "batch_size": 2,
                  "batch_pause_seconds": 25 * 60, "captcha_cooldown_seconds": 120 * 60}
        policy.update(overrides)
        return SubmissionPacer(self.path, **policy, clock=self.clock)

    def test_fixed_series_applies_before_any_captcha_and_counts_each_attempt(self):
        pacer = self.pacer()
        first = pacer.reserve_attempt()
        self.assertEqual((first.batch_count, first.wait_seconds, first.reason), (1, 180, "submit_interval"))
        self.clock.now += 180
        second = pacer.reserve_attempt()
        self.assertEqual((second.batch_count, second.wait_seconds, second.reason), (2, 1500, "batch_pause"))
        with self.assertRaises(PacingBlocked) as error:
            pacer.reserve_attempt()
        self.assertEqual(error.exception.status.ready_at, self.clock.now + 1500)
        self.clock.now += 1500
        self.assertEqual(pacer.reserve_attempt().batch_count, 1)

    def test_failed_click_or_process_restart_does_not_refund_reservation(self):
        self.pacer().reserve_attempt()
        self.clock.now += 10
        with self.assertRaises(PacingBlocked) as error:
            self.pacer().reserve_attempt()
        self.assertEqual(error.exception.status.wait_seconds, 170)
        self.clock.now += 170
        self.assertEqual(self.pacer().reserve_attempt().reason, "batch_pause")

    def test_first_captcha_is_persisted_even_when_the_caller_immediately_fails(self):
        with self.assertRaisesRegex(RuntimeError, "headless stop"):
            self.pacer().record_captcha()
            raise RuntimeError("headless stop")
        restored = self.pacer().status()
        self.assertEqual((restored.reason, restored.wait_seconds, restored.batch_count),
                         ("captcha_cooldown", 7200, 0))
        with self.assertRaises(PacingBlocked):
            self.pacer().reserve_attempt()

    def test_active_captcha_cooldown_does_not_slide_with_repeated_observations(self):
        pacer = self.pacer()
        initial = pacer.record_captcha()
        original = self.path.read_bytes()
        self.clock.now += 60
        again = self.pacer().record_captcha()
        self.assertEqual(again.ready_at, initial.ready_at)
        self.assertEqual(again.wait_seconds, 7140)
        self.assertEqual(self.path.read_bytes(), original)
        self.clock.now = initial.ready_at + 5
        self.assertEqual(pacer.record_captcha().wait_seconds, 7200)

    def test_completed_captcha_cooldown_starts_a_new_series(self):
        pacer = self.pacer()
        pacer.reserve_attempt()
        pacer.record_captcha()
        self.clock.now += 7200
        self.assertEqual(self.pacer().reserve_attempt().batch_count, 1)

    def test_longer_batch_pause_does_not_hide_the_independent_active_captcha_deadline(self):
        pacer = self.pacer(batch_pause_seconds=3 * 3600)
        pacer.reserve_attempt()
        self.clock.now += 180
        pacer.reserve_attempt()
        status = pacer.record_captcha()
        self.assertEqual(status.reason, "batch_pause")
        self.assertEqual(status.ready_at, self.clock.now + 10800)
        self.assertEqual(status.captcha_until, self.clock.now + 7200)
        self.assertEqual(status.batch_until, self.clock.now + 10800)
        self.assertEqual(status.submit_until, self.clock.now + 180)
        self.assertEqual(pacer.status(), status)

    def test_short_restart_retains_partial_series_but_a_full_rest_starts_over(self):
        pacer = self.pacer()
        pacer.reserve_attempt()
        self.clock.now += 600
        self.assertEqual(self.pacer().reserve_attempt().batch_count, 2)
        self.clock.now += 1500
        self.assertEqual(pacer.reserve_attempt().batch_count, 1)
        self.clock.now += 1501
        self.assertEqual(self.pacer().reserve_attempt().batch_count, 1)

    def test_two_existing_instances_reload_the_same_committed_deadline(self):
        first, second = self.pacer(), self.pacer()
        first.reserve_attempt()
        self.assertEqual(second.status().wait_seconds, 180)
        original = self.path.read_bytes()
        with self.assertRaises(PacingBlocked):
            second.reserve_attempt()
        self.assertEqual(self.path.read_bytes(), original)

    def test_two_threads_cannot_reserve_the_same_available_slot(self):
        pacers = [self.pacer(), self.pacer()]
        barrier = threading.Barrier(2)
        results = []

        def reserve(pacer):
            barrier.wait(timeout=5)
            try:
                pacer.reserve_attempt()
                results.append("reserved")
            except PacingBlocked:
                results.append("blocked")

        threads = [threading.Thread(target=reserve, args=(pacer,)) for pacer in pacers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ["reserved", "blocked"])
        self.assertEqual(self.pacer().status().batch_count, 1)

    def test_missing_state_is_ready_and_status_does_not_create_a_reservation(self):
        status = self.pacer().status()
        self.assertEqual(asdict(status), {"ready_at": self.clock.now, "wait_seconds": 0,
                         "reason": "ready", "batch_count": 0, "last_attempt_at": 0,
                         "captcha_until": 0, "batch_until": 0, "submit_until": 0})
        self.assertFalse(self.path.exists())

    def test_corruption_is_fail_closed_for_reads_reservations_and_captcha(self):
        malformed = ["not-json-synthetic-private", "{}", json.dumps({"version": 2}),
                     json.dumps(pacing._empty_state() | {"last_attempt_at": True}),
                     json.dumps(pacing._empty_state() | {"batch_count": -1}),
                     json.dumps(pacing._empty_state() | {"batch_until": float("nan")}),
                     json.dumps(pacing._empty_state() | {"last_attempt_at": 50, "submit_until": 40})]
        for raw in malformed:
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                for operation in (self.pacer().status, self.pacer().reserve_attempt, self.pacer().record_captcha):
                    with self.assertRaises(PacingStateError) as error:
                        operation()
                    self.assertNotIn("synthetic-private", str(error.exception))
                self.assertEqual(self.path.read_text(encoding="utf-8"), raw)

    def test_atomic_write_failure_preserves_previous_state_and_removes_temporary_file(self):
        pacer = self.pacer()
        pacer.reserve_attempt()
        original = self.path.read_bytes()
        self.clock.now += 180
        with patch.object(pacing.os, "replace", side_effect=PermissionError("synthetic disk failure")):
            with self.assertRaises(PacingStateError):
                pacer.reserve_attempt()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".submission-pacing-*.tmp")), [])
        self.assertEqual(pacer.status().batch_count, 1)

    def test_failed_first_reservation_is_not_reported_as_success(self):
        with patch.object(pacing.os, "replace", side_effect=PermissionError):
            with self.assertRaises(PacingStateError):
                self.pacer().reserve_attempt()
        self.assertFalse(self.path.exists())

    def test_zero_policy_does_not_clear_an_existing_persistent_cooldown(self):
        self.pacer().record_captcha()
        disabled = self.pacer(submit_interval_seconds=0, batch_size=0,
                              batch_pause_seconds=0, captcha_cooldown_seconds=0)
        self.assertEqual(disabled.status().reason, "captcha_cooldown")
        with self.assertRaises(PacingBlocked):
            disabled.reserve_attempt()

    def test_zero_policy_is_ready_without_preexisting_deadlines(self):
        disabled = self.pacer(submit_interval_seconds=0, batch_size=0,
                              batch_pause_seconds=0, captcha_cooldown_seconds=0)
        self.assertEqual(disabled.reserve_attempt().reason, "ready")
        self.assertEqual(disabled.reserve_attempt().reason, "ready")

    def test_wait_uses_interruptible_small_chunks_and_does_not_reserve(self):
        pacer = self.pacer()
        pacer.reserve_attempt()
        original = self.path.read_bytes()
        result = pacer.wait_until_ready(sleep=self.clock.sleep)
        self.assertEqual(result.reason, "ready")
        self.assertEqual(self.clock.sleeps, [30] * 6)
        self.assertEqual(result.batch_count, 1)
        self.assertEqual(self.path.read_bytes(), original)

    def test_wait_interruption_never_consumes_another_attempt(self):
        pacer = self.pacer()
        pacer.reserve_attempt()
        original = self.path.read_bytes()

        def stop(_seconds):
            raise StopRequested()

        with self.assertRaises(StopRequested):
            pacer.wait_until_ready(sleep=stop)
        self.assertEqual(self.path.read_bytes(), original)

    def test_wait_and_reserve_rechecks_when_another_owner_takes_the_ready_slot(self):
        pacer, other = self.pacer(), self.pacer()
        reserve = pacer.reserve_attempt
        raced = False

        def reserve_with_race():
            nonlocal raced
            if not raced:
                raced = True
                other.reserve_attempt()
            return reserve()

        with patch.object(pacer, "reserve_attempt", side_effect=reserve_with_race):
            status = pacer.wait_and_reserve(sleep=self.clock.sleep)
        self.assertEqual((status.batch_count, status.reason), (2, "batch_pause"))
        self.assertEqual(sum(self.clock.sleeps), 180)

    def test_invalid_policy_and_clock_are_rejected(self):
        for key, value in (("submit_interval_seconds", -1), ("batch_size", True),
                           ("batch_size", 1.5), ("batch_pause_seconds", float("inf")),
                           ("captcha_cooldown_seconds", float("nan"))):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.pacer(**{key: value})
        self.clock.now = float("nan")
        with self.assertRaises(PacingStateError):
            self.pacer().status()

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_state_file_has_private_permissions(self):
        self.pacer().reserve_attempt()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
