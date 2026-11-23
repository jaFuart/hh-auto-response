"""Personal claims differ from knowledge, scoped cases and a team's technology stack."""

import unittest
from dataclasses import replace

from hh_auto.ai.claims import personal_claim_issues
from hh_auto.ai.profile import CandidateMetric, CandidateProject, parse_candidate_profile


def scoped_profile():
    return parse_candidate_profile({
        "schema_version": "1.0",
        "candidate": {"primary_title": "Python Backend Developer"},
        "competencies": {
            "Backend": {
                "hands_on": ["Python", "Git", "Frappe", "PostgreSQL", "Redis в проекте Parcel Workflow"],
                "familiar": ["асинхронная SQLAlchemy (принципы, без подтверждённого production-опыта)",
                             "Go и React входят в командный стек; личная практика требует уточнения"],
                "needs_confirmation": ["Redis как универсальное кэш-хранилище вне кейсов Frappe",
                                       "PostgreSQL performance tuning/EXPLAIN ANALYZE углублённо"],
            },
        },
        "projects": [
            {"name": "Parcel Workflow", "role": "разработчик приложения", "technology": ["Python", "Frappe", "Redis"],
             "personal_contribution": ["кэш токенов и справочников в Redis в Parcel Workflow"]},
            {"name": "Archive Pilot", "role": "совместная разработка продукта", "technology": ["Go (командный стек)"],
             "personal_contribution": ["файловая панель", "внутренний пилот: 4 региона, 240 потенциальных пользователей"],
             "evidence": "README archive-pilot"},
        ],
        "sources": [{"reference": "https://example.test/repositories/archive-pilot"}],
    })


