"""Candidate grammatical gender is independent of interlocutors, source text and choice labels."""

import json
from types import SimpleNamespace
import unittest

from hh_auto.ai.client import FormQuestion
from hh_auto.ai.language import candidate_gender_rule, normalize_candidate_text
from tests.test_active_generation import DESCRIPTION, RESUME, StubModel


class CandidateGenderGrammar(unittest.TestCase):
    def test_male_normalizes_typical_first_person_forms_including_implicit_subjects(self):
        cases = {
            "Работала с PostgreSQL. Готова к работе.": "Работал с PostgreSQL. Готов к работе.",
            "Я разрабатывала API и использовала Python.": "Я разрабатывал API и использовал Python.",
            "В проекте занималась интеграциями, затем настроила проверки.": "В проекте занимался интеграциями, затем настроил проверки.",
            "Работал(а) с Python. Готов(а) приступить. Я уверен(а) в решении.": "Работал с Python. Готов приступить. Я уверен в решении.",
            "Я работал/а с Django. Готов/а приступить.": "Я работал с Django. Готов приступить.",
            "Была знакома с SQLAlchemy.": "Был знаком с SQLAlchemy.",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                result = normalize_candidate_text(source, SimpleNamespace(candidate_gender="male"))
                self.assertEqual(result, expected)
                self.assertEqual(normalize_candidate_text(result, SimpleNamespace(candidate_gender="male")), result)

    def test_third_person_fixed_interest_phrase_and_interlocutor_quotes_are_unchanged(self):
        cases = (
            "Команда работала с PostgreSQL. Компания создала сервис.",
            "Команда «Феникс» работала над API.",
            "В проекте работала распределённая команда.",
            "На проекте была готова система.",
            "Мне предложила компания новую задачу.",
            "Меня заинтересовала данная вакансия!",
            "Моя команда была готова к запуску. В проекте система работала стабильно.",
            "Была создана интеграция. Была разработана система обработки данных.",
            'Рекрутер написала: «Я работала с этой системой». В сообщении было: "Готова обсудить".',
        )
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(normalize_candidate_text(source, SimpleNamespace(candidate_gender="male")), source)

    def test_female_setting_changes_only_candidate_predicates(self):
        cases = {
            "Работал с PostgreSQL. Готов приступить.": "Работала с PostgreSQL. Готова приступить.",
            "Я использовал Python и занимался API.": "Я использовала Python и занималась API.",
            "Работал(а) с Python. Я уверен(а) в решении.": "Работала с Python. Я уверена в решении.",
            "Я работал/а с Django.": "Я работала с Django.",
            "Команда работала. Я был готов.": "Команда работала. Я была готова.",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(normalize_candidate_text(source, SimpleNamespace(candidate_gender="female")), expected)

    def test_old_configs_default_to_male_and_prompts_explain_candidate_gender(self):
        self.assertEqual(normalize_candidate_text("Готова приступить.", SimpleNamespace()), "Готов приступить.")
        self.assertIn("Кандидат — мужчина", candidate_gender_rule(SimpleNamespace()))
        self.assertIn("Кандидат — женщина", candidate_gender_rule(SimpleNamespace(candidate_gender="female")))


class CandidateGenderPublicOutputs(unittest.TestCase):
    def test_chat_reply_is_male_but_question_and_analysis_source_stay_exact(self):
        question = "Команда работала с PostgreSQL; расскажите о своём опыте?"
        model = StubModel([json.dumps({"kind": "question", "question": question,
                                      "reply": "Работала с PostgreSQL и использовала Python."}, ensure_ascii=False)])
        result = model.decide(RESUME, "", "Работодатель: " + question, [])
        self.assertEqual(result.reply, "Работал с PostgreSQL и использовал Python.")
        self.assertEqual(result.question, question)
        self.assertEqual(model.last_chat_record["question"], question)
        self.assertIn("Кандидат — мужчина", model.generation_calls[0]["messages"][0]["content"])

    def test_letter_body_is_male_and_vacancy_grammar_is_preserved(self):
        body = ("В Courier Desk разрабатывала адаптеры доставки, использовала Python и Frappe. "
                "Реализовала обработку ошибок API и валидацию заявок.")
        model = StubModel([body, body, body])
        description = DESCRIPTION + " Команда работала над запуском сервиса."
        result = model.write_letter(RESUME, "", "Python developer", "Тестовая компания", description, 1800)
        self.assertIn("разрабатывал адаптеры доставки", result)
        self.assertIn("Реализовал обработку ошибок", result)
        self.assertIn("очень заинтересовала данная вакансия!", result)
        self.assertIn("Команда работала", model.last_letter_record["analysis"]["full_text"])
        self.assertIn("Кандидат — мужчина", model.generation_calls[0]["messages"][0]["content"])

    def test_form_text_is_normalized_but_employer_question_and_choice_labels_are_not(self):
        model = StubModel([json.dumps({"can_answer": True, "text": "Работала с PostgreSQL."}, ensure_ascii=False)])
        question = FormQuestion(0, "Расскажите, как команда работала с PostgreSQL?", "text")
        result = model.fill_form(RESUME, "", "Backend vacancy", [question])
        self.assertTrue(result.can_answer)
        self.assertEqual(result.answers[0].text, "Работал с PostgreSQL.")
        self.assertEqual(result.answers[0].question.text, question.text)
        choice = FormQuestion(1, "Выберите точный текст", "single", ["Работал(а)", "Готова", "Другое"])
        answer = model._to_answer(choice, {"choices": ["Готова"]}, 2)
        self.assertEqual(answer.choices, [1])
        self.assertEqual(choice.options, ["Работал(а)", "Готова", "Другое"])


if __name__ == "__main__":
    unittest.main()
