"""A due delivery queue is retried automatically and never becomes new search discovery."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from hh_auto.core import control
from hh_auto.core.config import Config
from hh_auto.core.delivery import ApplicationDelivery
from hh_auto.core.server import ServerRunner, application_retries_due


class ServerApplicationRetryTest(unittest.TestCase):
    def setUp(self):
        control.reset()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(search_url="https://hh.ru/search/vacancy?resume=chosen",
                          resume_url="https://hh.ru/resume/chosen")
        self.cfg.schedule.enabled = True
        self.cfg.schedule.times = ["10:00"]
        self.cfg.schedule.days = [str(n) for n in range(1, 8)]
        self.cfg.watch.robot_replies = False
        self.at = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        self.queue = ApplicationDelivery(self.root / "data/application-delivery.json")

    def pending(self, resume="chosen"):
        self.queue.claim(resume, "vac1", now=self.at)
        self.queue.record_result(resume, "vac1", confirmed=False, outbound_attempted=False, now=self.at)

    def test_due_retry_runs_without_daily_slot_and_is_headless(self):
        self.pending()
        calls = []

        def retry(cfg):
            calls.append(cfg)
            self.queue.cancel("chosen", "vac1", "test completed", now=self.at + timedelta(seconds=31))
            return 0

        def no_new_search(_):
            self.fail("A retry must not create a new daily discovery job")

        runner = ServerRunner(self.root, no_new_search, no_new_search, retry_job=retry,
                              clock=lambda: self.at + timedelta(seconds=31))
        self.assertEqual(runner.tick(self.cfg), [("application_retries", 0)])
        self.assertTrue(calls[0].browser.headless)
        self.assertEqual(runner.tick(self.cfg), [])

    def test_backoff_does_not_become_a_busy_retry_loop(self):
        self.pending()
        self.assertFalse(application_retries_due(self.root, self.cfg, self.at + timedelta(seconds=29)))
        self.assertTrue(application_retries_due(self.root, self.cfg, self.at + timedelta(seconds=31)))

    def test_disabled_schedule_and_changed_resume_do_not_send_retries(self):
        self.pending()
        self.cfg.schedule.enabled = False
        self.assertFalse(application_retries_due(self.root, self.cfg, self.at + timedelta(seconds=31)))
        self.cfg.schedule.enabled = True
        self.cfg.resume_url = "https://hh.ru/resume/other"
        self.assertFalse(application_retries_due(self.root, self.cfg, self.at + timedelta(seconds=31)))
