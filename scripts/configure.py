"""Создать безопасную конфигурацию или проверить её локально без браузера и модели."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values
from hh_auto.ai.profile import ProfileError, load_profile_file
from hh_auto.core.config import AI_PROVIDERS, ConfigError, load_config, validate_config, validate_search_source
from hh_auto.core.config_file import update_text
from hh_auto.core.paths import PROJECT_DIR
from hh_auto.web.session import SessionError, read_snapshot


class SetupError(ValueError):
    pass


BROWSER_BACKENDS = ("playwright", "seleniumbase_uc")


def _resume_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9]{1,128}", value):
        return value
    parsed = urlparse(value)
    match = re.fullmatch(r"/resume/([A-Za-z0-9]{1,128})/?", parsed.path)
    if (parsed.scheme == "https" and parsed.hostname == "hh.ru" and not parsed.username and not parsed.password
            and parsed.port in (None, 443) and not parsed.query and not parsed.fragment and match):
        return match[1]
    raise SetupError("--resume: нужен ID или ссылка https://hh.ru/resume/ID без дополнительных параметров")


def _api_url(value: str) -> None:
    parsed = urlparse(value)
    if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise SetupError("base_url: нужен адрес HTTP(S) без ключей, логина, query и fragment")


def _profile(source: Path, mapping: Path | None = None, notes: Path | None = None):
    if source.suffix.casefold() not in {".json", ".toml"}:
        raise SetupError("профиль должен быть файлом JSON или TOML")
    if mapping:
        if mapping.suffix.casefold() not in {".md", ".markdown"}:
            raise SetupError("карта компетенций должна быть файлом Markdown (.md)")
        if mapping.stat().st_size > 256 * 1024:
            raise SetupError("карта компетенций превышает 256 КиБ")
        text = mapping.read_text(encoding="utf-8-sig")
        if not re.search(r"(?m)^###\s+\d+\.\s+\S", text):
            raise SetupError("карта компетенций: нужны заголовки ### 1. Название проекта")
    profile = load_profile_file(source, competency_map_path=mapping, profile_notes_path=notes)
    if mapping and not any(project.supplementary.strip() for project in profile.projects):
        raise SetupError("в карте нет непустого кейса с названием, совпадающим с проектом профиля")
    return profile


def _atomic_write(path: Path, content: bytes, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".hh-init-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        elif os.name == "nt":
            # Windows rename is atomic and refuses an existing target; hard links may need unavailable FS privileges.
            os.rename(temporary, path)
        else:
            # POSIX rename would overwrite a concurrent target; a hard link publishes the complete file without clobbering.
            os.link(temporary, path)
    except FileExistsError as error:
        raise SetupError(f"файл уже существует: {path}; для замены нужен --overwrite") from error
    finally:
        temporary.unlink(missing_ok=True)


def initialize(data_dir: Path, *, provider: str = "openai", model: str = "", gender: str = "male",
               profile_source: Path | None = None, map_source: Path | None = None, resume: str = "",
               base_url: str = "", query: str = "", browser_backend: str = "playwright", overwrite: bool = False,
               template: Path = PROJECT_DIR / "config.server.example.toml") -> dict:
    """Validate all inputs before creating destinations. Only explicitly supplied evidence is copied."""
    root = data_dir.expanduser().resolve()
    config = root / "config.server.toml"
    if config.exists() and not overwrite:
        raise SetupError(f"конфиг уже существует: {config}; для новой инициализации нужен --overwrite")
    if provider not in AI_PROVIDERS or gender not in {"male", "female"}:
        raise SetupError("неверный provider или gender")
    if browser_backend not in BROWSER_BACKENDS:
        raise SetupError("browser_backend: допустимы playwright и seleniumbase_uc")
    model = model.strip() or AI_PROVIDERS[provider][1]
    if not model or re.search(r"\bsk-[A-Za-z0-9_-]{12,}", model):
        raise SetupError("укажите имя модели; API-ключ вводится только в .env")
    effective_url = base_url.strip() or AI_PROVIDERS[provider][0]
    _api_url(effective_url)
    cfg = load_config(template, check_search_url=False, upgrade=False, load_env=False)
    cfg.ai.provider, cfg.ai.model, cfg.ai.candidate_gender = provider, model, gender
    cfg.ai.base_url = base_url.strip()
    cfg.ai.api_key = ""
    cfg.ai.profile_path = cfg.ai.competency_map_path = cfg.ai.profile_notes_path = ""
    cfg.schedule.enabled = False
    cfg.watch.robot_replies = cfg.watch.invitations = False
    cfg.raising.enabled = cfg.dream.enabled = False
    cfg.browser.headless, cfg.browser.hide_window, cfg.browser.channel = True, False, ""
    cfg.browser.backend = browser_backend
    cfg.ai.only_robot = True
    payloads: list[tuple[Path, bytes]] = []
    if map_source and not profile_source:
        raise SetupError("--competency-map требует явно выбранного --profile")
    if profile_source:
        source = profile_source.expanduser().resolve()
        mapping = map_source.expanduser().resolve() if map_source else None
        _profile(source, mapping)
        destination = root / "data" / ("candidate_profile" + source.suffix.casefold())
        cfg.ai.profile_path = destination.relative_to(root).as_posix()
        payloads.append((destination, source.read_bytes()))
        if mapping:
            destination = root / "data" / "competency_map.md"
            cfg.ai.competency_map_path = destination.relative_to(root).as_posix()
            payloads.append((destination, mapping.read_bytes()))
    if resume:
        identity = _resume_id(resume)
        cfg.resume_url = f"https://hh.ru/resume/{identity}"
        cfg.search_url = "https://hh.ru/search/vacancy?" + urlencode({"from": "resumelist", "resume": identity})
        cfg.skip_on_resume_mismatch = True
    if query.strip():
        cfg.search.mode, cfg.search.query = "query", query.strip()
    validate_config(cfg)
    # An absent resume/search is a legitimate initial state; CLI selection after login completes it.
    if resume or query.strip():
        validate_search_source(cfg)
    updates = {"": {"resume_url": cfg.resume_url, "search_url": cfg.search_url, "skip_on_resume_mismatch": True},
               "ai": {"provider": provider, "model": model, "candidate_gender": gender, "base_url": cfg.ai.base_url,
                      "api_key": "", "profile_path": cfg.ai.profile_path, "competency_map_path": cfg.ai.competency_map_path,
                      "profile_notes_path": "", "only_robot": True},
               "schedule": {"enabled": False}, "watch": {"robot_replies": False, "invitations": False},
               "raising": {"enabled": False}, "dream": {"enabled": False},
               "browser": {"backend": browser_backend, "headless": True, "hide_window": False, "channel": ""},
               "search": {"mode": cfg.search.mode, "query": cfg.search.query}}
    rendered, _ = update_text(template.read_text(encoding="utf-8"), updates)
    payloads.append((config, rendered.encode("utf-8")))
    for destination, _ in payloads:
        if destination.exists() and not overwrite:
            raise SetupError(f"файл уже существует: {destination}; ничего не изменено, используйте --overwrite")
    # Preserve an existing key file even when config/profile replacement is explicitly requested.
    env_file = root / ".env"
    if not env_file.exists():
        payloads.append((env_file, b"# Uncomment and fill your provider key locally. Never publish this file.\n# OPENAI_API_KEY=\n# HH_AI_API_KEY=\n"))
    for destination, content in payloads:
        _atomic_write(destination, content, overwrite=overwrite and destination != env_file)
    return {"config": str(config), "env_file": str(env_file), "files": len(payloads),
            "profile_configured": bool(profile_source), "automatic_tasks_enabled": False,
            "browser_backend": browser_backend}


def doctor(config: Path, data_dir: Path, *, env_file: Path | None = None, environ: dict | None = None) -> dict:
    """Read only explicit configuration paths; no upgrades, browser, API, or environment mutation."""
    checks = []

    def add(item, ok, message, *, warning=False):
        checks.append({"item": item, "status": "ok" if ok else "warning" if warning else "error", "message": message})

    try:
        cfg = load_config(config, check_search_url=False, upgrade=False, load_env=False)
    except (ConfigError, OSError, ValueError, TypeError) as error:
        add("config", False, "Конфигурация не прочитана или не прошла валидацию: " + _safe_error(error))
        return {"ok": False, "checks": checks}
    add("config", True, "Формат конфигурации корректен")
    add("gender", True, "Род кандидата: " + cfg.ai.candidate_gender)
    backend = cfg.browser.backend
    add("browser_backend", True,
        "seleniumbase_uc: на Linux используются полный Chrome и внутренний Xvfb; зависимости и работу браузера doctor не проверяет"
        if backend == "seleniumbase_uc" else "playwright: встроенный Chromium; работу браузера doctor не проверяет")
    try:
        _api_url(cfg.ai.base_url or AI_PROVIDERS[cfg.ai.provider][0])
        if not (cfg.ai.model or AI_PROVIDERS[cfg.ai.provider][1]).strip():
            raise SetupError("имя модели не задано")
        add("model", True, "Поставщик и имя модели настроены; доступность API локально не проверяется")
    except (ValueError, TypeError, AttributeError) as error:
        add("model", False, _safe_error(error))
    key_file = env_file if env_file is not None else config.resolve().parent / ".env"
    environment = dict(os.environ if environ is None else environ)
    try:
        from_file = dotenv_values(key_file, interpolate=False, encoding="utf-8-sig") if key_file.is_file() else {}
        present = lambda value: isinstance(value, str) and bool(value.strip())
        available = lambda name: present(environment[name] if name in environment else from_file.get(name))
        key_present = present(cfg.ai.api_key) or available("HH_AI_API_KEY") or cfg.ai.provider == "openai" and available("OPENAI_API_KEY")
        required = cfg.ai.provider != "ollama"
        add("api_key", bool(key_present) or not required,
            "Ключ настроен (значение скрыто)" if key_present else "Ollama не требует ключа" if not required
            else "Добавьте OPENAI_API_KEY для OpenAI или HH_AI_API_KEY для выбранного API в указанный .env")
    except (OSError, UnicodeError):
        add("api_key", False, "Указанный .env не прочитан")
    try:
        if cfg.ai.profile_path:
            resolve = lambda text: Path(text).expanduser() if Path(text).expanduser().is_absolute() else config.resolve().parent / text
            profile = _profile(resolve(cfg.ai.profile_path), resolve(cfg.ai.competency_map_path) if cfg.ai.competency_map_path else None,
                               resolve(cfg.ai.profile_notes_path) if cfg.ai.profile_notes_path else None)
            add("profile", True, f"Профиль корректен; подтверждённых кейсов: {len(profile.projects)}")
        elif cfg.ai.competency_map_path or cfg.ai.profile_notes_path:
            raise SetupError("Карта задана без профиля")
        else:
            add("profile", False, "Дополнительный профиль не подключён; используются факты выбранного резюме", warning=True)
    except (ProfileError, OSError, ValueError) as error:
        add("profile", False, "Профиль или карта не прошли проверку: " + _safe_error(error))
    add("resume", bool(cfg.resume_id), "ID резюме настроен; принадлежность проверяет resumes select" if cfg.resume_id
        else "Выберите своё резюме: resumes list, затем resumes select --id ID")
    try:
        validate_search_source(cfg)
        add("search", True, "Источник вакансий настроен; параметры запроса скрыты")
    except ConfigError:
        add("search", False, "Настройте поиск по выбранному резюме или [search] mode/query")
    root = data_dir.resolve()
    cache = root / "data" / "resume.txt"
    identity = root / "data" / "resume.txt.id"
    try:
        cached = bool(cfg.resume_id and cache.stat().st_size and identity.read_text(encoding="utf-8").strip() == cfg.resume_id)
    except (OSError, UnicodeError):
        cached = False
    add("resume_cache", cached, "Кэш привязан к выбранному ID" if cached else "Обновите выбранный текст: resumes fetch", warning=True)
    try:
        read_snapshot(root / "data" / "hh-session.json")
        add("session", True, "Локальный файл сессии корректен; актуальность на hh проверяет session check")
    except SessionError:
        add("session", False, "Нужен локальный session login и перенос проверенной сессии; cookies не выводятся")
    add("headless", cfg.browser.headless,
        "Невидимый Linux-режим: Chrome с внутренним Xvfb" if cfg.browser.headless and backend == "seleniumbase_uc"
        else "Headless включён" if cfg.browser.headless else "Включите [browser] headless для сервера")
    request_timing = {
        "limits": {name: getattr(cfg.limits, name) for name in (
            "submission_interval_seconds", "submission_batch_size", "submission_batch_pause_minutes",
            "captcha_cooldown_minutes", "read_interval_seconds", "max_responses_per_day")},
        "watch": {name: getattr(cfg.watch, name) for name in (
            "robot_every_minutes", "robot_max_chats_per_check", "robot_reply_interval_seconds")},
    }
    limits, watch = request_timing["limits"], request_timing["watch"]
    add("request_timing", True,
        f"Настройки: интервал откликов {limits['submission_interval_seconds']:g} с; "
        f"серия {limits['submission_batch_size']} и пауза {limits['submission_batch_pause_minutes']:g} мин; "
        f"CAPTCHA-пауза {limits['captcha_cooldown_minutes']:g} мин; "
        f"интервал чтения {limits['read_interval_seconds']:g} с; "
        f"дневной лимит откликов {limits['max_responses_per_day']}. "
        "0 отключает соответствующее ограничение; текущие сроки пауз показывает server --status")
    add("robot_timing", True,
        f"Настройки роботов: цикл {watch['robot_every_minutes']} мин; "
        f"открытий чатов за цикл не более {watch['robot_max_chats_per_check']}; "
        f"интервал ответов {watch['robot_reply_interval_seconds']:g} с. "
        "0 отключает ограничение открытий или интервала ответов; очередь может занять несколько циклов")
    automatic = bool(cfg.schedule.enabled or cfg.watch.robot_replies)
    add("automation", True, "Автозадачи включены: проверьте расписание перед запуском" if automatic else
        "Автозадачи выключены; init не отправляет отклики и сообщения")
    return {"ok": not any(check["status"] == "error" for check in checks), "checks": checks,
            "automatic_tasks_enabled": automatic, "browser_backend": backend, "request_timing": request_timing}


def _safe_error(error) -> str:
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", str(error))
    return re.sub(r"https?://[^\s<>]+", "[URL скрыт]", text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "doctor"))
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("HH_AUTO_HOME") or PROJECT_DIR / "hh-data"))
    parser.add_argument("--config", type=Path, help="doctor: явный конфиг; по умолчанию <data-dir>/config.server.toml")
    parser.add_argument("--env-file", type=Path, help="doctor: явный .env; по умолчанию рядом с конфигом")
    parser.add_argument("--provider", choices=tuple(AI_PROVIDERS), default="openai")
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--gender", choices=("male", "female"), default="male")
    parser.add_argument("--browser-backend", choices=BROWSER_BACKENDS, default="playwright",
                        help="init: playwright или seleniumbase_uc (Linux: Chrome с внутренним Xvfb)")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--competency-map", type=Path)
    parser.add_argument("--resume", default="", help="ID/URL; принадлежность позже проверяется через resumes select")
    parser.add_argument("--query", default="")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "init" and (args.config or args.env_file):
        parser.error("init использует <data-dir>/config.server.toml и .env; --config/--env-file предназначены для doctor")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        if args.action == "init":
            result = initialize(args.data_dir, provider=args.provider, model=args.model, gender=args.gender,
                profile_source=args.profile, map_source=args.competency_map, resume=args.resume,
                base_url=args.base_url, query=args.query, browser_backend=args.browser_backend, overwrite=args.overwrite)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print("Конфигурация создана: " + result["config"])
                print("Браузерный движок: " + result["browser_backend"])
                print("Заполните ключ вручную: " + result["env_file"])
                print("Расписание и автоответы выключены. Далее: session login → resumes list/select/fetch → configure doctor.")
            return 0
        result = doctor(args.config or args.data_dir / "config.server.toml", args.data_dir, env_file=args.env_file)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for check in result["checks"]:
                print(f"{check['status'].upper()}: {check['item']}: {check['message']}")
            print("Локальная проверка завершена. Браузер и модель не запускались.")
        return 0 if result["ok"] else 2
    except (SetupError, ConfigError, ProfileError, OSError, UnicodeError, ValueError) as error:
        print("Ошибка настройки: " + _safe_error(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
