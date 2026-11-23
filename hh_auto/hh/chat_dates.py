"""Даты последних сообщений из списка hh: машинная дата или подпись chat-cell-creation-time."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Literal

ROBOT_CHAT_DAYS = 30
ChatDateStatus = Literal["recent", "old", "unknown"]

_WEEKDAYS = {
    "пн": 0, "понедельник": 0, "вт": 1, "вторник": 1, "ср": 2, "среда": 2,
    "чт": 3, "четверг": 3, "пт": 4, "пятница": 4, "сб": 5, "суббота": 5,
    "вс": 6, "воскресенье": 6,
}
_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def _annual_date(day: int, month: int, year: int | None, today: date) -> date | None:
    if year is not None:
        try:
            return date(year + 2000 if year < 100 else year, month, day)
        except ValueError:
            return None
    # Без года hh показывает последнюю прошедшую дату, в том числе через границу декабря/января.
    # 29 февраля ищем в предыдущем високосном году, а не превращаем в несуществующую дату.
    for candidate_year in range(today.year, today.year - 5, -1):
        try:
            candidate = date(candidate_year, month, day)
        except ValueError:
            continue
        if candidate <= today:
            return candidate
    return None


def parse_chat_date(value: object, *, now: datetime | None = None) -> date | None:
    """Дата сообщения в местном времени. Неизвестную подпись не считаем сегодняшней."""
    now = now or datetime.now().astimezone()
    today = now.date()
    if isinstance(value, datetime):
        return value.astimezone(now.tzinfo).date() if value.tzinfo is not None else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) or isinstance(value, str) and re.fullmatch(r"\d{10,13}(?:\.\d+)?", value.strip()):
        try:
            stamp = float(value)
            if not math.isfinite(stamp) or stamp < 1_000_000_000:
                return None
            if stamp >= 1_000_000_000_000:
                stamp /= 1000
            return datetime.fromtimestamp(stamp, now.tzinfo).date()
        except (ValueError, OverflowError, OSError):
            return None
    if not isinstance(value, str):
        return None
    text = " ".join(value.replace("\u00a0", " ").strip().lower().replace("ё", "е").split())
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("z", "+00:00"))
        return parsed.astimezone(now.tzinfo).date() if parsed.tzinfo is not None else parsed.date()
    except ValueError:
        pass
    if re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", text):
        return today
    relative = re.fullmatch(r"(сегодня|вчера)(?:,?\s+(?:[01]?\d|2[0-3]):[0-5]\d)?", text)
    if relative:
        return today - timedelta(days=relative[1] == "вчера")
    if text.rstrip(".") in _WEEKDAYS:
        offset = (today.weekday() - _WEEKDAYS[text.rstrip(".")]) % 7
        return today - timedelta(days=offset or 7)
    numeric = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?:,?\s+\d{1,2}:\d{2})?", text)
    if numeric:
        return _annual_date(int(numeric[1]), int(numeric[2]), int(numeric[3]) if numeric[3] else None, today)
    named = re.fullmatch(r"(\d{1,2})\s+([а-я]+)\.?(?:\s+(\d{4}|\d{2}))?(?:,?\s+\d{1,2}:\d{2})?", text)
    if named and named[2][:3] in _MONTHS:
        return _annual_date(int(named[1]), _MONTHS[named[2][:3]], int(named[3]) if named[3] else None, today)
    return None


def chat_date_status(value: object, *, now: datetime | None = None, days: int = ROBOT_CHAT_DAYS) -> ChatDateStatus:
    """Включительно по календарной дате: сегодня минус days. Будущая/непонятная дата — unknown."""
    now = now or datetime.now().astimezone()
    parsed = parse_chat_date(value, now=now)
    if parsed is None or parsed > now.date():
        return "unknown"
    return "recent" if parsed >= now.date() - timedelta(days=days) else "old"
