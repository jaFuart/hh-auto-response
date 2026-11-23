"""Pacing at application boundaries: real private state, fake browser, no network."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from hh_auto import app
from hh_auto.core import rate_limits
from hh_auto.core.config import Config
from hh_auto.core.delivery import ApplicationDelivery
from hh_auto.core.server import ServerRunner
from hh_auto.core.state import State
from hh_auto.hh import raising, responder, runner
from hh_auto.hh.responder import Result, Status
from hh_auto.hh.search import Vacancy
from hh_auto.hh.submission_pacing import PacingStateError, SubmissionPacer
from hh_auto.web import auth, browser, captcha


MOSCOW = ZoneInfo("Europe/Moscow")
TODAY = datetime(2026, 11, 23, 0, 30, tzinfo=MOSCOW)


class PrivateStateCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cfg = Config(search_url="https://hh.ru/search/vacancy", resume_url="https://hh.ru/resume/selected")
        self.cfg.limits.submission_interval_seconds = 180
        self.cfg.limits.submission_batch_size = 2
        self.cfg.limits.submission_batch_pause_minutes = 25
        self.cfg.limits.captcha_cooldown_minutes = 120
        self.cfg.limits.max_responses_per_day = 10
        self.pacer = rate_limits.submission_pacer(self.cfg, self.root, clock=lambda: 1000)
        self.queue = ApplicationDelivery(self.root / "data/application-delivery.json")

    def write_history(self, records):
        path = self.root / "data/state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"applied": records, "skipped": {}}), encoding="utf-8")

    def claim(self, vacancy_id, *, at=TODAY, cancelled=False):
        self.assertTrue(self.queue.claim("selected", vacancy_id, now=at))
        if cancelled:
            self.queue.cancel("selected", vacancy_id, now=at)

    def snapshot_files(self):
        return {path.relative_to(self.root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}


class DailyQuotaTests(PrivateStateCase):
    def test_union_of_confirmed_and_uncertain_ids_including_cancelled_delivery(self):
        self.write_history({"101": {"at": "2026-11-23 00:01:00"},
                            "102": {"at": "2026-11-23 00:02:00"},
                            "old": {"at": "2026-11-22 23:00:00"}})
        self.claim("101")  # Same vacancy in both stores counts once.
        self.claim("103", cancelled=True)  # Cancellation cannot erase an ambiguous actual outbound.
        self.claim("104")
        self.queue.record_result("selected", "104", confirmed=False, outbound_attempted=False, now=TODAY)
        self.assertEqual(rate_limits.confirmed_today(self.root, TODAY), 3)
        self.assertTrue(self.queue.get("selected", "103")["delivery_uncertain"])

    def test_utc_claims_are_counted_by_moscow_day_not_utc_day(self):
        self.claim("101", at=datetime(2026, 11, 22, 21, 1, tzinfo=timezone.utc))
        self.claim("102", at=datetime(2026, 11, 22, 20, 59, tzinfo=timezone.utc))
        self.assertEqual(rate_limits.confirmed_today(self.root, TODAY), 1)

    def test_only_current_unsubmitted_claim_is_excluded_not_confirmed_history(self):
        self.write_history({"101": {"at": "2026-11-23 00:01:00"}})
        self.claim("101")
        self.claim("102")
        self.claim("103", cancelled=True)
        self.assertEqual(rate_limits.confirmed_today(self.root, TODAY, exclude_uncertain_id="102"), 2)
        self.assertEqual(rate_limits.confirmed_today(self.root, TODAY, exclude_uncertain_id="101"), 3)

    def test_quota_defers_until_next_moscow_midnight(self):
        self.cfg.geo.timezone = "Europe/Moscow"
        self.cfg.limits.max_responses_per_day = 1
        self.claim("101")

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return TODAY.astimezone(tz) if tz else TODAY.replace(tzinfo=None)

        with patch.object(rate_limits, "datetime", FixedDateTime):
            with self.assertRaises(rate_limits.WorkDeferred) as caught:
                rate_limits.check_daily_limit(self.cfg, self.root)
        self.assertEqual(caught.exception.status.reason, "daily_limit")
        self.assertEqual(datetime.fromtimestamp(caught.exception.status.ready_at, MOSCOW),
                         datetime(2026, 11, 24, tzinfo=MOSCOW))

    def test_malformed_history_and_queue_fail_closed(self):
        path = self.root / "data/state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        for raw in ("{", "[]", '{"applied":[]}', '{"applied":{"101":null}}'):
            with self.subTest(raw=raw):
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(PacingStateError):
                    rate_limits.confirmed_today(self.root, TODAY)
        path.unlink()
        self.queue.path.write_text("{", encoding="utf-8")
        with self.assertRaises(PacingStateError):
            rate_limits.confirmed_today(self.root, TODAY)

    def test_invalid_or_missing_applied_timestamp_cannot_reduce_quota(self):
        for record in ({}, {"at": None}, {"at": "not-a-date"}, {"at": "2026-11-23 impossible-time"}):
            with self.subTest(record=record):
                self.write_history({"101": record})
                with self.assertRaises(PacingStateError):
                    rate_limits.confirmed_today(self.root, TODAY)

    def test_uncertain_claim_without_timezone_fails_closed(self):
        self.claim("101")
        raw = json.loads(self.queue.path.read_text(encoding="utf-8"))
        next(iter(raw["applications"].values()))["claimed_at"] = "2026-11-23T00:01:00"
        self.queue.path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(PacingStateError):
            rate_limits.confirmed_today(self.root, TODAY)

    def test_status_and_quota_reads_create_no_private_files_or_locks(self):
        self.assertEqual(rate_limits.confirmed_today(self.root, TODAY), 0)
        self.assertEqual(self.pacer.peek_status().reason, "ready")
        self.assertEqual(self.snapshot_files(), {})
        server = ServerRunner(self.root, Mock(), Mock(), clock=lambda: TODAY)
        with patch.object(rate_limits, "app_root", return_value=self.root):
            server.status(self.cfg)
        self.assertEqual(self.snapshot_files(), {})

    def test_status_peek_preserves_existing_state_bytes_and_mtime(self):
        self.pacer.reserve_attempt()
        before = self.snapshot_files()
        self.assertEqual(self.pacer.peek_status().reason, "submit_interval")
        self.assertEqual(self.snapshot_files(), before)


class CaptchaIntegrationTests(PrivateStateCase):
    def test_first_headless_captcha_persists_cooldown_before_deferral_without_waiting(self):
        self.cfg.browser.headless = True
        with patch.object(browser, "_cfg", self.cfg), \
                patch.object(browser, "captcha_shown", return_value=True), \
                patch.object(rate_limits, "submission_pacer", return_value=self.pacer), \
                patch.object(browser.control, "sleep") as sleep, patch.object(captcha, "relay_for") as relay:
            with self.assertRaises(rate_limits.WorkDeferred) as caught:
                browser.wait_captcha(Mock())
        self.assertEqual(caught.exception.status.reason, "captcha_cooldown")
        fresh = SubmissionPacer(self.pacer.path, submit_interval_seconds=180, batch_size=2,
                                batch_pause_seconds=1500, captcha_cooldown_seconds=7200, clock=lambda: 1001)
        self.assertEqual(fresh.peek_status().captcha_until, 8200)
        sleep.assert_not_called()
        relay.assert_not_called()

    def test_monitor_is_allowed_during_batch_pause_but_blocked_during_captcha_cooldown(self):
        clock = [1000]
        pacer = rate_limits.submission_pacer(self.cfg, self.root, clock=lambda: clock[0])
        pacer.reserve_attempt()
        clock[0] += 180
        pacer.reserve_attempt()
        self.assertEqual(pacer.peek_status().reason, "batch_pause")
        with patch.object(rate_limits, "submission_pacer", return_value=pacer), \
                patch.object(rate_limits.time, "time", side_effect=lambda: clock[0]):
            rate_limits.check_captcha_cooldown(self.cfg, self.root)
            pacer.record_captcha()
            with self.assertRaises(rate_limits.WorkDeferred) as caught:
                rate_limits.check_captcha_cooldown(self.cfg, self.root)
        self.assertEqual(caught.exception.status.reason, "captcha_cooldown")


class ReadSpacingIntegrationTests(PrivateStateCase):
    def test_region_and_login_gets_obey_the_same_read_interval(self):
        self.cfg.geo.check_ip = False
        self.cfg.limits.read_interval_seconds = 10
        page = Mock(url="https://hh.ru/")
        page.context.cookies.return_value = [{"name": "hhrole", "value": "applicant"}]
        page.locator.return_value.count.return_value = 0
        now, reads = [1000.0], []

        def read(url, **kwargs):
            page.url = url
            reads.append((url, now[0]))
            return Mock(status=200)

        page.goto.side_effect = read
        with patch.object(browser, "_cfg", self.cfg), patch.object(browser, "_last_read_at", 0), \
                patch.object(browser.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(browser.control, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)), \
                patch.object(browser, "wait_captcha"), patch.object(auth, "wait_captcha"), \
                patch.object(auth, "save_session"), patch.object(rate_limits, "submission_pacer", return_value=self.pacer):
            browser.check_region(page, self.cfg)
            self.assertTrue(auth.is_logged_in(page))
        self.assertEqual([row[0] for row in reads], [browser.HH_HOME, auth.RESUMES_URL])
        self.assertGreaterEqual(reads[1][1] - reads[0][1], 10)

    def test_raising_list_read_waits_before_get_and_checks_captcha_before_parsing(self):
        page, events = Mock(), []
        page.goto.side_effect = lambda *args, **kwargs: events.append("get")
        page.evaluate.side_effect = lambda *args: events.append("parse") or []
        with patch.object(raising, "on_resumes_page", return_value=False), \
                patch.object(raising, "wait_read_interval", side_effect=lambda: events.append("gate")), \
                patch.object(raising, "wait_captcha", side_effect=lambda *args: events.append("captcha")):
            self.assertEqual(raising.list_resumes(page), [])
        self.assertEqual(events, ["gate", "get", "captcha", "parse"])

    def test_each_resume_raise_checks_the_shared_gate_before_its_post(self):
        self.cfg.raising.enabled = self.cfg.raising.all_resumes = True
        page, events = Mock(), []
        page.context.cookies.return_value = []
        page.evaluate.side_effect = lambda script, args: events.append("touch:" + args["id"]) or 200
        with patch.object(raising, "list_resumes", return_value=[raising.Resume("a"), raising.Resume("b")]), \
                patch.object(raising, "wait_read_interval", side_effect=lambda cfg: events.append("gate")):
            result = raising.raise_due(page, self.cfg, self.root, force=True)
        self.assertEqual(events, ["gate", "touch:a", "gate", "touch:b"])
        self.assertEqual(len(result.raised), 2)

    def test_optional_auto_raise_cannot_swallow_cooldown_or_broken_pacing_state(self):
        cooldown = rate_limits.WorkDeferred(self.pacer.record_captcha())
        for error in (cooldown, PacingStateError("broken state")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(app.raising, "raise_due", side_effect=error):
                with self.assertRaises(type(error)):
                    app._raise_if_due(Mock(), self.cfg, self.root)


class ResponderReservationTests(PrivateStateCase):
    def setUp(self):
        super().setUp()
        self.page = Mock(url=self.cfg.search_url)
        self.vacancy = Vacancy("101", "Python developer", "Company", "Откликнуться")
        self.engine = responder.Responder(self.page, self.cfg, self.root / "snapshots", None)
        self.card = Mock()
        self.card.count.return_value = 1
        self.button = self.card.locator.return_value.first
        self.button.count.return_value = 1
        self.button.inner_text.return_value = "Откликнуться"
        self.patches = [patch.object(responder, "submission_pacer", return_value=self.pacer),
                        patch.object(responder, "check_daily_limit"),
                        patch.object(responder, "captcha_shown", return_value=False),
                        patch.object(self.engine, "_close_dialogs"), patch.object(self.engine, "_pause"),
                        patch.object(self.engine, "_card", return_value=self.card),
                        patch.object(self.engine, "_wait_click_outcome", return_value=responder._Outcome.QUICK),
                        patch.object(self.engine, "_handle_quick", return_value=Result(Status.APPLIED))]
        (_, self.quota, _, _, _, _, self.outcome, _) = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)

    def test_card_and_form_share_one_reserved_attempt(self):
        self.engine._direct_vacancy_id = "101"
        self.engine._reserve_submission()
        self.engine._reserve_submission()
        status = self.pacer.peek_status()
        self.assertEqual(status.batch_count, 1)
        self.assertEqual(status.last_attempt_at, 1000)
        self.quota.assert_called_once_with(self.cfg, exclude_uncertain_id="101")

    def test_reservation_is_durable_before_native_card_click(self):
        seen = []

        def click():
            record = json.loads(self.pacer.path.read_text(encoding="utf-8"))
            seen.append(record["last_attempt_at"])
            self.assertEqual(record["batch_count"], 1)

        self.button.click.side_effect = click
        self.engine._apply(self.vacancy)
        self.assertEqual(seen, [1000])

    def test_denied_reservation_never_clicks_card_or_marks_outbound(self):
        self.pacer.reserve_attempt()
        with self.assertRaises(rate_limits.WorkDeferred):
            self.engine._apply(self.vacancy)
        self.button.click.assert_not_called()
        self.outcome.assert_not_called()
        self.assertFalse(self.engine.clicked)

    def test_denied_reservation_never_clicks_final_submit(self):
        self.pacer.reserve_attempt()
        button = Mock()
        with patch.object(self.engine, "_wait_enabled", return_value=True):
            with self.assertRaises(rate_limits.WorkDeferred):
                self.engine._submit(Mock(), button, "Откликнуться")
        button.click.assert_not_called()
        self.assertFalse(self.engine.clicked)

    def test_corrupt_pacing_state_propagates_through_public_apply(self):
        self.pacer.path.parent.mkdir(parents=True, exist_ok=True)
        self.pacer.path.write_text("{", encoding="utf-8")
        self.cfg.letter.mode = "none"
        with self.assertRaises(PacingStateError):
            self.engine.apply(self.vacancy)
        self.button.click.assert_not_called()

    def test_daily_only_quota_does_not_count_its_unsubmitted_claim_against_itself(self):
        self.cfg.limits.submission_interval_seconds = 0
        self.cfg.limits.submission_batch_size = 0
        self.cfg.limits.submission_batch_pause_minutes = 0
        self.cfg.limits.captcha_cooldown_minutes = 0
        self.cfg.limits.max_responses_per_day = 1
        self.cfg.limits.max_pages = 1
        self.cfg.letter.mode = "none"
        self.assertFalse(rate_limits.enabled(self.cfg))
        self.quota.side_effect = lambda cfg, **kwargs: rate_limits.check_daily_limit(cfg, self.root, **kwargs)
        state = State(self.root / "data/state.json")
        results, stats = Mock(), runner.Stats()
        results.recent_letters.return_value = []
        with patch.object(runner, "Responder", return_value=self.engine), patch.object(runner, "Pacer"), \
                patch.object(runner, "open_results_page", return_value=[self.vacancy]), \
                patch.object(runner, "next_page_info", return_value=(False, "end")), \
                patch.object(runner, "collect_vacancies", return_value=[]), \
                patch.object(runner, "wait_all_cards"), patch.object(runner, "_sleep"), \
                patch.object(self.engine, "on_search_page", return_value=False), \
                patch.object(self.engine, "back_to_results"):
            runner.run(self.page, self.cfg, runner.RunOptions(), state, results, stats, None,
                       self.root / "logs/snapshots")
        self.assertEqual(stats.applied, 1)
        self.button.click.assert_called_once()
        self.assertEqual(self.queue.get("selected", "101")["status"], "confirmed")
        self.assertFalse(self.pacer.path.exists())


class DryRunPacingTests(PrivateStateCase):
    def test_dry_run_neither_claims_nor_reserves_attempt_even_when_quota_is_full(self):
        self.cfg.limits.max_pages = 1
        self.cfg.limits.max_responses_per_day = 1
        self.write_history({"old-confirmed": {"at": "2026-11-23 00:01:00"}})
        state = State(self.root / "data/state.json")
        vacancy = Vacancy("101", "Python developer", "Company", "Откликнуться", True)
        stats = runner.Stats()
        engine, results = Mock(), Mock()
        results.recent_letters.return_value = []
        engine.on_search_page.return_value = False
        with patch.object(runner, "Responder", return_value=engine), patch.object(runner, "Pacer"), \
                patch.object(runner, "ApplicationDelivery") as delivery, \
                patch.object(runner, "submission_pacer") as pacer, \
                patch.object(runner, "check_daily_limit") as quota, \
                patch.object(runner, "open_results_page", return_value=[vacancy]), \
                patch.object(runner, "next_page_info", return_value=(False, "end")), \
                patch.object(runner, "collect_vacancies", return_value=[]), \
                patch.object(runner, "wait_all_cards"), patch.object(runner, "_sleep"):
            runner.run(Mock(url=self.cfg.search_url), self.cfg, runner.RunOptions(dry_run=True), state,
                       results, stats, None, self.root / "logs/snapshots")
        self.assertEqual(stats.dry_run, 1)
        engine.apply.assert_not_called()
        engine.apply_on_vacancy.assert_not_called()
        delivery.assert_not_called()
        pacer.assert_not_called()
        quota.assert_not_called()
        self.assertFalse(self.pacer.path.exists())


if __name__ == "__main__":
    unittest.main()
