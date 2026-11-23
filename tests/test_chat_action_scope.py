"""Chat answers cannot initiate applications or act in another chat; synthetic evidence only."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hh_auto.ai.client import AiClient, Decision, chat_action_request, review, safe_chat_button
from hh_auto.core.config import Ai, Config
from hh_auto.hh import chats
from playwright.sync_api import Error as PlaywrightError
from tests.test_robot_chats import MemoryLog


QUESTION = "Есть ли у вас опыт с Python?"
SCOPE = {"chat_id": "chat1", "message_id": "chatik-chat-message-100", "attribute": "data-qa",
         "question": QUESTION, "buttons": ["Да", "Нет"], "existing_application": False}
START_QUESTION = "Готовы начать интервью в этом чате?"


class ChatActionPolicy(unittest.TestCase):
    def client(self, decision):
        client = AiClient(Ai(provider="custom", base_url="http://example.invalid/v1", model="test"))
        client._decide = Mock(return_value=decision)
        return client

    def test_application_invitation_interest_and_start_labels_are_never_answers(self):
        for label in ("Откликнуться", "Принять приглашение", "Интересно", "Да, интересно", "Начать",
                      "Продолжить", "Добавить сопроводительное", "Отправить", "Принять", "Apply",
                      "Accept invitation", "Start", "Send", "I'm interested"):
            with self.subTest(label=label):
                self.assertFalse(safe_chat_button(label, QUESTION))
                client = self.client(Decision("question", QUESTION, button=label))
                decision = client.decide("Python", "", QUESTION, [], [label])
                self.assertEqual(decision.kind, "human")
                self.assertFalse(decision.button)
                self.assertFalse(decision.reply)

    def test_application_interest_and_new_screening_questions_are_left_without_model_call(self):
        for question in ("Хотите откликнуться на вакансию?", "Вас заинтересовала эта вакансия?",
                         "Готовы начать опрос по вакансии?", "Принять приглашение?",
                         "Are you interested in this position?", "Would you like to apply for this job?"):
            with self.subTest(question=question):
                client = self.client(Decision("question", question, button="Да"))
                self.assertEqual(client.decide("Python", "", question, [], ["Да"]).__dict__["kind"], "human")
                client._decide.assert_not_called()

    def test_ordinary_question_options_continue_and_notifications_do_not_prove_an_answer(self):
        for label in ("Да", "Нет", "Python", "PostgreSQL", "До одного года"):
            self.assertTrue(safe_chat_button(label, QUESTION))
        self.assertFalse(safe_chat_button("Да", "Спасибо за информацию."))
        self.assertTrue(safe_chat_button("Python", "Выберите используемый язык"))
        client = self.client(Decision("question", QUESTION, button="Да"))
        self.assertEqual(client.decide("Python", "", QUESTION, [], ["Да"]).button, "Да")

    def test_motivation_question_is_not_confused_with_accepting_job_interest(self):
        question = "Почему вас заинтересовала наша вакансия?"
        self.assertFalse(chat_action_request(question))
        client = self.client(Decision("question", question, "Интересны задачи разработки API."))
        self.assertEqual(client.decide("Python", "", question, []).kind, "question")

    def test_prompt_does_not_authorize_accepting_an_invitation(self):
        client = self.client(Decision("info"))
        text = "\n".join(message["content"] for message in client.build_messages("Python", "", QUESTION, buttons=["Да"]))
        self.assertNotIn("ответ на приглашение hh — тогда кнопка", text)
        self.assertIn("не принимай приглашение", text)

    def test_existing_application_allows_only_interview_controls_and_records_the_flag(self):
        for text in ("Начать", "Продолжить", "Да, интересно"):
            with self.subTest(text=text):
                self.assertFalse(safe_chat_button(text, START_QUESTION))
                self.assertTrue(safe_chat_button(text, START_QUESTION, existing_application=True))
                client = self.client(Decision("question", START_QUESTION, button=text))
                decision = client.decide("Python", "", START_QUESTION, [], [text], existing_application=True)
                self.assertEqual(decision.button, text)
                self.assertTrue(client.last_chat_record["existing_application"])
                client._decide.assert_called_once()
        for question in ("Начнём?", "Продолжим?", "Интересно?", "Нажмите «Начать», чтобы продолжить анкету"):
            self.assertTrue(chat_action_request(question))
            self.assertFalse(chat_action_request(question, existing_application=True))

    def test_existing_application_never_authorizes_new_application_or_invitation(self):
        for label in ("Откликнуться", "Принять приглашение", "Добавить сопроводительное", "Apply"):
            with self.subTest(label=label):
                self.assertFalse(safe_chat_button(label, QUESTION, existing_application=True))
                client = self.client(Decision("question", QUESTION, button=label))
                self.assertEqual(client.decide("Python", "", QUESTION, [], [label], existing_application=True).kind, "human")
        client = self.client(Decision("question", "Принять приглашение?", button="Да"))
        self.assertEqual(client.decide("Python", "", "Принять приглашение?", [], ["Да"], existing_application=True).kind, "human")
        client._decide.assert_not_called()

    def test_existing_bot_screening_does_not_authorize_a_scheduled_human_meeting(self):
        decision = Decision("question", START_QUESTION, button="Начать")
        self.assertEqual(review(decision, START_QUESTION, [], existing_application=True), "")
        self.assertEqual(decision.kind, "question")
        scheduled = "Готовы начать интервью завтра в 15:00?"
        decision = Decision("question", scheduled, button="Начать")
        self.assertTrue(review(decision, scheduled, [], existing_application=True))
        self.assertEqual(decision.kind, "human")


class ScopedChatActions(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        self.cfg.ai.enabled = self.cfg.ai.only_robot = True
        self.chat = chats._Chat("chat1", "Разработчик", "Компания", QUESTION, True, latest_at="12:00")
        self.sender = chats._Sender("Робот hh", SCOPE["message_id"], provider_robot=True)
        self.page = Mock(url="https://hh.ru/chat/chat1")
        self.ai = Mock(notes=[], last_chat_record={})
        self.ai.decide.return_value = Decision("question", QUESTION, button="Да")
        self.log = MemoryLog()
        self.stats = chats.ChatStats()
        self.transcript = f"Разработчик\n{QUESTION}"

    def converse(self):
        return chats._converse(self.page, self.chat, self.cfg, self.ai, "Python", self.stats, False,
                               self.log, [], self.transcript, "Разработчик — Компания")

    def patches(self, *, scope=None):
        replacements = {
            "_buttons": Mock(return_value=["Да"]),
            "_button_scope": Mock(return_value=scope or copy.deepcopy(SCOPE)),
            "_current_sender": Mock(return_value=self.sender),
            "_transcript": Mock(return_value=self.transcript),
            "_wait_next_message": Mock(return_value=None),
            "_send": Mock(return_value=True),
            "_press": Mock(return_value=True),
        }
        for name, replacement in replacements.items():
            manager = patch.object(chats, name, replacement)
            manager.start()
            self.addCleanup(manager.stop)
        return replacements

    def test_engine_rejects_application_button_without_pending_or_outbound(self):
        self.ai.decide.return_value = Decision("question", QUESTION, button="Откликнуться")
        replacements = self.patches(scope={**SCOPE, "buttons": ["Откликнуться"]})
        replacements["_buttons"].return_value = ["Откликнуться"]
        self.converse()
        replacements["_press"].assert_not_called()
        replacements["_send"].assert_not_called()
        self.assertEqual(self.log.text, "")

    def test_wrong_chat_scope_rejects_globally_matching_answer_without_pending(self):
        replacements = self.patches(scope={**SCOPE, "chat_id": "another-chat"})
        self.converse()
        replacements["_press"].assert_not_called()
        self.assertEqual(self.log.text, "")

    def test_unknown_provider_or_stale_message_cannot_offer_buttons(self):
        for sender, scope in ((chats._Sender("Робот-рекрутер", SCOPE["message_id"]), SCOPE),
                              (self.sender, {**SCOPE, "message_id": "chatik-chat-message-99"})):
            with self.subTest(sender=sender):
                with patch.object(chats, "_buttons", return_value=["Да"]), \
                        patch.object(chats, "_button_scope", return_value=scope), \
                        patch.object(chats, "_current_sender", return_value=sender), \
                        patch.object(chats, "_press") as press:
                    self.converse()
                press.assert_not_called()
                self.assertEqual(self.log.text, "")

    def test_valid_current_robot_question_button_preserves_pending_and_confirmed_records(self):
        replacements = self.patches()
        self.converse()
        replacements["_press"].assert_called_once()
        self.assertEqual(replacements["_press"].call_args.kwargs["chat_id"], "chat1")
        self.assertEqual(replacements["_press"].call_args.kwargs["message_id"], SCOPE["message_id"])
        rows = [json.loads(line) for line in self.log.text.splitlines()]
        self.assertTrue(rows[0]["pending"])
        self.assertTrue(rows[1]["sent"])
        self.assertEqual(self.stats.answered, 1)

    def test_start_button_works_for_proven_application_in_the_exact_current_chat(self):
        self.transcript = f"Разработчик\n{START_QUESTION}"
        scope = {**SCOPE, "question": START_QUESTION, "buttons": ["Начать"], "existing_application": True}
        replacements = self.patches(scope=scope)
        replacements["_buttons"].return_value = ["Начать"]
        self.ai.decide.return_value = Decision("question", START_QUESTION, button="Начать")
        self.converse()
        self.ai.decide.assert_called_once()
        self.assertTrue(self.ai.decide.call_args.kwargs["existing_application"])
        replacements["_press"].assert_called_once()
        self.assertEqual(self.stats.answered, 1)

    def test_start_without_application_proof_or_in_another_chat_is_not_sent_to_model(self):
        self.transcript = f"Разработчик\n{START_QUESTION}"
        for scope in ({**SCOPE, "question": START_QUESTION, "buttons": ["Начать"]},
                      {**SCOPE, "question": START_QUESTION, "chat_id": "other", "existing_application": True}):
            with self.subTest(scope=scope):
                with patch.object(chats, "_button_scope", return_value=scope), \
                        patch.object(chats, "_current_sender", return_value=self.sender):
                    self.converse()
        self.ai.decide.assert_not_called()
        self.assertEqual(self.log.text, "")

    def test_text_yes_loses_application_proof_after_reply_wait_and_never_claims_or_sends(self):
        self.transcript = f"Разработчик\n{START_QUESTION}"
        scope = {**SCOPE, "question": START_QUESTION, "buttons": [], "existing_application": True}
        replacements = self.patches(scope=scope)
        replacements["_buttons"].return_value = []
        self.ai.decide.return_value = Decision("question", START_QUESTION, "Да.")

        def after_wait(*args):
            scope["existing_application"] = False
            return True

        with patch.object(chats, "_wait_robot_reply_slot", side_effect=after_wait):
            self.converse()
        self.assertTrue(self.ai.decide.call_args.kwargs["existing_application"])
        replacements["_send"].assert_not_called()
        self.assertEqual(self.log.text, "")

    def test_text_yes_rechecks_application_proof_after_fill_and_releases_pending(self):
        real_send = chats._send
        self.transcript = f"Разработчик\n{START_QUESTION}"
        scope = {**SCOPE, "question": START_QUESTION, "buttons": [], "existing_application": True}
        replacements = self.patches(scope=scope)
        replacements["_buttons"].return_value = []
        self.ai.decide.return_value = Decision("question", START_QUESTION, "Да.")
        box, composer = Mock(), Mock()
        composer.get_attribute.return_value = "chatik-message-input"
        box.evaluate.return_value = True
        box.input_value.return_value = "Да."
        box.fill.side_effect = lambda text, **kwargs: scope.update(existing_application=False) if text else None
        with patch.object(chats, "_send", side_effect=real_send), \
                patch.object(chats, "_composer_input", return_value=(box, composer)), \
                patch.object(chats.control, "sleep"):
            self.converse()
        box.press.assert_not_called()
        rows = [json.loads(line) for line in self.log.text.splitlines()]
        self.assertTrue(rows[0]["pending"])
        self.assertTrue(rows[1]["released"])
        self.assertEqual(chats._past_answers(self.log, self.chat.id)[1], set())

    def test_wrong_chat_url_blocks_text_and_buttons_before_model_or_pending(self):
        replacements = self.patches()
        for url in ("https://hh.ru/chat/other", "https://other.invalid/chat/chat1", "https://hh.ru/chat"):
            with self.subTest(url=url):
                self.page.url = url
                self.converse()
        self.ai.decide.assert_not_called()
        replacements["_press"].assert_not_called()
        replacements["_send"].assert_not_called()
        self.assertEqual(self.log.text, "")

    def test_navigation_during_model_call_cannot_create_pending_text_send(self):
        self.ai.decide.side_effect = lambda *args: setattr(self.page, "url", "https://hh.ru/chat/other") or Decision(
            "question", QUESTION, "Да, использовал Python.")
        replacements = self.patches()
        self.converse()
        replacements["_send"].assert_not_called()
        self.assertEqual(self.log.text, "")

    def test_same_turn_guard_includes_exact_chat_url(self):
        with patch.object(chats, "_current_sender", return_value=self.sender), \
                patch.object(chats, "_transcript", return_value=self.transcript):
            key = chats._turn_key(self.chat, self.transcript, self.sender)
            self.assertTrue(chats._same_robot_turn(self.page, self.cfg, self.chat, key))
            self.page.url = "https://hh.ru/chat/other"
            self.assertFalse(chats._same_robot_turn(self.page, self.cfg, self.chat, key))

    def test_unconfirmed_chat_navigation_stops_instead_of_using_previous_panel(self):
        self.page.wait_for_url.side_effect = PlaywrightError("timeout")
        with patch.object(chats.browser, "wait_read_interval"), patch.object(chats.control, "sleep"):
            with self.assertRaisesRegex(PlaywrightError, "точный ID"):
                chats._click_chat(self.page, self.chat, self.cfg)

    def test_old_panel_is_not_accepted_after_three_seconds_or_at_timeout(self):
        clock = [0.0]
        with patch.object(chats, "_transcript", return_value="Другая вакансия\nЧужой вопрос?"), \
                patch.object(chats.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(chats.control, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            self.assertIsNone(chats._wait_transcript(self.page, self.chat, timeout=4, cfg=self.cfg))
        self.assertGreaterEqual(clock[0], 4)

    def test_press_uses_current_message_locator_and_never_global_text_match(self):
        message = self.page.locator.return_value.filter.return_value
        message.count.return_value = 1
        button = message.locator.return_value.filter.return_value.filter.return_value
        button.count.return_value = 1
        with patch.object(chats, "_button_scope", return_value=copy.deepcopy(SCOPE)), \
                patch.object(chats, "_transcript", side_effect=[self.transcript, self.transcript + "\nДа"]), \
                patch.object(chats.control, "sleep"):
            self.assertTrue(chats._press(self.page, "Да", guard=lambda: True, chat_id="chat1",
                                        message_id=SCOPE["message_id"]))
        self.page.locator.assert_called_once_with('[data-qa="chatik-chat-message-100"]')
        button.click.assert_called_once()
        button.last.click.assert_not_called()

    def test_press_independently_rejects_cta_and_wrong_chat_before_click(self):
        for text, scope in (("Откликнуться", {**SCOPE, "buttons": ["Откликнуться"]}),
                            ("Да", {**SCOPE, "chat_id": "other"})):
            with self.subTest(text=text):
                with patch.object(chats, "_button_scope", return_value=scope), \
                        patch.object(chats, "_transcript", return_value=self.transcript):
                    with self.assertRaises(chats._SendNotAttempted):
                        chats._press(self.page, text, chat_id="chat1", message_id=SCOPE["message_id"])
        self.page.locator.assert_not_called()

    def test_button_turn_change_after_delay_releases_pending_instead_of_sending(self):
        message = self.page.locator.return_value.filter.return_value
        message.count.return_value = 1
        button = message.locator.return_value.filter.return_value.filter.return_value
        button.count.return_value = 1
        changed = {**SCOPE, "message_id": "chatik-chat-message-101"}
        with patch.object(chats, "_buttons", return_value=["Да"]), \
                patch.object(chats, "_button_scope", side_effect=[SCOPE, SCOPE, SCOPE, changed]), \
                patch.object(chats, "_current_sender", return_value=self.sender), \
                patch.object(chats, "_transcript", return_value=self.transcript), \
                patch.object(chats.control, "sleep"):
            self.converse()
        button.click.assert_not_called()
        rows = [json.loads(line) for line in self.log.text.splitlines()]
        self.assertTrue(rows[0]["pending"])
        self.assertTrue(rows[1]["released"])
        self.assertEqual(chats._past_answers(self.log, self.chat.id)[1], set())


def browser_smoke():
    """Optional real Chromium DOM regression; every request is fulfilled locally or aborted."""
    from playwright.sync_api import sync_playwright

    html = '''<!doctype html><meta charset="utf-8"><body><div id="app"><aside><a data-qa="chatik-open-chat-chat1">Чат</a></aside>
    <main><h3>Разработчик</h3><section id="messages"><div data-qa="chatik-chat-message-100">
    <div class="magritte-bot___fixture"><span data-qa="chat-bubble-label">Робот hh</span>
    <p>Есть ли у вас опыт с Python?</p><button id="answer" onclick="reply()">Да</button>
    <button id="danger" onclick="window.danger++">Откликнуться</button></div></div></section>
    <div data-qa="chatik-message-input"><textarea></textarea></div></main></div>
    <button id="global" onclick="window.danger++">Да</button><script>
    window.danger=0;window.answers=0;function reply(){window.answers++;
    document.getElementById('messages').insertAdjacentHTML('beforeend',
    '<div data-qa="chatik-chat-message-101"><div class="magritte-outgoing___fixture">Вы: Да</div></div>');}
    </script></body>'''
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True, chromium_sandbox=False)
        except PlaywrightError as error:
            if "Executable doesn't exist" not in str(error):
                raise
            browser = pw.chromium.launch(channel="chrome", headless=True, chromium_sandbox=False)
        try:
            context = browser.new_context()
            context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html)
                          if route.request.url == "https://hh.test/chat/chat1" else route.abort())
            page = context.new_page()
            page.goto("https://hh.test/chat/chat1")
            assert chats._buttons(page) == ["Да"]
            try:
                chats._press(page, "Откликнуться", chat_url="https://hh.test/chat")
            except chats._SendNotAttempted:
                pass
            else:
                raise AssertionError("Application CTA accepted")
            assert chats._press(page, "Да", chat_id="chat1", message_id=SCOPE["message_id"],
                                chat_url="https://hh.test/chat")
            assert page.evaluate("window.answers") == 1
            assert page.evaluate("window.danger") == 0
            assert chats._buttons(page) == []
            assert chats._current_sender(page).outgoing
            application = ('<div data-qa="chatik-chat-message-99"><div class="magritte-outgoing___fixture">'
                           '<div data-qa="chat-bubble-message-title">Отклик на вакансию</div></div></div>')

            def start_page(markup="", *, outside=False):
                page.goto("https://hh.test/chat/chat1")
                page.locator("main p").evaluate("node => node.textContent = 'Готовы начать интервью в этом чате?'")
                page.locator("#answer").evaluate("node => node.textContent = 'Начать'")
                if markup:
                    page.locator("body" if outside else "#messages").evaluate(
                        "(node, html) => node.insertAdjacentHTML('afterbegin', html)", markup)

            for unproved in ("", application.replace("magritte-outgoing___fixture", "magritte-incoming___fixture"),
                             application.replace('data-qa="chat-bubble-message-title"', 'class="plain-text"'),
                             application.replace('chatik-chat-message-99', 'chatik-chat-message-unknown')):
                start_page(unproved)
                assert chats._button_scope(page)["existing_application"] is False
                assert chats._buttons(page) == []
                try:
                    chats._press(page, "Начать", chat_url="https://hh.test/chat")
                except chats._SendNotAttempted:
                    pass
                else:
                    raise AssertionError("Start accepted without own system application bubble")
            start_page(application, outside=True)
            assert chats._button_scope(page)["existing_application"] is False
            start_page(application)
            assert chats._button_scope(page)["existing_application"] is True
            assert chats._buttons(page) == ["Начать"]
            try:
                chats._press(page, "Откликнуться", chat_url="https://hh.test/chat")
            except chats._SendNotAttempted:
                pass
            else:
                raise AssertionError("Existing application authorized a new application CTA")
            assert chats._press(page, "Начать", chat_id="chat1", message_id=SCOPE["message_id"],
                                chat_url="https://hh.test/chat")
            assert page.evaluate("window.answers") == 1
            assert page.evaluate("window.danger") == 0
            print("PASS: scoped robot answers; interview start needs own same-panel system application proof; no CTA/external requests.")
        finally:
            browser.close()


if __name__ == "__main__":
    import sys
    if "--browser" in sys.argv:
        browser_smoke()
    else:
        unittest.main()
