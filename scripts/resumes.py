"""Список, выбор и текст собственного резюме через сохранённую сессию hh; без откликов и сообщений."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto.app import _with_browser, select_account_resume
from hh_auto.core.config import ConfigError, load_config
from hh_auto.core.config_file import write_values
from hh_auto.core.logger import log, setup_logging
from hh_auto.core.paths import PROJECT_DIR
from hh_auto.hh.raising import list_resumes
from hh_auto.hh.resume import fetch_resume_text
from hh_auto.web.browser import FatalError


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "select", "fetch"))
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("HH_AUTO_HOME") or PROJECT_DIR / "hh-data"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--id", help="ID собственного резюме из команды list")
    parser.add_argument("--json", action="store_true", help="список резюме в JSON; без текста резюме")
    args = parser.parse_args(argv)
    args.data_dir = args.data_dir.expanduser().resolve()
    args.config = args.config.expanduser().resolve() if args.config else args.data_dir / "config.server.toml"
    if args.action == "select" and not args.id:
        parser.error("для select нужен --id")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    previous_home = os.environ.get("HH_AUTO_HOME")
    os.environ["HH_AUTO_HOME"] = str(args.data_dir)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        cfg = load_config(args.config, check_search_url=False, upgrade=False)
        session_cfg = copy.deepcopy(cfg)
        session_cfg.browser.headless = True
        session_cfg.browser.hide_window = False
        log_path = setup_logging(args.data_dir / "logs", False, name="resumes")
        result = []

        def work(page):
            if args.action in ("list", "select"):
                owned = list_resumes(page)
                if args.action == "list":
                    result.extend({"id": item.id, "title": item.title, "selected": item.id == cfg.resume_id} for item in owned)
                    return
                if args.id not in {item.id for item in owned}:
                    raise FatalError("ID не найден среди собственных резюме; конфигурация не изменена")
                select_account_resume(cfg, args.id)
                write_values(args.config, {"": {"resume_url": cfg.resume_url, "search_url": cfg.search_url,
                                                "skip_on_resume_mismatch": True}})
                if os.name != "nt":
                    args.config.chmod(0o600)
                log.success(f"Выбрано резюме {args.id}; фильтры поиска сохранены. Для текста выполните resumes.py fetch.")
                return
            if not cfg.resume_id:
                raise FatalError("сначала выберите резюме: resumes.py list, затем select --id ID")
            cache = args.data_dir / "data" / "resume.txt"
            text = fetch_resume_text(page, cfg, cache, require_selected=True)
            if not text.strip():
                raise FatalError("текст выбранного резюме не прочитан; проверьте сессию и выбранный ID")
            if os.name != "nt":
                for path in (cache, Path(str(cache) + ".id")):
                    if path.exists():
                        path.chmod(0o600)
            log.success(f"Текст выбранного резюме сохранён: {cache} ({len(text)} символов)")

        code = _with_browser(session_cfg, args.data_dir, work, auto_raise=False, login=False)
        if args.action == "list" and code == 0:
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                for item in result:
                    print(f"{'*' if item['selected'] else ' '} {item['id']}  {item['title'] or '(без названия)'}")
                print(f"Всего резюме: {len(result)}. * — выбрано в конфигурации.")
        return code
    except (ConfigError, OSError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    finally:
        if previous_home is None:
            os.environ.pop("HH_AUTO_HOME", None)
        else:
            os.environ["HH_AUTO_HOME"] = previous_home


if __name__ == "__main__":
    raise SystemExit(main())
