"""Свой промпт для ответов ([ai] answer_prompt) и защита от команд в тексте работодателя. Сеть не нужна.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # пакет hh_auto лежит в папке проекта

from hh_auto.ai.client import (FILTER_PROMPT, FORM_PROMPT, INJECTION_GUARD, LETTER_PROMPT, SYSTEM_PROMPT,  # noqa: E402
                               AiClient, FormQuestion, fix_text_answer)
from hh_auto.core.config import Ai  # noqa: E402
from hh_auto.ai.language import candidate_gender_rule

STYLE = "пиши с маленькой буквы, без длинного тире"


class Recorder(AiClient):
    """Клиент без сети: запоминает, что ушло нейросети, и отвечает заготовкой."""

    def __init__(self, answer: str, **settings):
        super().__init__(Ai(provider="custom", base_url="http://example.invalid/v1", model="test", **settings))
        self.answer = answer
        self.sent: list[list[dict]] = []

    def complete(self, messages, **_kwargs) -> str:
        self.sent.append(messages)
        return self.answer


def system_of(messages: list[dict]) -> str:
    return next(m["content"] for m in messages if m["role"] == "system")


class Guard(unittest.TestCase):
    def test_every_prompt_treats_employer_text_as_data(self):
        for name, prompt in (("чат", SYSTEM_PROMPT), ("анкета", FORM_PROMPT), ("письмо", LETTER_PROMPT),
                             ("фильтр", FILTER_PROMPT)):
            self.assertIn(INJECTION_GUARD.strip(), prompt, name)

    def test_letter_still_ends_with_output_format(self):
        self.assertTrue(LETTER_PROMPT.rstrip().endswith("Верни только текст письма."))

    def test_question_about_a_bot_is_left_to_the_person(self):
        self.assertIn("отвечает ли за соискателя нейросеть или бот", SYSTEM_PROMPT)
        self.assertIn("отвечает ли за соискателя\n  нейросеть или бот", FORM_PROMPT)


class AnswerPrompt(unittest.TestCase):
    def test_chat_preserves_own_style_before_the_explicit_gender_rule(self):
        ai = Recorder("{}", answer_prompt=STYLE)
        system = system_of(ai.build_messages("резюме", "", "Работодатель: Какой у вас опыт?"))
        self.assertIn(STYLE, system)
        self.assertTrue(system.rstrip().endswith(candidate_gender_rule(ai.cfg)))
        self.assertLess(system.index(STYLE), system.index(candidate_gender_rule(ai.cfg)))
        self.assertIn("ТРЕБОВАНИЯ СОИСКАТЕЛЯ К ОТВЕТАМ", system)

    def test_form_gets_own_prompt(self):
        ai = Recorder('{"can_answer": true, "text": "работал с django 2 года"}', answer_prompt=STYLE)
        ai._answer_question("резюме", "", "Python-разработчик", FormQuestion(0, "Опыт с Django?", "text"))
        system = system_of(ai.sent[0])
        self.assertIn(STYLE, system)
        self.assertTrue(system.rstrip().endswith(candidate_gender_rule(ai.cfg)))
        self.assertLess(system.index(STYLE), system.index(candidate_gender_rule(ai.cfg)))

    def test_without_own_prompt_nothing_is_added(self):
        ai = Recorder("{}")
        system = system_of(ai.build_messages("резюме", "", "Работодатель: Какой у вас опыт?"))
        self.assertNotIn("ТРЕБОВАНИЯ СОИСКАТЕЛЯ К ОТВЕТАМ", system)

    def test_letter_uses_its_own_prompt_only(self):
        ai = Recorder("Здравствуйте! Пишу по делу.", answer_prompt=STYLE)
        ai.write_letter("резюме", "", "Python-разработчик", "Ромашка", "описание")
        self.assertNotIn(STYLE, system_of(ai.sent[0]))


class LowercaseStyle(unittest.TestCase):
    def test_lowercase_answer_stays_lowercase(self):
        q = FormQuestion(0, "Какой у вас опыт с Django?", "text")
        self.assertEqual(fix_text_answer(q, "работал с django 2 года", "", ""), "работал с django 2 года")

    def test_trimmed_opener_keeps_capital(self):
        q = FormQuestion(0, "Какой у вас опыт с Django?", "text")
        text = fix_text_answer(q, "Меня заинтересовала ваша вакансия. С Django работал 2 года.", "", "")
        self.assertTrue(text[:1].isupper(), text)


if __name__ == "__main__":
    unittest.main()
