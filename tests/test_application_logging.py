"""The audit records submitted field text, not a merely generated draft."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hh_auto.core.config import Config
from hh_auto.hh.responder import Responder, Result, Status
from hh_auto.hh.search import Vacancy
from hh_auto.reports.results import ResultsBook


class ApplicationLogging(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        self.cfg.letter.mode = "ai"
        self.ai = Mock()
        self.ai.write_letter.return_value = "Prepared letter that meets the chosen vacancy requirements."
        self.ai.last_letter_record = {}
        self.vac = Vacancy("101", "Python", "Company", "Откликнуться", snippet="API work")
        self.responder = Responder(Mock(url="https://hh.ru/search/vacancy"), self.cfg, Path("unused"), None,
                                   self.ai, "Python resume", recent_letters=["Prior confirmed letter"])

    def test_log_uses_verified_prepared_field_and_keeps_generation_metadata_separate(self):
        textarea = Mock()
        textarea.input_value.return_value = self.ai.write_letter.return_value
        button = Mock()
        button.evaluate.return_value = {"values": [self.ai.write_letter.return_value]}

        def generate(*args, **kwargs):
            self.ai.last_letter_record = {"selected_projects": ["VPN"], "model_version": "test"}
            return "Prepared letter that meets the chosen vacancy requirements."

        def apply(vac):
            self.responder.page.url = "https://hh.ru/applicant/vacancy_response?vacancyId=101"
            self.assertTrue(self.responder._put_letter(textarea, self.ai.write_letter.return_value))
            self.assertIsNone(self.responder._submit(Mock(), button, "Send"))
            return Result(Status.APPLIED, letter=True)

        self.ai.write_letter.side_effect = generate
        with patch("hh_auto.hh.responder.fetch_vacancy_text", return_value="Complete vacancy"), \
                patch.object(self.responder, "_apply", side_effect=apply), \
                patch.object(self.responder, "application_status", return_value="not_applied"), \
                patch.object(self.responder, "_direct_resume_matches", return_value=True), \
                patch.object(self.responder, "_reserve_submission"), \
                patch.object(self.responder, "_wait_enabled", return_value=True), \
                patch.object(self.responder, "_pause"), patch.object(self.responder, "_visible", return_value=False), \
                patch.object(self.responder, "on_search_page", return_value=True), \
                patch.object(self.responder, "_questions_shown", return_value=False), \
                patch("hh_auto.hh.responder.captcha_shown", return_value=False):
            result = self.responder.apply(self.vac)
        self.assertEqual(result.letter_text, self.ai.write_letter.return_value)
        self.assertEqual(result.generation["selected_projects"], ["VPN"])
        self.assertEqual(result.generation["prepared_letter"], result.letter_text)
        self.assertTrue(result.delivery["outbound_attempted"])
        self.assertTrue(result.delivery["letter_attachment_confirmed"])
        self.assertEqual(self.ai.write_letter.call_args.kwargs["vacancy_id"], "101")
        self.assertEqual(self.ai.write_letter.call_args.kwargs["recent_letters"], ["Prior confirmed letter"])

    def test_prepared_or_filled_draft_never_claims_actual_letter_on_failed_attachment(self):
        self.responder._prepared_ai_letter = ("101", "Prepared draft")
        self.responder._filled_letter_text = "Filled but not submitted"
        self.responder.clicked = True
        result = self.responder._finish_result(self.vac, Result(Status.APPLIED, letter=False))
        self.assertEqual(result.letter_text, "")
        self.assertEqual(result.generation["filled_letter"], "Filled but not submitted")
        self.assertFalse(result.delivery["letter_attachment_confirmed"])
        self.assertEqual(result.delivery["status"], "confirmed")

    def test_results_preserve_metadata_and_only_confirmed_texts_become_recent(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        book = ResultsBook(Path(folder.name))
        with patch.object(book, "save_xlsx"):
            book.add(self.vac, Result(Status.FAILED, letter_text="Unsent", generation={"prepared_letter": "Draft"},
                                      delivery={"status": "unknown"}))
            book.add(self.vac, Result(Status.APPLIED, letter=True, letter_text="Actual sent",
                                      generation={"prompt_version": "v2"}, delivery={"status": "confirmed"}))
            book.add(self.vac, Result(Status.APPLIED, letter=False, letter_text="Unattached draft"))
        records = book.records()
        self.assertEqual(records[0]["letter_text"], "")
        self.assertEqual(records[0]["generation"]["prepared_letter"], "Draft")
        self.assertEqual(records[1]["letter_text"], "Actual sent")
        self.assertEqual(records[1]["generation"]["prompt_version"], "v2")
        self.assertEqual(book.recent_letters(), ["Actual sent"])
        self.assertEqual(book.recent_letters(0), [])

    def test_probe_requires_explicit_evidence_and_never_sends(self):
        page = self.responder.page
        page.url = self.vac.url
        confirmed = Mock()
        confirmed.or_.return_value = confirmed
        confirmed.filter.return_value = confirmed
        available = Mock()
        available.filter.return_value = available
        available.count.return_value = 1
        available.nth.return_value.inner_text.return_value = "Откликнуться"
        available.nth.return_value.is_enabled.return_value = True
        page.locator.side_effect = lambda selector: confirmed if "view-topic" in selector else available
        page.get_by_text.return_value = confirmed
        with patch("hh_auto.hh.responder.open_url"), patch("hh_auto.hh.responder.captcha_shown", return_value=False), \
                patch("hh_auto.hh.responder.hh_role", return_value="applicant"), \
                patch("hh_auto.hh.responder.control.sleep"):
            confirmed.count.return_value = 1
            self.assertEqual(self.responder.application_status(self.vac), "applied")
            confirmed.count.return_value = 0
            self.assertEqual(self.responder.application_status(self.vac), "not_applied")
            available.count.return_value = 0
            with patch("hh_auto.hh.responder.time.monotonic", side_effect=[0, 0, 9]):
                self.assertEqual(self.responder.application_status(self.vac), "unknown")
        available.nth.return_value.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
