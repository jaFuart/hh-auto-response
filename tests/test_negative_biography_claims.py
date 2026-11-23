"""Unknown experience is neither invented practice nor an invented lack of practice."""

from dataclasses import replace
import unittest

from hh_auto.ai.claims import fact_judge_messages, personal_claim_issues
from hh_auto.ai.profile import CandidateAnswer, parse_candidate_profile


def candidate():
    return parse_candidate_profile({
        "schema_version": "1.0", "candidate": {"primary_title": "Python developer"},
        "competencies": {"Backend": {"hands_on": ["Python", "Django"],
                                      "familiar": ["SQLAlchemy: принципы, без подтверждённого production-опыта"],
                                      "needs_confirmation": ["Kafka"]}},
        "projects": [],
        "validation_priority": ["Уточнить, работал ли кандидат с ClickHouse, dbt и антифродом."],
    })


class NegativeBiographyEvidence(unittest.TestCase):
    def setUp(self):
        self.profile = candidate()

    def test_unknown_topics_do_not_authorize_negative_biography_in_either_language(self):
        unsupported = (
            "Фермы аккаунтов не запускал.",
            "Кейса по антифроду у меня нет.",
            "Практического опыта ClickHouse/dbt нет.",
            "При этом подтверждённого production-опыта с ClickHouse, dbt и Kafka у меня нет.",
            "Нет подтверждённого production-опыта с ClickHouse, dbt и Kafka.",
            "С Kafka не работал.",
            "С SQLAlchemy не работал в production.",
            "I have never used ClickHouse or dbt.",
            "I have no production experience with SQLAlchemy.",
            "I do not have a case in anti-fraud.",
        )
        for text in unsupported:
            with self.subTest(text=text):
                issues = personal_claim_issues(text, self.profile)
                self.assertTrue(any("Отрицание личного опыта" in item for item in issues))

    def test_explicit_candidate_negative_fact_supports_matching_declensions_not_other_unknowns(self):
        profile = replace(self.profile, clarifications=("Кейса по антифроду нет.", "С Kafka не работал."))
        for text in ("У меня нет кейса по антифроду.", "Практического опыта с Kafka нет."):
            with self.subTest(text=text):
                self.assertEqual(personal_claim_issues(text, profile), [])
        self.assertTrue(personal_claim_issues("Практического опыта ClickHouse нет.", profile))
        self.assertTrue(personal_claim_issues("Фермы аккаунтов не запускал.", profile))

    def test_one_known_denial_does_not_authorize_a_negative_technology_list(self):
        profile = replace(self.profile, clarifications=("С Kafka не работал.",))
        text = "При этом подтверждённого production-опыта с ClickHouse, dbt и Kafka у меня нет."
        self.assertTrue(personal_claim_issues(text, profile))

    def test_unconfirmed_inability_to_provide_a_case_is_not_a_candidate_fact(self):
        text = "Конкретный кейс обхода защиты привести не могу."
        self.assertTrue(personal_claim_issues(text, self.profile))
        self.assertEqual(personal_claim_issues(text, self.profile, source_facts=text), [])

    def test_raw_current_fact_or_verified_example_can_ground_a_negative(self):
        text = "Фермы аккаунтов не запускал."
        self.assertEqual(personal_claim_issues(text, self.profile, source_facts="Фермы аккаунтов я не запускал."), [])
        example = CandidateAnswer("Работали с Kafka?", "С Kafka не работал.")
        profile = replace(self.profile, answering_examples=(example,))
        self.assertEqual(personal_claim_issues("Практического опыта с Kafka нет.", profile), [])
        self.assertTrue(personal_claim_issues("С ClickHouse не работал.", profile))

    def test_unknown_evidence_and_instructions_are_not_explicit_negative_facts(self):
        for source in ("SQLAlchemy: без подтверждённого production-опыта.",
                       "Неизвестно, есть ли опыт SQLAlchemy.",
                       "Не утверждай, что опыта SQLAlchemy нет.",
                       "Уточнить опыт SQLAlchemy; не нужно утверждать, что опыта SQLAlchemy нет."):
            with self.subTest(source=source):
                self.assertTrue(personal_claim_issues("Практического опыта SQLAlchemy нет.", self.profile,
                                                     source_facts=source))

    def test_specific_production_absence_does_not_authorize_a_blanket_denial(self):
        source = "I have no production experience with SQLAlchemy."
        self.assertEqual(personal_claim_issues(source, self.profile, source_facts=source), [])
        self.assertTrue(personal_claim_issues("I have never used SQLAlchemy.", self.profile, source_facts=source))

    def test_knowledge_and_omitting_unverified_numbers_are_not_negative_biography(self):
        for text in ("Знаком с принципами асинхронной SQLAlchemy.",
                     "Точный масштаб не указываю, чтобы не добавлять непроверенные цифры.",
                     "В обработке данных не было пропусков.",
                     "SQLAlchemy использует AsyncSession для асинхронных запросов."):
            with self.subTest(text=text):
                self.assertEqual(personal_claim_issues(text, self.profile), [])

    def test_fact_judge_distinguishes_unknown_evidence_from_negative_biography(self):
        prompt = fact_judge_messages("С Kafka не работал.", "Kafka needs_confirmation")[0]["content"]
        self.assertIn("Отрицательные факты биографии", prompt)
        self.assertIn("требуют явного заявления кандидата", prompt)


if __name__ == "__main__":
    unittest.main()
