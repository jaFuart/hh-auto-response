"""An API outage must not turn a notification into an unsolicited technical reply."""
import unittest
from unittest.mock import Mock

from hh_auto.ai.client import AiClient, AiError
from hh_auto.ai.profile import CandidateProfile
from hh_auto.core.config import Ai


class AutomaticAnswerBoundaryTest(unittest.TestCase):
    def client(self):
        client = AiClient(Ai(provider="custom", base_url="http://example.invalid/v1", model="test"))
        client.profile = CandidateProfile(role="Python Developer", confirmed_skills=("Python",))
        client.complete = Mock(side_effect=AiError("offline"))
        return client

    def test_notification_does_not_receive_an_outage_fallback(self):
        decision = self.client().decide("Python", "", "Спасибо, информация сохранена.", [])
        self.assertEqual(decision.kind, "info")
        self.assertFalse(decision.reply)

    def test_imperative_technical_question_still_gets_an_automatic_answer(self):
        decision = self.client().decide("Python", "", "Расскажите о вашем опыте с Kafka", [])
        self.assertEqual(decision.kind, "question")
        self.assertTrue(decision.reply)

    def test_outage_salary_and_date_answers_use_the_actual_topic(self):
        context = "Ожидаю 150 000–200 000 рублей на руки. Дата выхода пока не определена."
        salary = self.client().decide("Python", context, "Какие зарплатные ожидания?", [])
        self.assertIn("150", salary.reply)
        date = self.client().decide("Python", context, "Когда готовы приступить к работе?", [])
        self.assertIn("выхода", date.reply)
        self.assertNotIn("Python", date.reply)
