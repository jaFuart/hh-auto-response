"""Persistent submission reservations and CAPTCHA cooldowns, independent of browser delivery."""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

FILE = "submission-pacing.json"
MAX_STATE_BYTES = 65_536
WAIT_SLICE = 30
_LOCKS: dict[str, threading.RLock] = {}


class PacingStateError(Exception):
    """State cannot be safely read or committed; submission must not proceed."""


@dataclass(frozen=True)
class PacingStatus:
    ready_at: float
    wait_seconds: float
    reason: str
    batch_count: int = 0
    last_attempt_at: float = 0.0
    captcha_until: float = 0.0
    batch_until: float = 0.0
    submit_until: float = 0.0


class PacingBlocked(Exception):
    def __init__(self, status: PacingStatus):
        self.status = status
        super().__init__(f"отправка отложена: {status.reason}, ещё {math.ceil(status.wait_seconds)} с")


def _duration(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name}: нужно конечное неотрицательное число")
    return float(value)


def _empty_state() -> dict:
    return {"version": 1, "last_attempt_at": 0.0, "submit_until": 0.0,
            "batch_count": 0, "batch_until": 0.0, "captcha_until": 0.0}


class SubmissionPacer:
    def __init__(self, path: Path, *, submit_interval_seconds: float, batch_size: int,
                 batch_pause_seconds: float, captcha_cooldown_seconds: float, clock=None):
        self.path = Path(path)
        self.interval = _duration(submit_interval_seconds, "submit_interval_seconds")
        self.batch_pause = _duration(batch_pause_seconds, "batch_pause_seconds")
        self.captcha_cooldown = _duration(captcha_cooldown_seconds, "captcha_cooldown_seconds")
        if type(batch_size) is not int or batch_size < 0:
            raise ValueError("batch_size: нужно неотрицательное целое число")
        self.batch_size = batch_size
        self.clock = clock or time.time
        self._thread_lock = _LOCKS.setdefault(str(self.path.resolve()), threading.RLock())

    def _now(self) -> float:
        try:
            return _duration(self.clock(), "clock")
        except (TypeError, ValueError, OverflowError) as error:
            raise PacingStateError("не удалось определить время для лимита отправок") from error

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            stream = None
            locked = False
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                stream = self.path.with_name(self.path.name + ".lock").open("a+b")
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0, os.SEEK_END)
                    if not stream.tell():
                        stream.write(b"\0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as error:
                if stream is not None:
                    stream.close()
                raise PacingStateError("не удалось заблокировать состояние лимита отправок") from error
            try:
                yield
            finally:
                if stream is not None:
                    if locked:
                        with suppress(OSError):
                            if os.name == "nt":
                                stream.seek(0)
                                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                            else:
                                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    stream.close()

    def _load(self) -> dict:
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError("oversized state")
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _empty_state()
        except (OSError, UnicodeError, ValueError) as error:
            raise PacingStateError("не удалось прочитать состояние лимита отправок; отправка остановлена") from error
        try:
            if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1:
                raise ValueError("unknown version")
            for key in ("last_attempt_at", "submit_until", "batch_until", "captcha_until"):
                _duration(state[key], key)
            if type(state["batch_count"]) is not int or state["batch_count"] < 0:
                raise ValueError("invalid count")
            if state["submit_until"] < state["last_attempt_at"]:
                raise ValueError("invalid deadline")
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise PacingStateError("состояние лимита отправок повреждено; отправка остановлена") from error
        return state

    def _save(self, state: dict) -> None:
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".submission-pacing-", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                if os.name == "posix":
                    os.fchmod(stream.fileno(), 0o600)
                json.dump(state, stream, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError) as error:
            raise PacingStateError("не удалось сохранить лимит отправок; отправка остановлена") from error
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def _status(self, state: dict, now: float) -> PacingStatus:
        deadline, reason = max((state["submit_until"], "submit_interval"),
                               (state["batch_until"], "batch_pause"),
                               (state["captcha_until"], "captcha_cooldown"),
                               key=lambda item: (item[0], {"submit_interval": 0, "batch_pause": 1,
                                                           "captcha_cooldown": 2}[item[1]]))
        return PacingStatus(max(now, float(deadline)), max(0.0, float(deadline) - now),
                            reason if deadline > now else "ready", state["batch_count"],
                            float(state["last_attempt_at"]), float(state["captcha_until"]),
                            float(state["batch_until"]), float(state["submit_until"]))

    def status(self) -> PacingStatus:
        with self._locked():
            return self._status(self._load(), self._now())

    def peek_status(self) -> PacingStatus:
        """Read the atomically replaced state without creating directories or a lock file."""
        return self._status(self._load(), self._now())

    def reserve_attempt(self) -> PacingStatus:
        """Commit before the final submit click. Failures and restarts never refund this attempt."""
        with self._locked():
            state, now = self._load(), self._now()
            status = self._status(state, now)
            if status.wait_seconds:
                raise PacingBlocked(status)
            if state["batch_until"] <= now:
                if state["batch_until"] or (self.batch_pause and state["batch_count"]
                                           and now - state["last_attempt_at"] >= self.batch_pause):
                    state["batch_count"] = 0
                state["batch_until"] = 0.0
            state["last_attempt_at"] = now
            state["submit_until"] = now + self.interval
            if self.batch_size and self.batch_pause:
                state["batch_count"] += 1
                if state["batch_count"] >= self.batch_size:
                    state["batch_until"] = now + self.batch_pause
            else:
                state["batch_count"] = 0
            self._save(state)
            return self._status(state, now)

    def record_captcha(self) -> PacingStatus:
        """Persist the first encounter before any wait/error; repeated active observations do not extend it."""
        with self._locked():
            state, now = self._load(), self._now()
            if self.captcha_cooldown and state["captcha_until"] <= now:
                state["captcha_until"] = now + self.captcha_cooldown
                state["batch_count"] = 0
                self._save(state)
            return self._status(state, now)

    def wait_until_ready(self, *, sleep=None) -> PacingStatus:
        """Optional interruptible waiting; the scheduler may instead defer directly to status().ready_at."""
        if sleep is None:
            from ..core import control

            sleep = control.sleep
        while True:
            status = self.status()
            if not status.wait_seconds:
                return status
            sleep(min(WAIT_SLICE, status.wait_seconds))

    def wait_and_reserve(self, *, sleep=None) -> PacingStatus:
        while True:
            self.wait_until_ready(sleep=sleep)
            try:
                return self.reserve_attempt()
            except PacingBlocked:
                continue  # A second owner reserved after our readiness check.
