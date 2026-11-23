"""Retries open only the observed GET form and require selected resume ID before submission."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hh_auto.core.config import Config
from hh_auto.hh.responder import Responder, Result, Status
from hh_auto.hh.search import Vacancy


class TargetedRetryForm(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        self.cfg.letter.mode = "none"
        self.vac = Vacancy("101", "Python", "Company", "Откликнуться")
        self.page = Mock(url=self.vac.url)
        self.link, self.submit, self.body = Mock(), Mock(), Mock()
        self.link.filter.return_value.first = self.link
        self.link.count.return_value = 1
        self.link.get_attribute.return_value = "/applicant/vacancy_response?vacancyId=101&employerId=999"
        self.page.locator.side_effect = lambda selector: (self.link if "link-top" in selector else
                                                          self.body if selector == "body" else self.submit)
        self.responder = Responder(self.page, self.cfg, Path("unused"), "Python resume")
        self.patches = [patch.object(self.responder, "application_status", return_value="not_applied"),
                        patch.object(self.responder, "_close_dialogs"),
                        patch.object(self.responder, "_visible", side_effect=lambda locator: locator is self.submit),
                        patch.object(self.responder, "_select_direct_resume", return_value=True),
                        patch.object(self.responder, "_questions_shown", return_value=False),
                        patch.object(self.responder, "_handle_modal", return_value=Result(Status.APPLIED)),
                        patch("hh_auto.hh.responder.captcha_shown", return_value=False),
                        patch("hh_auto.hh.responder.hh_role", return_value="applicant"),
                        patch("hh_auto.hh.responder.open_url", side_effect=lambda page, url: setattr(page, "url", url))]
        self.probe, self.close, self.visible, self.selected, self.questions, self.modal, self.captcha, self.role, self.open = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def test_verified_target_opens_observed_get_without_clicking_quick_apply(self):
        result = self.responder.apply_on_vacancy(self.vac)
        self.assertIs(result.status, Status.APPLIED)
        self.assertEqual(self.open.call_args.args[1], "https://hh.ru/applicant/vacancy_response?vacancyId=101&employerId=999")
        self.link.click.assert_not_called()
        self.selected.assert_called_once()
        self.modal.assert_called_once()

    def test_missing_selected_resume_id_never_reaches_form_submission(self):
        self.selected.return_value = False
        result = self.responder.apply_on_vacancy(self.vac)
        self.assertIs(result.status, Status.FAILED)
        self.assertFalse(result.delivery["outbound_attempted"])
        self.modal.assert_not_called()
        self.link.click.assert_not_called()

    def test_untrusted_or_wrong_vacancy_link_is_never_followed(self):
        for href in ("https://example.org/applicant/vacancy_response?vacancyId=101",
                     "/applicant/vacancy_response?vacancyId=102", "", "javascript:void(0)"):
            with self.subTest(href=href):
                self.page.url = self.vac.url
                self.link.get_attribute.return_value = href
                result = self.responder.apply_on_vacancy(self.vac)
                self.assertIs(result.status, Status.FAILED)
                self.assertFalse(result.delivery["outbound_attempted"])
        self.open.assert_not_called()

    def test_unknown_status_never_opens_form_or_calls_generation(self):
        self.probe.return_value = "unknown"
        result = self.responder.apply_on_vacancy(self.vac)
        self.assertIs(result.status, Status.FAILED)
        self.open.assert_not_called()
        self.modal.assert_not_called()

    def test_selected_id_is_exact_and_ambiguous_controls_fail_closed(self):
        self.page.evaluate.return_value = ["selected"]
        self.assertTrue(self.responder._direct_resume_matches())
        self.page.evaluate.return_value = ["selected", "other"]
        self.assertFalse(self.responder._direct_resume_matches())
        self.page.evaluate.return_value = ["other"]
        self.assertFalse(self.responder._direct_resume_matches())

    def test_resume_is_rechecked_at_irreversible_form_send(self):
        self.responder._direct_vacancy_id = "101"
        self.responder._letter_field = None
        with patch.object(self.responder, "_wait_enabled", return_value=True), \
                patch.object(self.responder, "_pause"), \
                patch.object(self.responder, "_direct_resume_matches", return_value=False):
            error = self.responder._submit(Mock(), self.submit, "Send")
        self.assertIn("ID", error)
        self.submit.click.assert_not_called()
        self.assertFalse(self.responder.clicked)


if __name__ == "__main__":
    unittest.main()
