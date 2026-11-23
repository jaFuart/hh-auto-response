"""AI applications require the prepared letter in the current form before any application click."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError

from hh_auto.core.config import Config
from hh_auto.core.delivery import ApplicationDelivery
from hh_auto.core.state import State
from hh_auto.hh import responder as module, runner
from hh_auto.hh.responder import Responder, Result, Status
from hh_auto.hh.search import Vacancy


LETTER = "Добрый день!\n\nРазрабатывал API и проверял данные."


class RequiredAiLetter(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(search_url="https://hh.ru/search/vacancy", resume_url="https://hh.ru/resume/selected")
        self.cfg.letter.mode = "ai"
        self.cfg.letter.fallback_text = "Старый запасной шаблон не должен уходить вместо подготовленного письма."
        self.page = Mock(url="https://hh.ru/applicant/vacancy_response?vacancyId=101")
        self.sender = Responder(self.page, self.cfg, Path("unused"), "Python", Mock(), "Python resume")
        self.sender._direct_vacancy_id = self.sender._quota_vacancy_id = "101"
        self.sender._prepared_ai_letter = ("101", LETTER)
        self.vacancy = Vacancy("101", "Python", "Synthetic", "Откликнуться")
        self.button = Mock()
        self.button.evaluate.return_value = {"values": [LETTER]}
        self.patches = [patch.object(self.sender, "_pause"),
                        patch.object(self.sender, "_wait_enabled", return_value=True),
                        patch.object(self.sender, "_direct_resume_matches", return_value=True),
                        patch.object(self.sender, "_reserve_submission"),
                        patch.object(self.sender, "_visible", return_value=False),
                        patch.object(self.sender, "_on_response_origin", return_value=True),
                        patch.object(self.sender, "_questions_shown", return_value=False),
                        patch.object(module, "captcha_shown", return_value=False),
                        patch.object(module.control, "sleep")]
        (_, _, self.resume, self.reserve, _, _, _, _, _) = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)

    def submit(self, **kwargs):
        return self.sender._submit(Mock(), self.button, "Откликнуться", **kwargs)

    def test_fill_requires_nonempty_exact_readback_with_only_line_end_normalization(self):
        for value in ("", "Other letter", " " + LETTER, LETTER.lower()):
            box = Mock()
            box.is_enabled.return_value = box.is_editable.return_value = True
            box.input_value.return_value = value
            with self.subTest(value=value):
                self.assertFalse(self.sender._put_letter(box, LETTER, wait=0))
                self.assertEqual(self.sender._filled_letter_text, "")
        box.input_value.return_value = LETTER.replace("\n", "\r\n")
        self.assertTrue(self.sender._put_letter(box, LETTER, wait=0))

    def test_failed_ai_insertion_never_uses_fallback_template(self):
        with patch.object(self.sender, "_ai_letter", return_value=False), \
                patch.object(self.sender, "_fill_letter") as fallback:
            self.assertFalse(self.sender._write_letter(Mock(), Mock(), self.vacancy))
        fallback.assert_not_called()

    def test_missing_empty_changed_or_ambiguous_main_field_never_clicks_or_reserves(self):
        for proof in (None, {}, {"values": []}, {"values": [""]}, {"values": ["Changed"]},
                      {"values": [LETTER, ""]}, {"values": [LETTER, "Wrong mirror"]}):
            self.button.evaluate.return_value = proof
            with self.subTest(proof=proof):
                self.assertIn("отправка запрещена", self.submit())
        self.button.click.assert_not_called()
        self.reserve.assert_not_called()
        self.assertFalse(self.sender.clicked)

    def test_cleared_field_after_reservation_is_checked_again_before_click(self):
        self.button.evaluate.side_effect = [{"values": [LETTER]}, {"values": [""]}]
        self.assertIn("отправка запрещена", self.submit())
        self.reserve.assert_called_once()
        self.button.click.assert_not_called()
        self.assertFalse(self.sender.clicked)

    def test_resume_change_before_click_cannot_send(self):
        self.resume.side_effect = [True, False]
        self.assertIn("ID", self.submit())
        self.button.click.assert_not_called()

    def test_letter_for_another_vacancy_or_untrusted_form_is_never_submitted(self):
        self.sender._prepared_ai_letter = ("102", LETTER)
        self.assertIn("привязано", self.submit())
        self.sender._prepared_ai_letter = ("101", LETTER)
        for url in ("https://hh.ru/applicant/vacancy_response?vacancyId=102",
                    "https://other.test/applicant/vacancy_response?vacancyId=101",
                    "https://hh.ru/search/vacancy?vacancyId=101"):
            self.page.url = url
            with self.subTest(url=url):
                self.assertIn("адрес формы", self.submit())
        self.button.click.assert_not_called()
        self.reserve.assert_not_called()

    def test_detached_saved_dialog_is_not_proof_of_the_main_form(self):
        previous = Mock()
        previous.input_value.side_effect = PlaywrightError("detached")
        self.sender._letter_field = previous
        self.sender._filled_letter_text = LETTER
        self.button.evaluate.return_value = None
        self.assertIn("отправка запрещена", self.submit())
        self.button.click.assert_not_called()
        previous.input_value.assert_not_called()

    def test_saved_main_field_can_be_verified_even_after_editor_was_removed(self):
        previous = Mock()
        previous.input_value.side_effect = PlaywrightError("detached")
        self.sender._letter_field = previous
        self.assertIsNone(self.submit())
        self.button.click.assert_called_once()
        result = self.sender._finish_result(self.vacancy, Result(Status.APPLIED, letter=True))
        self.assertTrue(result.delivery["letter_attachment_confirmed"])
        self.assertEqual(result.letter_text, LETTER)
        self.assertEqual(result.generation["prepared_letter"], LETTER)

    def test_draft_save_is_not_an_application_attempt(self):
        self.assertIsNone(self.submit(draft_only=True))
        self.button.click.assert_called_once()
        self.reserve.assert_not_called()
        self.assertFalse(self.sender.clicked)
        self.assertFalse(self.sender._letter_submission_verified)

    def test_unreadable_field_fails_closed(self):
        self.button.evaluate.side_effect = PlaywrightError("detached")
        self.assertIn("недоступно", self.submit())
        self.button.click.assert_not_called()

    def test_missing_optional_toggle_or_missing_expanded_form_prevents_application_submit(self):
        dialog = Mock()
        dialog.locator.return_value.count.return_value = 0
        with patch.object(self.sender, "_response_dialog", return_value=dialog), \
                patch.object(self.sender, "_resume_matches", return_value=True), \
                patch.object(self.sender, "_close_dialogs"), patch.object(self.sender, "_snapshot_once"), \
                patch.object(self.sender, "_submit") as submit:
            for toggle in (None, Mock()):
                if toggle is not None:
                    toggle.inner_text.return_value = "Добавить сопроводительное"
                with self.subTest(toggle=toggle), patch.object(self.sender, "_letter_toggle", return_value=toggle), \
                        patch.object(self.sender, "_wait_modal_letter", return_value=None):
                    result = self.sender._handle_modal(self.vacancy, Mock())
                    self.assertIs(result.status, Status.FAILED)
                    self.assertFalse(result.remember)
            submit.assert_not_called()

    def test_failed_optional_save_or_insertion_does_not_fall_through_to_application(self):
        for optional in (False, None):
            dialog = Mock()
            dialog.locator.return_value.count.return_value = 0
            with self.subTest(optional=optional), patch.object(self.sender, "_response_dialog", return_value=dialog), \
                    patch.object(self.sender, "_resume_matches", return_value=True), \
                    patch.object(self.sender, "_close_dialogs"), \
                    patch.object(self.sender, "_optional_letter", return_value=optional), \
                    patch.object(self.sender, "_submit") as submit:
                self.assertIs(self.sender._handle_modal(self.vacancy, Mock()).status, Status.FAILED)
                submit.assert_not_called()

    def test_questions_form_without_a_letter_field_never_sends(self):
        with patch.object(self.sender, "_open_questions_letter", return_value=None), \
                patch.object(self.sender, "_snapshot"), patch.object(self.sender, "_submit") as submit:
            result = self.sender._send_questions_form(self.vacancy, [], "test")
        self.assertIs(result.status, Status.FAILED)
        submit.assert_not_called()

    def test_no_letter_confirmation_from_an_unsubmitted_or_empty_draft(self):
        for text in ("", LETTER):
            self.sender._filled_letter_text = text
            self.sender._letter_submission_verified = False
            result = self.sender._finish_result(self.vacancy, Result(Status.APPLIED, letter=True))
            self.assertFalse(result.letter)
            self.assertFalse(result.delivery["letter_attachment_confirmed"])
            self.assertEqual(result.letter_text, "")

    def test_public_ai_apply_routes_to_get_form_even_when_timing_limits_are_zero(self):
        self.sender._direct_vacancy_id = None
        with patch.object(self.sender, "apply_on_vacancy", return_value=Result(Status.FAILED)) as direct:
            self.sender.apply(self.vacancy)
        direct.assert_called_once_with(self.vacancy)

    def test_ai_never_reaches_card_click_even_if_private_apply_called_directly(self):
        self.sender._direct_vacancy_id = None
        with patch.object(self.sender, "_close_dialogs"), patch.object(self.sender, "_card") as card:
            self.assertIs(self.sender._apply(self.vacancy).status, Status.FAILED)
        card.assert_not_called()

    def test_already_applied_probe_remains_readonly_without_claiming_a_new_letter(self):
        self.sender._direct_vacancy_id = None
        with patch.object(self.sender, "application_status", return_value="applied"), \
                patch.object(self.sender, "_apply") as apply:
            result = self.sender.apply(self.vacancy)
        self.assertIs(result.status, Status.APPLIED)
        self.assertTrue(result.delivery["reconciled"])
        self.assertFalse(result.delivery["outbound_attempted"])
        self.assertFalse(result.letter)
        apply.assert_not_called()

    def test_other_letter_modes_keep_their_existing_optional_submission_policy(self):
        self.button.evaluate.return_value = None
        for mode in ("none", "template", "generate"):
            self.cfg.letter.mode = mode
            with self.subTest(mode=mode):
                self.assertIsNone(self.submit())
        self.assertEqual(self.button.click.call_count, 3)
        self.button.evaluate.assert_not_called()


class RequiredAiRunner(unittest.TestCase):
    def test_zero_timing_ai_uses_direct_get_path_not_card_apply(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = Config(search_url="https://hh.ru/search/vacancy", resume_url="https://hh.ru/resume/selected")
            cfg.letter.mode = "ai"
            cfg.limits.max_pages = 1
            vacancy = Vacancy("101", "Python", "Synthetic", "Откликнуться")
            engine, results, stats = Mock(), Mock(), runner.Stats()
            engine.on_search_page.return_value = False
            engine.apply_on_vacancy.return_value = Result(Status.APPLIED, letter=True, letter_text=LETTER,
                                                         delivery={"outbound_attempted": True})
            results.recent_letters.return_value = []
            with patch.object(runner, "Responder", return_value=engine), patch.object(runner, "Pacer"), \
                    patch.object(runner, "open_results_page", return_value=[vacancy]), \
                    patch.object(runner, "next_page_info", return_value=(False, "end")), \
                    patch.object(runner, "wait_all_cards"), patch.object(runner, "_sleep"):
                runner.run(Mock(url=cfg.search_url), cfg, runner.RunOptions(), State(root / "data/state.json"),
                           results, stats, None, root / "logs/snapshots", ai=Mock(), resume_text="Python")
            engine.apply.assert_not_called()
            engine.apply_on_vacancy.assert_called_once_with(vacancy)
            self.assertEqual(stats.applied, 1)
            self.assertEqual(ApplicationDelivery(root / "data/application-delivery.json").get(
                "selected", "101")["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
