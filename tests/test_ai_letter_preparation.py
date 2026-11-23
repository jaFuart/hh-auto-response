"""Ошибка генерации не должна отправлять быстрый отклик. Проверки без сети и браузера."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto.ai.client import AiError  # noqa: E402
from hh_auto.core.config import Config  # noqa: E402
from hh_auto.hh.responder import Responder, Result, Status  # noqa: E402
from hh_auto.hh.search import Vacancy  # noqa: E402
from hh_auto.web.browser import CaptchaShown  # noqa: E402


class PrepareBeforeApply(unittest.TestCase):
    LETTER = "Разрабатывал сервисы на Python. Этот опыт подходит для задач вашей команды."

    def setUp(self):
        self.cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        self.cfg.letter.mode = "ai"
        self.cfg.letter.fallback_text = "Запасной текст не должен обходить подготовку персонального письма."
        self.ai = Mock()
        self.ai.write_letter.return_value = self.LETTER
        self.responder = Responder(Mock(), self.cfg, Path("unused"), None, self.ai, "Опыт разработки на Python")
        self.vacancy = Vacancy("101", "Разработчик", "Компания", "Откликнуться", snippet="Короткая карточка")
        self.fetch = patch("hh_auto.hh.responder.fetch_vacancy_text", return_value="Полное описание вакансии")
        self.fetch_mock = self.fetch.start()
        self.addCleanup(self.fetch.stop)
        self.status = patch.object(self.responder, "application_status", return_value="not_applied")
        self.status.start()
        self.addCleanup(self.status.stop)

    def test_api_error_fails_safely_before_apply_without_template_fallback(self):
        self.ai.write_letter.side_effect = AiError("API недоступен")
        with patch.object(self.responder, "_apply") as apply:
            result = self.responder.apply(self.vacancy)
        self.assertIs(result.status, Status.FAILED)
        self.assertFalse(result.remember)
        self.assertFalse(self.responder.clicked)
        apply.assert_not_called()

    def test_empty_letter_is_technical_failure_before_apply(self):
        for text in ("", " \n\t"):
            with self.subTest(text=text), patch.object(self.responder, "_apply") as apply:
                self.ai.write_letter.return_value = text
                result = self.responder.apply(self.vacancy)
                self.assertIs(result.status, Status.FAILED)
                apply.assert_not_called()

    def test_missing_full_description_uses_card_snippet_without_skipping(self):
        self.fetch_mock.return_value = " \n"
        with patch.object(self.responder, "_apply", return_value=Result(Status.APPLIED)) as apply:
            result = self.responder.apply(self.vacancy)
        self.assertIs(result.status, Status.APPLIED)
        self.assertEqual(self.ai.write_letter.call_args.args[4], self.vacancy.snippet)
        self.assertEqual(result.generation["description_source"], "card_snippet")
        apply.assert_called_once()

    def test_missing_client_skips_without_request_or_apply(self):
        self.responder.ai = None
        with patch.object(self.responder, "_apply") as apply:
            result = self.responder.apply(self.vacancy)
        self.assertIs(result.status, Status.FAILED)
        self.fetch_mock.assert_not_called()
        apply.assert_not_called()

    def test_letter_is_generated_before_first_click_and_reused_for_insertion(self):
        events = []
        textarea = Mock()

        def write(*args, **kwargs):
            events.append("generate")
            return self.LETTER

        def apply(vacancy):
            events.append("click")
            self.assertTrue(self.responder._ai_letter(textarea, vacancy))
            self.assertTrue(self.responder._ai_letter(textarea, vacancy))
            return Result(Status.APPLIED, letter=True)

        self.ai.write_letter.side_effect = write
        with patch.object(self.responder, "_apply", side_effect=apply), \
                patch.object(self.responder, "_pause"), patch.object(self.responder, "_put_letter", return_value=True) as put:
            self.assertIs(self.responder.apply(self.vacancy).status, Status.APPLIED)
        self.assertEqual(events, ["generate", "click"])
        self.ai.write_letter.assert_called_once()
        self.fetch_mock.assert_called_once()
        self.assertEqual([call.args for call in put.call_args_list], [(textarea, self.LETTER), (textarea, self.LETTER)])

    def test_captcha_retry_reuses_prepared_text_without_another_paid_request(self):
        textarea = Mock()
        attempts = []

        def apply(vacancy):
            attempts.append(vacancy.id)
            if len(attempts) == 1:
                raise CaptchaShown()
            self.assertTrue(self.responder._ai_letter(textarea, vacancy))
            return Result(Status.APPLIED, letter=True)

        with patch.object(self.responder, "_apply", side_effect=apply), \
                patch.object(self.responder, "_wait_captcha") as wait, patch.object(self.responder, "_pause"), \
                patch.object(self.responder, "_put_letter", return_value=True):
            self.assertIs(self.responder.apply(self.vacancy).status, Status.APPLIED)
        self.assertEqual(attempts, [self.vacancy.id, self.vacancy.id])
        self.ai.write_letter.assert_called_once()
        self.fetch_mock.assert_called_once()
        wait.assert_called_once()

    def test_next_vacancy_gets_its_own_prepared_letter(self):
        other = Vacancy("102", "Другой разработчик", "Другая компания", "Откликнуться")
        other_letter = self.LETTER + " Другое письмо."
        self.ai.write_letter.side_effect = [self.LETTER, other_letter]
        textarea = Mock()

        def apply(vacancy):
            self.assertTrue(self.responder._ai_letter(textarea, vacancy))
            return Result(Status.APPLIED, letter=True)

        with patch.object(self.responder, "_apply", side_effect=apply), \
                patch.object(self.responder, "_pause"), patch.object(self.responder, "_put_letter", return_value=True) as put:
            self.responder.apply(self.vacancy)
            self.responder.apply(other)
        self.assertEqual(self.ai.write_letter.call_count, 2)
        self.assertEqual([call.args[1] for call in put.call_args_list], [self.LETTER, other_letter])

    def test_hh_generation_fallback_keeps_card_description_behavior(self):
        self.cfg.letter.mode = "generate"
        self.fetch_mock.return_value = ""
        self.assertEqual(self.responder._generate_ai_letter(self.vacancy), self.LETTER)
        self.assertEqual(self.ai.write_letter.call_args.args[4], self.vacancy.snippet)


if __name__ == "__main__":
    unittest.main()
