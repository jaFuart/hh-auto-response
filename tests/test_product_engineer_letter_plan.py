"""Product-role targeting uses synthetic personal evidence, not team stacks or global tool skills."""

from __future__ import annotations

import copy
import unittest

from hh_auto.ai.application import analyze_vacancy, make_letter_plan


PRODUCT_TASKS = ("Чем предстоит заниматься:\n"
                 "- Создавать пользовательские интерфейсы и backend API продукта.\n"
                 "- Быстро собирать прототипы и проверять гипотезы по обратной связи.\n"
                 "Требования:\nPython, React, Cursor, Codex, prompt engineering и AI-assisted development.")


def case(identifier, tasks, *, description="", personal=(), team=(), stack=()):
    return {"id": identifier, "name": "Synthetic " + identifier, "description": description,
            "tasks": list(tasks), "personal_technologies": list(personal),
            "team_technologies": list(team), "technologies": list(stack), "kind": "personal"}


POSTAL = case("postal", ["Разрабатывал серверную логику на Python и API обработки почтовых отправлений",
                         "Настраивал согласования и валидацию заявок"],
              description="Корпоративный workflow почтовых отправлений", personal=("Python", "REST API"))
PRODUCT = case("product", ["Создал пользовательский интерфейс и backend API",
                           "Собрал прототип продукта и проверял гипотезы по обратной связи"],
               personal=("Python", "React", "FastAPI"))


def profile(projects):
    return {"confirmed_skills": ["Python", "React", "Cursor", "Codex", "Prompt engineering",
                                  "AI-assisted development"], "projects": copy.deepcopy(projects)}


def plan(projects, *, title="AI Product Engineer", description=PRODUCT_TASKS):
    analysis = analyze_vacancy(title, "Synthetic employer", description)
    return make_letter_plan(analysis, profile(projects))


