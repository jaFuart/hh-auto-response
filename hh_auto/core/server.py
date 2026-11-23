"""Последовательный серверный запуск по календарю: без окна, с памятью запусков между перезапусками."""

from __future__ import annotations

import copy
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import control
from .config import Config, validate_search_source
from .logger import log
from .delivery import ApplicationDelivery, DeliveryError
from .rate_limits import EXIT_DEFERRED, confirmed_today, enabled as pacing_enabled, submission_pacer

DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_CATCH_UP_MINUTES = 60
STATE_FILE = "server-state.json"


def application_retries_due(root: Path, cfg: Config, now: datetime | None = None) -> bool:
    """Retry previously claimed vacancies only; disabling the application schedule also disables retries."""
    if not cfg.schedule.enabled:
        return False
    try:
        rows = ApplicationDelivery(root / "data" / "application-delivery.json").due(now=now)
    except DeliveryError as error:
        log.error(str(error))
        return False
    selected = cfg.resume_id or "(auto)"
    return any(row["resume_id"] == selected for row in rows)


class ServerError(Exception):
    pass


@dataclass(frozen=True)
class ResponseRunResult:
    code: int
    applied: int

    def __post_init__(self):
        if type(self.code) is not int or type(self.applied) is not int or self.applied < 0:
            raise ValueError("неверный результат запуска откликов")


def server_timezone(name: str = DEFAULT_TIMEZONE) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ServerError(f"неизвестный часовой пояс {name}; установите tzdata") from error


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ServerError("серверное время должно содержать часовой пояс")
    return value


def headless_config(cfg: Config, timezone_name: str = DEFAULT_TIMEZONE) -> Config:
    result = copy.deepcopy(cfg)
    result.browser.headless = True
    result.browser.channel = ""
    result.browser.hide_window = False
    result.browser.show_on_captcha = False
    result.geo.timezone = timezone_name
    # Сервер не ждёт интерактивного решения капчи. Восстановить вход можно отдельной командой login.
    result.notify.telegram_captcha = False
    result.notify.desktop = False
    return result


def _slots(cfg: Config, day: datetime) -> list[datetime]:
    if not cfg.schedule.enabled or str(day.isoweekday()) not in cfg.schedule.days:
        return []
    return [day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            for hour, minute in cfg.schedule.slots()]


def due_response_slots(cfg: Config, now: datetime, claimed: dict,
                       catch_up_minutes: int = DEFAULT_CATCH_UP_MINUTES) -> list[datetime]:
    """Только календарные слоты в окне догоняющего запуска; повтор уже начатого слота запрещён."""
    end = _aware(now).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=catch_up_minutes)
    slots = []
    day = start.replace(hour=0, minute=0)
    while day.date() <= end.date():
        slots.extend(slot for slot in _slots(cfg, day)
                     if start <= slot <= end and slot.isoformat(timespec="minutes") not in claimed)
        day += timedelta(days=1)
    return sorted(slots)


def next_response_slot(cfg: Config, now: datetime, claimed: dict) -> datetime | None:
    now = _aware(now)
    for offset in range(8):
        day = now + timedelta(days=offset)
        for slot in _slots(cfg, day):
            if slot >= now and slot.isoformat(timespec="minutes") not in claimed:
                return slot
    return None


