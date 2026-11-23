"""Export verified hh authentication for a server, or check it without applications/messages."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto.app import _with_browser  # noqa: E402
from hh_auto.core.config import ConfigError, load_config  # noqa: E402
from hh_auto.core.logger import log, setup_logging  # noqa: E402
from hh_auto.core.paths import PROJECT_DIR  # noqa: E402
from hh_auto.web.session import SessionError, save_session, snapshot_path  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Временный локальный вход hh и перенос сессии на сервер, без откликов и сообщений")
    parser.add_argument("action", choices=("login", "export", "check"))
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("HH_AUTO_HOME") or PROJECT_DIR / "hh-data"))
    parser.add_argument("--config", type=Path, help="конфигурация профиля браузера")
    parser.add_argument("--headless", action="store_true", help="экспортировать из профиля без открытия окна")
    parser.add_argument("--session", "--output", type=Path, help="файл сессии (по умолчанию HH_AUTO_HOME/data/hh-session.json)")
    parser.add_argument("--restore-session", action="store_true", help="явно восстановить файл даже в существующий профиль")
    parser.add_argument("--channel", choices=("chrome", "msedge"), help="только для локального login: установленный Chrome/Edge вместо Chromium")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = args.data_dir.expanduser().resolve()
    env_names = ("HH_AUTO_HOME", "HH_AUTO_SESSION_PATH", "HH_AUTO_RESTORE_SESSION")
    previous_env = {name: os.environ.get(name) for name in env_names}
    os.environ["HH_AUTO_HOME"] = str(root)
    if args.session is not None:
        os.environ["HH_AUTO_SESSION_PATH"] = str(args.session.resolve())
    if args.restore_session:
        os.environ["HH_AUTO_RESTORE_SESSION"] = "1"
    try:
        cfg = load_config(args.config or root / "config.server.toml", check_search_url=False, upgrade=False)
        cfg.browser.headless = args.action != "login"
        cfg.browser.hide_window = False
        if args.action == "login":
            cfg.browser.profile_dir = "browser-profile-login"
            cfg.browser.channel = args.channel or ""
            cfg.browser.login_timeout = max(cfg.browser.login_timeout, 600)
        log_path = setup_logging(root / "logs", False, name="session")

        def verified(page):
            if args.action in ("login", "export"):
                counts = save_session(page.context)
                log.success(f"Сессия hh экспортирована: {snapshot_path()} (cookies: {counts['cookies']}, "
                            f"localStorage origins: {counts['origins']})")
            else:
                log.success("Вход соискателя hh проверен. Отклики и сообщения не отправлялись.")

        return _with_browser(cfg, root, verified, auto_raise=False, login=args.action == "login")
    except (ConfigError, SessionError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    finally:
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == "__main__":
    sys.exit(main())
