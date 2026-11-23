"""Portable hh.ru authentication state, kept outside the source tree and browser-specific encryption."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from ..core.paths import app_root

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


class SessionError(ValueError):
    """An authentication snapshot cannot be safely used. Messages never contain cookie values."""


def snapshot_path() -> Path:
    override = os.environ.get("HH_AUTO_SESSION_PATH", "").strip()
    return Path(override).expanduser() if override else app_root() / "data" / "hh-session.json"


def _hh_domain(domain: object) -> bool:
    if not isinstance(domain, str):
        return False
    host = domain.lstrip(".").lower()
    return host == "hh.ru" or host.endswith(".hh.ru")


def _hh_origin(origin: object) -> bool:
    if not isinstance(origin, str):
        return False
    try:
        parsed = urlparse(origin)
        return (parsed.scheme == "https" and _hh_domain(parsed.hostname) and not parsed.username
                and not parsed.password and parsed.port in (None, 443)
                and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def validate_state(state: object, *, strict_domains: bool = True, now: float | None = None) -> dict:
    """Keep only hh cookies/localStorage; imports reject foreign origins instead of injecting them."""
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not isinstance(state.get("origins"), list):
        raise SessionError("неверный формат сессии hh: нужны cookies и origins")
    current = time.time() if now is None else now
    cookies = []
    for source in state["cookies"]:
        if not isinstance(source, dict):
            raise SessionError("неверная запись cookie в сессии hh")
        if not _hh_domain(source.get("domain")):
            if strict_domains:
                raise SessionError("файл сессии содержит cookies другого сайта")
            continue
        if not all(isinstance(source.get(key), str) for key in ("name", "value", "domain", "path")):
            raise SessionError("неверные поля cookie в сессии hh")
        if not source["name"] or not source["path"].startswith("/"):
            raise SessionError("неверное имя или путь cookie в сессии hh")
        expires = source.get("expires", -1)
        if isinstance(expires, bool) or not isinstance(expires, (float, int)) or not math.isfinite(expires):
            raise SessionError("неверный срок действия cookie в сессии hh")
        if expires != -1 and expires <= current:
            continue
        cookie = {key: source[key] for key in ("name", "value", "domain", "path")}
        cookie["expires"] = expires
        for key in ("httpOnly", "secure"):
            value = source.get(key, False)
            if not isinstance(value, bool):
                raise SessionError("неверные флаги cookie в сессии hh")
            cookie[key] = value
        same_site = source.get("sameSite", "Lax")
        if same_site not in ("Strict", "Lax", "None"):
            raise SessionError("неверный sameSite cookie в сессии hh")
        cookie["sameSite"] = same_site
        if "partitionKey" in source:
            if not _hh_origin(source["partitionKey"]):
                if strict_domains:
                    raise SessionError("файл сессии содержит раздел cookie другого сайта")
                continue
            cookie["partitionKey"] = source["partitionKey"]
        cookies.append(cookie)
    origins = []
    for source in state["origins"]:
        if not isinstance(source, dict):
            raise SessionError("неверная запись localStorage в сессии hh")
        if not _hh_origin(source.get("origin")):
            if strict_domains:
                raise SessionError("файл сессии содержит localStorage другого сайта")
            continue
        entries = source.get("localStorage", [])
        if not isinstance(entries, list) or not all(isinstance(entry, dict)
                and isinstance(entry.get("name"), str) and isinstance(entry.get("value"), str) for entry in entries):
            raise SessionError("неверные поля localStorage в сессии hh")
        origins.append({"origin": source["origin"],
                        "localStorage": [{"name": item["name"], "value": item["value"]} for item in entries]})
    if not any(cookie["name"] == "hhrole" and cookie["value"] == "applicant" for cookie in cookies):
        raise SessionError("в файле нет действующей сессии соискателя hh")
    return {"cookies": cookies, "origins": origins}


def read_snapshot(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise SessionError("файл сессии hh слишком большой")
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, SessionError):
            raise
        raise SessionError("не удалось прочитать файл сессии hh") from error
    return validate_state(state)


def save_session(context, path: Path | None = None) -> dict[str, int]:
    """Called only after hh verified the account; write atomically with private POSIX permissions."""
    state = validate_state(context.storage_state(), strict_domains=False)
    target = path if path is not None else snapshot_path()
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix=".hh-session-", suffix=".tmp", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
            json.dump(state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError as error:
        raise SessionError("не удалось сохранить переносимую сессию hh") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return {"cookies": len(state["cookies"]), "origins": len(state["origins"])}


def restore_session(context, *, fresh_profile: bool, force: bool = False, path: Path | None = None) -> bool:
    """Only a fresh profile is seeded automatically; an existing one requires an explicit restore."""
    if not fresh_profile and not force:
        return False
    target = path if path is not None else snapshot_path()
    if not target.exists():
        if force:
            raise SessionError("файл переносимой сессии hh не найден")
        return False
    state = read_snapshot(target)
    setter = getattr(context, "set_storage_state", None)
    if not callable(setter):
        raise SessionError("для переноса сессии hh нужен Playwright 1.59 или новее")
    try:
        setter(state)
    except Exception as error:
        raise SessionError("не удалось восстановить переносимую сессию hh") from error
    return True
