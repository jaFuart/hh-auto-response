"""Queue integration uses existing filters and never blindly repeats a possible send."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hh_auto.core.config import Config
from hh_auto.core.delivery import ApplicationDelivery
from hh_auto.core.state import State
from hh_auto.hh import runner
from hh_auto.hh.gph import GphCheck
from hh_auto.hh.responder import MOVED, Result, Status
from hh_auto.hh.search import Vacancy
from hh_auto.hh.vacancy_meta import VacancyMeta


class RunnerDelivery(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = Config(search_url="https://hh.ru/search/vacancy", resume_url="https://hh.ru/resume/selected")
        self.cfg.limits.max_pages = 1
        self.cfg.limits.max_consecutive_errors = 10
        self.state = State(self.root / "data" / "state.json")
        self.queue = ApplicationDelivery(self.root / "data" / "application-delivery.json", backoff_seconds=(0,))
        self.vac = Vacancy("101", "Python developer", "Company", "Откликнуться", True, "Python APIs",
                           VacancyMeta("101", match=1))
        self.page = Mock(url=self.cfg.search_url)
        self.results = Mock()
        self.results.recent_letters.return_value = ["Earlier actual letter"]
        self.responder = Mock(clicked=True, letter_kind="ai")
        self.responder.on_search_page.return_value = False
        self.responder.application_status.return_value = "not_applied"
        self.responder.apply.return_value = Result(Status.APPLIED, letter=True, letter_text="Actual text",
                                                  delivery={"outbound_attempted": True})
        self.responder.apply_on_vacancy.return_value = Result(Status.APPLIED, letter=True, letter_text="Retried text",
                                                             delivery={"outbound_attempted": True})
        self.patches = [patch.object(runner, "Responder", return_value=self.responder),
                        patch.object(runner, "ApplicationDelivery", return_value=self.queue),
                        patch.object(runner, "Pacer"), patch.object(runner, "_sleep"),
                        patch.object(runner, "open_results_page", return_value=[self.vac]),
                        patch.object(runner, "next_page_info", return_value=(False, "end")),
                        patch.object(runner, "collect_vacancies", return_value=[]),
                        patch.object(runner, "wait_all_cards")]
        self.constructor, self.delivery_cls, self.pacer, self.sleep, self.open, self.next, self.collect, self.wait = [p.start() for p in self.patches]
        self.pacer.return_value.rest_if_due.return_value = False
        for p in self.patches:
            self.addCleanup(p.stop)

    def run_bot(self, *, retry=False, dry=False, limit=None):
        stats = runner.Stats()
        runner.run(self.page, self.cfg, runner.RunOptions(retry_only=retry, dry_run=dry, limit=limit),
                   self.state, self.results, stats, None, self.root / "logs" / "snapshots")
        return stats

    def seed(self, *, unknown=False, vacancy=None):
        vac = vacancy or self.vac
        self.queue.claim("selected", vac.id, vacancy=runner._saved_vacancy(vac))
        self.queue.record_result("selected", vac.id, confirmed=False, outbound_attempted=unknown)

    def test_low_fit_is_advisory_and_claim_exists_before_first_apply(self):
        def applied(vac):
            self.assertEqual(self.queue.get("selected", vac.id)["status"], "in_flight")
            return Result(Status.APPLIED, letter=True, letter_text="Actual text", delivery={"outbound_attempted": True})
        self.responder.apply.side_effect = applied
        stats = self.run_bot()
        self.assertEqual(stats.applied, 1)
        self.assertEqual(self.queue.get("selected", "101")["status"], "confirmed")
        self.assertEqual(self.constructor.call_args.kwargs["recent_letters"], ["Earlier actual letter"])
        saved = self.queue.get("selected", "101")["vacancy"]
        self.assertEqual(set(saved), {"id", "title", "company", "button_text", "remote", "snippet"})
        self.assertEqual(self.results.add.call_args.args[1].delivery["retry_count"], 0)

    def test_previous_ten_applications_never_claim_or_apply_again(self):
        previous = [Vacancy(str(i), "Python", "Company " + str(i), "Откликнуться") for i in range(10)]
        for vacancy in previous:
            self.state.mark_applied(vacancy, True)
        self.open.return_value = previous
        with patch.object(self.queue, "claim", wraps=self.queue.claim) as claim:
            stats = self.run_bot()
        self.assertEqual(stats.already, 10)
        claim.assert_not_called()
        self.responder.apply.assert_not_called()

    def test_ambiguous_delivery_is_probed_and_cannot_click_even_after_not_applied(self):
        self.seed(unknown=True)
        self.responder.application_status.return_value = "not_applied"
        stats = self.run_bot(retry=True)
        self.assertEqual(stats.applied, 0)
        self.assertEqual(self.queue.get("selected", "101")["status"], "pending")
        self.responder.apply_on_vacancy.assert_not_called()
        self.responder.apply.assert_not_called()
        self.open.assert_not_called()
        self.collect.assert_not_called()

    def test_retry_reapplies_title_and_contract_filters_and_cancels_old_candidate(self):
        self.seed()
        self.cfg.filters.exclude_title_words = ["Python"]
        stats = self.run_bot(retry=True)
        self.assertEqual(sum(stats.skipped.values()), 1)
        self.assertEqual(self.queue.get("selected", "101")["status"], "cancelled")
        self.responder.apply_on_vacancy.assert_not_called()
        self.responder.application_status.assert_not_called()

    def test_retry_rechecks_current_gph_policy_before_claim(self):
        self.seed()
        self.cfg.filters.require_gph = True
        with patch.object(runner, "check_gph", return_value=GphCheck(False, "Только ТК")) as check:
            self.run_bot(retry=True)
        check.assert_called_once()
        self.responder.apply_on_vacancy.assert_not_called()
        self.assertEqual(self.queue.get("selected", "101")["status"], "cancelled")

    def test_retry_rechecks_full_description_exclusions_before_claim(self):
        self.seed()
        self.cfg.filters.exclude_description_words = ["Битрикс24", "amoCRM"]
        with patch.object(runner, "fetch_vacancy_text", return_value="Python APIs and Bitrix 24 integration."):
            stats = self.run_bot(retry=True)
        self.assertEqual(sum(stats.skipped.values()), 1)
        self.assertEqual(self.queue.get("selected", "101")["status"], "cancelled")
        self.responder.apply_on_vacancy.assert_not_called()
        self.responder.application_status.assert_not_called()

    def test_unavailable_description_keeps_saved_retry_pending(self):
        self.seed()
        self.cfg.filters.exclude_description_words = ["amoCRM"]
        with patch.object(runner, "fetch_vacancy_text", return_value=""):
            stats = self.run_bot(retry=True)
        self.assertEqual(sum(stats.skipped.values()), 1)
        row = self.queue.get("selected", "101")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.responder.apply_on_vacancy.assert_not_called()
        self.responder.application_status.assert_not_called()

    def test_safe_pending_retry_only_uses_targeted_apply_and_current_resume(self):
        self.seed()
        other = Vacancy("102", "Other", "Other company", "Откликнуться")
        self.queue.claim("another-resume", other.id, vacancy=runner._saved_vacancy(other))
        self.queue.record_result("another-resume", other.id, confirmed=False, outbound_attempted=False)
        stats = self.run_bot(retry=True)
        self.assertEqual(stats.applied, 1)
        self.responder.apply_on_vacancy.assert_called_once()
        self.assertEqual(self.responder.apply_on_vacancy.call_args.args[0].id, "101")
        self.responder.application_status.assert_called_once()
        self.open.assert_not_called()
        self.collect.assert_not_called()
        self.assertEqual(self.queue.get("another-resume", "102")["attempts"], 1)

    def test_status_applied_reconciles_history_without_counting_a_new_send(self):
        self.seed(unknown=True)
        self.responder.application_status.return_value = "applied"
        stats = self.run_bot(retry=True)
        self.assertEqual(stats.applied, 0)
        self.assertEqual(stats.already, 1)
        self.assertFalse(self.state.applied["101"]["letter"])
        self.assertEqual(self.queue.get("selected", "101")["status"], "confirmed")
        self.responder.apply_on_vacancy.assert_not_called()
        self.pacer.return_value.sent.assert_not_called()
        self.assertTrue(self.results.add.call_args.args[1].delivery["reconciled"])

    def test_no_more_than_three_real_attempts_and_no_quality_skip(self):
        self.responder.apply.return_value = Result(Status.FAILED, "API failure", delivery={"outbound_attempted": False})
        self.responder.apply_on_vacancy.return_value = Result(Status.FAILED, "API failure", delivery={"outbound_attempted": False})
        self.run_bot()
        for _ in range(4):
            self.run_bot(retry=True)
        self.assertEqual(self.responder.apply.call_count, 1)
        self.assertEqual(self.responder.apply_on_vacancy.call_count, 2)
        self.assertEqual(self.queue.get("selected", "101")["attempts"], 3)
        self.assertEqual(self.queue.get("selected", "101")["status"], "exhausted")

    def test_unknown_probes_are_bounded_and_never_send(self):
        self.seed(unknown=True)
        self.responder.application_status.return_value = "unknown"
        for _ in range(5):
            self.run_bot(retry=True)
        self.assertEqual(self.responder.application_status.call_count, 3)
        row = self.queue.get("selected", "101")
        self.assertEqual(row["status"], "exhausted")
        self.assertTrue(row["delivery_uncertain"])
        self.responder.apply.assert_not_called()
        self.responder.apply_on_vacancy.assert_not_called()
        self.assertFalse(self.queue.claim("another-resume", "101"))

    def test_dry_run_never_claims_or_sends(self):
        stats = self.run_bot(dry=True)
        self.assertEqual(stats.dry_run, 1)
        self.assertIsNone(self.queue.get("selected", "101"))
        self.responder.apply.assert_not_called()

    def test_permanent_skipped_result_is_terminal_but_moved_remains_retryable(self):
        self.responder.apply.return_value = Result(Status.SKIPPED, "Existing hh questionnaire rule",
                                                  delivery={"outbound_attempted": False})
        self.run_bot()
        self.assertEqual(self.queue.get("selected", "101")["status"], "cancelled")
        other = Vacancy("102", "Python", "Other company", "Откликнуться")
        self.open.return_value = [other]
        self.responder.apply.return_value = Result(Status.SKIPPED, MOVED, delivery={"outbound_attempted": False})
        self.run_bot()
        self.assertEqual(self.queue.get("selected", "102")["status"], "pending")

    def test_run_limit_prevents_claiming_a_second_application(self):
        other = Vacancy("102", "Python", "Other company", "Откликнуться")
        self.open.return_value = [self.vac, other]
        stats = self.run_bot(limit=1)
        self.assertEqual(stats.applied, 1)
        self.assertEqual(self.responder.apply.call_count, 1)
        self.assertIsNone(self.queue.get("selected", "102"))

    def test_retry_respects_company_limit_updated_since_first_attempt(self):
        self.seed()
        self.cfg.limits.per_company = 1
        self.state.mark_applied(Vacancy("999", "Other role", self.vac.company, "Откликнуться"), False)
        self.run_bot(retry=True)
        self.assertEqual(self.queue.get("selected", "101")["status"], "cancelled")
        self.responder.apply_on_vacancy.assert_not_called()

    def test_reconciliation_does_not_duplicate_an_already_recorded_delivery(self):
        self.seed(unknown=True)
        self.responder.application_status.return_value = "applied"
        self.results.records.return_value = [{"id": "101", "category": "applied", "letter": True}]
        stats = self.run_bot(retry=True)
        self.assertEqual(stats.applied, 0)
        self.results.add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
