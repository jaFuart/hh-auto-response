"""Exercise the public generation API with structured evidence and an offline model."""

from __future__ import annotations

import copy
import json
import re
import unittest

from hh_auto.ai.claims import personal_claim_issues
from hh_auto.ai.client import AiClient, AiError, PROMPT_VERSION
from hh_auto.ai.profile import parse_candidate_profile
from hh_auto.core.config import Ai


def professional_profile():
    return parse_candidate_profile({
        "schema_version": "1.0", "updated_at": "2026-11-14",
        "candidate": {"primary_title": "Python Backend Developer", "current_employer": "Новая компания",
                      "former_employer": "Прежняя компания"},
        "competencies": {
            "Backend": {"hands_on": ["Python", "FastAPI", "Frappe", "PostgreSQL", "Django", "asyncio", "REST API"],
                        "familiar": ["асинхронная SQLAlchemy (принципы, без подтверждённого production-опыта)"],
                        "needs_confirmation": ["Kafka", "Kubernetes"]},
            "Tools": {"hands_on": ["Git", "Linux", "Docker", "Docker Compose"]},
        },
        "projects": [
            {"name": "Courier Desk", "period": "2025–2026", "role": "backend-разработчик",
             "description": "Интеграции доставки: API провайдеров, валидация заявок, обработка статусов и ошибок.",
             "personal_contribution": ["Реализовал провайдерные адаптеры доставки и обработку ошибок API",
                                       "Добавил валидацию заявок и статусов"],
             "technology": ["Python", "Frappe", "REST API", "PostgreSQL"], "evidence": "личный код"},
            {"name": "ETL Workbench", "period": "2024–2025", "role": "Python-разработчик",
             "description": "Конвейер сбора, нормализации и проверки данных из HTTP-источников.",
             "personal_contribution": ["Разработал асинхронный сбор данных через asyncio",
                                       "Реализовал нормализацию данных и SQL-запросы"],
             "technology": ["Python", "asyncio", "PostgreSQL", "Docker"], "evidence": "личный код"},
        ],
        "screening_examples": [
            {"question": "Какие команды Git используете чаще всего?",
             "answer": "Использую status, diff, log, add, commit и push. Для работы с ветками применяю switch и merge."},
            {"question": "Работали ли с асинхронной SQLAlchemy?",
             "answer": "Знаком с принципами асинхронной SQLAlchemy. Практический опыт связан с PostgreSQL и Python-сервисами."},
        ],
    })


class StubModel(AiClient):
    """Model replies are scripted; validators and all public client methods remain real."""

    def __init__(self, replies=(), *, judge_replies=(), unavailable=False):
        super().__init__(Ai(provider="custom", base_url="http://localhost:1/v1", model="offline-model"))
        self.profile = professional_profile()
        self.replies = list(replies)
        self.judge_replies = list(judge_replies)
        self.unavailable = unavailable
        self.calls = []

    def complete(self, messages, max_tokens=800, json_mode=False):
        judge = any('"unsupported_claims"' in message["content"] for message in messages)
        topical = len(messages) == 1 and '"ok"' in messages[0]["content"]
        kind = "facts" if judge else "topic" if topical else "generation"
        self.calls.append({"kind": kind, "messages": copy.deepcopy(messages), "json_mode": json_mode})
        if self.unavailable:
            raise AiError("Модель временно недоступна")
        if topical:
            return '{"ok": true}'
        if judge:
            return self.judge_replies.pop(0) if self.judge_replies else '{"unsupported_claims": []}'
        if not self.replies:
            raise AssertionError("Unexpected extra generation call: " + messages[-1]["content"][:250])
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def generation_calls(self):
        return [item for item in self.calls if item["kind"] == "generation"]


RESUME = "Python backend: API, интеграции и работа с PostgreSQL."
DESCRIPTION = "Нужен Python-разработчик для интеграций API доставки, валидации заявок и обработки статусов."
GOOD_RU = (
    "В Courier Desk реализовал провайдерные адаптеры доставки, обработку ошибок API и валидацию заявок. "
    "Работал с серверной логикой на Python и Frappe, связывал внешние ответы со статусами внутренних процессов. "
    "Для вашей задачи важен именно такой опыт: интеграция должна корректно обрабатывать разные ответы провайдеров "
    "и сохранять согласованность данных. Могу подробно разобрать устройство адаптеров и свой вклад в реализацию."
)
GOOD_EN = (
    "In ETL Workbench I developed asynchronous data collection with Python and asyncio, followed by normalization "
    "and database queries. This work connects external HTTP sources with a repeatable processing workflow. "
    "For your video automation tasks, my relevant background is backend orchestration and data handling. "
    "I can explain the implementation choices and my contribution in detail. The available evidence concerns "
    "these engineering components rather than completed video production."
)


