"""Remove a nonfactual caveat after an affirmative answer without changing experience facts."""

import unittest

from hh_auto.ai.client import concise_answer


class AnswerConfidenceTest(unittest.TestCase):
    def test_django_answer_keeps_backend_facts_without_an_inline_caveat(self):
        source = ("С Django работал, но конкретные задачи с ним готов уточнить. "
                  "В Python-backend разрабатывал REST API и интеграции.")
        self.assertEqual(concise_answer(source),
                         "С Django работал. В Python-backend разрабатывал REST API и интеграции.")
        negative = ("С Django не работал, но конкретные задачи с ним готов уточнить. "
                    "Разрабатывал интеграции на Python.")
        self.assertEqual(concise_answer(negative), negative)
        factual = "С Django работал, но конкретные задачи с ним требовали анализа. Разрабатывал API."
        self.assertEqual(concise_answer(factual), factual)


if __name__ == "__main__":
    unittest.main()
