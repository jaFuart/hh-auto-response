"""Cooperative daily chunks and global cooldowns use local state and a synthetic clock only."""

from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from hh_auto.core import control
from hh_auto.core.config import Config
from hh_auto.core.delivery import ApplicationDelivery
from hh_auto.core.rate_limits import EXIT_DEFERRED, submission_pacer
from hh_auto.core.server import ResponseRunResult, ServerRunner, ServerState
from scripts import server as cli


class CooperativeServerTiming(unittest.TestCase):
    def setUp(self):
        control.reset()
        self.addCleanup(control.reset)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.clock = [datetime(2026, 11, 23, 7, 0, tzinfo=timezone.utc)]
        self.cfg = Config(search_url="https://hh.ru/search/vacancy?resume=chosen",
                          resume_url="https://hh.ru/resume/chosen")
        self.cfg.schedule.enabled = True
        self.cfg.schedule.times = ["10:00"]
        self.cfg.schedule.days = list("1234567")
        self.cfg.limits.max_responses = 10
        self.cfg.limits.submission_interval_seconds = 180
        self.cfg.limits.submission_batch_size = 2
        self.cfg.limits.submission_batch_pause_minutes = 25
        self.cfg.limits.captcha_cooldown_minutes = 120
        self.cfg.limits.max_responses_per_day = 10
        self.cfg.watch.robot_replies = True
        self.cfg.watch.robot_every_minutes = 15
        self.responses = Mock(return_value=ResponseRunResult(0, 0))
        self.robots = Mock(return_value=0)
        self.retries = Mock(return_value=0)
        self.count = 0

    def runner(self):
        return ServerRunner(self.root, self.responses, self.robots, retry_job=self.retries,
                            clock=lambda: self.clock[0])

    def pacer(self):
        return submission_pacer(self.cfg, self.root, clock=lambda: self.clock[0].timestamp())

    def record_applied(self, count):
        path = self.root / "data/state.json"
        state = json.loads(path.read_text()) if path.exists() else {"applied": {}, "skipped": {}}
        for _ in range(count):
            self.count += 1
            state["applied"][f"v{self.count}"] = {"at": self.runner().now().strftime("%Y-%m-%d %H:%M:%S")}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))

    def seed_waiting(self, runner, *, applied=0, target=10, next_at=None):
        slot = runner.now().replace(hour=10, minute=0, second=0, microsecond=0)
        runner.state.claim_response(slot, runner.now(), target)
        runner.state.finish_response(slot, runner.now(), EXIT_DEFERRED, applied=applied,
                                     next_at=next_at or runner.now(), reason="batch_pause")
        return slot

    def pending_retry(self):
        queue = ApplicationDelivery(self.root / "data/application-delivery.json")
        before = self.clock[0] - timedelta(minutes=1)
        queue.claim("chosen", "retry", now=before)
        queue.record_result("chosen", "retry", confirmed=False, outbound_attempted=False, now=before)
        return queue

    def test_full_chunks_continue_after_catchup_and_restart_without_repeating_progress(self):
        chunks = []

        def apply_chunk(cfg):
            chunks.append(cfg.limits.max_responses)
            for index in range(cfg.limits.max_responses):
                if index:
                    self.clock[0] += timedelta(seconds=180)
                self.pacer().reserve_attempt()
                self.record_applied(1)
            return ResponseRunResult(0, cfg.limits.max_responses)

        self.responses.side_effect = apply_chunk
        for index in range(5):
            runner = self.runner()
            self.assertIn(("responses", 0), runner.tick(self.cfg))
            record = next(iter(ServerState(self.root).responses.values()))
            self.assertEqual(record["applied"], (index + 1) * 2)
            self.assertEqual(record["target"], 10)
            self.assertEqual(record["status"], "done" if index == 4 else "waiting")
            if index < 4:
                self.clock[0] = datetime.fromisoformat(record["next_at"]).astimezone(timezone.utc)
        self.assertEqual(chunks, [2] * 5)
        self.assertGreater(self.runner().now().hour, 10)
        self.assertEqual(self.runner().tick(self.cfg), [])
        self.assertEqual(self.cfg.limits.max_responses, 10)
        self.assertEqual(self.responses.call_count, 5)

    def test_deferred_partial_count_is_saved_and_normal_pause_allows_robots(self):
        def one_then_pause(_):
            self.pacer().reserve_attempt()
            self.record_applied(1)
            return ResponseRunResult(EXIT_DEFERRED, 1)

        self.responses.side_effect = one_then_pause
        runner = self.runner()
        self.assertEqual(runner.tick(self.cfg), [("responses", EXIT_DEFERRED), ("robot_replies", 0)])
        record = next(iter(ServerState(self.root).responses.values()))
        self.assertEqual((record["status"], record["applied"], record["reason"]), ("waiting", 1, "submit_interval"))
        self.clock[0] += timedelta(seconds=179)
        self.assertEqual(self.runner().tick(self.cfg), [])
        self.assertEqual(self.responses.call_count, 1)
        self.clock[0] += timedelta(seconds=1)
        self.runner().tick(self.cfg)
        self.assertEqual(next(iter(ServerState(self.root).responses.values()))["applied"], 2)

    def test_short_success_exhausts_candidates_and_legacy_int_does_not_invent_progress(self):
        self.responses.return_value = ResponseRunResult(0, 1)
        runner = self.runner()
        runner.tick(self.cfg)
        record = next(iter(runner.state.responses.values()))
        self.assertEqual((record["status"], record["applied"], record["reason"]), ("done", 1, "exhausted"))
        self.assertEqual(self.runner().tick(self.cfg), [])
        self.clock[0] += timedelta(days=1)
        self.responses.return_value = 0
        self.runner().tick(self.cfg)
        record = list(ServerState(self.root).responses.values())[-1]
        self.assertEqual((record["status"], record["applied"]), ("done", 0))

    def test_global_captcha_cooldown_prevents_every_job_and_survives_restart(self):
        self.pending_retry()
        self.pacer().record_captcha()
        runner = self.runner()
        self.assertEqual(runner.tick(self.cfg), [])
        record = next(iter(runner.state.responses.values()))
        self.assertEqual((record['status'], record['reason']), ('waiting', 'captcha_cooldown'))
        self.assertEqual(self.runner().tick(self.cfg), [])
        self.responses.assert_not_called()
        self.robots.assert_not_called()
        self.retries.assert_not_called()
        status = runner.status(self.cfg)
        deadline = datetime.fromisoformat(status["submission_pacing"]["captcha_until"])
        self.assertEqual(status["submission_pacing"]["wait_seconds"], 120 * 60)
        self.assertGreaterEqual(datetime.fromisoformat(status["robot_next_at"]), deadline)
        self.assertFalse(status["application_retries_due"])
        self.assertTrue(status["application_retries_queued"])

    def test_captcha_before_first_slot_preserves_work_after_catchup_expires(self):
        self.pacer().record_captcha()
        self.assertEqual(self.runner().tick(self.cfg), [])
        self.clock[0] += timedelta(hours=2)
        self.responses.return_value = ResponseRunResult(0, 0)
        self.assertIn(('responses', 0), self.runner().tick(self.cfg))
        self.responses.assert_called_once()

    def test_captcha_during_responses_prevents_robot_and_retry_in_same_tick(self):
        self.pending_retry()

        def blocked(_):
            self.pacer().reserve_attempt()
            self.pacer().record_captcha()
            return ResponseRunResult(EXIT_DEFERRED, 0)

        self.responses.side_effect = blocked
        runner = self.runner()
        self.assertEqual(runner.tick(self.cfg), [("responses", EXIT_DEFERRED)])
        self.robots.assert_not_called()
        self.retries.assert_not_called()
        record = next(iter(runner.state.responses.values()))
        self.assertEqual((record["status"], record["reason"]), ("waiting", "captcha_cooldown"))

    def test_captcha_during_robot_defers_robot_and_blocks_retry(self):
        self.cfg.schedule.enabled = False

        def blocked(_):
            self.pacer().record_captcha()
            return EXIT_DEFERRED

        self.robots.side_effect = blocked
        runner = self.runner()
        self.assertEqual(runner.tick(self.cfg), [("robot_replies", EXIT_DEFERRED)])
        self.assertEqual(runner.state.data["robot_last"]["status"], "waiting")
        self.assertEqual(datetime.fromisoformat(runner.state.data["robot_next_at"]).timestamp(),
                         self.pacer().peek_status().captcha_until)

    def test_normal_submission_pause_blocks_retry_but_not_robots(self):
        self.pending_retry()
        self.pacer().reserve_attempt()
        runner = self.runner()
        self.assertEqual(runner.tick(self.cfg), [("robot_replies", 0)])
        self.responses.assert_not_called()
        self.retries.assert_not_called()
        record = next(iter(runner.state.responses.values()))
        self.assertEqual((record["status"], record["reason"]), ("waiting", "submit_interval"))

    def test_daily_quota_includes_previous_manual_success_and_stops_slot_without_blocking_robots(self):
        self.cfg.limits.max_responses_per_day = 1
        self.record_applied(1)
        self.pending_retry()
        runner = self.runner()
        self.assertEqual(runner.tick(self.cfg), [("robot_replies", 0)])
        self.responses.assert_not_called()
        self.retries.assert_not_called()
        record = next(iter(runner.state.responses.values()))
        self.assertEqual((record["status"], record["reason"]), ("done", "daily_limit"))
        status = runner.status(self.cfg)
        self.assertEqual((status["daily_responses_used"], status["daily_responses_remaining"]), (1, 0))
        self.assertEqual(status["submission_pacing"]["reason"], "daily_limit")

    def test_today_uncertain_delivery_reserves_daily_quota_and_retry_cannot_bypass_it(self):
        self.cfg.limits.max_responses_per_day = 1
        queue = ApplicationDelivery(self.root / "data/application-delivery.json")
        before = self.clock[0] - timedelta(minutes=1)
        queue.claim("chosen", "uncertain", now=before)
        queue.record_result("chosen", "uncertain", confirmed=False, outbound_attempted=True, now=before)
        runner = self.runner()
        self.assertEqual(runner.tick(self.cfg), [("robot_replies", 0)])
        self.responses.assert_not_called()
        self.retries.assert_not_called()
        self.assertEqual(runner.status(self.cfg)["daily_responses_used"], 1)

    def test_chunk_is_bounded_by_remaining_daily_quota(self):
        self.cfg.limits.max_responses_per_day = 2
        self.record_applied(1)

        def final_one(cfg):
            self.assertEqual(cfg.limits.max_responses, 1)
            self.record_applied(1)
            return ResponseRunResult(0, 1)

        self.responses.side_effect = final_one
        runner = self.runner()
        runner.tick(self.cfg)
        record = next(iter(runner.state.responses.values()))
        self.assertEqual((record["status"], record["applied"], record["reason"]), ("done", 1, "daily_limit"))

    def test_waiting_slot_does_not_resume_next_day_or_after_schedule_changes(self):
        runner = self.runner()
        slot = self.seed_waiting(runner)
        self.cfg.watch.robot_replies = False
        self.cfg.schedule.enabled = False
        self.assertEqual(self.runner().tick(self.cfg), [])
        self.cfg.schedule.enabled = True
        self.cfg.schedule.times = ["12:00"]
        self.assertEqual(self.runner().tick(self.cfg), [])
        self.cfg.schedule.times = ["10:00"]
        self.cfg.schedule.days = ["2"]  # fixture is Monday
        self.assertEqual(self.runner().tick(self.cfg), [])
        self.cfg.schedule.days = list("1234567")
        self.clock[0] += timedelta(days=1, minutes=61)
        self.assertEqual(self.runner().tick(self.cfg), [])
        self.responses.assert_not_called()
        self.assertEqual(ServerState(self.root).responses[slot.isoformat(timespec="minutes")]["status"], "waiting")

    def test_status_is_readonly_even_with_policy_enabled_and_missing_data_dir(self):
        with patch("hh_auto.hh.submission_pacing.SubmissionPacer._locked", side_effect=AssertionError("No status lock")):
            status = self.runner().status(self.cfg)
        self.assertEqual(status["submission_policy"]["submission_interval_seconds"], 180)
        self.assertEqual(status["daily_responses_remaining"], 10)
        self.assertFalse((self.root / "data").exists())
        self.assertNotIn("chosen", json.dumps(status))

    def test_waiting_slot_cannot_resume_if_previous_job_crossed_midnight(self):
        self.cfg.schedule.times = ["10:00", "10:01"]
        self.cfg.watch.robot_replies = False
        runner = self.runner()
        first = self.seed_waiting(runner)
        second = first + timedelta(minutes=1)
        runner.state.claim_response(second, runner.now(), 10)
        runner.state.finish_response(second, runner.now(), EXIT_DEFERRED,
                                     next_at=runner.now(), reason="continue")

        def crossed_day(_):
            self.clock[0] += timedelta(days=1)
            return ResponseRunResult(0, 0)

        self.responses.side_effect = crossed_day
        self.assertEqual(runner.tick(self.cfg), [("responses", 0)])
        self.responses.assert_called_once()
        self.assertEqual(ServerState(self.root).responses[second.isoformat(timespec="minutes")]["status"], "waiting")

    def test_status_reports_waiting_deadline_progress_without_mutating_state(self):
        runner = self.runner()
        deadline = runner.now() + timedelta(minutes=90)
        self.seed_waiting(runner, applied=4, next_at=deadline)
        before = {path: path.read_bytes() for path in (self.root / "data").iterdir() if path.is_file()}
        status = self.runner().status(self.cfg)
        self.assertEqual(datetime.fromisoformat(status["responses_resume_at"]), deadline)
        self.assertEqual(datetime.fromisoformat(status["next_response_at"]), deadline)
        self.assertEqual(next(iter(status["response_runs"].values()))["remaining"], 6)
        self.assertEqual({path: path.read_bytes() for path in (self.root / "data").iterdir() if path.is_file()}, before)

    def test_cli_once_preserves_actual_applied_count_and_treats_75_as_cooperative_success(self):
        def apply(cfg, options, root, stats, log_path):
            self.assertEqual(options.limit, 2)
            self.pacer().reserve_attempt()
            self.record_applied(1)
            stats.applied = 1
            return EXIT_DEFERRED

        def runner(*args, **kwargs):
            return ServerRunner(*args, clock=lambda: self.clock[0], **kwargs)

        environment = dict(os.environ)
        with patch.object(cli, "load_config", return_value=self.cfg), \
                patch.object(cli, "ServerRunner", side_effect=runner), \
                patch.object(cli.app, "run_responses", side_effect=apply), \
                patch.object(cli.app, "run_background", return_value=0), \
                patch.object(cli, "setup_logging", return_value=self.root / "synthetic.log"):
            self.assertEqual(cli.main(["--data-dir", str(self.root), "--once"]), 0)
        record = next(iter(ServerState(self.root).responses.values()))
        self.assertEqual((record["applied"], record["status"]), (1, "waiting"))
        self.assertEqual(dict(os.environ), environment)

    def test_cli_status_fails_closed_on_invalid_pacing_state_without_creating_lock(self):
        path = self.root / "data/submission-pacing.json"
        path.parent.mkdir()
        path.write_text("{broken")
        before = {item.name: item.read_bytes() for item in path.parent.iterdir()}
        errors = io.StringIO()
        with patch.object(cli, "load_config", return_value=self.cfg), \
                patch.object(cli.app, "run_responses", side_effect=AssertionError("No HH job")), \
                patch.object(cli.app, "run_background", side_effect=AssertionError("No HH job")), \
                redirect_stderr(errors):
            self.assertEqual(cli.main(["--data-dir", str(self.root), "--status"]), 2)
        self.assertIn("лимита отправок", errors.getvalue())
        self.assertEqual({item.name: item.read_bytes() for item in path.parent.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
