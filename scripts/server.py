"""Сервер без окна: календарные отклики и последовательная проверка роботов.

    python scripts/server.py --status   только состояние, без браузера и изменения файлов
    python scripts/server.py --once     выполнить один цикл задач, которым подошло время
    python scripts/server.py            постоянно, по config.toml; часы Europe/Moscow

Отклики разрешены только [schedule] enabled=true. При 10:00 окно догоняющего запуска по умолчанию до 11:00.
Явно отложенный слот продолжается в тот же день; неоднозначно прерванный слот не повторяется. Роботы проверяются только при
[watch] robot_replies=true; между завершением проверок выдерживается robot_every_minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto import app  # noqa: E402
from hh_auto.core import control  # noqa: E402
from hh_auto.core.config import ConfigError, load_config  # noqa: E402
from hh_auto.core.logger import log, setup_logging  # noqa: E402
from hh_auto.core.paths import PROJECT_DIR  # noqa: E402
from hh_auto.core.rate_limits import EXIT_DEFERRED, PacingStateError  # noqa: E402
from hh_auto.core.server import (DEFAULT_CATCH_UP_MINUTES, DEFAULT_TIMEZONE, ServerError, ServerRunner,
                                  ResponseRunResult, runner_lock)  # noqa: E402
from hh_auto.hh.runner import RunOptions, Stats  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("HH_AUTO_HOME") or PROJECT_DIR / "hh-data"))
    parser.add_argument("--config", type=Path, help="по умолчанию <data-dir>/config.toml")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--catch-up-minutes", type=int, default=DEFAULT_CATCH_UP_MINUTES)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="только JSON состояния, без браузера/записей")
    mode.add_argument("--once", action="store_true", help="один цикл только наступивших задач")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds должен быть от 1 до 60")
    if not 0 <= args.catch_up_minutes <= 1440:
        parser.error("--catch-up-minutes должен быть от 0 до 1440")
    args.data_dir = args.data_dir.expanduser().resolve()
    args.config = args.config.expanduser().resolve() if args.config else args.data_dir / "config.toml"
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    previous_data_home = os.environ.get("HH_AUTO_HOME")
    os.environ["HH_AUTO_HOME"] = str(args.data_dir)
    previous_handlers = {}
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    stop = threading.Event()

    def request_stop(*_):
        stop.set()
        control.request_stop()

    def responses(cfg):
        stats = Stats()
        code = app.run_responses(cfg, RunOptions(limit=cfg.limits.max_responses), args.data_dir, stats, log_path)
        return ResponseRunResult(code, stats.applied)

    def robots(cfg):
        return app.run_background(cfg, args.data_dir, log_path, login=False, tasks=["robot_replies"])

    def retries(cfg):
        stats = Stats()
        code = app.run_responses(cfg, RunOptions(retry_only=True, limit=min(3, cfg.limits.max_responses)),
                                 args.data_dir, stats, log_path)
        return ResponseRunResult(code, stats.applied)

    try:
        # Отдельный серверный конфиг не переписывается ни проверкой состояния, ни периодической загрузкой.
        cfg = load_config(args.config, check_search_url=False, upgrade=False)
        if args.status:
            runner = ServerRunner(args.data_dir, responses, robots, timezone_name=args.timezone,
                                  catch_up_minutes=args.catch_up_minutes, retry_job=retries)
            print(json.dumps(runner.status(cfg), ensure_ascii=False, indent=2))
            return 0
        with runner_lock(args.data_dir):
            # Память читается под тем же межпроцессным lock, что защищает весь срок работы scheduler.
            runner = ServerRunner(args.data_dir, responses, robots, timezone_name=args.timezone,
                                  catch_up_minutes=args.catch_up_minutes, retry_job=retries)
            log_path = setup_logging(args.data_dir / "logs", args.verbose, name="server")
            log.info(f"сервер без окна: {args.timezone}; окно догоняющего запуска {args.catch_up_minutes} мин")
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[sig] = signal.signal(sig, request_stop)
            while not stop.is_set():
                cfg = load_config(args.config, check_search_url=False, upgrade=False)
                jobs = runner.tick(cfg)
                if args.once:
                    return 1 if any(code not in (0, EXIT_DEFERRED) for _, code in jobs) else 0
                stop.wait(args.poll_seconds)
        return 0
    except (control.StopRequested, KeyboardInterrupt):
        return 130
    except (ConfigError, ServerError, PacingStateError, OSError) as error:
        print(f"Сервер остановлен: {error}", file=sys.stderr)
        return 2
    finally:
        if previous_data_home is None:
            os.environ.pop("HH_AUTO_HOME", None)
        else:
            os.environ["HH_AUTO_HOME"] = previous_data_home
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
