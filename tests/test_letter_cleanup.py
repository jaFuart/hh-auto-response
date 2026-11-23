"""Cover letters lose Markdown separators while retaining readable paragraphs and skill names."""

from __future__ import annotations

import unittest

from hh_auto.ai.client import clean_letter


class LetterCleanupTest(unittest.TestCase):
    def test_standalone_thematic_breaks_preserve_paragraph_boundaries(self):
        for separator in ("---", "----", "***", "*****", "___", "_____", "- - -", "* * *", "_ _ _",
                          "  -- -  ", "\t*\t* *\t", " _  __ "):
            with self.subTest(separator=separator):
                source = ("Здравствуйте!\n\n"
                          "Сейчас веду разработку продукта.\n" + separator + "\n"
                          "Умею выстраивать процессы и доводить задачи до результата.\n\n"
                          "Готов обсудить ваши задачи.")
                expected = ("Здравствуйте!\n\n"
                            "Сейчас веду разработку продукта.\n\n"
                            "Умею выстраивать процессы и доводить задачи до результата.\n\n"
                            "Готов обсудить ваши задачи.")
                self.assertEqual(clean_letter(source, 1000), expected)

    def test_existing_paragraphs_stay_readable_when_separators_have_blank_lines(self):
        source = ("---\nЗдравствуйте!\n\n***\n\n"
                  "**Работаю с C++ и C#.** Интеграции — моя сильная сторона.\n\n_ _ _\n\n"
                  "Обсудим задачи — предложу решение.\n___")
        self.assertEqual(clean_letter(source, 1000),
                         "Здравствуйте!\n\nРаботаю с C++ и C#. Интеграции — моя сильная сторона.\n\n"
                         "Обсудим задачи — предложу решение.")

    def test_inline_punctuation_and_skill_names_are_preserved(self):
        text = "Использую C++ и C#, HTTP-запросы и CI/CD. Backend --- интеграции, фронтенд — интерфейсы."
        self.assertEqual(clean_letter(text, 1000), text)


if __name__ == "__main__":
    unittest.main()