class ActiveLetterGeneration(unittest.TestCase):
    def write(self, model, *, description=DESCRIPTION, title="Python Integration Developer", vacancy_id="test-42"):
        return model.write_letter(RESUME, "", title, "Тестовая компания", description, 1800,
                                  vacancy_id=vacancy_id)

    def test_two_bounded_repairs_keep_the_real_case_and_record_the_final_result(self):
        bad = "Использовал Kafka в production и обслуживал 99 миллионов пользователей."
        still_bad = "Использовал Kafka в production для интеграций доставки."
        model = StubModel([bad, still_bad, GOOD_RU])
        result = self.write(model)
        self.assertIn("провайдерные адаптеры доставки", result)
        self.assertNotIn("Courier Desk", result)
        self.assertEqual(len(model.generation_calls), 3)
        self.assertEqual(personal_claim_issues(result, model.profile), [])
        record = model.last_letter_record
        self.assertEqual(record["revision_count"], 2)
        self.assertIn(bad, record["draft"])
        self.assertEqual(record["final_text"], result)
        self.assertEqual(record["prompt_version"], PROMPT_VERSION)
        self.assertEqual(record["model"], "offline-model")
        self.assertEqual(record["vacancy_id"], "test-42")
        self.assertTrue(record["generated_at"])
        self.assertFalse(record["fallback_used"])
        self.assertFalse(record["requires_approval"])
        self.assertTrue(record["matching_is_advisory_only"])
        self.assertTrue(any(project["name"] == "Courier Desk" for project in record["plan"]["selected_projects"]))
        case = next(project for project in record["plan"]["selected_projects"] if project["name"] == "Courier Desk")
        self.assertEqual(case["id"], model.profile.projects[0].id)
        self.assertIn("Реализовал провайдерные адаптеры доставки и обработку ошибок API", case["tasks"])

    def test_fact_judge_can_trigger_repair_without_losing_confirmed_tasks(self):
        draft = GOOD_RU + " Я также руководил большим отделом."
        model = StubModel([draft, GOOD_RU], judge_replies=[
            json.dumps({"unsupported_claims": ["Руководство большим отделом не подтверждено"]}, ensure_ascii=False),
            '{"unsupported_claims": []}',
        ])
        result = self.write(model)
        self.assertIn("обработку ошибок API", result)
        self.assertNotIn("большим отделом", result)
        self.assertIn("провайдерные адаптеры", result)
        self.assertEqual(model.last_letter_record["revision_count"], 1)
        self.assertEqual(len(model.generation_calls), 2)

    def test_unavailable_model_returns_nonempty_source_grounded_fallback(self):
        model = StubModel(unavailable=True)
        result = self.write(model)
        self.assertTrue(result.strip())
        self.assertEqual(personal_claim_issues(result, model.profile), [])
        self.assertNotRegex(result, r"Kafka|Kubernetes|99|портфолио|https?://")
        record = model.last_letter_record
        self.assertTrue(record["fallback_used"])
        self.assertEqual(record["revision_count"], 0)
        self.assertTrue(record["technical_notes"])
        self.assertEqual(record["final_text"], result)
        self.assertFalse(record["requires_approval"])

    def test_three_persistently_unsupported_drafts_stop_at_two_repairs_and_use_safe_facts(self):
        bad = "Использовал Kafka в production и обслуживал 99 миллионов пользователей."
        model = StubModel([bad, bad, bad])
        result = self.write(model)
        self.assertTrue(result.strip())
        self.assertNotRegex(result, r"Kafka|99")
        self.assertEqual(len(model.generation_calls), 3)
        self.assertEqual(model.last_letter_record["revision_count"], 2)
        self.assertTrue(model.last_letter_record["fallback_used"])
        self.assertEqual(personal_claim_issues(result, model.profile), [])

    def test_rd_required_answers_precede_any_greeting(self):
        description = (
            "R&D backend: исследуем качество данных и надёжность интеграций.\n"
            "Начните сопроводительное письмо с ответов на два вопроса:\n"
            "1. Какие задачи сбора и проверки данных вы решали?\n"
            "2. Какой ваш личный вклад в интеграции внешних API?\n"
            "Нужны конкретные подтверждённые примеры."
        )
        good = ("1. В ETL Workbench разрабатывал асинхронный сбор данных через asyncio, нормализацию "
                "и SQL-запросы. Это мой подтверждённый пример работы с внешними источниками и проверкой данных.\n\n"
                "2. В Courier Desk реализовал провайдерные адаптеры доставки, обработку ошибок API "
                "и валидацию заявок. Мой вклад — серверная логика интеграций и согласование статусов процессов.")
        model = StubModel([good])
        result = self.write(model, description=description, title="R&D Python Developer")
        self.assertTrue(result.startswith("1."))
        self.assertRegex(result, r"(?m)^2\.")
        self.assertNotRegex(result, r"^(?:Здравствуйте|Добрый|Hello)")
        self.assertEqual(len(model.last_letter_record["analysis"]["opening_questions"]), 2)
        self.assertFalse(any("objective:opening" in issue or "objective:questions" in issue
                             for issue in model.last_letter_record["validation_issues"]))

    def test_english_video_letter_preserves_normal_words_and_has_no_invented_portfolio(self):
        description = ("Build AI video automation with Python APIs and data-processing workflows. "
                       "Please write the cover letter in English. Include portfolio examples of completed AI videos.")
        model = StubModel([GOOD_EN])
        result = self.write(model, title="AI Video Engineer", description=description)
        self.assertIn("asynchronous data collection with Python and asyncio", result)
        self.assertNotIn("ETL Workbench", result)
        self.assertIn("repeatable processing workflow", result)
        self.assertEqual(model.last_letter_record["analysis"]["language"], "en")
        self.assertEqual(len(model.generation_calls), 1)
        self.assertNotRegex(result, r"https?://|attached|NDA|my completed videos")
        self.assertTrue(model.last_letter_record["plan"]["missing_materials"])
        self.assertFalse(any(issue.startswith("objective:") for issue in model.last_letter_record["validation_issues"]))

    def test_local_video_evidence_rule_does_not_accept_a_false_claim_after_repair_limit(self):
        description = ("Build AI video automation with Python APIs. Write the cover letter in English. "
                       "Include portfolio examples of completed AI videos.")
        bad = GOOD_EN + " I produced AI videos for advertising campaigns."
        model = StubModel([bad, bad, bad])
        result = self.write(model, title="AI Video Engineer", description=description)
        self.assertNotIn("I produced AI videos", result)
        self.assertFalse(any(issue.startswith("objective:video") for issue in model.last_letter_record["validation_issues"]))
        self.assertLessEqual(len(model.generation_calls), 3)


