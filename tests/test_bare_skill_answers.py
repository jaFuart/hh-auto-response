"""A model's unqualified yes must not invent a named tool or an experience duration."""

from dataclasses import replace
import json
import unittest

from hh_auto.ai.client import FormQuestion
from hh_auto.ai.profile import CandidateSkill
from tests.test_active_generation import RESUME, StubModel


class AlwaysYes(StubModel):
    def __init__(self, question="", *, kind="question"):
        super().__init__()
        self.question = question
        self.kind = kind
        skill = CandidateSkill("Power BI", "hands_on", "Power BI: практика подтверждена", aliases=("Power BI",))
        self.profile = replace(self.profile, confirmed_skills=(*self.profile.confirmed_skills, "Power BI"),
                               skills=(*self.profile.skills, skill))

    def complete(self, messages, max_tokens=800, json_mode=False):
        return json.dumps({"kind": self.kind, "question": self.question,
                           "reply": "Да." if self.kind == "question" else "",
                           "can_answer": True, "text": "Да."}, ensure_ascii=False)


class BareCompetenceAnswers(unittest.TestCase):
    def test_normal_model_yes_is_allowed_only_for_the_evidenced_target_and_condition(self):
        cases = (
            ("Работали с ERP системой Парус?", False),
            ("Работали с системой Парус?", False),
            ("У вас есть опыт работы с Power BI?", True),
            ("Работали с PowerBI?", True),
            ("Работали ли вы с Tableau?", False),
            ("Есть ли у вас 5 лет опыта с Power BI?", False),
            ("Работали с Power BI и Tableau?", False),
        )
        for question, supported in cases:
            with self.subTest(question=question):
                model = AlwaysYes(question)
                result = model.decide(RESUME + " ERP-системы", "", "Работодатель: " + question, [])
                self.assertEqual(result.kind, "question")
                self.assertTrue(result.reply)
                if supported:
                    self.assertEqual(result.reply, "Да.")
                else:
                    self.assertNotEqual(result.reply, "Да.")
                    self.assertNotRegex(result.reply, r"5\s+лет|(?:работал|использовал).{0,25}(?:Парус|Tableau)")

    def test_familiarity_question_can_confirm_knowledge_without_claiming_practical_use(self):
        question = "Знакомы ли вы с SQLAlchemy?"
        model = AlwaysYes(question)
        result = model.decide(RESUME, "", "Работодатель: " + question, [])
        self.assertEqual(result.kind, "question")
        self.assertEqual(result.reply, "Знаком с SQLAlchemy.")
        self.assertNotRegex(result.reply, r"работал|использовал|production")

    def test_explicit_technology_duration_can_support_a_yes_but_general_seniority_cannot(self):
        question = "Есть ли у вас 5 лет опыта с Power BI?"
        model = AlwaysYes(question)
        result = model.decide(RESUME, "С Power BI работал 5 лет.", "Работодатель: " + question, [])
        self.assertEqual(result.reply, "Да.")
        result = model.decide(RESUME, "Общий стаж разработки — 5 лет.", "Работодатель: " + question, [])
        self.assertNotEqual(result.reply, "Да.")
        self.assertEqual(result.reply, "С Power BI работал.")

    def test_unknown_tableau_uses_the_real_data_case_and_introduces_a_noun_task(self):
        question = "Работали ли вы с Tableau?"
        model = AlwaysYes(question)
        data_case = replace(model.profile.projects[1], tasks=("сбор и нормализация данных", "SQL-запросы"))
        model.profile = replace(model.profile, projects=(model.profile.projects[0], data_case))
        result = model.decide(RESUME, "", "Работодатель: " + question, [])
        self.assertIn("ETL Workbench", result.reply)
        self.assertIn("Мои задачи: сбор и нормализация данных.", result.reply)
        self.assertNotIn("Courier Desk", result.reply)
        self.assertNotRegex(result.reply, r"(?:работал|использовал).{0,25}Tableau")

    def test_generic_agreements_and_protected_classifications_are_preserved(self):
        for question in ("Начнём?", "Поиск работы актуален?"):
            with self.subTest(question=question):
                model = AlwaysYes(question)
                self.assertEqual(model.decide(RESUME, "", "Работодатель: " + question, []).reply, "Да.")
        for question in ("Пришлите скан паспорта.", "Когда вам удобно созвониться?"):
            with self.subTest(question=question):
                model = AlwaysYes(question, kind="human")
                self.assertEqual(model.decide(RESUME, "", "Работодатель: " + question, []).kind, "human")

    def test_public_text_form_rejects_unknown_parus_yes_and_keeps_confirmed_power_bi_yes(self):
        model = AlwaysYes()
        questions = [FormQuestion(0, "Работали с ERP системой Парус?", "text"),
                     FormQuestion(1, "У вас есть опыт работы с Power BI?", "text")]
        result = model.fill_form(RESUME + " ERP-системы", "", "Backend vacancy", questions)
        self.assertTrue(result.can_answer)
        self.assertNotEqual(result.answers[0].text, "Да.")
        self.assertNotRegex(result.answers[0].text, r"(?:работал|использовал).{0,20}Парус")
        self.assertEqual(result.answers[1].text, "Да.")
        self.assertEqual([answer.question.text for answer in result.answers], [question.text for question in questions])


if __name__ == "__main__":
    unittest.main()
