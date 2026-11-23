"""CLI selection binds an owned resume without sending, raising, or changing other filters."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from hh_auto.hh.raising import Resume
from hh_auto.web.browser import FatalError
from scripts import resumes, session


class ResumeCommands(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config = self.root / "config.server.toml"
        self.config.write_text('search_url = "https://hh.ru/search/vacancy?resume=old&area=1"\n'
                               'resume_url = "https://hh.ru/resume/old"\n'
                               '[filters]\ninclude_words = ["Python"] # preserve this\n', encoding="utf-8")
        self.page = Mock()

    def invoke(self, action, *more, work_error=False):
        def browser(cfg, root, work, *, auto_raise, login):
            self.assertTrue(cfg.browser.headless)
            self.assertFalse(cfg.browser.hide_window)
            self.assertFalse(auto_raise)
            self.assertFalse(login)
            cfg.browser.profile_dir = "runtime-absolute-browser-path"
            try:
                work(self.page)
            except FatalError:
                if not work_error:
                    raise
                return 1
            return 0
        self.output = StringIO()
        with patch.object(resumes, "_with_browser", side_effect=browser), \
                patch.object(resumes, "setup_logging", return_value=self.root / "log"), redirect_stdout(self.output):
            return resumes.main([action, "--data-dir", str(self.root), "--config", str(self.config), *more])

    def test_list_uses_saved_headless_session_and_excludes_card_text(self):
        original = self.config.read_bytes()
        with patch.object(resumes, "list_resumes", return_value=[Resume("owned", "Python", "Private body")]):
            self.assertEqual(self.invoke("list", "--json"), 0)
        self.assertEqual(json.loads(self.output.getvalue()), [{"id": "owned", "title": "Python", "selected": False}])
        self.assertEqual(self.config.read_bytes(), original)

    def test_select_checks_ownership_and_preserves_filters_comments_and_browser_config(self):
        with patch.object(resumes, "list_resumes", return_value=[Resume("owned", "Python")]):
            self.assertEqual(self.invoke("select", "--id", "owned"), 0)
        text = self.config.read_text(encoding="utf-8")
        saved = tomllib.loads(text)
        self.assertEqual(saved["resume_url"], "https://hh.ru/resume/owned")
        self.assertIn("resume=owned", saved["search_url"])
        self.assertIn("area=1", saved["search_url"])
        self.assertEqual(saved["filters"]["include_words"], ["Python"])
        self.assertIn("# preserve this", text)
        self.assertNotIn("runtime-absolute-browser-path", text)

    def test_foreign_resume_does_not_change_config(self):
        original = self.config.read_bytes()
        with patch.object(resumes, "list_resumes", return_value=[Resume("owned", "Python")]):
            self.assertEqual(self.invoke("select", "--id", "foreign", work_error=True), 1)
        self.assertEqual(self.config.read_bytes(), original)

    def test_fetch_requires_selected_identity_and_never_prints_resume(self):
        with patch.object(resumes, "fetch_resume_text", return_value="Private resume body") as fetch:
            self.assertEqual(self.invoke("fetch"), 0)
        self.assertTrue(fetch.call_args.kwargs["require_selected"])
        self.assertNotIn("Private resume body", self.output.getvalue())

    def test_data_home_is_restored_after_command(self):
        with patch.dict(os.environ, {"HH_AUTO_HOME": "original-data-home"}), \
                patch.object(resumes, "list_resumes", return_value=[]):
            self.invoke("list")
            self.assertEqual(os.environ["HH_AUTO_HOME"], "original-data-home")

    def test_only_login_command_can_open_a_visible_browser(self):
        for action in ("login", "export", "check"):
            def browser(cfg, root, work, *, auto_raise, login):
                self.assertEqual(cfg.browser.headless, action != "login")
                self.assertEqual(login, action == "login")
                self.assertFalse(auto_raise)
                return 0
            with self.subTest(action=action), patch.object(session, "_with_browser", side_effect=browser), \
                    patch.object(session, "setup_logging", return_value=self.root / "log"), \
                    patch.dict(os.environ):
                self.assertEqual(session.main([action, "--config", str(self.config), "--data-dir", str(self.root)]), 0)


if __name__ == "__main__":
    unittest.main()