class ServerState:
    """Запись running идёт ДО откликов. Неоднозначно прерванный слот автоматически не повторяется."""

    def __init__(self, root: Path):
        self.path = root / "data" / STATE_FILE
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.data = {"version": 1, "responses": {}}
        except (OSError, ValueError) as error:
            raise ServerError("не удалось прочитать память серверных запусков; автоматический повтор запрещён") from error
        if (not isinstance(self.data, dict) or self.data.get("version") != 1
                or not isinstance(self.data.get("responses"), dict)):
            raise ServerError("неверный формат памяти серверных запусков")
        if self.data.get("robot_next_at"):
            self._parse_time(self.data["robot_next_at"])

    @staticmethod
    def _parse_time(text: str) -> datetime:
        try:
            return _aware(datetime.fromisoformat(text))
        except (TypeError, ValueError) as error:
            raise ServerError("неверное время в памяти серверных запусков") from error

    @property
    def responses(self) -> dict:
        return self.data["responses"]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.path)

    def claim_response(self, slot: datetime, now: datetime, target: int = 0) -> bool:
        key = slot.isoformat(timespec="minutes")
        if key in self.responses:
            return False
        self.responses[key] = {"status": "running", "started_at": _aware(now).isoformat(), "code": None,
                               "target": target, "applied": 0}
        self._save()
        return True

    def resume_response(self, slot: datetime, now: datetime) -> bool:
        record = self.responses[slot.isoformat(timespec="minutes")]
        if record.get("status") != "waiting":
            return False
        record.update(status="running", started_at=_aware(now).isoformat(), code=None)
        record.pop("next_at", None)
        self._save()
        return True

    def finish_response(self, slot: datetime, now: datetime, code: int, *, applied: int = 0,
                        next_at: datetime | None = None, reason: str = "") -> None:
        record = self.responses[slot.isoformat(timespec="minutes")]
        record["applied"] = record.get("applied", 0) + applied
        record.update(status="waiting" if next_at else "done" if code in (0, EXIT_DEFERRED) else "failed",
                      finished_at=_aware(now).isoformat(), code=code, reason=reason)
        if next_at:
            record["next_at"] = _aware(next_at).isoformat()
        else:
            record.pop("next_at", None)
        self._save()

    def robot_due(self, now: datetime) -> bool:
        previous = self.data.get("robot_next_at")
        return not previous or self._parse_time(previous) <= _aware(now)

    def claim_robot(self, now: datetime, interval: int) -> None:
        self.data["robot_next_at"] = (_aware(now) + timedelta(minutes=interval)).isoformat()
        self.data["robot_last"] = {"status": "running", "started_at": now.isoformat(), "code": None}
        self._save()

    def finish_robot(self, now: datetime, interval: int, code: int, *, not_before: datetime | None = None) -> None:
        next_at = _aware(now) + timedelta(minutes=interval)
        self.data["robot_next_at"] = max(next_at, not_before or next_at).isoformat()
        self.data["robot_last"].update(status="waiting" if code == EXIT_DEFERRED else "done" if code == 0 else "failed",
                                       finished_at=now.isoformat(), code=code)
        self._save()


