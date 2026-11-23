"""Confirmed experience, explicit limitations and unknown technical topics remain distinct."""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from hh_auto.ai.client import AiClient, AiError, Decision, FormQuestion, concise_answer
from hh_auto.ai.profile import CandidateProfile
from hh_auto.core.config import Ai


class FactGuardTest(unittest.TestCase):
    def client(self, profile=None):
        client = AiClient(Ai(provider="custom", base_url="http://example.invalid/v1", model="test"))
        client.profile = profile or CandidateProfile()
        client.last_raw = ""
        return client

    def test_unknown_concept_can_be_explained_without_claiming_personal_experience(self):
        client = self.client()
        reply = "Kafka — распределённая платформа для обработки потоков событий."
        client.complete = Mock(side_effect=AiError("no live model"))
        decision = client._fix_reply(Decision("question", "Что такое Kafka?", reply), [], "Python", "")
        self.assertEqual(decision.reply, reply)

    def test_repairing_an_unfounded_denial_cannot_invent_another_tool(self):
        client = self.client(CandidateProfile(confirmed_skills=("Django",)))
        replies = iter([{"can_answer": True, "text": "Опыта с Django не было."},
                        {"can_answer": True, "text": "Работал с Django и Kubernetes."}])
        client._answer_question = lambda *args: next(replies, {"can_answer": True,
                                                               "text": "Работал с Django и Kubernetes."})
        fill = client.fill_form("Python", "", "Тестовая вакансия",
                                [FormQuestion(0, "Какой у вас опыт с Django?", "text")])
        self.assertTrue(fill.can_answer)
        self.assertIn("Django", fill.answers[0].text)
        self.assertNotIn("Kubernetes", fill.answers[0].text)
        self.assertNotRegex(fill.answers[0].text, r"(?i)не работал|опыта[^.!?]*(?:нет|не было)")

    def test_current_explicit_limitation_overrides_a_general_profile_skill(self):
        client = self.client(CandidateProfile(confirmed_skills=("Django",)))
        candidate = "Работал с Django."
        client.complete = lambda *args, **kwargs: json.dumps({"kind": "question", "question": "Опыт с Django?",
                                                             "reply": candidate}, ensure_ascii=False)
        decision = client._fix_reply(Decision("question", "Опыт с Django?", candidate), [], "Python",
                                     "Уточнение: с Django не работал.")
        if decision.kind == "question":
            self.assertNotRegex(decision.reply, r"(?i)(?<!не )\bработал с Django|(?<!не )\bиспользовал Django|есть.*опыт.*Django")

    def test_continued_learning_does_not_cancel_confirmed_practical_experience(self):
        client = self.client(CandidateProfile(confirmed_skills=("Django",)))
        self.assertTrue(client._unfounded_limitation("Опыт с Django?", "Python",
                                                     "Продолжаю изучать Django и его экосистему."))
        self.assertEqual(client._unsupported_technologies("Опыт с Django?", "Python",
                                                         "Продолжаю изучать Django и его экосистему."), [])

    def test_numeric_repair_cannot_override_an_explicit_limitation(self):
        client = self.client(CandidateProfile(confirmed_skills=("Django",)))
        question = FormQuestion(0, "Какой у вас опыт с Django?", "text")
        replies = iter([{"can_answer": True, "text": "С Django не работал, но запустил 7 сервисов."},
                        {"can_answer": True, "text": "Работал с Django."}])
        client._answer_question = lambda *args: next(replies, {"can_answer": True, "text": "Работал с Django."})
        fill = client.fill_form("Python", "С Django не работал.", "Вакансия", [question])
        self.assertTrue(fill.can_answer)
        self.assertNotRegex(fill.answers[0].text, r"(?i)(?<!не )\bработал с Django")
        self.assertNotIn("7", fill.answers[0].text)

    def test_factual_answer_drops_template_tail_but_keeps_a_standalone_unknown_answer(self):
        fact = "С Django работал. Разрабатывал серверную логику на Python."
        self.assertEqual(concise_answer(fact + " Конкретные задачи на Django готов обсудить отдельно."), fact)
        self.assertEqual(concise_answer("Готов обсудить на собеседовании."), "Готов обсудить на собеседовании.")


if __name__ == "__main__":
    unittest.main()
