"""Portable HH-only session state and fresh-profile import policy; no browser or network."""

from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hh_auto.core.config import Config
from hh_auto.web import auth, browser, session
from scripts import session as command


def state():
    return {"cookies": [{"name": "hhrole", "value": "applicant", "domain": ".hh.ru", "path": "/",
                          "expires": -1, "httpOnly": False, "secure": True, "sameSite": "Lax"},
                         {"name": "test-session", "value": "synthetic-token", "domain": "hh.ru", "path": "/",
                          "expires": -1, "httpOnly": True, "secure": True, "sameSite": "None"}],
            "origins": [{"origin": "https://hh.ru", "localStorage": [{"name": "test", "value": "synthetic-state"}]}]}


class PortableSessionTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "data" / "hh-session.json"
        self.context = Mock()
        self.context.storage_state.return_value = state()

    def test_export_filters_other_sites_and_restores_cookies_and_local_storage(self):
        original = state()
        original["cookies"].append(original["cookies"][1] | {"domain": "other.invalid"})
        original["origins"].append({"origin": "https://other.invalid", "localStorage": []})
        self.context.storage_state.return_value = original
        self.assertEqual(session.save_session(self.context, self.path), {"cookies": 2, "origins": 1})
        restored = Mock()
        self.assertTrue(session.restore_session(restored, fresh_profile=True, path=self.path))
        restored.set_storage_state.assert_called_once_with(state())
        self.assertEqual(len(original["cookies"]), 3)  # serialization does not mutate the live browser state

    def test_existing_profile_is_not_overwritten_even_with_an_available_snapshot(self):
        session.save_session(self.context, self.path)
        with patch.object(session, "read_snapshot") as read:
            self.assertFalse(session.restore_session(self.context, fresh_profile=False, path=self.path))
        read.assert_not_called()
        self.context.set_storage_state.assert_not_called()

    def test_existing_profile_can_be_restored_only_explicitly(self):
        session.save_session(self.context, self.path)
        self.assertTrue(session.restore_session(self.context, fresh_profile=False, force=True, path=self.path))
        self.context.set_storage_state.assert_called_once_with(state())

    def test_missing_file_is_optional_for_fresh_profiles_and_required_for_forced_restore(self):
        self.assertFalse(session.restore_session(self.context, fresh_profile=True, path=self.path))
        with self.assertRaisesRegex(session.SessionError, "не найден"):
            session.restore_session(self.context, fresh_profile=False, force=True, path=self.path)

    def test_import_rejects_foreign_origins_and_lookalike_domains(self):
        for cookie_domain, origin in (("hh.ru.other.invalid", "https://hh.ru"),
                                      ("hh.ru", "https://hh.ru.other.invalid"),
                                      ("hh.ru", "https://user@hh.ru"), ("hh.ru", "https://hh.ru/redirect")):
            with self.subTest(cookie_domain=cookie_domain, origin=origin):
                value = state()
                value["cookies"][0]["domain"] = cookie_domain
                value["origins"][0]["origin"] = origin
                with self.assertRaises(session.SessionError):
                    session.validate_state(value)

    def test_expired_or_non_applicant_state_is_not_accepted(self):
        for value in ("anonymous", "employer"):
            snapshot = state()
            snapshot["cookies"][0]["value"] = value
            with self.assertRaisesRegex(session.SessionError, "соискателя"):
                session.validate_state(snapshot)
        snapshot = state()
        snapshot["cookies"][0]["expires"] = 1
        with self.assertRaisesRegex(session.SessionError, "соискателя"):
            session.validate_state(snapshot)

    def test_bad_json_fails_without_exposing_values(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("synthetic-secret-not-json", encoding="utf-8")
        with self.assertRaises(session.SessionError) as error:
            session.read_snapshot(self.path)
        self.assertNotIn("synthetic-secret", str(error.exception))

    def test_failed_atomic_replace_keeps_previous_snapshot_and_removes_temporary_file(self):
        session.save_session(self.context, self.path)
        original = self.path.read_bytes()
        self.context.storage_state.return_value = state() | {"origins": []}
        with patch.object(session.os, "replace", side_effect=PermissionError("test")):
            with self.assertRaises(session.SessionError):
                session.save_session(self.context, self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".hh-session-*.tmp")), [])

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_snapshot_has_private_posix_mode(self):
        session.save_session(self.context, self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_older_playwright_reports_a_clear_error(self):
        session.save_session(self.context, self.path)
        old_context = Mock(spec=[])
        with self.assertRaisesRegex(session.SessionError, "1.59"):
            session.restore_session(old_context, fresh_profile=True, path=self.path)


class SessionIntegrationTest(unittest.TestCase):
    def test_verified_auth_refreshes_snapshot_and_anonymous_auth_does_not(self):
        page = Mock(url=auth.RESUMES_URL)
        page.locator.return_value.count.return_value = 0
        with patch.object(auth, "open_url"), patch.object(auth, "wait_captcha"), \
                patch.object(auth, "save_session") as save:
            page.context.cookies.return_value = [{"name": "hhrole", "value": "applicant"}]
            self.assertTrue(auth.is_logged_in(page))
            save.assert_called_once_with(page.context)
            save.reset_mock()
            page.context.cookies.return_value = [{"name": "hhrole", "value": "anonymous"}]
            self.assertFalse(auth.is_logged_in(page))
            save.assert_not_called()

    def test_snapshot_write_failure_does_not_invalidate_a_verified_browser_login(self):
        page = Mock(url=auth.RESUMES_URL)
        page.locator.return_value.count.return_value = 0
        page.context.cookies.return_value = [{"name": "hhrole", "value": "applicant"}]
        with patch.object(auth, "open_url"), patch.object(auth, "wait_captcha"), \
                patch.object(auth, "save_session", side_effect=session.SessionError("safe error")):
            self.assertTrue(auth.is_logged_in(page))

    def test_check_command_is_headless_readonly_and_never_raises_resume(self):
        cfg = Config(search_url="")
        with patch.object(command, "load_config", return_value=cfg), patch.object(command, "setup_logging"), \
                patch.object(command, "_with_browser", return_value=0) as run:
            self.assertEqual(command.main(["check", "--data-dir", "unused", "--config", "server.toml"]), 0)
        self.assertTrue(run.call_args.args[0].browser.headless)
        self.assertFalse(run.call_args.kwargs["auto_raise"])
        self.assertFalse(run.call_args.kwargs["login"])


if __name__ == "__main__":
    unittest.main()
