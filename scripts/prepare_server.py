"""Подготовить отдельные серверные настройки из текущего config.toml."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto.core.config import Config, ConfigError, load_config, save_config, validate_config, validate_search_source
from hh_auto.core.paths import app_root


def server_config(source: Config) -> Config:
    """Сохранить резюме, фильтры, лимиты и тексты, задав ежедневный запуск без окна."""
    cfg = deepcopy(source)
    cfg.browser.headless = True
    cfg.browser.hide_window = False
    cfg.browser.channel = ""
    cfg.browser.profile_dir = "browser-profile-server"
    cfg.geo.timezone = "Europe/Moscow"
    cfg.schedule.enabled = True
    cfg.schedule.times = ["10:00"]
    cfg.schedule.days = [str(day) for day in range(1, 8)]
    cfg.schedule.chats_after = False
    cfg.schedule.keep_awake = False
    cfg.watch.robot_replies = True
    cfg.watch.robot_every_minutes = 5
    cfg.ai.enabled = True
    cfg.ai.only_robot = True
    cfg.chats.mode = "all"
    validate_config(cfg)
    validate_search_source(cfg)
    if not cfg.resume_id:
        raise ConfigError("Для сервера сначала выберите резюме через scripts/resumes.py select или укажите resume_url.")
    return cfg


def prepare(source: Path, output: Path, *, overwrite: bool = False) -> Path:
    if source.resolve() == output.resolve():
        raise ConfigError("Серверный конфиг должен быть отдельным файлом; исходные настройки сохраняются.")
    if output.exists() and not overwrite:
        raise ConfigError(f"Файл уже существует: {output}. Для обновления используйте --overwrite.")
    cfg = server_config(load_config(source, check_search_url=False, upgrade=False))
    output.parent.mkdir(parents=True, exist_ok=True)
    # Конфиг может содержать пользовательский контекст и сохранённый API-ключ.
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        save_config(cfg, temporary)
        temporary.replace(output)
        if os.name != "nt":
            output.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def main(argv: list[str] | None = None) -> int:
    root = app_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=root / "config.toml")
    parser.add_argument("--output", type=Path, default=root / "config.server.toml")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        output = prepare(args.source, args.output, overwrite=args.overwrite)
    except (ConfigError, OSError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    print(f"Серверный конфиг сохранён: {output}")
    print("Отклики ежедневно в 10:00 Europe/Moscow; роботы каждые 5 минут; браузер без окна.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
