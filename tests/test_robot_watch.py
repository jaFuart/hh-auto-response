"""Background robot chat scheduling and an isolated robot-only policy, without a browser or network."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from hh_auto import app
from hh_auto.core.config import Config, ConfigError, validate_config
from hh_auto.hh import watch
from hh_auto.web.browser import FatalError


class RobotWatchScheduleTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(search_url="")
        self.root = Path("unused")
        self.now = datetime(2026, 11, 14, 12, 0)

    def test_monitor_is_disabled_by_default(self):
        self.assertFalse(self.cfg.watch.robot_replies)
        self.assertEqual(self.cfg.watch.robot_every_minutes, 5)
        with patch.object(watch, "last_run") as last:
            self.assertIsNone(watch.next_check(self.root, self.cfg, "robot_replies", self.now))
        last.assert_not_called()

    def test_enabled_monitor_is_due_on_first_check(self):
        self.cfg.watch.robot_replies = True
        with patch.object(watch, "last_run", return_value=None), patch.object(watch.raising, "next_due", return_value=None):
            self.assertEqual(watch.due_tasks(self.root, self.cfg, self.now), ["robot_replies"])
            self.assertEqual(watch.next_any(self.root, self.cfg, self.now), self.now)

    def test_robot_interval_is_independent_from_invitations(self):
        self.cfg.watch.robot_replies = True
        self.cfg.watch.invitations = True
        self.cfg.watch.every_minutes = 60
        previous = self.now - timedelta(minutes=5)
        with patch.object(watch, "last_run", return_value=previous), \
                patch.object(watch.raising, "next_due", return_value=None):
            self.assertEqual(watch.due_tasks(self.root, self.cfg, self.now), ["robot_replies"])
            self.assertEqual(watch.next_check(self.root, self.cfg, "invitations", self.now),
                             previous + timedelta(minutes=60))
            self.assertEqual(watch.next_any(self.root, self.cfg, self.now), self.now)

    def test_robot_task_waits_until_next_interval(self):
        self.cfg.watch.robot_replies = True
        with patch.object(watch, "last_run", return_value=self.now), \
                patch.object(watch.raising, "next_due", return_value=None):
            self.assertEqual(watch.due_tasks(self.root, self.cfg, self.now), [])
            self.assertEqual(watch.next_any(self.root, self.cfg, self.now), self.now + timedelta(minutes=5))

    def test_interval_validation_is_separate(self):
        self.cfg.watch.robot_replies = True
        self.cfg.watch.every_minutes = 1  # does not govern robot monitoring
        self.cfg.watch.robot_every_minutes = 1
        validate_config(self.cfg)
        self.cfg.watch.robot_every_minutes = 0
        with self.assertRaisesRegex(ConfigError, "robot_every_minutes"):
            validate_config(self.cfg)
        self.cfg.watch.robot_every_minutes = 5
        self.cfg.watch.invitations = True
        with self.assertRaisesRegex(ConfigError, "every_minutes"):
            validate_config(self.cfg)

    def test_monitor_settings_require_boolean_and_integer_values(self):
        self.cfg.watch.robot_replies = "false"
        with self.assertRaisesRegex(ConfigError, "без кавычек"):
            validate_config(self.cfg)
        self.cfg.watch.robot_replies = True
        for interval in ("5", True, 1.5):
            with self.subTest(interval=interval):
                self.cfg.watch.robot_every_minutes = interval
                with self.assertRaisesRegex(ConfigError, "robot_every_minutes"):
                    validate_config(self.cfg)


class BackgroundRobotPolicyTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        self.cfg.watch.robot_replies = True
        self.cfg.ai.enabled = False
        self.cfg.ai.only_robot = False
        self.cfg.chats.mode = "rejections"
        self.root = Path("unused")
        self.page = Mock()
        self.marks = self.enter_context(patch.object(watch, "mark"))
        self.book = self.enter_context(patch.object(app, "ResultsBook"))
        self.record = self.enter_context(patch.object(app, "record_stats"))
        self.enter_context(patch.object(app, "log_chat_summary"))
        self.enter_context(patch.object(app, "_prune_logs"))
        self.raise_due = self.enter_context(patch.object(app.raising, "raise_due"))
        self.invitations = self.enter_context(patch.object(app, "check_invitations"))

    def enter_context(self, context):
        value = context.start()
        self.addCleanup(context.stop)
        return value

    def browser(self, cfg, root, work, **kwargs):
        # Mirror the real helper's config mutation and FatalError boundary, without Playwright.
        cfg.browser.profile_dir = "changed-for-session"
        try:
            work(self.page)
        except FatalError as error:
            app.browser.last_error = str(error)
            return app.EXIT_FATAL
        return app.EXIT_OK

    def test_background_enforces_only_robot_without_mutating_settings(self):
        original_profile = self.cfg.browser.profile_dir
        result = {}

        def check(page, cfg, dry_run, stats, root, **kwargs):
            self.assertIs(page, self.page)
            self.assertTrue(cfg.ai.enabled)
            self.assertTrue(cfg.ai.only_robot)
            self.assertEqual(cfg.chats.mode, "all")
            self.assertEqual(cfg.resume_id, "selected")
            self.assertTrue(kwargs["require_selected_resume"])
            self.assertTrue(kwargs["include_read"])
            self.assertFalse(dry_run)
            stats.answered = 1

        with patch.object(app, "_with_browser", side_effect=self.browser) as browser, \
                patch.object(app, "read_rejections", side_effect=check) as read:
            code = app.run_background(self.cfg, self.root, Path("unused.log"), result,
                                      login=False, tasks=["robot_replies"])
        self.assertEqual(code, app.EXIT_OK)
        read.assert_called_once()
        self.assertFalse(browser.call_args.kwargs["auto_raise"])
        self.assertFalse(browser.call_args.kwargs["login"])
        self.assertFalse(self.cfg.ai.enabled)
        self.assertFalse(self.cfg.ai.only_robot)
        self.assertEqual(self.cfg.chats.mode, "rejections")
        self.assertEqual(self.cfg.browser.profile_dir, original_profile)
        self.assertEqual(result["robot_replies"].answered, 1)
        self.marks.assert_called_once_with(self.root, "robot_replies")
        self.raise_due.assert_not_called()
        self.invitations.assert_not_called()
        self.book.return_value.save_xlsx.assert_called_once_with()

    def test_failed_resume_or_ai_attempt_is_marked_and_reported(self):
        result = {}
        with patch.object(app, "_with_browser", side_effect=self.browser), \
                patch.object(app, "read_rejections", side_effect=FatalError("selected resume unavailable")):
            code = app.run_background(self.cfg, self.root, Path("unused.log"), result,
                                      login=False, tasks=["robot_replies"])
        self.assertEqual(code, app.EXIT_FATAL)
        self.assertEqual(result["error"], "selected resume unavailable")
        self.assertEqual(result["robot_replies"].answered, 0)
        self.marks.assert_called_once_with(self.root, "robot_replies")
        self.book.return_value.save_xlsx.assert_not_called()

    def test_disabled_monitor_does_not_open_browser(self):
        self.cfg.watch.robot_replies = False
        with patch.object(app, "_with_browser") as browser:
            self.assertEqual(app.run_background(self.cfg, self.root, Path("unused.log")), app.EXIT_OK)
        browser.assert_not_called()


class ManualChatScanPolicyTest(unittest.TestCase):
    def test_only_robot_ai_mode_also_scans_read_chats(self):
        for enabled, only_robot, mode, expected in ((True, True, "all", True),
                                                   (False, True, "all", False),
                                                   (True, False, "all", False),
                                                   (True, True, "rejections", False)):
            with self.subTest(enabled=enabled, only_robot=only_robot, mode=mode):
                cfg = Config(search_url="")
                cfg.ai.enabled = enabled
                cfg.ai.only_robot = only_robot
                cfg.chats.mode = mode
                page = Mock()

                def browser(cfg, root, work):
                    work(page)
                    return app.EXIT_OK

                with patch.object(app, "_with_browser", side_effect=browser), \
                        patch.object(app, "read_rejections") as read, patch.object(app, "log_chat_summary"), \
                        patch.object(app, "notify_summary"), patch.object(app, "ResultsBook"):
                    code = app.run_chats(cfg, True, Path("unused"), app.ChatStats(), Path("unused.log"))
                self.assertEqual(code, app.EXIT_OK)
                read.assert_called_once()
                self.assertEqual(read.call_args.kwargs["include_read"], expected)


if __name__ == "__main__":
    unittest.main()
