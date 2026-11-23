"""Retries count claims, preserve ID deduplication, and carry generation metadata."""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from hh_auto.ai.client import Decision
from hh_auto.core.config import Config
from hh_auto.hh import chats
from tests.test_robot_chats import MemoryLog


class RobotRetryLogging(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        self.cfg.ai.enabled = self.cfg.ai.only_robot = True
        self.question = "Какие команды Git используете?"
        self.chat = chats._Chat("chat1", "Python", "Company", self.question, False, latest_at="12:00")
        self.sender = chats._Sender("Помощник", "chatik-chat-message-123", provider_robot=True)
        self.key = chats._turn_key(self.chat, self.question, self.sender)
        self.log, self.stats = MemoryLog(), chats.ChatStats()
        self.ai = Mock(notes=[], last_chat_record={"prompt_version": "v2", "selected_case": "Git",
                                                  "api_key": "synthetic-secret", "nested": {"authorization": "test"}})
        self.ai.decide.return_value = Decision("question", self.question, "Использую git status, git diff и git commit.")
        self.patches = [patch.object(chats, "_buttons", return_value=[]),
                        patch.object(chats, "_wait_next_message", return_value=None),
                        patch.object(chats, "_transcript", return_value=self.question),
                        patch.object(chats, "_current_sender", return_value=self.sender),
                        patch.object(chats, "_send", return_value=True)]
        self.buttons, self.wait, self.transcript, self.current, self.send = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def converse(self):
        return chats._converse(Mock(url="https://hh.ru/chat/chat1"), self.chat, self.cfg, self.ai, "Python resume", self.stats, False,
                               self.log, [], self.question, "chat")

    def released(self, number):
        for _ in range(number):
            chats._log_answer(self.log, self.chat, self.question, "Earlier draft", sent=False,
                              turn_key=self.key, pending=True)
            chats._log_answer(self.log, self.chat, self.question, "Earlier draft", sent=False,
                              turn_key=self.key, released=True)

    def test_two_released_attempts_allow_exactly_third_send_with_metadata(self):
        self.released(2)
        self.converse()
        self.ai.decide.assert_called_once()
        self.send.assert_called_once()
        self.assertEqual(chats._send_attempts(self.log, self.chat.id, self.key), 3)
        self.assertEqual(chats._past_answers(self.log, self.chat.id),
                         (["Использую git status, git diff и git commit."], {self.key}))
        rows = [json.loads(line) for line in self.log.text.splitlines()]
        self.assertEqual(rows[-2]["generation"], rows[-1]["generation"])
        self.assertEqual(rows[-1]["generation"]["selected_case"], "Git")
        self.assertEqual(rows[-1]["generation"]["api_key"], "[REDACTED]")
        self.assertNotIn("synthetic-secret", self.log.text)

    def test_three_released_attempts_stop_before_fourth_generation_or_send(self):
        self.released(3)
        old = self.log.text
        with patch.object(chats.log, "warning") as warning:
            self.converse()
        self.ai.decide.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual(self.log.text, old)
        self.assertIn("3", warning.call_args.args[0])

    def test_new_message_id_has_independent_budget_after_prior_turn_exhaustion(self):
        self.released(3)
        self.current.return_value = chats._Sender("Помощник", "chatik-chat-message-124", provider_robot=True)
        self.converse()
        self.send.assert_called_once()

    def test_proven_pre_send_release_keeps_same_generation_record(self):
        self.send.side_effect = chats._SendNotAttempted("composer unavailable")
        self.converse()
        rows = [json.loads(line) for line in self.log.text.splitlines()]
        self.assertEqual(rows[0]["generation"], rows[1]["generation"])
        self.assertTrue(rows[0]["pending"])
        self.assertTrue(rows[1]["released"])
        self.assertEqual(chats._past_answers(self.log, self.chat.id), ([], set()))
        self.assertEqual(chats._send_attempts(self.log, self.chat.id, self.key), 1)

    def test_dry_run_claims_do_not_consume_attempt_budget(self):
        record = {"chat_id": self.chat.id, "turn_key": self.key, "pending": True, "dry_run": True}
        self.log.text = "\n".join(json.dumps(record) for _ in range(4))
        self.assertEqual(chats._send_attempts(self.log, self.chat.id, self.key), 0)

    def test_ambiguous_pending_is_never_released_or_retried(self):
        self.send.return_value = False
        self.converse()
        self.ai.decide.reset_mock()
        self.send.reset_mock()
        self.converse()
        self.ai.decide.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual(chats._send_attempts(self.log, self.chat.id, self.key), 1)
        self.assertIn(self.key, chats._past_answers(self.log, self.chat.id)[1])


if __name__ == "__main__":
    unittest.main()
