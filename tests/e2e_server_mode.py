"""Real headless browser + daily scheduler against an isolated hh/AI mock; no external requests."""

from __future__ import annotations

import os
import logging
import sys
import tempfile
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_hh import RESUME_ID, FakeLlm, MockChat, MockHh, use_mock, vacancy, write_config
from hh_auto import app
from hh_auto.core.config import load_config
from hh_auto.core.logger import setup_logging
from hh_auto.core.server import ServerRunner, runner_lock
from hh_auto.hh.runner import RunOptions, Stats


@contextmanager
def scratch():
    with tempfile.TemporaryDirectory(prefix="hh-server-e2e-") as folder:
        try:
            yield Path(folder)
        finally:
            logging.shutdown()


def main():
    llm = FakeLlm()
    try:
        with scratch() as root:
            os.environ["HH_AUTO_HOME"] = str(root)
            mock = MockHh(pages=[[vacancy(951, "Разработчик Python", "modal")]], chats=[
                MockChat("952", "Прочитанный вопрос робота", "Компания", [
                    {"text": "В каком городе вы живёте?", "author": "Компания", "robot": True}],
                         unread=False, script=[
                             {"text": "Спасибо за ответы! Анкета завершена.", "author": "Компания", "robot": True}])])
            use_mock(mock)
            write_config(root / "config.toml", llm.url, watch__robot_replies=True)
            cfg = load_config(root / "config.toml")
            cfg.browser.headless = False  # The scheduler must override this for both actual browser jobs.
            cfg.schedule.enabled = True
            cfg.schedule.times = ["10:00"]
            cfg.schedule.days = list("1234567")
            cfg.ai.only_robot = True
            log_path = setup_logging(root / "logs", name="server-e2e")
            used_headless = []

            def responses(job_cfg):
                used_headless.append(job_cfg.browser.headless)
                return app.run_responses(job_cfg, RunOptions(limit=1), root, Stats(), log_path)

            def robots(job_cfg):
                used_headless.append(job_cfg.browser.headless)
                return app.run_background(job_cfg, root, log_path, login=False, tasks=["robot_replies"])

            now = datetime(2026, 11, 14, 7, 0, tzinfo=timezone.utc)  # Moscow 10:00 on a UTC host.
            with runner_lock(root):
                runner = ServerRunner(root, responses, robots, clock=lambda: now)
                assert runner.tick(cfg) == [("responses", 0), ("robot_replies", 0)]
            assert used_headless == [True, True]
            applications = [row for row in mock.sent if row["id"] == "951"]
            assert len(applications) == 1
            assert applications[0]["native_form"] is True and applications[0]["letter"].strip()
            assert applications[0]["resume"] == RESUME_ID
            assert "/applicant/vacancy_response" in mock.visited
            assert mock.chat_sent == [{"chat": "952", "text": "Живу в городе Москва.", "kind": "text"}]
            assert not mock.outside, "A browser request escaped the mock"
            with runner_lock(root):
                restarted = ServerRunner(root, responses, robots, clock=lambda: now)
                assert restarted.tick(cfg) == [], "Restart must not repeat today's applications or chat check"
            assert used_headless == [True, True]
            assert not cfg.browser.headless, "The source desktop config was changed"
            print("PASS: Moscow daily slot, real headless responses + read robot reply, restart dedupe, no outside traffic.", flush=True)
    finally:
        llm.close()


if __name__ == "__main__":
    main()
