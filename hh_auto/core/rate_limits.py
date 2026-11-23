"""Application policy around durable pacing; no browser or network access."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from .paths import app_root
from ..hh.submission_pacing import FILE, PacingStateError, PacingStatus, SubmissionPacer

EXIT_DEFERRED = 75


class WorkDeferred(Exception):
    def __init__(self, status: PacingStatus):
        self.status = status
        super().__init__(f'пауза: {status.reason}; следующая попытка после '
                         f'{datetime.fromtimestamp(status.ready_at, ZoneInfo("Europe/Moscow")).isoformat(timespec="seconds")}')


def enabled(cfg) -> bool:
    limits = cfg.limits
    return bool(limits.submission_interval_seconds or limits.submission_batch_size or limits.captcha_cooldown_minutes)


def submission_pacer(cfg, root: Path | None = None, *, clock=None) -> SubmissionPacer:
    limits = cfg.limits
    return SubmissionPacer((root or app_root()) / 'data' / FILE,
        submit_interval_seconds=limits.submission_interval_seconds,
        batch_size=limits.submission_batch_size,
        batch_pause_seconds=limits.submission_batch_pause_minutes * 60,
        captcha_cooldown_seconds=limits.captcha_cooldown_minutes * 60, clock=clock)


def check_captcha_cooldown(cfg, root: Path | None = None) -> None:
    if not enabled(cfg):
        return
    status = submission_pacer(cfg, root).peek_status()
    if status.captcha_until > time.time():
        raise WorkDeferred(PacingStatus(status.captcha_until,
            max(0, status.captcha_until - time.time()), 'captcha_cooldown',
            status.batch_count, status.last_attempt_at, status.captcha_until,
            status.batch_until, status.submit_until))


def confirmed_today(root: Path, now: datetime, *, exclude_uncertain_id: str | None = None) -> int:
    from .state import State
    from .delivery import ApplicationDelivery, DeliveryError

    try:
        counted = set()
        for identity, row in State(root / 'data/state.json').applied.items():
            applied = datetime.fromisoformat(row['at'])
            if applied.tzinfo is None:
                applied = applied.replace(tzinfo=now.tzinfo)
            if applied.astimezone(now.tzinfo).date() == now.date():
                counted.add(identity)
        for row in ApplicationDelivery(root / 'data/application-delivery.json').snapshot():
            if not row.get('delivery_uncertain') or row['vacancy_id'] == exclude_uncertain_id:
                continue
            claimed = datetime.fromisoformat(row['claimed_at'])
            if claimed.tzinfo is None:
                raise ValueError('naive claim time')
            if claimed.astimezone(now.tzinfo).date() == now.date():
                counted.add(row['vacancy_id'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, DeliveryError) as error:
        raise PacingStateError('не удалось проверить дневной лимит; отправка остановлена') from error
    return len(counted)


def check_daily_limit(cfg, root: Path | None = None, *, exclude_uncertain_id: str | None = None) -> None:
    if not cfg.limits.max_responses_per_day:
        return
    root = root or app_root()
    now = datetime.now(ZoneInfo(cfg.geo.timezone or 'Europe/Moscow'))
    if confirmed_today(root, now, exclude_uncertain_id=exclude_uncertain_id) >= cfg.limits.max_responses_per_day:
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        raise WorkDeferred(PacingStatus(tomorrow.timestamp(), (tomorrow - now).total_seconds(), 'daily_limit'))
