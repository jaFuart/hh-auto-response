"""New personal evidence broadens a skill without rewriting its historical project scope."""

import json
import unittest

from hh_auto.ai.claims import personal_claim_issues
from hh_auto.ai.profile import parse_candidate_profile
from tests.test_active_generation import RESUME, StubModel


def updated_profile():
    return parse_candidate_profile({
        "schema_version": "1.0", "candidate": {"primary_title": "Python developer"},
        "competencies": {
            "Historical": {
                "hands_on": ["Python", "Redis в Post Service"],
                "familiar": ["Go/React/TypeScript/Next.js входят в командный стек; личная глубина не установлена"],
                "needs_confirmation": ["Django REST Framework (DRF)", "Celery/RQ production", "Pydantic/Alembic/WebSocket"],
            },
            "Current personal confirmations": {"hands_on": [
                "Go (Golang): небольшой практический опыт разработки",
                "C++: базовые знания и небольшой практический опыт, переписывание legacy-кода",
                "Delphi (Object Pascal): базовые знания и небольшой практический опыт, переписывание legacy-кода",
                "PHP: базовые знания и небольшой практический опыт, переписывание legacy-кода",
                "Java: базовые знания и небольшой практический опыт, переписывание legacy-кода",
                "Django REST Framework: REST API в проекте Learning Portal",
                "Celery: фоновые задачи в проекте Learning Portal", "YooKassa: платежи в проекте Learning Portal",
                "WireGuard: VPN-инфраструктура в проекте VPN Project", "Redis: очереди и кэширование",
                "React / TypeScript: интерфейсы веб-приложений", "Express.js: серверная часть приложений",
                "Power BI: отчёты и дашборды", "Power Query: преобразование данных",
                "Power Query M: запросы и преобразования", "Business Intelligence (BI): аналитика и отчётность",
                "HR Analytics: анализ текучести персонала", "R / RStudio: анализ данных и скрипты",
                "OpenAPI / Swagger: документирование API", "pytest-django: тестирование Django-приложений",
                "Ruff: линтинг", "Pyodide: Python в браузере", "Linux Networking: NAT, маршрутизация, nftables, TUN/TAP",
                "IPAM: управление адресацией", "GitHub Actions: сборки и релизы",
                "TOTP / 2FA: аутентификация", "CSRF / CSP: безопасность", "RBAC / ACL: разграничение прав",
                "Idempotency / Transactional Outbox: надёжные фоновые операции",
            ]},
        },
        "projects": [
            {"name": "Shared Storage", "role": "совместная разработка с командой", "description": "Хранение данных",
             "personal_contribution": ["файловая панель"],
             "technology": ["Go (командный стек)", "React/TypeScript (командный стек)"]},
            {"name": "Learning Portal", "role": "разработчик", "description": "REST API, фоновые задачи, платежи",
             "personal_contribution": ["REST API", "фоновые задачи", "интеграция платежей"],
             "technology": ["Django REST Framework", "Celery", "YooKassa"]},
            {"name": "VPN Project", "role": "разработчик", "description": "VPN-инфраструктура",
             "personal_contribution": ["WireGuard VPN-инфраструктура"], "technology": ["WireGuard"]},
        ],
        "screening_examples": [{"question": "Какие задачи решали с Django?", "answer": "Старый пример без конкретного кейса."}],
    })