class ProductEngineerLetterPlan(unittest.TestCase):
    def test_product_titles_with_and_without_spaces_have_their_own_family(self):
        for title in ("AI Product Engineer", "AIProductEngineer", "AI-Product-Engineer", "Product Engineer / AI"):
            with self.subTest(title=title):
                self.assertEqual(analyze_vacancy(title, "", "Python").role_family, "ai_product")
        for title in ("Product Engineer", "ProductEngineer", "Продуктовый инженер"):
            with self.subTest(title=title):
                self.assertEqual(analyze_vacancy(title, "", "Python").role_family, "product_engineering")

    def test_existing_ai_automation_title_keeps_its_family(self):
        analysis = analyze_vacancy("AI-разработчик", "", "Работать с Claude Code и автоматизировать бизнес.")
        self.assertEqual(analysis.role_family, "ai_automation")

    def test_confirmed_workflow_tools_are_matches_but_do_not_make_postal_case_direct(self):
        result = plan([POSTAL])
        for skill in ("Cursor", "Codex", "Prompt engineering", "AI-assisted development"):
            self.assertIn(skill, result.strongest_matches)
            self.assertNotIn(skill, result.selected_projects[0]["personal_technologies"])
        self.assertEqual(result.selected_projects[0]["relation"], "adjacent")
        self.assertTrue(result.selected_projects[0]["relation_reason"])
        self.assertIn("Из смежного опыта:", result.render_guidance())

    def test_real_product_tasks_outrank_a_verbose_etl_or_postal_case(self):
        etl = case("etl", ["Нормализовал данные и писал SQL" for _ in range(50)],
                   description=("API данные автоматизация backend бизнес продукт " * 100), personal=("Python", "SQL"))
        result = plan([etl, POSTAL, PRODUCT])
        self.assertEqual(result.selected_projects[0]["id"], "product")
        self.assertEqual(result.selected_projects[0]["relation"], "direct")
        self.assertNotIn("Из смежного опыта:", result.render_guidance())

    def test_team_ui_and_llm_stack_do_not_become_personal_product_evidence(self):
        team_case = case("team", ["Разрабатывал серверные API на Python"],
                         description="Продукт с UI, MVP, LLM и проверкой гипотез",
                         personal=("Python",), team=("React", "LangChain"),
                         stack=("Python", "React", "LangChain"))
        selected = plan([team_case]).selected_projects[0]
        self.assertEqual(selected["relation"], "adjacent")
        self.assertEqual(selected["personal_technologies"], ["Python"])
        self.assertEqual(selected["team_technologies"], ["React", "LangChain"])

    def test_explicit_llm_feature_requirement_needs_a_personal_llm_feature_case(self):
        description = PRODUCT_TASKS.replace("Требования:", "- Встраивать LLM API в пользовательские функции.\nТребования:")
        without_ai = plan([PRODUCT], description=description)
        self.assertEqual(without_ai.selected_projects[0]["relation"], "adjacent")
        self.assertIn("LLM", without_ai.selected_projects[0]["relation_reason"])
        ai_case = copy.deepcopy(PRODUCT)
        ai_case["id"], ai_case["name"] = "ai-product", "Synthetic AI product"
        ai_case["tasks"].append("Встроил LLM API в пользовательский интерфейс и backend продукта")
        with_ai = plan([PRODUCT, ai_case], description=description)
        self.assertEqual(with_ai.selected_projects[0]["id"], "ai-product")
        self.assertEqual(with_ai.selected_projects[0]["relation"], "direct")

    def test_ai_assisted_development_is_not_treated_as_an_llm_feature(self):
        result = plan([PRODUCT])
        self.assertEqual(result.selected_projects[0]["relation"], "direct")
        self.assertNotIn("LLM", " ".join(result.selected_projects[0]["tasks"]))
        self.assertIn("не доказывают личную интеграцию LLM", result.render_guidance())

    def test_llm_feature_under_requirements_is_preserved_even_when_task_section_exists(self):
        description = PRODUCT_TASKS + "\nОбязательно:\nОпыт интеграции LLM API в продукт."
        self.assertEqual(plan([PRODUCT], description=description).selected_projects[0]["relation"], "adjacent")

    def test_target_tasks_are_one_or_two_actual_responsibilities_not_invented_requirements(self):
        result = plan([PRODUCT], description=PRODUCT_TASKS + "\nУсловия:\nКомфортный офис и дружная команда.")
        selected = result.selected_projects[0]
        self.assertTrue(1 <= len(selected["target_tasks"]) <= 2)
        self.assertTrue(set(selected["target_tasks"]) <= set(result.analysis.responsibilities))
        self.assertTrue(any("интерфейс" in task for task in selected["target_tasks"]))
        self.assertNotIn("офис", " ".join(selected["target_tasks"]))
        self.assertIn("1–2 конкретными задачами вакансии", result.render_guidance())

    def test_no_missing_evidence_is_filled_from_global_cursor_skills(self):
        data = profile([POSTAL])
        without_tools = make_letter_plan(analyze_vacancy("AI Product Engineer", "", PRODUCT_TASKS),
                                         {**data, "confirmed_skills": ["Python"]})
        with_tools = make_letter_plan(analyze_vacancy("AI Product Engineer", "", PRODUCT_TASKS), data)
        self.assertEqual(with_tools.selected_projects, without_tools.selected_projects)
        self.assertFalse(with_tools.requires_approval)
        self.assertTrue(with_tools.automatic)

    def test_generic_specialization_relation_uses_personal_not_team_tools(self):
        item = case("kafka-team", ["Разрабатывал API на Python"], personal=("Python",),
                    team=("Kafka",), stack=("Python", "Kafka"))
        selected = plan([item], title="Data Engineer", description="Требования:\nKafka в production.").selected_projects[0]
        self.assertEqual(selected["relation"], "adjacent")

    def test_language_and_opening_questions_keep_priority_over_russian_adjacent_marker(self):
        description = ("Сопроводительное письмо на английском.\nНачните письмо с ответов на вопросы:\n"
                       "1. Какие прототипы создавали?\n2. Как проверяли пользовательские сценарии?\n" + PRODUCT_TASKS)
        result = plan([POSTAL], description=description)
        self.assertEqual(result.analysis.language, "en")
        self.assertEqual(len(result.analysis.opening_questions), 2)
        self.assertIn("Обязательный язык и начальные ответы", result.render_guidance())
        self.assertIn("НАЧНИ С ОТВЕТОВ ПО ПОРЯДКУ", result.render_guidance())


if __name__ == "__main__":
    unittest.main()
