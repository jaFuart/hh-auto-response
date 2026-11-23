"""Active instructions keep the requested opening without forcing a repetitive stack ending."""

import unittest

from hh_auto.ai.application import (RUSSIAN_LETTER_ASSESSMENT, RUSSIAN_LETTER_GREETING,
                                     analyze_vacancy, make_letter_plan, validate_letter)
from hh_auto.ai.client import LETTER_EXPERIENCE_FIRST, LETTER_PROMPT
from tests.test_active_generation import DESCRIPTION, professional_profile


class TaskFocusedLetterInstructions(unittest.TestCase):
    def test_active_prompt_keeps_the_opening_and_exceptions_but_does_not_require_a_stack_finale(self):
        self.assertIn(RUSSIAN_LETTER_GREETING, LETTER_PROMPT)
        self.assertIn(RUSSIAN_LETTER_ASSESSMENT, LETTER_PROMPT)
        self.assertIn("1–2 конкретных абзаца", LETTER_PROMPT)
        self.assertIn("с действиями", LETTER_PROMPT)
        self.assertIn("только если он полезен и не дублирует опыт", LETTER_PROMPT)
        self.assertNotIn("Последний абзац — релевантные подтверждённые навыки", LETTER_PROMPT)
        self.assertNotIn("отдельным последним абзацем", LETTER_EXPERIENCE_FIRST)
        self.assertIn("канцелярскими", LETTER_PROMPT)
        self.assertIn("Требуемый английский язык, обязательные вопросы", LETTER_PROMPT)
        self.assertIn("Зарплату — только по вопросу и из контекста", LETTER_PROMPT)

    def test_generated_plan_and_recent_history_do_not_reintroduce_a_mandatory_skills_paragraph(self):
        analysis = analyze_vacancy("Python integration developer", "Тестовая компания", DESCRIPTION)
        plan = make_letter_plan(analysis, professional_profile(), recent_letters=["В работе использую Python и SQL."])
        guidance = plan.render_guidance()
        self.assertIn("навыки и технологии показывай в контексте действий", guidance)
        self.assertIn("шаблонный финал не нужен", guidance)
        self.assertNotIn("последний абзац — подтверждённые", guidance)
        self.assertNotIn("Заверши абзацем релевантных навыков", guidance)

    def test_concrete_task_paragraph_without_a_skills_list_passes_existing_quality_checks(self):
        text = (RUSSIAN_LETTER_GREETING + "\n\n" + RUSSIAN_LETTER_ASSESSMENT +
                "\n\nРазрабатывал интеграции доставки: реализовал провайдерные адаптеры, проверку заявок "
                "и обработку ошибок API. Использовал Python и Frappe для серверной логики, связывал ответы "
                "внешних служб со статусами внутренних процессов и валидацией данных.")
        analysis = analyze_vacancy("Python integration developer", "Тестовая компания", DESCRIPTION)
        issues = validate_letter(text, analysis, professional_profile())
        self.assertFalse(any(issue.startswith("objective:") or issue.startswith("style:missing_case") for issue in issues), issues)
        self.assertNotIn("В работе использую", text)

    def test_employer_keyword_and_opening_questions_still_take_priority_in_plan(self):
        description = ("Сопроводительное должно начинаться со слова «Зэд.». "
                       "Начните письмо с ответов на вопросы:\n1. Какие API разрабатывали?\n"
                       "2. Как проверяли данные?\n" + DESCRIPTION)
        analysis = analyze_vacancy("Python integration developer", "Тестовая компания", description)
        guidance = make_letter_plan(analysis, professional_profile()).render_guidance()
        self.assertIn("Зэд.", analysis.required_keywords)
        self.assertEqual(len(analysis.opening_questions), 2)
        self.assertIn("НАЧНИ С ОТВЕТОВ ПО ПОРЯДКУ", guidance)


if __name__ == "__main__":
    unittest.main()