class ConfirmedSkillEvidence(unittest.TestCase):
    def setUp(self):
        self.profile = updated_profile()

    def test_source_aliases_have_the_same_practice_level_and_unknown_neighbors_stay_unknown(self):
        aliases = ("DRF", "Celery", "YooKassa", "ЮКасса", "WireGuard", "PowerBI", "PowerQuery", "M", "BI",
                   "HR Analytics", "R", "RStudio", "R Studio", "Golang", "C++", "Object Pascal", "PHP", "Java",
                   "React", "TypeScript", "ExpressJS", "OpenAPI", "Swagger", "pytest_django", "Ruff", "Pyodide",
                   "Linux Networking", "NAT", "nftables", "TUN/TAP", "IPAM", "GitHub Actions", "TOTP", "2FA",
                   "CSRF", "CSP", "RBAC", "ACL", "идемпотентность", "Outbox")
        for alias in aliases:
            with self.subTest(alias=alias):
                self.assertEqual(self.profile.skill_level(alias), "hands_on")
        for tool in ("Pydantic", "Alembic", "WebSocket", "RQ"):
            self.assertEqual(self.profile.skill_level(tool), "needs_confirmation")
        self.assertEqual(self.profile.skill_level("Next.js"), "familiar")
        self.assertEqual(self.profile.skill_level("JWT"), "")
        self.assertEqual(self.profile.approved_urls, ())

    def test_new_personal_go_frontend_and_redis_clear_only_obsolete_personal_limits(self):
        for text in ("Разрабатывал на Go.", "Разработал интерфейсы на React и TypeScript.",
                     "Использовал Redis для очередей и кэширования."):
            with self.subTest(text=text):
                self.assertEqual(personal_claim_issues(text, self.profile), [])
        self.assertTrue(personal_claim_issues("Разрабатывал на Next.js.", self.profile))
        self.assertEqual(self.profile.projects[0].personal_technologies, ())
        self.assertIn("Go (командный стек)", self.profile.projects[0].team_technologies)

    def test_named_project_requires_personal_source_association_even_for_a_globally_known_tool(self):
        good = ("В Learning Portal разрабатывал REST API на DRF.",
                "В Learning Portal использовал Celery для фоновых задач и YooKassa для платежей.",
                "В VPN Project настраивал WireGuard.",
                "В командный стек Shared Storage входят Go и React.")
        for text in good:
            with self.subTest(text=text):
                self.assertEqual(personal_claim_issues(text, self.profile), [])
        for text in ("В Shared Storage лично разрабатывал на Go.",
                     "В Learning Portal использовал Redis.", "В VPN Project внедрил IPAM."):
            with self.subTest(text=text):
                self.assertTrue(any("проект" in issue for issue in personal_claim_issues(text, self.profile)))

    def test_basic_and_small_practice_can_be_used_but_not_upgraded_to_expertise(self):
        levels = {name: self.profile.claim_limits(name)[-1].proficiency for name in ("C++", "Delphi", "PHP", "Java", "Go")}
        self.assertEqual(levels, {"C++": "basic", "Delphi": "basic", "PHP": "basic", "Java": "basic", "Go": "limited"})
        for text in ("Использовал C++ при переписывании legacy-кода.", "Работал с Go.", "Базово владею Delphi."):
            self.assertEqual(personal_claim_issues(text, self.profile), [], text)
        for text in ("Эксперт в Go.", "Профессионально владею PHP.", "У меня большой опыт C++."):
            self.assertTrue(any("Глубина" in issue for issue in personal_claim_issues(text, self.profile)), text)

    def test_drf_topic_does_not_reuse_old_django_example_and_related_cases_are_selected(self):
        self.assertEqual(self.profile.answer_example("Какие задачи решали с Django REST Framework?"), "")
        for query in ("DRF REST API", "Celery фоновые задачи", "YooKassa платежи"):
            self.assertEqual(self.profile.relevant_projects(query, 1)[0].name, "Learning Portal")
        self.assertEqual(self.profile.relevant_projects("WireGuard VPN", 1)[0].name, "VPN Project")

    def test_single_letters_and_express_brand_names_do_not_grant_unrelated_skills(self):
        profile = parse_candidate_profile({"schema_version": "1.0", "candidate": {"primary_title": "Developer"},
                  "competencies": {"Practice": {"hands_on": ["R&D эксперименты", "Major Express интеграция", "Power BI"]}},
                  "projects": []})
        self.assertEqual(profile.skill_level("R"), "")
        self.assertEqual(profile.skill_level("Express.js"), "")
        self.assertEqual(profile.skill_level("Business Intelligence"), "")

    def test_public_decide_uses_aliases_without_creating_years_or_project_connections(self):
        for term in ("DRF", "Golang", "Go (Golang)", "Object Pascal", "Delphi (Object Pascal)", "R Studio", "M",
                     "ExpressJS", "Outbox", "React / TypeScript", "OpenAPI / Swagger", "CSRF / CSP"):
            with self.subTest(term=term):
                model = StubModel(unavailable=True)
                model.profile = self.profile
                result = model.decide(RESUME, "", "Работодатель: Работали ли вы с " + term + "?", [])
                self.assertEqual(result.kind, "question")
                self.assertEqual(result.reply, "Да.")
                self.assertEqual(model.calls, [])

    def test_normal_model_yes_does_not_merge_cpp_with_unknown_csharp_or_invent_depth_and_years(self):
        for question, supported in (("Работали ли вы с C++?", True), ("Работали ли вы с C#?", False),
                                    ("Работали с Golang?", True), ("Вы эксперт по Go?", False),
                                    ("Владеете C++ на экспертном уровне?", False), ("10 лет C++?", False)):
            with self.subTest(question=question):
                model = StubModel()
                model.profile = self.profile
                model.complete = lambda *a, **kw: json.dumps({"kind": "question", "question": question, "reply": "Да."}, ensure_ascii=False)
                result = model.decide(RESUME, "", question, [])
                self.assertEqual(result.kind, "question")
                if supported:
                    self.assertEqual(result.reply, "Да.")
                else:
                    self.assertNotEqual(result.reply, "Да.")
                    self.assertNotRegex(result.reply, r"10\s+лет|(?:работал|использовал).{0,20}C#")


if __name__ == "__main__":
    unittest.main()
