"""Standard command explanations are distinct from claims about the candidate's past work."""

import unittest
from unittest.mock import Mock

from hh_auto.ai.client import AiClient, AiError, Decision, letter_invented_tech, reply_invented_numbers
from hh_auto.ai.profile import CandidateProfile
from hh_auto.core.config import Ai


class TechnicalRepliesTest(unittest.TestCase):
    def client(self, skills=("Git",)):
        client = AiClient(Ai(provider="custom", base_url="http://example.invalid/v1", model="test"))
        client.profile = CandidateProfile(confirmed_skills=skills)
        client.last_raw = ""
        client.complete = Mock(side_effect=AiError("offline model"))
        return client

    def fix(self, client, question, reply):
        return client._fix_reply(Decision("question", question, reply), [], "", "").reply

    def test_real_git_command_questions_keep_useful_answers_without_rewriting(self):
        reply = ("Для рабочего цикла: git status, git add и git commit; историю смотрю через git log, "
                 "перенос коммитов — git cherry-pick.")
        for question in ("Какие команды Git вы используете чаще всего?",
                         "Понял, а могли бы уточнить, какие именно команды Git вы применяете чаще всего в своей работе?"):
            with self.subTest(question=question):
                client = self.client()
                self.assertEqual(self.fix(client, question, reply), reply)
                client.complete.assert_not_called()

    def test_linux_knowledge_can_explain_commands_without_claiming_personal_usage(self):
        client = self.client()
        question = "Какие команды Linux полезны для диагностики и изменения прав?"
        reply = ("Для процессов подходят ps и top; для служб — systemctl status и journalctl. "
                 "Права меняет chmod 755.")
        self.assertEqual(self.fix(client, question, reply), reply)
        client.complete.assert_not_called()

    def test_unknown_linux_personal_experience_is_still_blocked(self):
        client = self.client()
        question = "Понял вас. Следующий вопрос: работали ли вы с терминалом Linux?"
        reply = "Работал с терминалом Linux и использовал ps и top."
        result = self.fix(client, question, reply)
        self.assertNotIn("Linux", result)
        self.assertNotIn("использовал ps", result)

    def test_command_arguments_do_not_whitelist_years_of_experience(self):
        question = "Какие команды Linux используете для настройки прав?"
        reply = "Применяю chmod 755. Администрировал Linux 9 лет."
        problems = reply_invented_numbers(reply, question, "Linux", "")
        self.assertNotIn("755", problems)
        self.assertIn("9 лет", problems)

    def test_git_commands_do_not_allow_unconfirmed_tools_or_employers(self):
        for reply, forbidden in (("Использую Git и внедрял Kubernetes.", "Kubernetes"),
                                 ("Применял git rebase в компании ExampleCorp.", "ExampleCorp")):
            with self.subTest(forbidden=forbidden):
                result = self.fix(self.client(), "Какие команды Git вы используете чаще всего?", reply)
                self.assertNotIn(forbidden, result)

    def test_cover_letter_technology_guard_remains_strict(self):
        self.assertIn("status", letter_invented_tech("Использую git status и git log.", "Git"))


if __name__ == "__main__":
    unittest.main()
