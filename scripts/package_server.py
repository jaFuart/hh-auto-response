"""Собрать переносимый архив сервера из явного списка файлов, без профиля браузера и журналов.

    python scripts/package_server.py
    python scripts/package_server.py --with-private-data

Второй вариант содержит ключ и сессию hh. Храните его как пароль и переносите только на свой сервер.
Скрипт ничего не отправляет в сеть и не запускает бота.
"""

from __future__ import annotations

import argparse
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREFIX = "hh-bot"
SOURCE_FILES = (
    "requirements.txt", "config.example.toml", "config.openai.example.toml", "config.server.example.toml",
    "candidate_profile.example.toml",
    "candidate_profile.example.json", "competency_map.example.md", "CONTRIBUTING.md",
    "docs/CONFIGURATION.md", "docs/ollama.md",
    ".env.example", ".dockerignore", "README.md", "SERVER_RU.md", "SETUP_RU.md", "LICENSE.md",
)
SOURCE_DIRS = {"hh_auto": {".py"}, "scripts": {".py", ".ps1", ".sh", ".cmd"},
               "docker": {".sh", ".yml", ".yaml"}}
PRIVATE_FILES = (".env", "hh-data/.env", "hh-data/config.server.toml")
PRIVATE_STATE = (
    "hh-session.json", "state.json", "responses.jsonl", "chat_answers.jsonl", "resume.txt", "resume.txt.id",
    "server-state.json", "watch.json", "pacing.json", "negotiations.json", "application-delivery.json",
    "candidate_profile.toml",
    "candidate_profile.json", "competency_map.md",
    "candidate_profile_hh.json", "competency_map_maxim.md", "hh_bot_codex_task.md",
)
MAX_PRIVATE_BYTES = 32 * 1024 * 1024
EXCLUDED_PARTS = {"__pycache__", ".git", ".venv", ".venv-build", ".tmp", "logs", "browser-profile", "deploy"}
EXCLUDED_SOURCE_PATHS = (
    "hh_auto/ui", "hh_auto/core/updater.py", "hh_auto/core/power.py",
    "scripts/gui.py", "scripts/start_windows.ps1", "scripts/setup_windows.ps1",
)


class PackageError(Exception):
    pass


@dataclass(frozen=True)
class PackageResult:
    path: Path
    files: int
    private_files: int


def _regular_inside(root: Path, path: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    return (path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root)
            and not any(relative == excluded or relative.startswith(excluded + "/") for excluded in EXCLUDED_SOURCE_PATHS)
            and not any(part in EXCLUDED_PARTS for part in path.relative_to(root).parts))


def collect_files(root: Path, *, with_private_data: bool = False) -> list[tuple[Path, bool]]:
    root = root.resolve()
    chosen: dict[Path, bool] = {}
    for name in SOURCE_FILES:
        path = root / name
        if _regular_inside(root, path):
            chosen[path] = False
    for folder, extensions in SOURCE_DIRS.items():
        base = root / folder
        if not base.is_dir() or base.is_symlink():
            continue
        for path in base.rglob("*"):
            if not _regular_inside(root, path):
                continue
            if any(part.startswith(".") for part in path.relative_to(base).parts):
                continue
            if path.suffix in extensions or (folder == "docker" and path.name == "Dockerfile"):
                chosen[path] = False
    if with_private_data:
        private_names = (*PRIVATE_FILES, *(f"hh-data/data/{name}" for name in PRIVATE_STATE))
        for name in private_names:
            path = root / name
            if _regular_inside(root, path):
                if path.stat().st_size > MAX_PRIVATE_BYTES:
                    raise PackageError(f"приватный файл превышает 32 МиБ: {name}; архив не создан")
                chosen[path] = True
    return sorted(chosen.items(), key=lambda item: item[0].relative_to(root).as_posix())


def package_server(root: Path = PROJECT_ROOT, output: Path | None = None, *,
                   with_private_data: bool = False) -> PackageResult:
    root = root.expanduser().resolve()
    output = (output or root / "hh-data" / "deploy" / "hh-bot-server.tar.gz").expanduser().resolve()
    if not output.name.endswith(".tar.gz"):
        raise PackageError("имя архива должно заканчиваться на .tar.gz")
    files = collect_files(root, with_private_data=with_private_data)
    if not files:
        raise PackageError("нет серверных файлов для архива")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".tmp")
    paths = [(path, PurePosixPath(PREFIX) / path.relative_to(root).as_posix(), private)
             for path, private in files if path != output and path != temp]
    directories = {PurePosixPath(PREFIX)}
    for _, archive_path, _ in paths:
        directories.update(parent for parent in archive_path.parents if str(parent) != ".")
    try:
        with tarfile.open(temp, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for folder in sorted(directories, key=lambda path: (len(path.parts), str(path))):
                info = tarfile.TarInfo(folder.as_posix())
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                archive.addfile(info)
            for path, archive_path, private in paths:
                info = tarfile.TarInfo(archive_path.as_posix())
                info.size = path.stat().st_size
                info.mtime = int(path.stat().st_mtime)
                info.mode = 0o600 if private else (0o755 if path.suffix == ".sh" else 0o644)
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
        os.chmod(temp, 0o600)
        temp.replace(output)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return PackageResult(output, len(paths), sum(private for _, _, private in paths))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--with-private-data", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = package_server(args.source_root, args.output, with_private_data=args.with_private_data)
    except (PackageError, OSError, tarfile.TarError) as error:
        parser.exit(1, f"Архив не создан: {error}\n")
    print(f"Архив: {result.path}")
    print(f"Файлов: {result.files}; приватных: {result.private_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
