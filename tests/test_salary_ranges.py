"""Salary ranges retain both user-specified bounds and tax/currency semantics, without live AI."""

from __future__ import annotations

import unittest

from hh_auto.ai.client import AiClient, Decision, salary_phrase
from hh_auto.core.config import Ai


class SalaryRangesTest(unittest.TestCase):
    def test_range_formats_retain_both_bounds_and_net_semantics(self):
        for context in ("Зарплатные ожидания: 180 000–240 000 рублей в месяц на руки.",
                        "Доход: 180-240 тыс. на руки.", "Ожидаю от 180 до 240 тысяч чистыми.",
                        "Опыт 3–5 лет. Зарплата: 180000 — 240000 руб. на руки.",
                        "Ожидаю 180к–240к на руки."):
            with self.subTest(context=context):
                self.assertEqual(salary_phrase(context), "180 000–240 000 ₽ на руки")

    def test_range_preserves_gross_and_explicit_currency(self):
        self.assertEqual(salary_phrase("Зарплата 180–240 тыс. до вычета налогов"),
                         "180 000–240 000 ₽ до вычета налогов")
        self.assertEqual(salary_phrase("Зарплата 1800–2400 EUR net"), "1 800–2 400 € на руки")

    def test_single_amount_behavior_is_unchanged(self):
        self.assertEqual(salary_phrase("Уровень дохода от 180"), "от 180 000 ₽")
        self.assertEqual(salary_phrase("Ожидаю 240 000 ₽ на руки"), "240 000 ₽ на руки")
        self.assertIsNone(salary_phrase("Готов обсудить условия"))

    def test_chat_preserves_equivalent_range_and_corrects_partial_or_changed_ranges(self):
        client = AiClient(Ai(provider="custom", base_url="http://example.invalid/v1", model="test"))
        context = "Зарплатные ожидания: 180 000–240 000 рублей в месяц на руки."
        question = "Какие у вас зарплатные ожидания?"
        equivalent = "Рассматриваю 180–240 тысяч на руки."
        decision = Decision(kind="question", question=question, reply=equivalent)
        self.assertEqual(client._fix_reply(decision, [], "", context).reply, equivalent)
        for reply in ("Ожидаю 240 000 ₽ на руки.", "От 180 до 300 тысяч на руки.",
                      "180–240 тысяч до вычета налогов.", "180–240 тысяч на руки, опыт 9 лет."):
            with self.subTest(reply=reply):
                decision = Decision(kind="question", question=question, reply=reply)
                self.assertEqual(client._fix_reply(decision, [], "", context).reply,
                                 "Ожидаю 180 000–240 000 ₽ на руки.")


if __name__ == "__main__":
    unittest.main()
