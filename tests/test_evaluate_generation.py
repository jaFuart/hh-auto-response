"""Isolated replay: source coverage, no hh transport, and no model call without --live."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hh_auto.core.config import Config
from scripts.evaluate_generation import (EvaluationError, evaluate, load_cases, model_network_only,
                                          readable_report, refresh_metrics, text_metrics)


class GenerationEvaluation(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.letters = [{"id": str(i), "title": f"Python {i}", "company": f"Company {i}", "url": "https://hh.ru/vacancy/1",
                         "description": "Build Python APIs", "old_text": f"Actual sent {i}",
                         "old_attached": True, "old_source": "actual_export"} for i in range(10)]
        self.chats = [{"id": str(i), "chat_id": "chat", "question": f"Question {i}?",
                       "transcript": f"Question {i}?", "sent": [], "old_text": "Old answer"} for i in range(5)]
        self.cfg = Config(search_url="")

    def _exports(self):
        items = [{"id": row["id"], "title": row["title"], "company": row["company"], "url": row["url"],
                  "vacancy_text": row["description"], "letter_text": "Do not prefer page copy"} for row in self.letters]
        pages = {"items": items + [dict(items[0])]}
        actual = {"items": [{"id": row["id"], "letter_text": row["old_text"], "letter_attached": True}
                             for row in self.letters]}
        messages, history = [], []
        for row in self.chats:
            messages.extend([{"id": row["id"], "text": row["question"], "robot": True, "outgoing": False},
                             {"id": "answer-" + row["id"], "text": "Actual answer " + row["id"], "outgoing": True}])
            history.append({"question": row["question"], "answer": row["old_text"], "sent": True})
        chat = {"chat_id": "chat", "messages": messages, "confirmed_sent_log": history}
        for filename, payload in (("vacancy_pages.json", pages), ("applications_source.json", actual), ("robot_chat.json", chat)):
            (self.root / filename).write_text(json.dumps(payload), encoding="utf-8")

    def test_default_offline_never_constructs_client_and_keeps_actual_baseline(self):
        factory = Mock(side_effect=AssertionError("Must remain offline"))
        result = evaluate(self.cfg, self.letters, self.chats, "", client_factory=factory)
        factory.assert_not_called()
        self.assertEqual(result["coverage"], {"letters": 10, "chats": 5, "all_cases_present": True,
                                              "new_letters_generated": 0, "new_chat_answers_generated": 0})
        self.assertEqual(result["letters"][0]["old"]["text"], "Actual sent 0")
        self.assertEqual(result["outbound_messages"], 0)

    def test_loader_deduplicates_ids_and_replays_prefix_only(self):
        self._exports()
        letters, chats = load_cases(self.root)
        self.assertEqual(len(letters), 10)
        self.assertEqual(letters[0]["old_text"], "Actual sent 0")
        self.assertEqual(chats[0]["transcript"], "Question 0?")
        self.assertNotIn("Actual answer 0", chats[0]["transcript"])
        self.assertIn("Actual answer 0", chats[1]["transcript"])
        self.assertNotIn("Actual answer 1", chats[1]["transcript"])
        self.assertEqual(chats[1]["sent"], ["Actual answer 0"])

    def test_missing_case_is_explicit_failure_not_partial_success(self):
        self._exports()
        path = self.root / "vacancy_pages.json"
        data = json.loads(path.read_text())
        data["items"] = data["items"][1:-1]
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(EvaluationError, "9"):
            load_cases(self.root)

    def test_live_collects_errors_and_covers_all_fifteen_cases(self):
        class FakeClient:
            last_letter_record = {}
            last_chat_record = {}
            notes = []

            def __init__(self):
                self.letters, self.chats, self.recents = [], [], []

            def write_letter(self, *args, vacancy_id, recent_letters, **kwargs):
                self.letters.append(vacancy_id)
                self.recents.append(list(recent_letters))
                if vacancy_id == "2":
                    raise RuntimeError("Transient failure sk-private0123456789")
                self.last_letter_record = {"selected_projects": ["API"], "final_letter": "New " + vacancy_id}
                return "New " + vacancy_id

            def decide(self, resume, context, transcript, sent, buttons):
                self.chats.append(transcript)
                return SimpleNamespace(kind="question", reply="New answer", button="")

        client = FakeClient()
        report = evaluate(self.cfg, self.letters, self.chats, "Resume", live=True, client_factory=lambda _: client)
        self.assertEqual(len(client.letters), 10)
        self.assertEqual(len(client.chats), 5)
        self.assertEqual(client.recents[3], ["New 0", "New 1"])
        self.assertEqual(report["coverage"]["new_letters_generated"], 9)
        self.assertEqual(report["coverage"]["new_chat_answers_generated"], 5)
        self.assertIn("[REDACTED]", report["letters"][2]["new"]["error"])
        self.assertNotIn("sk-private", json.dumps(report))
        self.assertEqual(report["letters"][1]["new"]["metadata"]["selected_projects"], ["API"])

    def test_startup_error_is_recorded_for_all_cases_without_leaking_secret(self):
        self.cfg.ai.api_key = "a-long-synthetic-secret"
        factory = Mock(side_effect=ValueError(self.cfg.ai.api_key))
        report = evaluate(self.cfg, self.letters, self.chats, "Resume", live=True, client_factory=factory)
        self.assertEqual(sum(r["new"]["status"] == "error" for r in report["letters"] + report["chats"]), 15)
        self.assertNotIn(self.cfg.ai.api_key, readable_report(report))

    def test_network_guard_blocks_all_offline_and_hh_online(self):
        with model_network_only("https://api.openai.com/v1", live=False):
            with self.assertRaises(EvaluationError):
                urllib.request.urlopen("https://api.openai.com/v1/models")
        with model_network_only("https://api.openai.com/v1", live=True):
            with self.assertRaises(EvaluationError):
                urllib.request.urlopen("https://hh.ru/chat")
        with self.assertRaises(EvaluationError):
            with model_network_only("https://hh.ru/api", live=True):
                pass

    def test_metrics_are_objective_and_do_not_gate_delivery(self):
        good = text_metrics("1. Автоматизирую проверки.\n2. Интересны задачи анализа рисков.", vacancy_id="139438300")
        bad = text_metrics("Здравствуйте! Какие задачи? Как устроена команда?", vacancy_id="139438300")
        self.assertTrue(good["instruction_checks"]["two_opening_answers_before_greeting"])
        self.assertFalse(bad["instruction_checks"]["two_opening_answers_before_greeting"])
        self.assertEqual(text_metrics("git status, git diff and git status")["git_commands"], ["git diff", "git status"])

    def test_project_aliases_count_canonical_and_known_unselected_cases(self):
        projects = [SimpleNamespace(name="TrinityEvents: ETL и интеграции"), SimpleNamespace(name="EduMind (MVP)")]
        metrics = text_metrics("В TrinityEvents автоматизировал загрузки; в EduMind работал над API.", projects=projects)
        self.assertEqual(metrics["explicit_project_name_mentions"], ["TrinityEvents: ETL и интеграции", "EduMind (MVP)"])
        self.assertEqual(text_metrics("Разрабатываю произвольный ETL.", projects=projects)["explicit_project_name_mentions"], [])

    def test_two_required_answers_can_extend_beyond_450_chars_but_must_precede_greeting(self):
        first = "1. " + "Подтверждённый опыт автоматизации. " * 30
        good = first + "\n2. Интересен анализ рисков.\nЗдравствуйте!"
        bad = first + "\nЗдравствуйте!\n2. Интересен анализ рисков."
        key = "two_opening_answers_before_greeting"
        self.assertTrue(text_metrics(good, vacancy_id="139438300")["instruction_checks"][key])
        self.assertFalse(text_metrics(bad, vacancy_id="139438300")["instruction_checks"][key])

    def test_metric_refresh_preserves_all_text_and_generation_evidence_without_model_or_network(self):
        report = evaluate(self.cfg, self.letters, self.chats, "")
        record = report["letters"][0]
        record["new"].update(status="generated", text="Saved real model output", metadata={
            "drafts": ["Original draft"], "model": "Original model", "selected_projects": ["Original case"]})
        record["new"]["metrics"] = {"stale": True}
        before = copy.deepcopy(report)
        with patch("hh_auto.ai.client.AiClient", side_effect=AssertionError("No model")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
            refreshed = refresh_metrics(report, self.cfg, "Python resume")
        self.assertNotIn("stale", refreshed["letters"][0]["new"]["metrics"])
        self.assertEqual(report, before)
        self.assertEqual(refreshed["letters"][0]["new"]["text"], before["letters"][0]["new"]["text"])
        for collection in ("letters", "chats"):
            for current, original in zip(refreshed[collection], before[collection]):
                for side in ("old", "new"):
                    current[side].pop("metrics")
                    original[side].pop("metrics")
                self.assertEqual(current, original)


if __name__ == "__main__":
    unittest.main()
