"""A headless-shell-only Docker install must reach Playwright's default browser launcher."""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError

from hh_auto.core.config import Config
from hh_auto.web import browser


class HeadlessShellTest(unittest.TestCase):
    def launch(self, headless):
        cfg = Config(search_url="")
        cfg.browser.channel = ""
        cfg.browser.headless = headless
        cfg.browser.hide_window = False
        cfg.browser.profile_dir = str(Path.cwd() / ".tmp" / "headless-shell-test-profile")
        playwright, context = Mock(), Mock()

        def persistent(**options):
            if options["channel"] is not None:
                raise PlaywrightError("Executable doesn't exist")
            return context

        playwright.chromium.launch_persistent_context.side_effect = persistent
        with patch.object(browser, "_bundled_chromium", return_value=False), \
                patch.object(browser, "restore_session", return_value=False), \
                patch.object(browser, "_watch_close"), patch.object(browser.control, "check"), \
                patch.object(browser, "_cfg", None):
            if headless:
                self.assertIs(browser.launch(playwright, cfg), context)
            else:
                with self.assertRaises(browser.FatalError):
                    browser.launch(playwright, cfg)
        return [call.kwargs["channel"] for call in playwright.chromium.launch_persistent_context.call_args_list]

    def test_headless_launch_can_use_shell_without_full_chromium_path(self):
        self.assertEqual(self.launch(True), ["chrome", "msedge", None])

    def test_headed_launch_still_skips_missing_full_chromium(self):
        self.assertEqual(self.launch(False), ["chrome", "msedge"])


if __name__ == "__main__":
    unittest.main()