@contextmanager
def runner_lock(root: Path):
    """Один scheduler на папку данных/профиль, включая разные контейнеры с общим volume."""
    path = root / "data" / "server.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except OSError as error:
        if not locked:
            raise ServerError("другой серверный процесс уже использует эту папку данных") from error
        raise
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class ServerRunner:
    def __init__(self, root: Path, responses_job: Callable[[Config], int | ResponseRunResult], robot_job: Callable[[Config], int], *,
                 timezone_name: str = DEFAULT_TIMEZONE, catch_up_minutes: int = DEFAULT_CATCH_UP_MINUTES,
                 clock: Callable[[], datetime] | None = None,
                 retry_job: Callable[[Config], int | ResponseRunResult] | None = None):
        self.root, self.responses_job, self.robot_job = root, responses_job, robot_job
        self.timezone_name = timezone_name
        self.zone = server_timezone(timezone_name)
        self.catch_up_minutes = catch_up_minutes
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.retry_job = retry_job
        self.state = ServerState(root)

    def now(self) -> datetime:
        return _aware(self.clock()).astimezone(self.zone)

    @staticmethod
    def _run(job: Callable, cfg: Config) -> int | ResponseRunResult:
        if control.user_stopped():
            raise control.StopRequested()
        control.reset()
        try:
            result = job(cfg)
            if control.user_stopped():
                raise control.StopRequested()
            return result if isinstance(result, ResponseRunResult) else int(result)
        except (control.StopRequested, KeyboardInterrupt):
            raise
        except Exception:
            log.exception("серверная задача завершилась с ошибкой")
            return 1

    def _pacing(self, cfg: Config):
        return submission_pacer(cfg, self.root, clock=lambda: self.now().timestamp()).peek_status()

    def _quota(self, cfg: Config, now: datetime) -> int | None:
        maximum = cfg.limits.max_responses_per_day
        return max(0, maximum - confirmed_today(self.root, now)) if maximum else None

    def _as_time(self, timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp, self.zone)

    def _captcha_until(self, cfg: Config) -> datetime | None:
        now = self.now()
        deadline = self._pacing(cfg).captcha_until
        return self._as_time(deadline) if deadline > now.timestamp() else None

    def _waiting_slots(self, cfg: Config, now: datetime, *, ready_only: bool = True) -> list[datetime]:
        if not cfg.schedule.enabled or str(now.isoweekday()) not in cfg.schedule.days:
            return []
        allowed = set(cfg.schedule.slots())
        slots = []
        for key, record in self.state.responses.items():
            if not isinstance(record, dict) or record.get("status") != "waiting":
                continue
            slot = self.state._parse_time(key).astimezone(self.zone)
            if slot.date() != now.date() or (slot.hour, slot.minute) not in allowed:
                continue
            if (type(record.get("target")) is not int or record["target"] <= 0
                    or type(record.get("applied")) is not int or record["applied"] < 0):
                raise ServerError("неверный прогресс отложенной серверной задачи")
            next_at = self.state._parse_time(record.get("next_at"))
            if not ready_only or next_at <= now:
                slots.append(slot)
        return sorted(slots)

    @staticmethod
    def _response_result(result: int | ResponseRunResult) -> ResponseRunResult:
        return result if isinstance(result, ResponseRunResult) else ResponseRunResult(result, 0)

    def _response_slot(self, cfg: Config, slot: datetime) -> int | None:
        key = slot.isoformat(timespec="minutes")
        fresh = key not in self.state.responses
        if fresh and not self.state.claim_response(slot, self.now(), cfg.limits.max_responses):
            return None
        record = self.state.responses[key]
        remaining = max(0, record["target"] - record["applied"])
        quota = self._quota(cfg, self.now())
        if not remaining or quota == 0:
            self.state.finish_response(slot, self.now(), 0, reason="daily_limit" if quota == 0 else "target_reached")
            return None
        pacing = self._pacing(cfg)
        if pacing_enabled(cfg) and pacing.wait_seconds:
            self.state.finish_response(slot, self.now(), EXIT_DEFERRED,
                                       next_at=self._as_time(pacing.ready_at), reason=pacing.reason)
            return None
        if not fresh and not self.state.resume_response(slot, self.now()):
            return None
        chunk = min(remaining, cfg.limits.submission_batch_size or remaining) if pacing_enabled(cfg) else remaining
        if quota is not None:
            chunk = min(chunk, quota)
        prepared = headless_config(cfg, self.timezone_name)
        prepared.limits.max_responses = chunk
        log.info(f"сервер: отклики по расписанию {slot:%d.%m.%Y %H:%M} ({self.timezone_name}), до {chunk}")
        try:
            result = self._response_result(self._run(self.responses_job, prepared))
        except (control.StopRequested, KeyboardInterrupt):
            self.state.finish_response(slot, self.now(), 130)
            raise
        remaining = max(0, remaining - result.applied)
        quota = self._quota(cfg, self.now())
        if result.code in (0, EXIT_DEFERRED):
            if not remaining or quota == 0:
                self.state.finish_response(slot, self.now(), result.code, applied=result.applied,
                                           reason="daily_limit" if quota == 0 else "target_reached")
            elif result.code == EXIT_DEFERRED or (pacing_enabled(cfg) and result.applied >= chunk):
                pacing = self._pacing(cfg)
                self.state.finish_response(slot, self.now(), EXIT_DEFERRED, applied=result.applied,
                                           next_at=self._as_time(pacing.ready_at),
                                           reason=pacing.reason if pacing.wait_seconds else "continue")
            else:
                self.state.finish_response(slot, self.now(), 0, applied=result.applied, reason="exhausted")
        else:
            self.state.finish_response(slot, self.now(), result.code, applied=result.applied)
        return result.code

    def tick(self, cfg: Config) -> list[tuple[str, int]]:
        """Один последовательный цикл; сохранённые паузы освобождают профиль между задачами."""
        ran = []
        if control.user_stopped():
            raise control.StopRequested()
        cooldown_until = self._captcha_until(cfg)
        if cooldown_until:
            if cfg.schedule.enabled:
                validate_search_source(cfg)
                for slot in due_response_slots(cfg, self.now(), self.state.responses, self.catch_up_minutes):
                    if self.state.claim_response(slot, self.now(), cfg.limits.max_responses):
                        self.state.finish_response(slot, self.now(), EXIT_DEFERRED,
                                                   next_at=cooldown_until, reason="captcha_cooldown")
            return ran
        if cfg.schedule.enabled:
            validate_search_source(cfg)
        slots = self._waiting_slots(cfg, self.now())
        slots.extend(due_response_slots(cfg, self.now(), self.state.responses, self.catch_up_minutes))
        for slot in sorted(slots):
            if control.user_stopped():
                raise control.StopRequested()
            if self._captcha_until(cfg):
                return ran
            record = self.state.responses.get(slot.isoformat(timespec="minutes"), {})
            if record.get("status") == "waiting" and slot.date() != self.now().date():
                continue
            code = self._response_slot(cfg, slot)
            if code is not None:
                ran.append(("responses", code))
        if control.user_stopped():
            raise control.StopRequested()
        if self._captcha_until(cfg):
            return ran
        if cfg.watch.robot_replies and self.state.robot_due(self.now()):
            interval = max(cfg.watch.robot_every_minutes, 1)
            self.state.claim_robot(self.now(), interval)
            try:
                code = self._response_result(self._run(self.robot_job, headless_config(cfg, self.timezone_name))).code
            except (control.StopRequested, KeyboardInterrupt):
                self.state.finish_robot(self.now(), interval, 130)
                raise
            self.state.finish_robot(self.now(), interval, code, not_before=self._captcha_until(cfg))
            ran.append(("robot_replies", code))
        if control.user_stopped():
            raise control.StopRequested()
        if self._captcha_until(cfg):
            return ran
        pacing = self._pacing(cfg)
        quota = self._quota(cfg, self.now())
        if (self.retry_job and not (pacing_enabled(cfg) and pacing.wait_seconds) and quota != 0
                and application_retries_due(self.root, cfg, self.now())):
            log.info("сервер: автоматические повторные попытки ранее выбранных вакансий")
            prepared = headless_config(cfg, self.timezone_name)
            prepared.limits.max_responses = min(prepared.limits.max_responses, quota) if quota is not None else prepared.limits.max_responses
            code = self._response_result(self._run(self.retry_job, prepared)).code
            ran.append(("application_retries", code))
        return ran

    def status(self, cfg: Config) -> dict:
        now = self.now()
        next_slot = next_response_slot(cfg, now, self.state.responses)
        due = due_response_slots(cfg, now, self.state.responses, self.catch_up_minutes)
        waiting = self._waiting_slots(cfg, now, ready_only=False)
        pacing = self._pacing(cfg)
        used = confirmed_today(self.root, now)
        maximum = cfg.limits.max_responses_per_day
        remaining = max(0, maximum - used) if maximum else None
        captcha_until = self._as_time(pacing.captcha_until) if pacing.captcha_until > now.timestamp() else None
        submit_until = self._as_time(pacing.ready_at) if pacing_enabled(cfg) and pacing.wait_seconds else now
        if captcha_until:
            submit_until = max(submit_until, captcha_until)
        wait_reason = pacing.reason if submit_until > now else "ready"
        if remaining == 0:
            reset_at = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            if reset_at > submit_until:
                submit_until, wait_reason = reset_at, "daily_limit"
        can_submit = submit_until <= now
        resume_at = min((max(self.state._parse_time(self.state.responses[slot.isoformat(timespec="minutes")]["next_at"]),
                            submit_until) for slot in waiting), default=None) if remaining != 0 else None
        next_at = min((value for value in (next_slot, resume_at, submit_until if due and remaining != 0 else None)
                       if value is not None), default=None)
        if can_submit:
            due.extend(slot for slot in waiting
                       if self.state._parse_time(self.state.responses[slot.isoformat(timespec="minutes")]["next_at"]) <= now)
        else:
            due = []
        robot_at = max(self.state._parse_time(self.state.data["robot_next_at"])
                       if self.state.data.get("robot_next_at") else now, now, captcha_until or now)
        retries_queued = application_retries_due(self.root, cfg, now)
        runs = {}
        for key, record in list(sorted(self.state.responses.items()))[-14:]:
            runs[key] = dict(record)
            if "target" in record and "applied" in record:
                runs[key]["remaining"] = max(0, record["target"] - record["applied"])
        return {"now": now.isoformat(), "timezone": self.timezone_name, "headless": True,
                "schedule_enabled": cfg.schedule.enabled, "times": cfg.schedule.times, "days": cfg.schedule.days,
                "catch_up_minutes": self.catch_up_minutes,
                "responses_due": [slot.isoformat() for slot in due],
                "next_response_at": next_at.isoformat() if next_at else None,
                "next_response_slot_at": next_slot.isoformat() if next_slot else None,
                "responses_resume_at": resume_at.isoformat() if resume_at else None,
                "submission_policy": {name: getattr(cfg.limits, name) for name in (
                    "submission_interval_seconds", "submission_batch_size", "submission_batch_pause_minutes",
                    "captcha_cooldown_minutes", "read_interval_seconds", "max_responses_per_day")},
                "submission_pacing": {
                    "enabled": pacing_enabled(cfg), "reason": wait_reason,
                    "ready_at": submit_until.isoformat(), "wait_seconds": max(0, (submit_until - now).total_seconds()),
                    "batch_count": pacing.batch_count,
                    "last_attempt_at": self._as_time(pacing.last_attempt_at).isoformat() if pacing.last_attempt_at else None,
                    "captcha_until": self._as_time(pacing.captcha_until).isoformat() if pacing.captcha_until else None,
                    "batch_until": self._as_time(pacing.batch_until).isoformat() if pacing.batch_until else None,
                    "submit_until": self._as_time(pacing.submit_until).isoformat() if pacing.submit_until else None},
                "daily_responses_used": used, "daily_responses_limit": maximum,
                "daily_responses_remaining": remaining,
                "robot_replies_enabled": cfg.watch.robot_replies,
                "robot_every_minutes": cfg.watch.robot_every_minutes,
                "robot_max_chats_per_check": cfg.watch.robot_max_chats_per_check,
                "robot_reply_interval_seconds": cfg.watch.robot_reply_interval_seconds,
                "application_retries_due": retries_queued and can_submit,
                "application_retries_queued": retries_queued,
                "robot_next_at": robot_at.isoformat() if cfg.watch.robot_replies else None,
                "robot_last": self.state.data.get("robot_last"),
                "response_runs": runs}
