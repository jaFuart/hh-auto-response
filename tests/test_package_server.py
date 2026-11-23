"""Переносимый серверный архив: явный allowlist, приватные файлы только по флагу, безопасные права."""

from __future__ import annotations

import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.package_server import package_server


class ServerPackage(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        fixtures = {
            "hh_auto/core/server.py": "# source\n", "scripts/server.py": "# source\n",
            "docker/Dockerfile": "FROM python:3.11\n", "docker/entrypoint.sh": "#!/bin/sh\n",
            "docker/docker-compose.yml": "services: {}\n", "requirements.txt": "playwright\n",
            "config.server.example.toml": "# example\n", ".env.example": "OPENAI_API_KEY=\n",
            "README.md": "Readme\n", "SERVER_RU.md": "Server\n", "LICENSE.md": "License\n",
            ".env": "OPENAI_API_KEY=private-test-key\n", "hh-data/config.server.toml": "# private config\n",
            "hh-data/.env": "OPENAI_API_KEY=data-local-private-key\n",
            "hh-data/data/hh-session.json": '{"private-test-cookie":true}',
            "hh-data/data/state.json": '{"applied":{}}', "hh-data/data/chat_answers.jsonl": "{}\n",
            "hh-data/data/resume.txt": "Private resume", "hh-data/data/resume.txt.id": "selected",
            "hh-data/data/candidate_profile.toml": "version = 1\n",
            "hh-data/data/market.json": "large analysis cache", "hh-data/config.toml": "# desktop config\n",
            "hh-data/browser-profile/chrome/Default/Cookies": "private browser database",
            "hh-data/logs/watch.log": "private logs", ".git/config": "git data", ".venv/secrets.py": "private env",
            ".tmp/debug.py": "private debug", "hh_auto/__pycache__/server.pyc": "bytecode",
            "scripts/.env": "private nested secret", "scripts/logs/secret.py": "private nested log",
            "hh_auto/ui/gui.py": "legacy gui", "hh_auto/core/updater.py": "legacy desktop updater",
            "hh_auto/core/power.py": "legacy sleep inhibitor", "scripts/gui.py": "legacy gui launcher",
            "scripts/start_windows.ps1": "legacy windows launcher", "scripts/setup_windows.ps1": "legacy windows setup",
        }
        for name, contents in fixtures.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")

    def members(self, result):
        with tarfile.open(result.path) as archive:
            return {member.name: member for member in archive.getmembers()}

    def test_default_archive_contains_code_only_and_never_includes_private_data(self):
        result = package_server(self.root)
        names = self.members(result)
        self.assertIn("hh-bot/hh_auto/core/server.py", names)
        self.assertIn("hh-bot/docker/Dockerfile", names)
        self.assertIn("hh-bot/.env.example", names)
        self.assertEqual(result.private_files, 0)
        self.assertNotIn("hh-bot/.env", names)
        self.assertFalse(any(name.startswith("hh-bot/hh-data/") for name in names))

    def test_private_flag_includes_only_small_whitelisted_session_resume_and_state(self):
        result = package_server(self.root, with_private_data=True)
        names = self.members(result)
        for name in (".env", "hh-data/.env", "hh-data/config.server.toml", "hh-data/data/hh-session.json",
                     "hh-data/data/state.json", "hh-data/data/chat_answers.jsonl", "hh-data/data/resume.txt",
                     "hh-data/data/candidate_profile.toml"):
            self.assertIn("hh-bot/" + name, names)
            self.assertEqual(names["hh-bot/" + name].mode, 0o600)
        self.assertNotIn("hh-bot/hh-data/config.toml", names)
        self.assertNotIn("hh-bot/hh-data/data/market.json", names)

    def test_both_modes_exclude_browser_logs_git_env_tmp_bytecode_and_archive_itself(self):
        for private in (False, True):
            with self.subTest(private=private):
                result = package_server(self.root, with_private_data=private)
                names = self.members(result)
                forbidden = ("browser-profile", "/logs/", ".git", ".venv", ".tmp", "__pycache__", "deploy", "scripts/.env",
                             "/ui/", "/updater.py", "/power.py", "/gui.py", "start_windows", "setup_windows")
                self.assertFalse(any(part in name for name in names for part in forbidden))
                self.assertFalse(any(name.endswith(".tar.gz") for name in names))
                self.assertTrue(all(member.mode == 0o700 for member in names.values() if member.isdir()))


if __name__ == "__main__":
    unittest.main()
