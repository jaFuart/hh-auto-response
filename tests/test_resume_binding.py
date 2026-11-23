"""Письма используют выбранное резюме; чужой и непривязанный кеш не допускаются. Без сети."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto import app  # noqa: E402
from hh_auto.core.config import Config  # noqa: E402
from hh_auto.hh import resume  # noqa: E402
from hh_auto.hh.responder import Responder  # noqa: E402
from hh_auto.hh.search import Vacancy  # noqa: E402
from hh_auto.web.browser import FatalError  # noqa: E402


class SelectedResumeCache(unittest.TestCase):
    TEXT = ("Python-разработчик\nПроживает: Москва\n" + "Разработка сервисов на Python. " * 15).strip()

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cache = Path(folder.name) / "resume.txt"
        self.identity = self.cache.with_name("resume.txt.id")
        self.cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        self.page = Mock(url="https://hh.ru/")

    def cached(self, text=TEXT, identity="selected"):
        self.cache.write_text(text, encoding="utf-8")
        if identity is not None:
            self.identity.write_text(identity, encoding="utf-8")

    def fetch_after_load_failure(self, *, require_selected=True):
        with patch.object(resume, "open_url", side_effect=RuntimeError("hh недоступен")):
            return resume.fetch_resume_text(self.page, self.cfg, self.cache, require_selected=require_selected)

    def test_matching_selected_cache_is_allowed_after_load_failure(self):
        self.cached()
        self.assertEqual(self.fetch_after_load_failure(), self.TEXT)

    def test_another_resume_cache_is_rejected(self):
        self.cached(identity="another")
        self.assertEqual(self.fetch_after_load_failure(), "")

    def test_legacy_unverified_cache_is_rejected(self):
        self.cached(identity=None)
        self.assertEqual(self.fetch_after_load_failure(), "")

    def test_blank_selected_cache_is_rejected(self):
        self.cached(text=" \n ")
        self.assertEqual(self.fetch_after_load_failure(), "")

    def test_selected_id_is_required_without_choosing_first_resume(self):
        self.cached()
        self.cfg.resume_url = ""
        with patch.object(resume, "_first_resume_id") as first, patch.object(resume, "open_url") as open_page:
            self.assertEqual(resume.fetch_resume_text(self.page, self.cfg, self.cache, require_selected=True), "")
        first.assert_not_called()
        open_page.assert_not_called()

    def test_redirect_to_another_resume_does_not_create_bound_cache(self):
        self.page.url = "https://hh.ru/resume/another"
        with patch.object(resume, "open_url"):
            self.assertEqual(resume.fetch_resume_text(self.page, self.cfg, self.cache, require_selected=True), "")
        self.page.locator.assert_not_called()
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.identity.exists())

    def test_url_is_checked_again_after_reading_resume(self):
        self.page.url = "https://hh.ru/resume/selected"
        area = self.page.locator.return_value.first

        def redirected_text():
            self.page.url = "https://hh.ru/resume/another"
            return self.TEXT

        area.inner_text.side_effect = redirected_text
        self.assertEqual(resume.fetch_resume_text(self.page, self.cfg, self.cache, require_selected=True), "")
        self.assertFalse(self.cache.exists())

    def test_success_records_selected_identity_before_profile_navigation(self):
        self.page.url = "https://hh.ru/resume/selected?from=search"
        self.page.locator.return_value.first.inner_text.return_value = self.TEXT

        def with_personal(page, text):
            page.url = resume.PROFILE_URL
            return text

        with patch.object(resume, "_with_personal", side_effect=with_personal):
            text = resume.fetch_resume_text(self.page, self.cfg, self.cache, require_selected=True)
        self.assertEqual(text, self.TEXT)
        self.assertEqual(self.cache.read_text(encoding="utf-8"), self.TEXT)
        self.assertEqual(self.identity.read_text(encoding="utf-8"), "selected")

    def test_nonletter_mode_keeps_legacy_cache_behavior(self):
        self.cached(identity=None)
        self.assertEqual(self.fetch_after_load_failure(require_selected=False), self.TEXT)


class LetterResumeRequired(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        self.cfg.letter.mode = "ai"
        self.vacancy = Vacancy("101", "Разработчик", "Компания", "Откликнуться")

    def test_form_ai_requires_selected_id_before_client_or_resume_loading(self):
        self.cfg.resume_url = ""
        with patch("hh_auto.ai.client.AiClient") as client, patch.object(resume, "fetch_resume_text") as fetch:
            with self.assertRaisesRegex(FatalError, "выберите резюме"):
                app._form_ai(Mock(), self.cfg, Path("unused"))
        client.assert_not_called()
        fetch.assert_not_called()

    def test_form_ai_requires_nonempty_selected_resume(self):
        with patch("hh_auto.ai.client.AiClient"), patch.object(resume, "fetch_resume_text", return_value=" \n") as fetch:
            with self.assertRaisesRegex(FatalError, "текст выбранного резюме"):
                app._form_ai(Mock(), self.cfg, Path("unused"))
        self.assertTrue(fetch.call_args.kwargs["require_selected"])

    def test_form_ai_passes_bound_resume_to_responder_pipeline(self):
        with patch("hh_auto.ai.client.AiClient") as client, \
                patch.object(resume, "fetch_resume_text", return_value="Моё выбранное резюме"):
            ai, text = app._form_ai(Mock(), self.cfg, Path("unused"))
        self.assertIs(ai, client.return_value)
        self.assertEqual(text, "Моё выбранное резюме")

    def test_blank_resume_stops_before_quick_apply_or_any_click(self):
        page, ai = Mock(), Mock()
        responder = Responder(page, self.cfg, Path("unused"), None, ai, " \n")
        with patch.object(responder, "_apply") as apply:
            with self.assertRaisesRegex(FatalError, "нет текста выбранного резюме"):
                responder.apply(self.vacancy)
        apply.assert_not_called()
        page.assert_not_called()
        ai.write_letter.assert_not_called()

    def test_blank_resume_cannot_reach_ai_even_if_letter_called_directly(self):
        ai = Mock()
        responder = Responder(Mock(), self.cfg, Path("unused"), None, ai, "")
        with patch("hh_auto.hh.responder.fetch_vacancy_text") as fetch:
            self.assertFalse(responder._ai_letter(Mock(), self.vacancy))
        fetch.assert_not_called()
        ai.write_letter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
