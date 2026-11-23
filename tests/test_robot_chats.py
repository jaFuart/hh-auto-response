"""Автоответы только текущему роботу, привязка резюме и защита от повторов. Без сети и файлов."""

from __future__ import annotations

import io
import sys
import unittest
from datetime import date, timedelta
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto.ai.client import Decision  # noqa: E402
from hh_auto.core.config import Config  # noqa: E402
from hh_auto.hh import chats  # noqa: E402
from hh_auto.web.browser import FatalError  # noqa: E402
from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REAL_SEND = chats._send


class MemoryLog:
    """Тот же append/read протокол jsonl, без прав доступа к временной папке Windows."""
    def __init__(self):
        self.text = ""
        self.parent = self

    def mkdir(self, **kwargs):
        pass

    def read_text(self, **kwargs):
        return self.text

    @contextmanager
    def open(self, mode, **kwargs):
        assert mode == "a"
        buffer = io.StringIO()
        yield buffer
        self.text += buffer.getvalue()


class RobotReplies(unittest.TestCase):
    QUESTION = "Какие навыки используете?"
    TRANSCRIPT = f"Разработчик\n{QUESTION}"

    def setUp(self):
        self.cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        self.cfg.ai.enabled = self.cfg.ai.only_robot = True
        self.cfg.ai.max_answers_per_chat = 5
        self.chat = chats._Chat("chat1", "Разработчик", "Компания", self.QUESTION, True, latest_at="12:00")
        self.robot = chats._Sender("Помощник", "chatik-chat-message-123", provider_robot=True)
        self.page = Mock(url="https://hh.ru/chat/chat1")
        self.ai = Mock(notes=[])
        self.ai.decide.side_effect = lambda *args: Decision("question", self.QUESTION, "Работаю с Python.")
        self.stats = chats.ChatStats()
        self.log = MemoryLog()
        self.patches = [patch.object(chats, "_buttons", return_value=[]),
                        patch.object(chats, "_wait_next_message", return_value=None),
                        patch.object(chats, "_transcript", return_value=self.TRANSCRIPT),
                        patch.object(chats, "_current_sender", return_value=self.robot),
                        patch.object(chats, "_send", return_value=True),
                        patch.object(chats, "_press", return_value=True)]
        self.buttons, self.wait, self.transcript, self.sender, self.send, self.press = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def converse(self, transcript=None, sent=None, resume="Python-разработчик"):
        return chats._converse(self.page, self.chat, self.cfg, self.ai, resume, self.stats, False,
                               self.log, sent if sent is not None else [], transcript or self.TRANSCRIPT, "чат")

    def test_human_mentioning_old_robot_is_not_sent_to_model(self):
        self.sender.return_value = chats._Sender("Анна", "chatik-chat-message-124")
        self.converse("Робот-рекрутер\nСтарая анкета завершена\nАнна\nРобот-рекрутер уже получил ваши ответы. Какие навыки используете?")
        self.ai.decide.assert_not_called()
        self.send.assert_not_called()
        self.press.assert_not_called()
        self.assertTrue(self.stats.other_unread)

    def test_unknown_sender_is_not_inferred_from_transcript(self):
        self.sender.return_value = None
        self.converse("ИИ-помощник\n" + self.QUESTION)
        self.ai.decide.assert_not_called()
        self.send.assert_not_called()

    def test_unknown_last_message_date_skips_before_model(self):
        self.chat.latest_at = ""
        self.converse()
        self.ai.decide.assert_not_called()
        self.send.assert_not_called()

    def test_date_cutoff_is_rechecked_after_model_before_sending(self):
        with patch.object(chats, "_chat_age_reason", side_effect=["", "последнее сообщение старше 30 дней"]):
            self.converse()
        self.ai.decide.assert_called_once()
        self.send.assert_not_called()

    def test_manually_answered_read_thread_is_left_quietly_when_latest_message_is_ours(self):
        self.sender.return_value = chats._Sender("Вы", "chatik-chat-message-124", outgoing=True)
        self.converse(self.TRANSCRIPT + "\nЯ уже ответил вручную.")
        self.ai.decide.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual(self.stats.other_unread, [])

    def test_robot_requires_selected_nonempty_resume_before_model(self):
        for selected, text in (("", "Python"), ("https://hh.ru/resume/selected", " \n")):
            with self.subTest(selected=selected):
                self.cfg.resume_url = selected
                self.converse(resume=text)
        self.ai.decide.assert_not_called()
        self.send.assert_not_called()

    def test_confirmed_provider_robot_answers_and_records_message_id(self):
        self.converse()
        self.assertEqual(self.send.call_args.args, (self.page, "Работаю с Python."))
        self.assertTrue(callable(self.send.call_args.kwargs["guard"]))
        self.assertEqual(self.stats.answered, 1)
        history, handled = chats._past_answers(self.log, self.chat.id)
        self.assertEqual(history, ["Работаю с Python."])
        self.assertIn(chats._turn_key(self.chat, self.TRANSCRIPT, self.robot), handled)

    def test_same_incoming_message_is_not_answered_after_restart(self):
        self.converse()
        self.ai.decide.reset_mock()
        self.send.reset_mock()
        # Имитируем свежий запуск и отстающий интерфейс: наш ответ ещё не появился в переписке.
        self.converse(sent=[])
        self.ai.decide.assert_not_called()
        self.send.assert_not_called()

    def test_six_historical_answers_do_not_consume_the_new_run_budget(self):
        self.cfg.ai.max_answers_per_chat = 6
        history = [f"Исторический ответ {index}." for index in range(6)]
        for index, answer in enumerate(history):
            sender = chats._Sender("Помощник", f"old-{index}", provider_robot=True)
            chats._log_answer(self.log, self.chat, "Старый вопрос?", answer, sent=True,
                              turn_key=chats._turn_key(self.chat, "Старый вопрос?", sender))
        run_sent = []
        self.converse(sent=run_sent)
        self.send.assert_called_once()
        self.assertEqual(self.ai.decide.call_args.args[3], history)
        self.assertEqual(run_sent, ["Работаю с Python."])
        self.chat.badge = True
        self.assertTrue(chats._robot_wrote_again(self.chat, run_sent, "Предыдущий вопрос?", 6))

    def test_already_answered_id_still_blocked_with_a_full_historical_budget(self):
        self.cfg.ai.max_answers_per_chat = 6
        for index in range(6):
            sender = self.robot if index == 0 else chats._Sender("Помощник", f"old-{index}", True)
            chats._log_answer(self.log, self.chat, "Старый вопрос?", f"Исторический ответ {index}.", sent=True,
                              turn_key=chats._turn_key(self.chat, "Старый вопрос?", sender))
        run_sent = []
        self.converse(sent=run_sent)
        self.ai.decide.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual(run_sent, [])

    def test_current_run_limit_is_preserved_while_conversation_continues(self):
        self.cfg.ai.max_answers_per_chat = 2
        second = chats._Sender("Помощник", "chatik-chat-message-124", provider_robot=True)
        self.sender.side_effect = [self.robot, self.robot, second, second]
        next_transcript = self.TRANSCRIPT + "\nРаботаю с Python.\n" + self.QUESTION
        self.wait.side_effect = [next_transcript, next_transcript + "\nРаботаю с Python.\n" + self.QUESTION]
        run_sent = []
        self.converse(sent=run_sent)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(len(run_sent), 2)
        self.chat.badge = True
        self.assertFalse(chats._robot_wrote_again(self.chat, run_sent, "Предыдущий вопрос?", 2))
        self.assertEqual(self.stats.questionnaires, 1)

    def test_history_context_merges_already_persisted_run_tail_without_duplicates(self):
        self.assertEqual(chats._answer_context(["А", "Б", "В"], ["Б", "В", "Г"], 6), ["А", "Б", "В", "Г"])
        self.assertEqual(chats._answer_context(["Да", "Да"], ["Да"], 6), ["Да", "Да"])

    def test_repeated_question_with_new_message_id_can_receive_same_answer(self):
        self.converse()
        new_robot = chats._Sender("Помощник", "chatik-chat-message-125", provider_robot=True)
        new_transcript = self.TRANSCRIPT + "\nРаботаю с Python.\n" + self.QUESTION
        self.sender.return_value = new_robot
        self.transcript.return_value = new_transcript
        self.converse(new_transcript, sent=[])
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.stats.answered, 2)

    def test_pending_delivery_is_not_retried_after_confirmation_failure(self):
        self.send.return_value = False
        self.converse()
        self.assertEqual(self.stats.answered, 0)
        self.assertIn('"pending": true', self.log.text)
        self.ai.decide.reset_mock()
        self.send.reset_mock()
        self.converse(sent=[])
        self.ai.decide.assert_not_called()
        self.send.assert_not_called()

    def test_proven_pre_send_failure_releases_claim_for_a_later_retry(self):
        self.send.side_effect = chats._SendNotAttempted("поле недоступно")
        self.converse()
        history, handled = chats._past_answers(self.log, self.chat.id)
        self.assertEqual(history, [])
        self.assertEqual(handled, set())
        self.assertIn('"released": true', self.log.text)
        self.send.side_effect = None
        self.send.return_value = True
        self.converse(sent=[])
        self.assertEqual(self.stats.answered, 1)

    def test_error_after_send_may_have_delivered_and_retains_claim(self):
        self.send.side_effect = PlaywrightError("click timed out after send may have started")
        with self.assertRaises(PlaywrightError):
            self.converse()
        self.assertIn(chats._turn_key(self.chat, self.TRANSCRIPT, self.robot),
                      chats._past_answers(self.log, self.chat.id)[1])

    def test_human_takeover_during_model_call_cannot_receive_robot_reply(self):
        self.sender.side_effect = [self.robot, chats._Sender("Анна", "chatik-chat-message-124")]
        self.converse()
        self.ai.decide.assert_called_once()
        self.send.assert_not_called()
        self.assertEqual(self.log.text, "")

    def test_new_robot_question_during_model_call_cannot_receive_old_answer(self):
        self.sender.side_effect = [self.robot, chats._Sender("Помощник", "chatik-chat-message-125", True)]
        self.converse()
        self.ai.decide.assert_called_once()
        self.send.assert_not_called()

    def test_takeover_or_manual_answer_during_fill_stops_before_send_and_releases_claim(self):
        for replacement in (chats._Sender("Анна", "124"), chats._Sender("Вы", "125", outgoing=True)):
            with self.subTest(outgoing=replacement.outgoing):
                self.log = MemoryLog()
                self.sender.return_value = self.robot
                box, scope = Mock(), Mock()
                scope.get_attribute.return_value = "chatik-message-input"
                box.evaluate.return_value = True
                box.input_value.return_value = "Работаю с Python."

                def filled(text, **kwargs):
                    if text:
                        self.sender.return_value = replacement

                box.fill.side_effect = filled
                with patch.object(chats, "_send", side_effect=REAL_SEND), \
                        patch.object(chats, "_composer_input", return_value=(box, scope)), \
                        patch.object(chats.control, "sleep"):
                    self.converse()
                box.press.assert_not_called()
                self.assertEqual(chats._past_answers(self.log, self.chat.id)[1], set())
                self.assertEqual(self.stats.answered, 0)

    def test_robot_is_rechecked_for_each_followup_turn(self):
        self.wait.return_value = "Анна\n" + self.QUESTION
        self.sender.side_effect = [self.robot, self.robot, chats._Sender("Анна", "chatik-chat-message-126")]
        self.converse()
        self.ai.decide.assert_called_once()
        self.send.assert_called_once()

    def test_persisted_last_answer_prevents_reanswer_when_it_is_last_in_transcript(self):
        chats._log_answer(self.log, self.chat, self.QUESTION, "Работаю с Python.", sent=True)
        self.converse(self.TRANSCRIPT + "\nРаботаю с Python.\n12:30\nПрочитано")
        self.ai.decide.assert_not_called()
        self.send.assert_not_called()


