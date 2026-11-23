"""Вход и список резюме не отправляют отклики; выбор меняет ID и в подборе вакансий. Без сети."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto import app  # noqa: E402
from hh_auto.core.config import Config  # noqa: E402
from hh_auto.hh.raising import Resume  # noqa: E402


class SelectedAccountResume(unittest.TestCase):
    def test_blank_search_becomes_suitable_for_selected_resume(self):
        cfg = Config(search_url="")
        app.select_account_resume(cfg, "abc123")
        self.assertEqual(cfg.resume_url, "https://hh.ru/resume/abc123")
        self.assertEqual(cfg.resume_id, "abc123")
        parsed = urlparse(cfg.search_url)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("https", "hh.ru", "/search/vacancy"))
        self.assertEqual(parse_qs(parsed.query)["resume"], ["abc123"])
        self.assertTrue(cfg.skip_on_resume_mismatch)

    def test_existing_search_resume_is_replaced_and_other_filters_survive(self):
        cfg = Config(search_url="https://hh.ru/search/vacancy?resume=old&area=1&area=2&accept_temporary=true&resume=other")
        app.select_account_resume(cfg, "new123")
        self.assertEqual(cfg.resume_id, "new123")
        query = parse_qs(urlparse(cfg.search_url).query)
        self.assertEqual(query["resume"], ["new123"])
        self.assertEqual(query["area"], ["1", "2"])
        self.assertEqual(query["accept_temporary"], ["true"])

    def test_query_mode_keeps_query_and_search_filters(self):
        cfg = Config(search_url="https://hh.ru/search/vacancy?resume=old&area=1")
        cfg.search.mode = "query"
        cfg.search.query = "разработчик Python"
        previous_url = cfg.search_url
        app.select_account_resume(cfg, "new123")
        self.assertEqual(cfg.resume_id, "new123")
        self.assertEqual(cfg.search_url, previous_url)
        self.assertEqual(cfg.search.query, "разработчик Python")

    def test_invalid_resume_id_cannot_build_a_foreign_url(self):
        cfg = Config(search_url="")
        with self.assertRaises(ValueError):
            app.select_account_resume(cfg, "../another?resume=bad")
        self.assertEqual(cfg.resume_url, "")


class ReadAccountResumes(unittest.TestCase):
    def test_list_uses_visible_login_without_auto_raise_and_keeps_cfg_unmodified(self):
        cfg = Config(search_url="")
        cfg.browser.headless = True
        cfg.browser.hide_window = True
        result = [Resume("stale", "Старое резюме")]
        page = Mock()

        def session(session_cfg, root, work, auto_raise=True, login=True):
            self.assertIsNot(session_cfg, cfg)
            self.assertFalse(session_cfg.browser.headless)
            self.assertFalse(session_cfg.browser.hide_window)
            self.assertFalse(auto_raise)
            self.assertTrue(login)
            session_cfg.browser.profile_dir = "mutated-profile-path"
            work(page)
            return app.EXIT_OK

        listed = [Resume("first123", "Разработчик", "Личные данные из карточки"),
                  Resume("second456", "Разработчик", "Другие данные")]
        with patch.object(app, "_with_browser", side_effect=session), \
                patch.object(app.raising, "list_resumes", return_value=listed) as read:
            self.assertEqual(app.fetch_account_resumes(cfg, Path("unused"), Path("unused.log"), result), app.EXIT_OK)
        self.assertEqual([(item.id, item.title) for item in result], [("first123", "Разработчик"), ("second456", "Разработчик")])
        self.assertEqual([item.text for item in result], ["", ""])
        self.assertTrue(cfg.browser.headless)
        self.assertTrue(cfg.browser.hide_window)
        self.assertEqual(cfg.browser.profile_dir, "browser-profile")
        read.assert_called_once_with(page)

    def test_failed_or_cancelled_login_clears_previous_list(self):
        result = [Resume("stale", "Старое резюме")]
        with patch.object(app, "_with_browser", return_value=app.EXIT_STOPPED), patch.object(app.raising, "list_resumes") as read:
            self.assertEqual(app.fetch_account_resumes(Config(search_url=""), Path("unused"), Path("unused.log"), result),
                             app.EXIT_STOPPED)
        self.assertEqual(result, [])
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
