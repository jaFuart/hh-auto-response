"""Серверное расписание, headless-политика, память и lock. Без браузера, аккаунта и API."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from hh_auto.core import control
from hh_auto.core.config import Config, load_config
from hh_auto.core.server import (ServerError, ServerRunner, ServerState, due_response_slots,
                                  headless_config, runner_lock, server_timezone)
from scripts import server as cli


class ServerSchedule(unittest.TestCase):
    def setUp(self):
        control.reset()
        self.addCleanup(control.reset)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = Config(search_url="https://hh.ru/search/vacancy?resume=selected")
        self.cfg.schedule.enabled = True
        self.cfg.schedule.days = list("1234567")
        self.cfg.schedule.times = ["10:00"]
        self.clock = [datetime(2026, 11, 14, 7, 0, tzinfo=timezone.utc)]  # 10:00 Moscow
        self.responses, self.robots = Mock(return_value=0), Mock(return_value=0)

    def runner(self, **kwargs):
        return ServerRunner(self.root, self.responses, self.robots, clock=lambda: self.clock[0], **kwargs)

    def test_moscow_schedule_runs_from_utc_host_and_forces_only_copied_headless_config(self):
        self.cfg.watch.robot_replies = True
        original_channel = self.cfg.browser.channel
        runner = self.runner()
        self.assertEqual(runner.tick(self.cfg), [("responses", 0), ("robot_replies", 0)])
        passed = self.responses.call_args.args[0]
        self.assertIsNot(passed, self.cfg)
        self.assertTrue(passed.browser.headless)
        self.assertEqual(passed.browser.channel, "")
        self.assertFalse(passed.notify.telegram_captcha)
        self.assertEqual(passed.geo.timezone, "Europe/Moscow")
        self.assertEqual(self.cfg.browser.channel, original_channel)
        self.assertFalse(self.cfg.browser.headless)
        self.assertEqual(runner.now().hour, 10)

    def test_disabled_schedule_never_runs_responses(self):
        self.cfg.schedule.enabled = False
        self.assertEqual(self.runner().tick(self.cfg), [])
        self.responses.assert_not_called()

    def test_startup_before_time_or_outside_catchup_does_not_send_immediately(self):
        for hour, minute in ((6, 59), (8, 1), (13, 0)):
            with self.subTest(utc_hour=hour):
                self.clock[0] = self.clock[0].replace(hour=hour, minute=minute)
                self.assertEqual(self.runner().tick(self.cfg), [])
        self.responses.assert_not_called()

    def test_explicit_catchup_window_and_zero_catchup(self):
        zone = server_timezone()
        now = datetime(2026, 11, 14, 10, 45, tzinfo=zone)
        self.assertEqual(len(due_response_slots(self.cfg, now, {}, 60)), 1)
        self.assertEqual(due_response_slots(self.cfg, now, {}, 0), [])
        self.assertEqual(due_response_slots(self.cfg, now.replace(hour=11, minute=1), {}, 60), [])

    def test_calendar_weekdays_are_honored(self):
        self.cfg.schedule.days = ["1"]  # fixture is Saturday
        self.assertEqual(self.runner().tick(self.cfg), [])

    def test_restart_cannot_repeat_completed_daily_slot(self):
        self.runner().tick(self.cfg)
        self.assertEqual(self.runner().tick(self.cfg), [])
        self.responses.assert_called_once()

    def test_crash_after_claim_cannot_repeat_ambiguous_daily_slot(self):
        runner = self.runner()
        slot = runner.now()
        runner.state.claim_response(slot, slot)
        self.assertEqual(self.runner().tick(self.cfg), [])
        self.responses.assert_not_called()
        self.assertEqual(next(iter(ServerState(self.root).responses.values()))["status"], "running")

    def test_robot_job_is_serial_and_daily_slot_catches_up_after_busy_scan(self):
        self.cfg.watch.robot_replies = True
        self.clock[0] = self.clock[0].replace(hour=6, minute=59)
        order = []

        def robots(cfg):
            order.append("robots")
            self.clock[0] += timedelta(minutes=8)
            return 0

        self.robots.side_effect = robots
        self.responses.side_effect = lambda cfg: order.append("responses") or 0
        runner = self.runner()
        self.assertEqual(runner.tick(self.cfg), [("robot_replies", 0)])
        self.assertEqual(runner.tick(self.cfg), [("responses", 0)])
        self.assertEqual(order, ["robots", "responses"])
        self.clock[0] += timedelta(minutes=5)
        self.assertEqual(runner.tick(self.cfg), [("robot_replies", 0)])

    def test_failed_daily_job_is_recorded_and_not_retried_on_restart(self):
        self.responses.return_value = 1
        self.assertEqual(self.runner().tick(self.cfg), [("responses", 1)])
        self.assertEqual(self.runner().tick(self.cfg), [])
        record = next(iter(ServerState(self.root).responses.values()))
        self.assertEqual((record["status"], record["code"]), ("failed", 1))

    def test_stop_during_response_cannot_start_next_robot_job(self):
        self.cfg.watch.robot_replies = True

        def stopped(cfg):
            control.request_stop()
            return 130

        self.responses.side_effect = stopped
        with self.assertRaises(control.StopRequested):
            self.runner().tick(self.cfg)
        self.robots.assert_not_called()
        self.assertEqual(next(iter(ServerState(self.root).responses.values()))["code"], 130)

    def test_status_does_not_create_data_files_or_expose_credentials(self):
        self.cfg.ai.api_key = "private-test-credential"
        status = self.runner().status(self.cfg)
        self.assertIn("responses_due", status)
        self.assertEqual(status["robot_every_minutes"], self.cfg.watch.robot_every_minutes)
        self.assertNotIn("private-test-credential", json.dumps(status))
        self.assertFalse((self.root / "data").exists())

    def test_second_runner_cannot_acquire_same_data_lock_until_first_exits(self):
        with runner_lock(self.root):
            with self.assertRaises(ServerError):
                with runner_lock(self.root):
                    self.fail("second runner acquired the lock")
        with runner_lock(self.root):
            pass


class ServerConfigAndCli(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config = self.root / "config.toml"
        self.config.write_text('search_url = ""\n', encoding="utf-8")

    def test_readonly_load_keeps_config_and_no_backup_is_written(self):
        before = self.config.read_bytes()
        load_config(self.config, check_search_url=False, upgrade=False)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.config.with_suffix(".toml.bak").exists())

    def test_status_cli_is_readonly_and_never_invokes_jobs(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(cli.main(["--data-dir", str(self.root), "--status"]), 0)
        self.assertFalse(json.loads(stream.getvalue())["schedule_enabled"])
        self.assertEqual(self.config.read_text(encoding="utf-8"), 'search_url = ""\n')
        self.assertFalse((self.root / "data").exists())
        self.assertFalse((self.root / "logs").exists())

    def test_custom_data_dir_controls_session_home_despite_conflicting_environment(self):
        cfg = Config(search_url="")
        cfg.watch.robot_replies = True
        conflicting = str(self.root / "other-account")

        def robots(passed, root, log_path, **kwargs):
            self.assertEqual(root, self.root.resolve())
            self.assertEqual(os.environ["HH_AUTO_HOME"], str(self.root.resolve()))
            return 0

        with patch.dict(os.environ, {"HH_AUTO_HOME": conflicting}), patch.object(cli, "load_config", return_value=cfg), \
                patch.object(cli.app, "run_background", side_effect=robots) as job, \
                patch.object(cli, "setup_logging", return_value=self.root / "synthetic.log"):
            self.assertEqual(cli.main(["--data-dir", str(self.root), "--once"]), 0)
            self.assertEqual(os.environ["HH_AUTO_HOME"], conflicting)
        job.assert_called_once()

    def test_unknown_timezone_is_a_clear_error(self):
        with self.assertRaisesRegex(ServerError, "часовой пояс"):
            server_timezone("No/Such_Zone")

    def test_headless_policy_does_not_change_schedule_or_enable_actions(self):
        cfg = Config(search_url="")
        prepared = headless_config(cfg)
        self.assertFalse(prepared.schedule.enabled)
        self.assertFalse(prepared.watch.robot_replies)


if __name__ == "__main__":
    unittest.main()