class RobotIdentity(unittest.TestCase):
    def test_provider_bot_ui_marker_works_without_historical_text_marker(self):
        page = Mock()
        page.evaluate.return_value = {"author": "Помощник", "message_id": "chatik-chat-message-123", "provider_robot": True}
        sender = chats._current_sender(page)
        self.assertTrue(chats._robot_sender(sender, []))
        self.assertEqual(sender.message_id, "chatik-chat-message-123")

    def test_explicit_latest_author_label_works_with_configured_markers(self):
        self.assertTrue(chats._robot_sender(chats._Sender("Робот-рекрутер hh.ru", "123"), ["Робот-рекрутер"]))
        self.assertFalse(chats._robot_sender(chats._Sender("Анна", "123"), ["Робот-рекрутер", ""]))
        self.assertFalse(chats._robot_sender(None, ["Робот-рекрутер"]))

    def test_message_body_is_not_accepted_as_author_proof(self):
        page = Mock()
        page.evaluate.return_value = {"message_id": "123", "body": "Робот-рекрутер", "provider_robot": True}
        self.assertIsNone(chats._current_sender(page))

    def test_turn_id_is_independent_of_clock_and_old_messages_when_provider_id_present(self):
        chat = chats._Chat("chat1", "", "", "", True)
        sender = chats._Sender("Помощник", "123", True)
        self.assertEqual(chats._turn_key(chat, "вопрос 12:30", sender), chats._turn_key(chat, "вопрос 12:31", sender))
        self.assertNotEqual(chats._turn_key(chat, "вопрос", sender), chats._turn_key(chat, "вопрос", chats._Sender("Помощник", "124", True)))


