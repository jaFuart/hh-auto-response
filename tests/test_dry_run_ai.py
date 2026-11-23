"""A filter preview does not require AI letters or forms; an enabled AI filter still runs."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hh_auto import app
from hh_auto.ai.client import AiError
from hh_auto.core.config import Config
from hh_auto.hh.runner import RunOptions
from hh_auto.web.browser import FatalError


class DryRunAiTest(unittest.TestCase):
    def config(self):
        cfg = Config(search_url="")
        cfg.ai.provider = "openai"
        cfg.ai.enabled = True
        cfg.letter.mode = "ai"
        return cfg

    def test_dry_run_bypasses_missing_key_and_resume_for_letters_and_forms(self):
        cfg = self.config()
        with patch("hh_auto.ai.client.AiClient", side_effect=AiError("missing key")) as client, \
                patch("hh_auto.hh.resume.fetch_resume_text") as fetch:
            self.assertEqual(app._form_ai(Mock(), cfg, Path("unused"), dry_run=True), (None, ""))
        client.assert_not_called()
        fetch.assert_not_called()

    def test_normal_run_still_requires_selected_resume(self):
        cfg = self.config()
        with patch("hh_auto.ai.client.AiClient") as client:
            with self.assertRaisesRegex(FatalError, "выберите резюме"):
                app._form_ai(Mock(), cfg, Path("unused"))
        client.assert_not_called()

    def test_normal_run_still_requires_working_key_and_resume_text(self):
        cfg = self.config()
        cfg.resume_url = "https://hh.ru/resume/selected"
        with patch("hh_auto.ai.client.AiClient", side_effect=AiError("missing key")), \
                patch("hh_auto.hh.resume.fetch_resume_text") as fetch:
            with self.assertRaisesRegex(FatalError, "missing key"):
                app._form_ai(Mock(), cfg, Path("unused"))
        fetch.assert_not_called()
        with patch("hh_auto.ai.client.AiClient"), \
                patch("hh_auto.hh.resume.fetch_resume_text", return_value=""):
            with self.assertRaisesRegex(FatalError, "текст выбранного резюме"):
                app._form_ai(Mock(), cfg, Path("unused"))

    def test_dry_run_constructs_client_for_enabled_ai_filter_without_resume(self):
        cfg = self.config()
        cfg.ai_filter.enabled = True
        with patch("hh_auto.ai.client.AiClient") as client, \
                patch("hh_auto.hh.resume.fetch_resume_text") as fetch:
            ai, text = app._form_ai(Mock(), cfg, Path("unused"), dry_run=True)
        client.assert_called_once_with(cfg.ai)
        fetch.assert_not_called()
        self.assertIs(ai, client.return_value)
        self.assertEqual(text, "")

    def test_apply_work_passes_preview_flag(self):
        cfg = self.config()
        cfg.stats.skill_tips = False
        with patch.object(app, "verify_resume"), patch.object(app, "_form_ai", return_value=(None, "")) as form, \
                patch.object(app, "State"), patch.object(app, "run"):
            app._apply_work(Mock(), cfg, RunOptions(dry_run=True), Path("unused"), Mock(), Mock())
        self.assertTrue(form.call_args.kwargs["dry_run"])


if __name__ == "__main__":
    unittest.main()