class PersonalClaimEvidence(unittest.TestCase):
    def setUp(self):
        self.profile = scoped_profile()

    def test_familiar_sqlalchemy_allows_knowledge_but_not_production_practice(self):
        self.assertEqual(personal_claim_issues("Знаком с принципами асинхронной SQLAlchemy.", self.profile), [])
        self.assertTrue(personal_claim_issues("Использовал SQLAlchemy в production-сервисах.", self.profile))
        self.assertTrue(personal_claim_issues("Есть практический опыт с SQLAlchemy.", self.profile))
        self.assertEqual(personal_claim_issues("SQLAlchemy использует AsyncSession для асинхронных запросов.", self.profile), [])

    def test_team_tools_cannot_be_claimed_as_personal_implementation(self):
        for text in ("Разрабатывал микросервисы на Go.", "Разработал интерфейсы на React.",
                     "Использовал Python, Go и React для своих production-сервисов."):
            with self.subTest(text=text):
                self.assertTrue(personal_claim_issues(text, self.profile))
        self.assertEqual(personal_claim_issues("В командный стек Archive Pilot входят Go и React.", self.profile), [])
        self.assertEqual(personal_claim_issues("В командном проекте использовались Go и React.", self.profile), [])
        self.assertEqual(personal_claim_issues("В проекте используется Go.", self.profile), [])
        self.assertEqual(personal_claim_issues("Знаком с Go по командной кодовой базе.", self.profile), [])
        self.assertTrue(personal_claim_issues("Уверенно владею React.", self.profile))
        self.assertEqual(personal_claim_issues("I developed data workflows and go through logs during diagnostics.", self.profile), [])
        self.assertTrue(personal_claim_issues("I developed backend services in Go.", self.profile))

    def test_redis_token_cache_case_is_allowed_without_authorizing_universal_cache_claims(self):
        confirmed = "В Parcel Workflow использовал Redis для кэша токенов и справочников."
        self.assertEqual(personal_claim_issues(confirmed, self.profile), [])
        self.assertEqual(personal_claim_issues("В Frappe использовал Redis для кэша токенов.", self.profile), [])
        unconfirmed = "Использовал Redis как универсальный кэш во всех production-сервисах."
        self.assertTrue(personal_claim_issues(unconfirmed, self.profile))
        self.assertTrue(personal_claim_issues("В Parcel Workflow использовал Redis как брокер очередей.", self.profile))
        self.assertTrue(personal_claim_issues("Использовал Redis для кэша HTTP-ответов.", self.profile))
        self.assertTrue(personal_claim_issues("В Parcel Workflow использовал Redis для токенов, а также как брокер очередей.", self.profile))

    def test_declared_drf_alias_cannot_bypass_its_unconfirmed_level(self):
        data = {
            "schema_version": "1.0", "candidate": {"primary_title": "Python developer"},
            "competencies": {"Backend": {"hands_on": ["Django"], "needs_confirmation": ["Django REST Framework (DRF)"]}},
            "projects": [],
        }
        profile = parse_candidate_profile(data)
        self.assertTrue(personal_claim_issues("Использовал DRF для разработки API.", profile))

    def test_unconfirmed_advanced_aspect_does_not_erase_confirmed_basic_tool_practice(self):
        self.assertEqual(personal_claim_issues("Использовал PostgreSQL и писал SQL-запросы.", self.profile), [])
        self.assertTrue(personal_claim_issues("Применял EXPLAIN ANALYZE для PostgreSQL performance tuning.", self.profile))
        self.assertEqual(personal_claim_issues("Разрабатывал приложения на Frappe.", self.profile), [])

    def test_potential_pilot_users_do_not_become_actual_users_or_commercial_clients(self):
        valid = "Archive Pilot проходил внутренний пилот в 4 регионах для 240 потенциальных пользователей."
        self.assertEqual(personal_claim_issues(valid, self.profile), [])
        for invalid in ("Archive Pilot обслуживает 240 реальных пользователей.",
                        "Archive Pilot внедрён у коммерческого клиента."):
            with self.subTest(text=invalid):
                self.assertTrue(personal_claim_issues(invalid, self.profile))

    def test_only_source_provided_repository_url_is_allowed(self):
        self.assertEqual(personal_claim_issues("Код: https://example.test/repositories/archive-pilot.", self.profile), [])
        for url in ("https://portfolio.example/candidate", "https://github.com/example/invented-repository",
                    "https://example.test/repositories/archive-pilot/video"):
            with self.subTest(url=url):
                self.assertTrue(personal_claim_issues("Пример проекта: " + url, self.profile))

    def test_current_employer_guard_uses_profile_identity_and_allows_parent_company_note(self):
        profile = replace(self.profile, current_employer="Example Works (дочерняя компания ТДР Банка)",
                          former_employer="ТДР Банк")
        for invalid in ("Сейчас работаю в ТДР Банке.", "I work at TDR Bank.", "I work at TDRBank.",
                        "Мой текущий работодатель — ТДР Банк."):
            with self.subTest(text=invalid):
                self.assertTrue(personal_claim_issues(invalid, profile))
        for valid in ("Раньше работал в ТДР Банке.", "Сейчас работаю в Example Works.",
                      "Работаю в Example Works, дочерней компании ТДР Банка."):
            with self.subTest(text=valid):
                self.assertEqual(personal_claim_issues(valid, profile), [])
        changed = replace(profile, current_employer="Second Works", former_employer="Earlier Labs")
        self.assertTrue(personal_claim_issues("Currently I work at Earlier Labs.", changed))
        self.assertEqual(personal_claim_issues("I work at TDR Bank.", changed), [])

    def test_pilot_guard_uses_structured_metric_and_case_restrictions_not_fixture_name(self):
        pilot = CandidateProject("Warehouse Trial", limitations=("Только внутренний пилот.",),
                                 metrics=(CandidateMetric("2 400 потенциальных пользователей", "potential"),))
        production = CandidateProject("Public Portal", metrics=(CandidateMetric("2400 пользователей"),))
        profile = replace(self.profile, projects=(pilot, production))
        for invalid in ("Warehouse Trial обслуживает 2400 пользователей.",
                        "Warehouse Trial обслуживает 2 400 пользователей.",
                        "Warehouse Trial serves 2,400 users.",
                        "Warehouse Trial проходил проверку. Сейчас обслуживает 2400 пользователей.",
                        "Warehouse Trial — коммерческое внедрение."):
            with self.subTest(text=invalid):
                self.assertTrue(personal_claim_issues(invalid, profile))
        for valid in ("Warehouse Trial рассчитан на 2400 потенциальных пользователей.",
                      "Warehouse Trial — внутренний пилот, не коммерческое внедрение.",
                      "Public Portal обслуживает 2400 пользователей.",
                      "Warehouse Trial — внутренний пилот. Public Portal обслуживает 2400 пользователей.",
                      "Public Portal — коммерческое внедрение."):
            with self.subTest(text=valid):
                self.assertEqual(personal_claim_issues(valid, profile), [])
        self.assertTrue(personal_claim_issues(
            "Warehouse Trial обслуживает 2400 пользователей. Public Portal имеет потенциальную аудиторию.", profile))

    def test_redis_scope_follows_renamed_source_case_without_promoting_other_tools(self):
        old_case = self.profile.projects[0]
        new_case = replace(old_case, name="Registry Workflow",
                           tasks=("Кэш токенов и справочников в Redis в Registry Workflow",))
        skills = tuple(replace(skill, detail=skill.detail.replace("Parcel Workflow", "Registry Workflow"))
                       for skill in self.profile.skills)
        profile = replace(self.profile, projects=(new_case,), skills=skills)
        self.assertEqual(personal_claim_issues(
            "В Registry Workflow использовал Redis для кэша токенов.", profile), [])
        for invalid in ("В Parcel Workflow использовал Redis для кэша токенов.",
                        "В Python использовал Redis для кэша токенов.",
                        "В Registry Workflow использовал Redis как брокер очередей."):
            with self.subTest(text=invalid):
                self.assertTrue(personal_claim_issues(invalid, profile))


if __name__ == "__main__":
    unittest.main()