class ResumeBeforeRobotChats(unittest.TestCase):
    def test_only_robot_requires_selected_resume_before_opening_chats(self):
        cfg = Config(search_url="")
        cfg.ai.enabled = cfg.ai.only_robot = True
        page = Mock()
        with patch.object(chats, "_make_ai", return_value=Mock()), patch.object(chats, "fetch_resume_text") as fetch:
            with self.assertRaisesRegex(FatalError, "выберите резюме"):
                chats.read_rejections(page, cfg)
        fetch.assert_not_called()
        page.goto.assert_not_called()

    def test_monitor_rejects_blank_selected_resume_before_opening_chats(self):
        cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        cfg.ai.enabled = True
        page = Mock()
        with patch.object(chats, "_make_ai", return_value=Mock()), patch.object(chats, "fetch_resume_text", return_value="") as fetch:
            with self.assertRaisesRegex(FatalError, "нет текста выбранного резюме"):
                chats.read_rejections(page, cfg, require_selected_resume=True)
        self.assertTrue(fetch.call_args.kwargs["require_selected"])
        page.goto.assert_not_called()

    def test_monitor_cannot_fall_back_to_reading_chats_when_ai_is_unavailable(self):
        page = Mock()
        with patch.object(chats, "_make_ai", return_value=None):
            with self.assertRaisesRegex(FatalError, "ИИ недоступен"):
                chats.read_rejections(page, Config(search_url=""), require_selected_resume=True)
        page.goto.assert_not_called()


