"""Optional UC backend contracts, without opening a browser or contacting a service."""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hh_auto.core import control
from hh_auto.core.config import Browser, Config, ConfigError, validate_config
from hh_auto.web import browser
from hh_auto.web import seleniumbase_browser as uc


class BackendConfigTest(unittest.TestCase):
    def test_default_and_explicit_backend_validation(self):
        self.assertEqual(Browser().backend, "playwright")
        cfg = Config(search_url="")
        cfg.browser.backend = "seleniumbase_uc"
        validate_config(cfg)
        cfg.browser.backend = "selenium"
        with self.assertRaisesRegex(ConfigError, "backend"):
            validate_config(cfg)

    def test_uc_cannot_silently_launch_another_browser(self):
        cfg = Config(search_url="")
        cfg.browser.backend = "seleniumbase_uc"
        cfg.browser.channel = "msedge"
        with self.assertRaisesRegex(ConfigError, "channel"):
            validate_config(cfg)


class LauncherProtocolTest(unittest.TestCase):
    def setUp(self):
        control.reset()
        self.addCleanup(control.reset)

    def test_cdp_endpoint_must_be_local_without_embedded_credentials(self):
        self.assertEqual(uc.checked_endpoint("http://127.0.0.1:1234"), "http://127.0.0.1:1234")
        for value in ("http://external.invalid:1234", "http://user:secret@localhost:1234",
                      "file:///profile", "http://localhost:1234/other", "http://localhost:broken"):
            with self.subTest(value=value), self.assertRaises(uc.UcLaunchError):
                uc.checked_endpoint(value)

    def test_worker_owns_sb_until_stop_and_opens_only_blank_page(self):
        events = []
        sb = Mock()
        sb.get_endpoint_url.return_value = "http://127.0.0.1:1234"

        class Manager:
            def __enter__(self):
                events.append("entered")
                return sb

            def __exit__(self, *_args):
                events.append("closed")

        factory = Mock(return_value=Manager())

        def emit(message):
            self.assertEqual(events, ["entered"])
            self.assertEqual(message, {"status": "ready", "endpoint": "http://127.0.0.1:1234"})
            events.append("ready")

        uc._serve({"profile": "/data/uc-profile", "virtual_display": True}, io.StringIO("stop\n"),
                  emit, factory)
        self.assertEqual(events, ["entered", "ready", "closed"])
        sb.activate_cdp_mode.assert_called_once_with("about:blank")
        opts = factory.call_args.kwargs
        self.assertTrue(opts["uc"])
        self.assertTrue(opts["uc_subprocess"])
        self.assertTrue(opts["xvfb"])
        self.assertFalse(opts["headless"])
        self.assertEqual(opts["user_data_dir"], "/data/uc-profile")

    def test_worker_exits_on_parent_eof_too(self):
        manager = Mock()
        manager.__enter__ = Mock(return_value=Mock(get_endpoint_url=lambda: "http://localhost:1234"))
        manager.__exit__ = Mock()
        uc._serve({"profile": "/data/uc"}, io.StringIO(""), Mock(), Mock(return_value=manager))
        manager.__exit__.assert_called_once()

    def test_worker_gracefully_closes_chrome_before_seleniumbase_teardown(self):
        events = []
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        process = Mock(pid=712345)
        process.poll.return_value = None
        process.wait.side_effect = lambda **_kwargs: events.append("chrome-exited")

        async def send(command):
            self.assertEqual(command, "Browser.close")
            events.append("browser-close")

        sb = Mock()
        sb.driver.browser_process = process
        sb.driver.browser_pid = process.pid
        sb.cdp.loop = loop
        sb.cdp.driver.connection.send = send
        sb.get_endpoint_url.return_value = "http://127.0.0.1:1234"

        class Manager:
            def __enter__(self):
                return sb

            def __exit__(self, *_args):
                events.append("sb-teardown")

        protocol = types.SimpleNamespace(browser=types.SimpleNamespace(close=lambda: "Browser.close"))
        with patch.dict(uc.sys.modules, {"mycdp": protocol}):
            uc._serve({"profile": "/data/uc"}, io.StringIO("stop\n"), Mock(), lambda **_kwargs: Manager())
        self.assertEqual(events, ["browser-close", "chrome-exited", "sb-teardown"])
        process.wait.assert_called_once_with(timeout=5)
        self.assertFalse(hasattr(sb.driver, "browser_pid"))
        self.assertTrue(sb.driver._already_quit)

    def test_graceful_close_waits_for_chrome_even_when_browser_close_disconnects(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        sb = Mock()
        sb.driver.browser_process.poll.return_value = None
        sb.cdp.loop = loop

        async def send(_command):
            raise ConnectionError("synthetic closed connection")

        sb.cdp.driver.connection.send = send
        protocol = types.SimpleNamespace(browser=types.SimpleNamespace(close=lambda: "Browser.close"))
        with patch.dict(uc.sys.modules, {"mycdp": protocol}):
            self.assertTrue(uc._graceful_close(sb))
        sb.driver.browser_process.wait.assert_called_once_with(timeout=5)

    def test_graceful_close_timeout_returns_to_bounded_existing_cleanup(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        sb = Mock()
        sb.driver.browser_process.poll.return_value = None
        sb.driver.browser_process.wait.side_effect = subprocess.TimeoutExpired("chrome", 5)
        sb.cdp.loop = loop

        async def send(_command):
            return None

        sb.cdp.driver.connection.send = send
        protocol = types.SimpleNamespace(browser=types.SimpleNamespace(close=lambda: "Browser.close"))
        with patch.dict(uc.sys.modules, {"mycdp": protocol}):
            self.assertFalse(uc._graceful_close(sb))
        sb.driver.browser_process.wait.assert_called_once_with(timeout=5)

    def test_window_arguments_are_not_broken_at_commas(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = str(Path(directory) / "chrome")
            options = uc._sb_options({"profile": "/data/uc", "args": ["--window-position=48000,48000",
                                     "--window-size=1440,900"], "binary_location": binary,
                                     "driver_version": "158.0.8127.36"})
        self.assertEqual(options["window_position"], "48000,48000")
        self.assertNotIn("window-position", options["chromium_arg"])
        self.assertEqual(options["binary_location"], binary)
        self.assertEqual(options["driver_version"], "keep")

    def test_pinned_cft_reuses_installed_driver_and_resolves_legacy_alias(self):
        payload = {"profile": "/data/uc", "binary_location": "cft", "driver_version": "158.0.8127.36"}
        with tempfile.TemporaryDirectory() as directory:
            binary = str(Path(directory) / "chrome-for-testing")
            with patch.object(uc.shutil, "which", return_value=binary):
                options = uc._sb_options(payload)
        self.assertEqual(options["binary_location"], binary)
        self.assertEqual(options["driver_version"], "keep")
        self.assertEqual(payload["driver_version"], "158.0.8127.36")
        with patch.object(uc.shutil, "which", return_value=None):
            with self.assertRaisesRegex(uc.UcLaunchError, "Chrome for Testing"):
                uc._sb_options(payload)

    @unittest.skipUnless(os.name == "posix", "POSIX symlink fixture")
    def test_browser_shortcut_resolves_to_accepted_chrome_executable_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "cft_drivers" / "chrome-linux64" / "chrome"
            target.parent.mkdir(parents=True)
            target.write_text("synthetic executable", encoding="utf-8")
            shortcut = root / "chrome-for-testing"
            shortcut.symlink_to(target)
            options = uc._sb_options({"profile": str(root / "profile"), "binary_location": str(shortcut),
                                      "driver_version": "158.0.8127.36"})
            self.assertEqual(options["binary_location"], str(target.resolve()))
            self.assertEqual(Path(options["binary_location"]).name, "chrome")
            self.assertEqual(options["driver_version"], "keep")
            with patch.object(uc.shutil, "which", return_value=str(shortcut)):
                aliased = uc._sb_options({"profile": str(root / "profile"), "binary_location": "cft"})
            self.assertEqual(aliased["binary_location"], str(target.resolve()))

    def test_private_configuration_travels_over_stdin_not_argv(self):
        cfg = Config(search_url="")
        cfg.browser.headless = True
        cfg.geo.proxy = "http://synthetic-user:secret@127.0.0.1:8080"
        process = Mock(stdin=io.StringIO())
        with patch.object(uc.sys, "platform", "linux"), patch.object(uc.shutil, "which", return_value="Xvfb"), \
                patch.object(uc.importlib.util, "find_spec", return_value=object()), \
                patch.object(uc.subprocess, "Popen", return_value=process) as popen, \
                patch.object(uc, "UcProcess"):
            uc.start(cfg, Path("uc-profile"), sandbox=False, args=[])
        self.assertNotIn("synthetic-user:secret", str(popen.call_args))
        sent = json.loads(process.stdin.getvalue())
        self.assertEqual(sent["proxy"], cfg.geo.proxy)
        self.assertTrue(sent["virtual_display"])
        self.assertIs(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_uc_headless_on_windows_fails_before_spawning(self):
        cfg = Config(search_url="")
        cfg.browser.headless = True
        with patch.object(uc.sys, "platform", "win32"), patch.object(uc.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(uc.UcLaunchError, "Xvfb"):
                uc.start(cfg, Path("uc-profile"), sandbox=True, args=[])
        popen.assert_not_called()

    def test_protocol_error_does_not_leak_child_details(self):
        process = Mock(pid=2147483646, stdout=io.StringIO(uc.PROTOCOL + json.dumps({"status": "error", "code": "browser",
                              "detail": "synthetic-private"}) + "\n"), stdin=io.StringIO())
        process.poll.return_value = 0
        owner = uc.UcProcess(process)
        with self.assertRaises(uc.UcLaunchError) as error:
            owner.endpoint(timeout=1)
        self.assertNotIn("synthetic-private", str(error.exception))
        owner.close()

    def test_worker_sanitizes_system_exit_during_import_or_browser_startup(self):
        original_import = builtins.__import__
        module = types.ModuleType("seleniumbase")
        module.SB = Mock()
        for stage in ("import", "startup"):
            with self.subTest(stage=stage):
                output = io.StringIO()

                def import_module(name, *args, **kwargs):
                    if stage == "import" and name == "seleniumbase":
                        raise SystemExit("synthetic-private-proxy")
                    return original_import(name, *args, **kwargs)

                with patch.dict(uc.sys.modules, {"seleniumbase": module}), \
                        patch.dict(os.environ, {"TZ": "UTC"}), \
                        patch.object(uc.sys, "stdin", io.StringIO('{"profile":"/tmp/synthetic"}\n')), \
                        patch.object(uc.sys, "stdout", output), \
                        patch.object(builtins, "__import__", side_effect=import_module), \
                        patch.object(uc, "_serve", side_effect=SystemExit("synthetic-private-proxy")):
                    self.assertEqual(uc._worker(), 1)
                self.assertEqual(output.getvalue(), uc.PROTOCOL + '{"status": "error", "code": "browser"}\n')

    def test_worker_keeps_keyboard_interrupt_interruptible(self):
        module = types.ModuleType("seleniumbase")
        module.SB = Mock()
        output = io.StringIO()
        with patch.dict(uc.sys.modules, {"seleniumbase": module}), \
                patch.dict(os.environ, {"TZ": "UTC"}), \
                patch.object(uc.sys, "stdin", io.StringIO('{"profile":"/tmp/synthetic"}\n')), \
                patch.object(uc.sys, "stdout", output), \
                patch.object(uc, "_serve", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                uc._worker()
        self.assertEqual(output.getvalue(), "")

    def test_owned_process_shutdown_is_graceful_and_idempotent(self):
        process = Mock(pid=2147483646, stdout=io.StringIO(""), stdin=io.StringIO())
        process.poll.side_effect = [None, 0, 0]
        owner = uc.UcProcess(process)
        with patch.object(owner, "_terminate_group") as terminate, \
                patch.object(owner, "_cleanup_posix_group"):
            owner.close()
            owner.close()
        process.wait.assert_called_once_with(timeout=uc.STOP_TIMEOUT)
        terminate.assert_not_called()

    def test_hung_owned_process_group_is_terminated(self):
        process = Mock(pid=2147483646, stdout=io.StringIO(""), stdin=io.StringIO())
        process.poll.side_effect = [None, None, None]
        process.wait.side_effect = [subprocess.TimeoutExpired("worker", uc.STOP_TIMEOUT), 0]
        owner = uc.UcProcess(process)
        with patch.object(owner, "_terminate_group") as terminate, patch.object(uc.os, "name", "nt"):
            owner.close()
        terminate.assert_called_once()

    def test_exited_worker_still_releases_owned_posix_descendant_group(self):
        process = Mock(pid=712345, stdout=io.StringIO(""), stdin=io.StringIO())
        process.poll.return_value = 1
        owner = uc.UcProcess(process)
        with patch.object(uc.os, "name", "posix"), \
                patch.object(uc.os, "killpg", create=True) as killpg, \
                patch.object(owner, "_wait_group_exit", return_value=True):
            owner.close()
        self.assertEqual(killpg.call_args_list, [unittest.mock.call(712345, 0),
                                               unittest.mock.call(712345, uc.signal.SIGTERM)])
        process.wait.assert_not_called()

    def test_graceful_worker_exit_also_releases_remaining_group(self):
        process = Mock(pid=712345, stdout=io.StringIO(""), stdin=io.StringIO())
        process.poll.side_effect = [None, 0, 0]
        owner = uc.UcProcess(process)
        with patch.object(uc.os, "name", "posix"), \
                patch.object(uc.os, "killpg", create=True) as killpg, \
                patch.object(owner, "_wait_group_exit", return_value=True):
            owner.close()
        process.wait.assert_called_once_with(timeout=uc.STOP_TIMEOUT)
        killpg.assert_any_call(712345, uc.signal.SIGTERM)

    def test_empty_group_after_worker_exit_is_not_signalled(self):
        process = Mock(pid=712345, stdout=io.StringIO(""), stdin=io.StringIO())
        process.poll.return_value = 0
        owner = uc.UcProcess(process)
        with patch.object(uc.os, "name", "posix"), \
                patch.object(uc.os, "killpg", side_effect=ProcessLookupError, create=True) as killpg:
            owner.close()
            owner.close()
        killpg.assert_called_once_with(712345, 0)

    def test_remaining_owned_group_is_killed_after_bounded_term_wait(self):
        process = Mock(pid=712345, stdout=io.StringIO(""), stdin=io.StringIO())
        process.poll.return_value = 0
        owner = uc.UcProcess(process)
        with patch.object(uc.os, "name", "posix"), \
                patch.object(uc.os, "killpg", create=True) as killpg, \
                patch.object(uc.signal, "SIGKILL", 9, create=True), \
                patch.object(owner, "_wait_group_exit", side_effect=[False, True]) as wait:
            owner.close()
        self.assertEqual(killpg.call_args_list, [unittest.mock.call(712345, 0),
                         unittest.mock.call(712345, uc.signal.SIGTERM), unittest.mock.call(712345, 9)])
        self.assertEqual(wait.call_args_list, [unittest.mock.call(2), unittest.mock.call(1)])

    def test_real_worker_isolated_from_parent_event_loop_and_closes_on_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "seleniumbase"
            package.mkdir()
            marker = root / "closed.txt"
            (package / "__init__.py").write_text(
                "import asyncio, os\nfrom contextlib import contextmanager\nfrom pathlib import Path\n"
                "class Browser:\n"
                "    def activate_cdp_mode(self, url):\n"
                "        assert url == 'about:blank'\n"
                "        asyncio.run(asyncio.sleep(0))\n"
                "    def get_endpoint_url(self): return 'http://127.0.0.1:1234'\n"
                "@contextmanager\ndef SB(**kwargs):\n"
                "    try: yield Browser()\n"
                "    finally: Path(os.environ['HH_UC_TEST_CLOSED']).write_text('closed')\n",
                encoding="utf-8")
            cfg = Config(search_url="")
            cfg.browser.headless = False

            async def while_loop_is_running():
                owner = uc.start(cfg, root / "profile", sandbox=True, args=[])
                try:
                    self.assertEqual(owner.endpoint(timeout=10), "http://127.0.0.1:1234")
                    self.assertFalse(marker.exists())
                finally:
                    owner.close()
                self.assertEqual(owner.process.returncode, 0)

            with patch.dict(os.environ, {"PYTHONPATH": str(root), "HH_UC_TEST_CLOSED": str(marker)}), \
                    patch.object(uc.importlib.util, "find_spec", return_value=object()):
                asyncio.run(while_loop_is_running())
            self.assertEqual(marker.read_text(), "closed")


class BrowserBridgeTest(unittest.TestCase):
    def setUp(self):
        control.reset()
        self.addCleanup(control.reset)
        self.addCleanup(browser._uc_owners.clear)
        self.addCleanup(browser._uc_page_sessions.clear)
        self.cfg = Config(search_url="")
        self.cfg.browser.backend = "seleniumbase_uc"
        self.cfg.browser.headless = True
        self.cfg.browser.profile_dir = "nonexistent-uc-test-profile"
        self.pw = Mock()
        self.context = Mock(pages=[Mock()])
        self.context.pages[0].evaluate.return_value = True
        self.pw.chromium.connect_over_cdp.return_value.contexts = [self.context]
        self.owner = Mock()
        self.owner.endpoint.return_value = "http://127.0.0.1:1234"

    def test_existing_default_context_is_restored_and_closed_with_its_owner(self):
        with patch.object(uc, "start", return_value=self.owner), \
                patch.object(browser, "restore_session", return_value=True) as restore, \
                patch.object(browser, "_watch_close"):
            result = browser.launch(self.pw, self.cfg)
            self.assertIs(result, self.context)
            self.pw.chromium.launch_persistent_context.assert_not_called()
            restore.assert_called_once_with(self.context, fresh_profile=True, force=False)
            self.context.pages[0].set_viewport_size.assert_not_called()
            browser.close_context(result)
        self.context.close.assert_called_once()
        self.owner.close.assert_called_once()

    def test_context_close_event_also_releases_external_owner(self):
        with patch.object(uc, "start", return_value=self.owner), \
                patch.object(browser, "restore_session", return_value=False), patch.object(browser, "_watch_close"):
            browser.launch(self.pw, self.cfg)
        events = {args[0]: args[1] for args, _kwargs in self.context.on.call_args_list}
        events["close"]()
        browser._close_uc_owner(self.context)
        self.owner.close.assert_called_once()

    def test_existing_sb_locale_is_not_overridden_again_for_existing_or_new_pages(self):
        with patch.object(uc, "start", return_value=self.owner), \
                patch.object(browser, "restore_session", return_value=False), patch.object(browser, "_watch_close"):
            browser.launch(self.pw, self.cfg)
        events = {args[0]: args[1] for args, _kwargs in self.context.on.call_args_list}
        new_page = Mock()
        new_page.evaluate.return_value = True
        events["page"](new_page)
        sent = self.context.new_cdp_session.return_value.send.call_args_list
        self.assertFalse(any(call.args[0] == "Emulation.setLocaleOverride" for call in sent))
        self.assertEqual(sum(call.args[0] == "Emulation.setTimezoneOverride" for call in sent), 2)
        self.assertEqual(new_page.evaluate.call_args.args[1], self.cfg.geo.locale)
        self.context.pages[0].set_viewport_size.assert_not_called()
        new_page.set_viewport_size.assert_not_called()
        self.assertFalse(any(call.args[0] == "Emulation.setDeviceMetricsOverride" for call in sent))

    def test_unconfigured_locale_is_set_once_and_verified(self):
        self.context.pages[0].evaluate.side_effect = [False, True]
        with patch.object(uc, "start", return_value=self.owner), \
                patch.object(browser, "restore_session", return_value=False), patch.object(browser, "_watch_close"):
            browser.launch(self.pw, self.cfg)
        sent = self.context.new_cdp_session.return_value.send.call_args_list
        self.assertEqual(sum(call.args[0] == "Emulation.setLocaleOverride" for call in sent), 1)
        self.assertEqual(self.context.pages[0].evaluate.call_count, 2)

    def test_locale_mismatch_cannot_silently_continue(self):
        self.context.pages[0].evaluate.return_value = False
        with patch.object(uc, "start", return_value=self.owner):
            with self.assertRaisesRegex(browser.FatalError, "локаль"):
                browser.launch(self.pw, self.cfg)
        self.owner.close.assert_called()

    def test_new_page_locale_session_remains_attached_until_that_page_closes(self):
        first_session, new_session = Mock(), Mock()
        self.context.new_cdp_session.side_effect = [first_session, new_session]
        with patch.object(uc, "start", return_value=self.owner), \
                patch.object(browser, "restore_session", return_value=False), patch.object(browser, "_watch_close"):
            browser.launch(self.pw, self.cfg)
        events = {args[0]: args[1] for args, _kwargs in self.context.on.call_args_list}
        page = Mock()
        locale = {"value": "en-US"}
        page.evaluate.side_effect = lambda *_args: locale["value"] == self.cfg.geo.locale

        def send(method, params):
            if method == "Emulation.setLocaleOverride":
                locale["value"] = params["locale"]

        new_session.send.side_effect = send
        new_session.detach.side_effect = lambda: locale.update(value="en-US")
        events["page"](page)
        self.assertTrue(page.evaluate("locale readback"))
        new_session.detach.assert_not_called()
        first_session.detach.assert_not_called()
        closed = {args[0]: args[1] for args, _kwargs in page.on.call_args_list}
        closed["close"]()
        self.assertEqual(locale["value"], "en-US")
        new_session.detach.assert_called_once()
        browser.close_context(self.context)
        first_session.detach.assert_called_once()
        new_session.detach.assert_called_once()
        self.assertNotIn(id(self.context), browser._uc_page_sessions)

    def test_failed_page_setup_detaches_its_session_and_releases_owner(self):
        session = self.context.new_cdp_session.return_value
        session.send.side_effect = RuntimeError("synthetic setup failure")
        session.detach.side_effect = RuntimeError("already closed")
        with patch.object(uc, "start", return_value=self.owner):
            with self.assertRaisesRegex(RuntimeError, "setup failure"):
                browser.launch(self.pw, self.cfg)
        session.detach.assert_called_once()
        self.owner.close.assert_called()
        self.assertNotIn(id(self.context), browser._uc_page_sessions)

    def test_cdp_failure_cleans_up_owner_without_browser_channel_fallback(self):
        self.pw.chromium.connect_over_cdp.side_effect = RuntimeError("synthetic connection failure")
        with patch.object(uc, "start", return_value=self.owner):
            with self.assertRaises(RuntimeError):
                browser.launch(self.pw, self.cfg)
        self.owner.close.assert_called_once()
        self.pw.chromium.launch_persistent_context.assert_not_called()

    def test_context_close_failure_still_releases_owner(self):
        browser._uc_owners[id(self.context)] = self.owner
        self.context.close.side_effect = RuntimeError("close failed")
        with self.assertRaises(RuntimeError):
            browser.close_context(self.context)
        self.owner.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
