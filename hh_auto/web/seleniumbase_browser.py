"""Owned SeleniumBase Chrome process, bridged to the existing Playwright page API."""

from __future__ import annotations

import contextlib
import asyncio
import importlib.util
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from ..core import control

PROTOCOL = "HH_UC\t"
START_TIMEOUT = 90.0
STOP_TIMEOUT = 10


class UcLaunchError(Exception):
    pass


def checked_endpoint(value: str) -> str:
    """The debugging socket must stay inside the current host/container."""
    try:
        url = urlparse(value)
        valid = (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")
                 and url.port is not None and not url.username and not url.password
                 and url.path in ("", "/") and not url.query and not url.fragment)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise UcLaunchError("SeleniumBase вернул недопустимый локальный CDP endpoint")
    return value


class UcProcess:
    """Own one launcher process group; never terminate unrelated Chrome instances."""

    def __init__(self, process):
        self.process = process
        self.messages: queue.Queue = queue.Queue()
        self.closed = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                if line.startswith(PROTOCOL):
                    try:
                        self.messages.put(json.loads(line[len(PROTOCOL):]))
                    except ValueError:
                        self.messages.put({"status": "error", "code": "protocol"})
        finally:
            self.messages.put({"status": "error", "code": "exited"})

    def endpoint(self, timeout: float = START_TIMEOUT) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            control.check()
            try:
                message = self.messages.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if isinstance(message, dict) and message.get("status") == "ready":
                return checked_endpoint(message.get("endpoint", ""))
            code = message.get("code", "protocol") if isinstance(message, dict) else "protocol"
            hints = {"dependency": "установите optional зависимости SeleniumBase",
                     "display": "для Linux без окна установите Xvfb и xauth",
                     "browser": "проверьте полный Chrome/CfT и совместимый UC driver",
                     "exited": "дочерний процесс завершился до запуска Chrome"}
            raise UcLaunchError("SeleniumBase UC не запустился: " + hints.get(code, "ошибка протокола запуска"))
        raise UcLaunchError("SeleniumBase UC не запустился за 90 секунд")

    def close(self):
        if self.closed:
            return
        self.closed = True
        process = self.process
        try:
            if process.poll() is None:
                with contextlib.suppress(OSError, ValueError):
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                with contextlib.suppress(OSError, ValueError):
                    process.stdin.close()
                try:
                    process.wait(timeout=STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    pass
            # The session leader can exit before its Chrome/Xvfb descendants.
            if os.name == "posix":
                self._cleanup_posix_group()
            elif process.poll() is None:
                self._terminate_group(signal.SIGTERM)
            if process.poll() is None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)
        finally:
            with contextlib.suppress(OSError, ValueError):
                process.stdin.close()
            self.reader.join(timeout=0.5)
            if not self.reader.is_alive():
                with contextlib.suppress(OSError, ValueError):
                    process.stdout.close()

    def _posix_group_exists(self):
        self.process.poll()  # Reap the leader so it does not look like a surviving group member.
        try:
            os.killpg(self.process.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _wait_group_exit(self, timeout):
        deadline = time.monotonic() + timeout
        while self._posix_group_exists():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
        return True

    def _cleanup_posix_group(self):
        if not self._posix_group_exists():
            return
        self._terminate_group(signal.SIGTERM)
        if not self._wait_group_exit(2):
            self._terminate_group(signal.SIGKILL)
            self._wait_group_exit(1)

    def _terminate_group(self, sig):
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            if os.name == "posix":
                os.killpg(self.process.pid, sig)
            else:
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, check=False, timeout=5)