class ActiveChatGeneration(unittest.TestCase):
    def test_model_answer_can_explain_familiar_sqlalchemy_without_claiming_personal_production(self):
        question = "Знакомы ли вы с асинхронной SQLAlchemy?"
        answer = "Знаком с принципами асинхронной SQLAlchemy. Основная практика работы с данными связана с PostgreSQL."
        response = json.dumps({"kind": "question", "question": question, "reply": answer}, ensure_ascii=False)
        model = StubModel([response])
        decision = model.decide(RESUME, "", "Работодатель: " + question, [])
        self.assertEqual(decision.kind, "question")
        self.assertEqual(decision.reply, answer)
        self.assertEqual(personal_claim_issues(decision.reply, model.profile), [])
        self.assertEqual(len(model.generation_calls), 1)

    def test_model_failure_uses_the_existing_source_example_and_keeps_reply_automatic(self):
        model = StubModel(unavailable=True)
        question = "Какие команды Git используете чаще всего?"
        decision = model.decide(RESUME, "", "Работодатель: " + question, [])
        self.assertEqual(decision.kind, "question")
        self.assertIn("status", decision.reply)
        self.assertIn("merge", decision.reply)
        self.assertEqual(personal_claim_issues(decision.reply, model.profile), [])
        self.assertFalse(model.last_chat_record["requires_approval"])
        self.assertEqual(model.last_chat_record["final_text"], decision.reply)
        self.assertEqual(model.last_chat_record["prompt_version"], PROMPT_VERSION)

    def test_model_failure_keeps_familiar_sqlalchemy_as_knowledge_not_production(self):
        model = StubModel(unavailable=True)
        decision = model.decide(RESUME, "", "Работодатель: Работали ли с асинхронной SQLAlchemy?", [])
        self.assertEqual(decision.kind, "question")
        self.assertTrue(decision.reply.startswith("Знаком с принципами"))
        self.assertEqual(personal_claim_issues(decision.reply, model.profile), [])
        self.assertNotIn("Использовал SQLAlchemy", decision.reply)

    def test_model_failure_for_unknown_ordinary_tool_uses_safe_adjacent_facts(self):
        model = StubModel(unavailable=True)
        decision = model.decide(RESUME, "", "Работодатель: Какой у вас опыт с Kafka?", [])
        self.assertEqual(decision.kind, "question")
        self.assertTrue(decision.reply.strip())
        self.assertEqual(personal_claim_issues(decision.reply, model.profile), [])
        self.assertNotRegex(decision.reply, r"(?:с Kafka не работал|опыта с Kafka нет|Kafka.{0,20}\d+\s+лет)")
        self.assertFalse(model.last_chat_record["requires_approval"])


if __name__ == "__main__":
    unittest.main()