class ScanReadChats(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        self.cfg.ai.enabled = self.cfg.ai.only_robot = True
        self.cfg.stats.funnel = self.cfg.notify.invitations = False
        self.page = Mock(url="https://hh.ru/chat")
        self.page.locator.return_value.count.return_value = 1
        self.patches = [patch.object(chats, "_make_ai", return_value=Mock()),
                        patch.object(chats, "fetch_resume_text", return_value="Python-разработчик"),
                        patch.object(chats, "wait_captcha"), patch.object(chats, "hh_role", return_value="applicant"),
                        patch.object(chats, "_disable_only_unread", return_value=True),
                        patch.object(chats, "_enable_only_unread", return_value=False),
                        patch.object(chats, "_filter_checked", return_value=False),
                        patch.object(chats, "_scroll_list", return_value=False),
                        patch.object(chats, "_handle_with_ai", return_value="переписка"),
                        patch.object(chats, "_open_chat", return_value=True)]
        (self.make_ai, self.resume, self.captcha, self.role, self.disable, self.enable,
         self.checked, self.scroll, self.handle, self.open) = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def test_full_scan_includes_read_questions_but_does_not_reopen_read_rejections(self):
        read_question = chats._Chat("read", "Разработчик", "Компания", "Ваш город?", False, latest_at="12:00")
        read_rejection = chats._Chat("old-rejection", "Разработчик", "Компания", "Отказ", False, latest_at="12:00")
        new_rejection = chats._Chat("new-rejection", "Разработчик", "Компания", "Отказ", True, latest_at="12:00")
        with patch.object(chats, "_collect", return_value=[read_question, read_rejection, new_rejection]):
            stats = chats.read_rejections(self.page, self.cfg, include_read=True)
        self.assertEqual([call.args[1].id for call in self.handle.call_args_list], ["read"])
        self.assertEqual([call.args[1].id for call in self.open.call_args_list], ["new-rejection"])
        self.disable.assert_called_once()
        self.enable.assert_not_called()
        self.assertEqual(stats.rejections_read, 1)

    def test_read_questions_on_later_virtualized_pages_are_scanned(self):
        first = chats._Chat("first", "Разработчик", "Компания", "Ваш город?", False, latest_at="12:00")
        second = chats._Chat("second", "Разработчик", "Компания", "Ваш опыт?", False, latest_at="12:00")
        pages = [[first], [second]]
        page_number = [0]

        def scroll(*args):
            if page_number[0] == 0:
                page_number[0] = 1
                return True
            return False

        self.scroll.side_effect = scroll
        with patch.object(chats, "_collect", side_effect=lambda *args: pages[page_number[0]]):
            chats.read_rejections(self.page, self.cfg, include_read=True)
        self.assertEqual([call.args[1].id for call in self.handle.call_args_list], ["first", "second"])

    def test_unread_only_filter_is_disabled_again_if_hh_reenables_it(self):
        question = chats._Chat("read", "Разработчик", "Компания", "Ваш город?", False, latest_at="12:00")
        self.checked.side_effect = [False, True]
        with patch.object(chats, "_collect", return_value=[question]):
            chats.read_rejections(self.page, self.cfg, include_read=True)
        self.assertEqual(self.disable.call_count, 2)
        self.handle.assert_called_once()

    def test_full_scan_stops_if_unread_filter_cannot_be_disabled(self):
        self.disable.return_value = False
        with self.assertRaisesRegex(FatalError, "выключить"):
            chats.read_rejections(self.page, self.cfg, include_read=True)
        self.handle.assert_not_called()

    def test_legacy_default_still_skips_read_chats(self):
        read_question = chats._Chat("read", "Разработчик", "Компания", "Ваш город?", False, latest_at="12:00")
        with patch.object(chats, "_collect", return_value=[read_question]):
            chats.read_rejections(self.page, self.cfg)
        self.handle.assert_not_called()
        self.enable.assert_called_once()
        self.disable.assert_not_called()

    def test_old_or_undated_threads_are_skipped_before_opening_including_unread_rejection(self):
        old = (date.today() - timedelta(days=31)).isoformat()
        items = [chats._Chat("old-robot", "", "", "Ваш город?", False, latest_at=old),
                 chats._Chat("old-rejection", "", "", "Отказ", True, latest_at=old),
                 chats._Chat("undated", "", "", "Ваш город?", True)]
        with patch.object(chats, "_collect", return_value=items):
            chats.read_rejections(self.page, self.cfg, include_read=True)
        self.handle.assert_not_called()
        self.open.assert_not_called()


class ChatComposer(unittest.TestCase):
    def test_actual_hh_composer_uses_enter_without_any_send_button_click(self):
        box, scope = Mock(), Mock()
        scope.get_attribute.return_value = "chatik-message-input"
        box.evaluate.return_value = True
        box.input_value.return_value = ""
        with patch.object(chats, "_composer_input", return_value=(box, scope)), \
                patch.object(chats.control, "sleep"), patch.object(chats, "_transcript", return_value="Тестовый ответ"):
            self.assertTrue(chats._send(Mock(), "Тестовый ответ", guard=lambda: True))
        box.press.assert_called_once_with("Enter", timeout=8_000)
        scope.locator.assert_not_called()

    def test_quick_answer_guard_runs_after_delay_before_button_click(self):
        page = Mock()
        button = page.locator.return_value.filter.return_value.filter.return_value
        button.count.return_value = 1
        with patch.object(chats, "_transcript", return_value="Вопрос?"), patch.object(chats.control, "sleep"):
            with self.assertRaises(chats._SendNotAttempted):
                chats._press(page, "Да", guard=lambda: False)
        button.last.click.assert_not_called()

    def test_send_fills_without_clicking_field_and_uses_only_scoped_send_button(self):
        box, scope, page = Mock(), Mock(), Mock()
        box.evaluate.return_value = True
        box.input_value.return_value = ""
        send = scope.locator.return_value.filter.return_value
        send.count.return_value = 1
        with patch.object(chats, "_composer_input", return_value=(box, scope)), \
                patch.object(chats.control, "sleep"), patch.object(chats, "_transcript", return_value="Тестовый ответ"):
            self.assertTrue(chats._send(page, "Тестовый ответ"))
        box.click.assert_not_called()
        box.fill.assert_called_once_with("Тестовый ответ", timeout=5_000)
        send.first.click.assert_called_once_with(timeout=8_000)
        page.locator.assert_not_called()

    def test_fill_failure_is_proven_not_attempted(self):
        box, scope = Mock(), Mock()
        box.fill.side_effect = PlaywrightError("field not editable")
        with patch.object(chats, "_composer_input", return_value=(box, scope)):
            with self.assertRaises(chats._SendNotAttempted):
                chats._send(Mock(), "Тестовый ответ")
        scope.locator.assert_not_called()

    def test_send_click_error_remains_ambiguous(self):
        box, scope = Mock(), Mock()
        send = scope.locator.return_value.filter.return_value
        send.count.return_value = 1
        send.first.click.side_effect = PlaywrightError("send click timed out")
        with patch.object(chats, "_composer_input", return_value=(box, scope)), patch.object(chats.control, "sleep"):
            with self.assertRaises(PlaywrightError):
                chats._send(Mock(), "Тестовый ответ")


if __name__ == "__main__":
    unittest.main()
