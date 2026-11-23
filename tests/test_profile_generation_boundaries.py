"""Automatic source-based recovery must preserve protected boundaries and source evidence."""

from dataclasses import replace
import unittest

from hh_auto.ai.claims import personal_claim_issues
from hh_auto.ai.profile import parse_candidate_profile
from tests.test_active_generation import DESCRIPTION, GOOD_RU, RESUME, StubModel
from tests.test_rich_candidate_profile import example_profile


class ProtectedProfileRecovery(unittest.TestCase):
    def test_truthy_profile_does_not_override_protected_requests_after_api_failure(self):
        requests = (
            "Пришлите скан паспорта и СНИЛС.",
            "Когда вам удобно созвониться?",
            "Вы бот или человек?",
            "Are you an AI or a human?",
            "Готовы принять оффер и подписать договор?",
            "Какие команды Git используете? Пришлите пароль от репозитория.",
            "Какие команды Git используете? Укажите код из SMS.",
        )
        for request in requests:
            with self.subTest(request=request):
                model = StubModel(unavailable=True)
                self.assertTrue(model.profile)
                result = model.decide(RESUME, "", "Работодатель: " + request, [])
                self.assertEqual(result.kind, "human")
                self.assertEqual(result.reply, "")
                self.assertEqual(model.last_chat_record["kind"], "human")

    def test_same_profile_automatically_answers_an_ordinary_unknown_tool_with_adjacent_facts(self):
        model = StubModel(unavailable=True)
        result = model.decide(RESUME, "", "Работодатель: Какой у вас опыт с Kafka?", [])
        self.assertEqual(result.kind, "question")
        self.assertTrue(result.reply.strip())
        self.assertEqual(personal_claim_issues(result.reply, model.profile), [])
        self.assertNotRegex(result.reply, r"(?:использовал|работал|внедрял).{0,30}Kafka")
        self.assertNotRegex(result.reply, r"(?:с Kafka не работал|опыта с Kafka нет)")
        self.assertEqual(model.last_chat_record["profile_schema_version"], "1.0")
        self.assertEqual(model.last_chat_record["profile_updated_at"], "2026-11-14")


class SourceEvidenceIntegration(unittest.TestCase):
    def test_outage_reply_preserves_the_confirmed_redis_case_scope(self):
        model = StubModel(unavailable=True)
        model.profile = parse_candidate_profile(example_profile())
        result = model.decide(RESUME, "", "Работодатель: Расскажите о практическом применении Redis?", [])
        self.assertEqual(result.kind, "question")
        self.assertEqual(personal_claim_issues(result.reply, model.profile), [])
        self.assertIn("Post Service", result.reply)
        self.assertNotIn("универсальный", result.reply)

    def test_validation_priorities_are_metadata_not_facts_or_outage_answers(self):
        raw = example_profile()
        plans = ["Уточнить конкретные задачи и проекты с Django.",
                 "Проверить точный объём практики Redis и Kubernetes."]
        raw["validation_priority"] = plans
        profile = parse_candidate_profile(raw)
        self.assertEqual(profile.validation_priorities, tuple(plans))
        self.assertEqual(profile.clarifications, ())
        self.assertEqual(profile.to_dict()["validation_priorities"], tuple(plans))
        for plan in plans:
            self.assertNotIn(plan, profile.render_facts())
            self.assertNotIn(plan, profile.render("Django Redis Kubernetes"))
        model = StubModel(unavailable=True)
        model.profile = profile
        result = model.decide(RESUME, "", "Работодатель: Сколько лет вы работали с Django?", [])
        self.assertEqual(result.kind, "question")
        self.assertNotRegex(result.reply, r"(?i)уточнить|проверить|конкретные задачи и проекты")
        self.assertNotRegex(result.reply, r"\d+\s+лет")
        self.assertIn("Django", result.reply)

    def test_a_source_provided_repository_url_survives_the_actual_russian_letter_guard(self):
        url = "https://github.com/example/courier-desk"
        good = GOOD_RU + " Код проекта: " + url + "."
        model = StubModel([good, good, good])
        projects = (replace(model.profile.projects[0], allowed_urls=(url,)), *model.profile.projects[1:])
        model.profile = replace(model.profile, projects=projects, approved_urls=(url,))
        result = model.write_letter(RESUME, "", "Python Integration Developer", "Тестовая компания",
                                    DESCRIPTION + " Приложите ссылку на код релевантного проекта.", 1800)
        self.assertIn(url, result)
        self.assertFalse(model.last_letter_record["fallback_used"])
        self.assertEqual(personal_claim_issues(result, model.profile), [])


if __name__ == "__main__":
    unittest.main()
