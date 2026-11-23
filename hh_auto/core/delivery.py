"""Durable application claims and bounded retries. Unknown delivery is never permission to resend.

The caller re-runs normal filters and probes hh before reconciling an ambiguous claim.
This module makes no network requests and never sends an application itself.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


class DeliveryError(Exception):
    pass


def _time(now: datetime | None = None) -> datetime:
    result = now or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise DeliveryError("время очереди должно содержать часовой пояс")
    return result


def delivery_key(resume_id: str, vacancy_id: str) -> str:
    return hashlib.sha256(f"{resume_id}:{vacancy_id}".encode()).hexdigest()


class ApplicationDelivery:
    """Each claim is saved before the first potentially irreversible card click.

    claim -> record_result. After a crash, due returns in_flight/unknown rows for a
    read-only hh probe; reconcile('not_applied') is the only way to release those.
    Per-file locks protect concurrent claims; atomic/fsynced writes survive restarts.
    """

    STATES = {"in_flight", "unknown", "pending", "confirmed", "exhausted", "cancelled"}

    def __init__(self, path: Path, max_attempts: int = 3, backoff_seconds: tuple[int, ...] = (30, 120, 300)):
        if max_attempts < 1 or not backoff_seconds or any(seconds < 0 for seconds in backoff_seconds):
            raise ValueError("invalid retry limits")
        self.path = Path(path)
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._load()  # fail closed before any caller can send

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "applications": {}}
        except (OSError, ValueError) as error:
            raise DeliveryError("не удалось прочитать очередь откликов; автоматический повтор запрещён") from error
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("applications"), dict):
            raise DeliveryError("неверный формат очереди откликов; автоматический повтор запрещён")
        for key, row in data["applications"].items():
            if (not isinstance(row, dict) or row.get("status") not in self.STATES
                    or not isinstance(row.get("vacancy_id"), str) or not isinstance(row.get("resume_id"), str)
                    or type(row.get("attempts")) is not int or row["attempts"] < 1
                    or key != delivery_key(row["resume_id"], row["vacancy_id"])):
                raise DeliveryError("повреждена запись очереди откликов; автоматический повтор запрещён")
            try:
                _time(datetime.fromisoformat(row["next_at"]))
            except (ValueError, KeyError, TypeError) as error:
                raise DeliveryError("повреждено время очереди откликов") from error
        return data

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if not stream.tell():
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _save(self, data: dict):
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.path)

    def _delay(self, row: dict) -> timedelta:
        return timedelta(seconds=self.backoff_seconds[min(row["attempts"] - 1, len(self.backoff_seconds) - 1)])

    def get(self, resume_id: str, vacancy_id: str) -> dict | None:
        row = self._load()["applications"].get(delivery_key(resume_id, vacancy_id))
        return copy.deepcopy(row)

    def snapshot(self) -> list[dict]:
        """Read all claims without writing files, including terminal uncertain deliveries."""
        return copy.deepcopy(list(self._load()["applications"].values()))

    def due(self, *, now: datetime | None = None) -> list[dict]:
        current = _time(now)
        return [copy.deepcopy(row) for row in self._load()["applications"].values()
                if row["status"] in {"pending", "unknown", "in_flight"}
                and datetime.fromisoformat(row["next_at"]) <= current]

    def claim(self, resume_id: str, vacancy_id: str, *, vacancy: dict | None = None,
              now: datetime | None = None) -> bool:
        current, key = _time(now), delivery_key(resume_id, vacancy_id)
        with self._locked():
            data = self._load()
            rows = data["applications"]
            # hh accepts one application per vacancy, regardless of a later resume selection.
            if any(row["vacancy_id"] == vacancy_id and (row["status"] in {"confirmed", "unknown", "in_flight"}
                   or row.get("delivery_uncertain") is True)
                   for row in rows.values()):
                return False
            row = rows.get(key)
            if row and (row["status"] != "pending" or row["attempts"] >= self.max_attempts
                        or datetime.fromisoformat(row["next_at"]) > current):
                return False
            if row is None:
                row = rows[key] = {"idempotency_key": key, "resume_id": resume_id, "vacancy_id": vacancy_id,
                                   "attempts": 0, "created_at": current.isoformat(), "vacancy": vacancy or {}}
            row.update(status="in_flight", attempts=row["attempts"] + 1, claimed_at=current.isoformat(),
                       reason="", updated_at=current.isoformat(), delivery_uncertain=True)
            row["next_at"] = (current + self._delay(row)).isoformat()
            if vacancy is not None:
                row["vacancy"] = dict(vacancy)
            self._save(data)
            return True

    def record_result(self, resume_id: str, vacancy_id: str, *, confirmed: bool,
                      outbound_attempted: bool, reason: str = "", now: datetime | None = None) -> dict:
        """Only an error before any possible outbound is directly eligible for retry."""
        return self._update(resume_id, vacancy_id,
                            "applied" if confirmed else ("unknown" if outbound_attempted else "not_applied"),
                            reason=reason, now=now, probe=False)

    def reconcile(self, resume_id: str, vacancy_id: str, status: str, *, reason: str = "",
                  now: datetime | None = None) -> dict:
        """status comes from fresh authenticated hh evidence: applied / not_applied / unknown."""
        if status not in {"applied", "not_applied", "unknown"}:
            raise ValueError("status must be applied, not_applied or unknown")
        return self._update(resume_id, vacancy_id, status, reason=reason, now=now, probe=True)

    def cancel(self, resume_id: str, vacancy_id: str, reason: str = "", *,
               now: datetime | None = None) -> dict:
        """An existing queued attempt no longer passes filters or has a permanent hh limitation.

        Cancellation does not consume another attempt and cannot erase confirmed delivery.
        """
        current, key = _time(now), delivery_key(resume_id, vacancy_id)
        with self._locked():
            data = self._load()
            row = data["applications"].get(key)
            if row is None:
                raise DeliveryError("отменяемого отклика нет в очереди")
            if row["status"] == "confirmed":
                return copy.deepcopy(row)
            row.update(status="cancelled", reason=reason, updated_at=current.isoformat())
            self._save(data)
            return copy.deepcopy(row)

    def _update(self, resume_id: str, vacancy_id: str, status: str, *, reason: str,
                now: datetime | None, probe: bool) -> dict:
        current, key = _time(now), delivery_key(resume_id, vacancy_id)
        with self._locked():
            data = self._load()
            row = data["applications"].get(key)
            if row is None:
                raise DeliveryError("попытка отклика не была заранее записана в очередь")
            if row["status"] == "confirmed":
                return copy.deepcopy(row)  # never downgrade delivery evidence
            if probe and status == "unknown":
                row["probes"] = row.get("probes", 0) + 1
            next_status = ("confirmed" if status == "applied" else "unknown" if status == "unknown"
                           else "exhausted" if row["attempts"] >= self.max_attempts else "pending")
            if status == "unknown" and row.get("probes", 0) >= self.max_attempts:
                next_status = "exhausted"
                reason = (reason + "; " if reason else "") + "доставка неизвестна после ограниченного числа проверок; повтор запрещён"
            row.update(status=next_status, reason=reason, updated_at=current.isoformat(),
                       next_at=(current + self._delay(row)).isoformat(), delivery_uncertain=status == "unknown")
            if status == "applied":
                row["confirmed_at"] = current.isoformat()
            self._save(data)
            return copy.deepcopy(row)
