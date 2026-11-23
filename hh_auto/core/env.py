"""Load application secrets from explicit local .env files without executing or expanding them."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from .paths import PROJECT_DIR, is_frozen


def load_local_env(config_path: Path) -> None:
    """Process environment wins; a .env beside config wins over the source root .env.

    Do not search parent folders or interpolate variables: secrets are literal text.
    Packaged applications use only the selected data/config directory.
    """
    paths = [config_path.resolve().parent / ".env"]
    if not is_frozen():
        paths.append(PROJECT_DIR / ".env")
    for path in dict.fromkeys(paths):
        if path.is_file():
            load_dotenv(path, override=False, interpolate=False, encoding="utf-8-sig")
