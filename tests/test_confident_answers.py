"""Подтверждённый профиль дополняет резюме в чатах и анкетах; HTTP заменён ответами модели."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto.ai.client import AiClient, Decision, FormQuestion  # noqa: E402
from hh_auto.core.config import Ai  # noqa: E402

QUESTION = "Расскажите, с какими задачами в Django вы уже работали?"
RESUME = "Python-разработчик. FastAPI, PostgreSQL, REST API."
CONFIRMED = "С Django работал: создавал модели и представления, выполнял CRUD-операции."
NEGATIVE = "С Django не работал, но готов быстро освоить."
PROFILE = '''version = 1
role = "Python-разработчик"
confirmed_skills = ["Python", "Django", "PostgreSQL"]
clarifications = ["С Django работал: создавал модели и представления, выполнял CRUD-операции."]
'''


def chat_reply(text: str, question: str = QUESTION) -> dict:
    return {"kind": "question", "question": question, "reply": text}


class RecordedClient(AiClient):
    """Все преобразования настоящие; подменён только сетевой complete, включая редактор ответа."""

    def __init__(self, cfg: Ai, *replies: dict | str):
        super().__init__(cfg)
        self.replies = list(replies)
        self.requests: list[list[dict]] = []

    def complete(self, messages, **_kwargs) -> str:
        self.requests.append(copy.deepcopy(messages))
        if "Отвечает ли ответ по существу" in messages[-1]["content"]:
            return '{"ok": true}'
        if not self.replies:
            raise AssertionError("Неожиданный запрос к модели")
        result = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else result


class ConfidentAnswersTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="hh-candidate-facts-")
        self.addCleanup(directory.cleanup)
        self.profile_path = Path(directory.name) / "profile.toml"
        self.profile_path.write_text(PROFILE, encoding="utf-8")

    def client(self, *replies: dict | str) -> RecordedClient:
        return RecordedClient(Ai(provider="custom", base_url="http://example.invalid/v1", model="test",
                                 profile_path=str(self.profile_path)), *replies)

    def assert_confident_django(self, text: str):
        self.assertIn("Django", text)
        self.assertRegex(text.lower(), r"модел|представлен|crud")
        self.assertNotRegex(text.lower(), r"не работал|нет опыта|опыта.{0,15}не было|готов быстро освоить|знаком базово")
        self.assertNotIn("Google", text)
        self.assertNotRegex(text.lower(), r"\b(?:5|пять)\s+(?:лет|год)")

    def assert_profile_in_requests(self, requests: list[list[dict]]):
        prompts = [messages for messages in requests if any(message["role"] == "system" for message in messages)]
        self.assertTrue(prompts)
        for messages in prompts:
            body = "\n".join(message["content"] for message in messages if message["role"] == "user")
            self.assertIn(CONFIRMED, body)

    def test_build_messages_uses_profile_even_when_resume_omits_django(self):
        self.assertNotIn("Django", RESUME)
        ai = self.client()
        messages = ai.build_messages(RESUME, "Предпочитаю ГПХ.", f"Робот-рекрутер\n{QUESTION}")
        body = messages[-1]["content"]
        self.assertIn(CONFIRMED, body)
        self.assertIn("Предпочитаю ГПХ.", body)
        self.assertEqual(body.count(CONFIRMED), 1)

    def test_confirmed_django_answer_survives_real_chat_guards(self):
        ai = self.client(chat_reply(CONFIRMED))
        decision = ai.decide(RESUME, "", f"Робот-рекрутер\n{QUESTION}", [])
        self.assertEqual(decision.kind, "question")
        self.assert_confident_django(decision.reply)
        self.assert_profile_in_requests(ai.requests)

    def test_negative_chat_draft_is_rewritten_from_confirmed_facts(self):
        ai = self.client(chat_reply(NEGATIVE), chat_reply(CONFIRMED))
        decision = ai.decide(RESUME, "", f"Робот-рекрутер\n{QUESTION}", [])
        self.assertEqual(decision.kind, "question")
        self.assert_confident_django(decision.reply)
        self.assertGreaterEqual(len(ai.requests), 2)
        self.assert_profile_in_requests(ai.requests)

    def test_stubborn_negative_model_uses_confirmed_fallback(self):
        ai = self.client(chat_reply(NEGATIVE))
        ai.last_raw = json.dumps(chat_reply(NEGATIVE), ensure_ascii=False)
        messages = ai.build_messages(RESUME, "", f"Робот-рекрутер\n{QUESTION}")
        decision = ai._fix_reply(Decision("question", QUESTION, NEGATIVE), messages, RESUME, "")
        self.assertEqual(decision.kind, "question")
        self.assert_confident_django(decision.reply)

    def test_skill_confirmation_alone_does_not_invent_a_project_or_duration(self):
        self.profile_path.write_text('version = 1\nconfirmed_skills = ["Django"]\n', encoding="utf-8")
        ai = self.client(chat_reply(NEGATIVE))
        ai.last_raw = json.dumps(chat_reply(NEGATIVE), ensure_ascii=False)
        messages = ai.build_messages(RESUME, "", f"Робот-рекрутер\n{QUESTION}")
        decision = ai._fix_reply(Decision("question", QUESTION, NEGATIVE), messages, RESUME, "")
        self.assertEqual(decision.kind, "question")
        self.assertIn("Django", decision.reply)
        self.assertNotRegex(decision.reply.lower(), r"не работал|нет опыта|проект|компани|\b\d+\s+(?:лет|год)")

    def test_json_retry_retains_profile_facts(self):
        ai = self.client("это не JSON", chat_reply(CONFIRMED))
        decision = ai.decide(RESUME, "", f"Робот-рекрутер\n{QUESTION}", [])
        self.assertEqual(decision.kind, "question")
        self.assert_confident_django(decision.reply)
        self.assert_profile_in_requests(ai.requests)

    def test_form_answer_has_the_same_confirmed_facts(self):
        ai = self.client({"can_answer": True, "text": CONFIRMED})
        fill = ai.fill_form(RESUME, "", "Backend-разработчик", [FormQuestion(0, QUESTION, "text")])
        self.assertTrue(fill.can_answer)
        self.assert_confident_django(fill.answers[0].text)
        self.assert_profile_in_requests(ai.requests)

    def test_negative_form_draft_is_corrected_without_claiming_a_gap(self):
        ai = self.client({"can_answer": True, "text": NEGATIVE}, {"can_answer": True, "text": CONFIRMED})
        fill = ai.fill_form(RESUME, "", "Backend-разработчик", [FormQuestion(0, QUESTION, "text")])
        self.assertTrue(fill.can_answer)
        self.assert_confident_django(fill.answers[0].text)
        self.assert_profile_in_requests(ai.requests)

    def test_empty_form_reply_retry_retains_profile(self):
        ai = self.client({"can_answer": True, "text": ""}, {"can_answer": True, "text": CONFIRMED})
        fill = ai.fill_form(RESUME, "", "Backend-разработчик", [FormQuestion(0, QUESTION, "text")])
        self.assertTrue(fill.can_answer)
        self.assert_confident_django(fill.answers[0].text)
        self.assertGreaterEqual(len(ai.requests), 2)
        self.assert_profile_in_requests(ai.requests)

    def test_multi_choice_skills_include_profile_but_not_unknown_technology(self):
        ai = self.client()
        question = FormQuestion(0, "С какими технологиями вы работали?", "multi",
                                ["Python", "Django", "Kubernetes"])
        fill = ai.fill_form(RESUME, "", "Backend-разработчик", [question])
        self.assertTrue(fill.can_answer)
        self.assertEqual(fill.answers[0].choices, [0, 1])

    def test_unknown_technology_is_neither_affirmed_nor_declared_absent(self):
        question = "Расскажите о вашем опыте с Kubernetes?"
        ai = self.client(chat_reply("С Kubernetes работал, настраивал кластеры.", question),
                         chat_reply("Мой основной опыт связан с Python и Django.", question))
        decision = ai.decide(RESUME, "", f"Робот-рекрутер\n{question}", [])
        self.assertNotRegex(decision.reply.lower(), r"с kubernetes работал|настраивал кластер")
        self.assertNotRegex(decision.reply.lower(), r"не работал|нет опыта|опыта.{0,15}не было")

    def test_fabricated_duration_and_employer_do_not_survive_chat_or_form_retry(self):
        invented = "С Django работал 5 лет в компании Google, создавал модели и представления."
        chat = self.client(chat_reply(invented), chat_reply(CONFIRMED))
        decision = chat.decide(RESUME, "", f"Робот-рекрутер\n{QUESTION}", [])
        self.assert_confident_django(decision.reply)
        form = self.client({"can_answer": True, "text": invented}, {"can_answer": True, "text": CONFIRMED})
        fill = form.fill_form(RESUME, "", "Backend-разработчик", [FormQuestion(0, QUESTION, "text")])
        self.assertTrue(fill.can_answer)
        self.assert_confident_django(fill.answers[0].text)


if __name__ == "__main__":
    unittest.main()