def start(cfg, profile: Path, *, sandbox: bool, args: list[str]) -> UcProcess:
    """Run SB outside the event loop already owned by sync_playwright in the parent."""
    if cfg.browser.channel not in ("", "chrome"):
        raise UcLaunchError("SeleniumBase UC поддерживает только Chrome (channel = chrome или пусто)")
    if cfg.browser.headless and not sys.platform.startswith("linux"):
        raise UcLaunchError("SeleniumBase UC без видимого окна поддерживается на Linux через Xvfb; "
                            "для этой системы выберите playwright или headless = false")
    if importlib.util.find_spec("seleniumbase") is None:
        raise UcLaunchError("не установлен SeleniumBase — установите optional UC зависимости")
    if cfg.browser.headless and not shutil.which("Xvfb"):
        raise UcLaunchError("для SeleniumBase UC без окна на Linux нужен Xvfb")
    cfg.geo.playwright_proxy()  # Validate before passing credentials privately to the child.
    payload = {"profile": str(profile.resolve()), "virtual_display": bool(cfg.browser.headless),
               "locale": cfg.geo.locale, "timezone": cfg.geo.timezone,
               "proxy": cfg.geo.proxy, "sandbox": sandbox, "args": args,
               "binary_location": os.environ.get("HH_SB_BINARY_LOCATION", ""),
               "driver_version": os.environ.get("HH_SB_CHROME_VERSION", "")}
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    options = {"start_new_session": True} if os.name == "posix" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    process = subprocess.Popen([sys.executable, "-m", "hh_auto.web.seleniumbase_browser", "--worker"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", env=env, bufsize=1, **options)
    owner = UcProcess(process)
    try:
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()
    except BaseException:
        owner.close()
        raise
    return owner


def _sb_options(payload: dict) -> dict:
    binary_location = payload.get("binary_location") or None
    if binary_location == "cft":
        binary_location = shutil.which("chrome-for-testing")
        if not binary_location:
            raise UcLaunchError("Chrome for Testing не найден: установите закреплённый браузер при сборке")
    if binary_location and Path(binary_location).is_file():
        # SeleniumBase validates the executable basename; our installed shortcut has a custom name.
        binary_location = str(Path(binary_location).resolve())
    args = []
    window_position = None
    window_size = "1440,900"
    for arg in payload.get("args", []):
        value = arg.removeprefix("--")
        if value.startswith("window-position="):
            window_position = value.split("=", 1)[1]
        elif value.startswith("window-size="):
            window_size = value.split("=", 1)[1]
        else:
            args.append(value)
    args.append("remote-debugging-address=127.0.0.1")
    if not payload.get("sandbox", False):
        args.append("no-sandbox")
    return {"uc": True, "uc_subprocess": True, "browser": "chrome", "headless": False,
            "headed": True, "xvfb": bool(payload.get("virtual_display")),
            "xvfb_metrics": "1440,900", "window_size": window_size, "window_position": window_position,
            "user_data_dir": payload["profile"], "locale_code": payload.get("locale", "ru-RU"),
            "proxy": payload.get("proxy") or None, "chromium_arg": ",".join(args),
            "binary_location": binary_location,
            # SB compares the installed UC major with a supplied full version and needlessly downloads.
            # The image verifies the complete browser/driver pin at build time; reuse that installed driver.
            "driver_version": "keep" if payload.get("driver_version") else None,
            "test": False, "no_screenshot": True}


def _serve(payload, stream, emit, sb_factory):
    """Only about:blank is opened here. Account navigation stays in the existing caller."""
    with sb_factory(**_sb_options(payload)) as sb:
        sb.activate_cdp_mode("about:blank")
        endpoint = checked_endpoint(sb.get_endpoint_url())
        emit({"status": "ready", "endpoint": endpoint})
        try:
            for command in stream:
                if command.strip() == "stop":
                    break
        finally:
            _graceful_close(sb)


def _graceful_close(sb) -> bool:
    """Let Chrome flush its persistent profile before SeleniumBase's signal-based teardown."""
    process = getattr(getattr(sb, "driver", None), "browser_process", None)
    if process is None:
        return False

    def mark_exited():
        sb.driver._already_quit = True
        if getattr(sb.driver, "browser_pid", None) == process.pid:
            with contextlib.suppress(AttributeError):
                del sb.driver.browser_pid

    try:
        if process.poll() is not None:
            mark_exited()
            return True
        import mycdp

        loop = sb.cdp.loop
        if not loop.is_closed():
            with contextlib.suppress(Exception):
                loop.run_until_complete(asyncio.wait_for(
                    sb.cdp.driver.connection.send(mycdp.browser.close()), timeout=2))
        process.wait(timeout=5)
        mark_exited()
        return True
    except Exception:
        return False  # The existing SB teardown and owned process-group cleanup remain bounded fallbacks.


def _worker() -> int:
    output = sys.stdout

    def emit(value):
        output.write(PROTOCOL + json.dumps(value, ensure_ascii=False) + "\n")
        output.flush()

    try:
        payload = json.loads(sys.stdin.readline(65_537))
        with contextlib.redirect_stdout(sys.stderr):
            try:
                from seleniumbase import SB
            except ImportError:
                emit({"status": "error", "code": "dependency"})
                return 1
            if payload.get("virtual_display") and not shutil.which("Xvfb"):
                emit({"status": "error", "code": "display"})
                return 1
            os.environ["TZ"] = payload.get("timezone", "Europe/Moscow")
            if hasattr(time, "tzset"):
                time.tzset()
            _serve(payload, sys.stdin, emit, SB)
        return 0
    except (Exception, SystemExit):
        # Selenium/Chrome exceptions may contain proxy credentials or profile contents.
        emit({"status": "error", "code": "browser"})
        return 1


if __name__ == "__main__":
    raise SystemExit(_worker() if sys.argv[1:] == ["--worker"] else 2)
