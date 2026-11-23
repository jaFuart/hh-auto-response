"""Durable bounded robot-chat inspection, without hh or model requests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError

from hh_auto.ai.client import Decision
from hh_auto.core.config import Config
from hh_auto.hh import chats
from hh_auto.web.browser import FatalError


def thread(number: int, *, unread: bool = False, last: str = "Да", latest_at: str = "12:00"):
    return chats._Chat(str(number), "Разработчик", "Компания", last, unread,
                       latest_at=latest_at, url=f"https://hh.ru/chat/{number}?hhtmFrom=app")


class ScanQueueTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / chats.ROBOT_SCAN_FILE
        self.scan = chats._RobotScanState(self.path)

    def inspect(self, source, budget=10, resume="selected"):
        selected = self.scan.select(source, resume, budget)
        for chat in selected:
            self.scan.checked(resume, chat.id)
        return [chat.id for chat in selected]

    def test_unchanged_read_threads_eventually_revisited_after_restart(self):
        source = [thread(number) for number in range(100)]
        seen = set()
        for _ in range(17):
            self.scan = chats._RobotScanState(self.path)
            selected = self.inspect(source)
            self.assertEqual(len(selected), 10)
            seen.update(selected)
        self.assertEqual(seen, {chat.id for chat in source})
        # A new identical read message has no new sidebar identity. It must still be inspected later.
        revisited = set()
        for _ in range(17):
            revisited.update(self.inspect(source))
        self.assertEqual(revisited, seen)

    def test_continuous_unread_arrivals_cannot_starve_existing_fifo(self):
        originals = [thread(number) for number in range(100)]
        self.scan.select(originals, "selected", 10)
        seen = set()
        for cycle in range(17):
            arriving = [thread(1000 + cycle * 10 + index, unread=True) for index in range(10)]
            seen.update(self.inspect(arriving + originals))
        self.assertTrue({chat.id for chat in originals}.issubset(seen))

    def test_priority_uses_at_most_four_of_ten_places(self):
        originals = [thread(number) for number in range(20)]
        self.scan.select(originals, "selected", 10)
        arriving = [thread(number, unread=True) for number in range(100, 120)]
        selected = self.scan.select(arriving + originals, "selected", 10)
        self.assertLessEqual(sum(int(chat.id) >= 100 for chat in selected), 4)
        self.assertTrue(set(str(number) for number in range(6)).issubset({chat.id for chat in selected}))

    def test_changed_preview_and_user_read_questions_get_priority_without_changing_fifo_order(self):
        source = [thread(number) for number in range(20)]
        self.scan.select(source, "selected", 10)
        changed = [thread(19, last="Новый вопрос?"), *source[:-1]]
        selected = self.scan.select(changed, "selected", 10)
        self.assertIn("19", [chat.id for chat in selected])
        self.assertTrue(set(str(number) for number in range(6)).issubset({chat.id for chat in selected}))

    def test_budget_one_progresses_without_priority_and_only_checked_entries_rotate(self):
        source = [thread(number, unread=True) for number in range(5)]
        first = self.scan.select(source, "selected", 1)[0]
        self.assertEqual(first.id, "0")
        self.assertEqual(self.scan.select(source, "selected", 1)[0].id, "0")
        self.scan.checked("selected", first.id)
        self.assertEqual(self.scan.select(source, "selected", 1)[0].id, "1")

    def test_resume_queues_are_independent_and_absent_chats_are_pruned(self):
        source = [thread(number) for number in range(5)]
        self.assertEqual(self.inspect(source, 1), ["0"])
        self.assertEqual(self.inspect(source, 1, resume="other"), ["0"])
        self.assertEqual(self.inspect(source, 1), ["1"])
        self.scan.select([thread(4)], "selected", 1)
        state = json.loads(self.path.read_text(encoding="utf-8"))
        entry = state["resumes"][self.scan._resume_key("selected")]
        self.assertEqual(entry["queue"], ["4"])
        self.assertEqual(set(entry["signatures"]), {"4"})
        self.assertNotIn("Новый вопрос", self.path.read_text(encoding="utf-8"))

    def test_corrupt_or_uncommittable_state_fails_before_selection_or_reservation(self):
        self.path.write_text('{"version":1,"last_reply_at":"wrong","resumes":{}}', encoding="utf-8")
        with self.assertRaises(FatalError):
            self.scan.select([thread(1)], "selected", 10)
        with self.assertRaises(FatalError):
            self.scan.reply_slot(30, reserve=True)
        self.path.unlink()
        with patch.object(chats.os, "replace", side_effect=OSError("read-only disk")):
            with self.assertRaises(FatalError):
                self.scan.reply_slot(30, reserve=True)
        self.assertFalse(self.path.exists())

    def test_persisted_reply_interval_survives_restart_and_queue_update(self):
        with patch.object(chats.time, "time", return_value=1000):
            self.assertEqual(self.scan.reply_slot(30, reserve=True), 0)
        self.scan.select([thread(1)], "selected", 10)
        fresh = chats._RobotScanState(self.path)
        with patch.object(chats.time, "time", return_value=1005):
            self.assertEqual(fresh.reply_slot(30, reserve=True), 25)
        with patch.object(chats.time, "time", return_value=1030):
            self.assertEqual(fresh.reply_slot(30, reserve=True), 0)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["last_reply_at"], 1030)
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


class BoundedScanTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        self.cfg.ai.enabled = self.cfg.ai.only_robot = True
        self.cfg.stats.funnel = self.cfg.notify.invitations = False
        self.page = Mock(url="https://hh.ru/chat")
        self.patches = [patch.object(chats, "_make_ai", return_value=Mock()),
                        patch.object(chats, "fetch_resume_text", return_value="Python-разработчик"),
                        patch.object(chats, "wait_captcha"), patch.object(chats, "hh_role", return_value="applicant"),
                        patch.object(chats, "_disable_only_unread", return_value=True),
                        patch.object(chats, "_filter_checked", return_value=False),
                        patch.object(chats, "_handle_with_ai", return_value=""),
                        patch.object(chats, "_open_chat", return_value=True),
                        patch.object(chats.control, "sleep"), patch.object(chats.browser, "wait_read_interval")]
        (_, _, _, _, self.disable, _, self.handle, self.open, _, self.read_wait) = [p.start() for p in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)

    def run_scan(self, **kwargs):
        return chats.read_rejections(self.page, self.cfg, root=self.root, include_read=True,
                                     require_selected_resume=True, **kwargs)

    def test_full_sidebar_discovery_but_at_most_ten_openings(self):
        pages = [[thread(number) for number in range(offset, offset + 20)] for offset in range(0, 100, 20)]
        with patch.object(chats, "_collect", side_effect=pages) as collect, \
                patch.object(chats, "_scroll_list", side_effect=[True] * 4 + [False]):
            stats = self.run_scan(max_chats_per_check=10)
        self.assertEqual(collect.call_count, 5)
        self.assertEqual(self.handle.call_count, 10)
        self.assertEqual((stats.checked, stats.deferred), (10, 90))
        state = json.loads((self.root / "data" / chats.ROBOT_SCAN_FILE).read_text(encoding="utf-8"))
        self.assertEqual(len(state["resumes"][chats._RobotScanState._resume_key("selected")]["queue"]), 100)

    def test_recent_read_thread_below_old_or_unknown_threads_is_still_checked(self):
        source = [thread(1, latest_at="2000-01-01"), thread(2, latest_at=""), thread(3),
                  thread(4, last="Отказ", unread=False), thread(5, last="Отказ", unread=True)]
        with patch.object(chats, "_collect", return_value=source), patch.object(chats, "_scroll_list", return_value=False):
            stats = self.run_scan(max_chats_per_check=10)
        self.assertEqual([call.args[1].id for call in self.handle.call_args_list], ["3"])
        self.assertEqual([call.args[1].id for call in self.open.call_args_list], ["5"])
        self.assertEqual(stats.rejections_read, 1)
        self.assertEqual(stats.checked, 2)

    def test_zero_or_none_preserves_legacy_full_scan_without_creating_queue(self):
        for budget in (None, 0):
            with self.subTest(budget=budget), patch.object(chats, "_collect", return_value=[thread(1)]), \
                    patch.object(chats, "_scroll_list", return_value=False):
                self.run_scan(max_chats_per_check=budget)
        self.assertEqual(self.handle.call_count, 2)
        self.assertFalse((self.root / "data" / chats.ROBOT_SCAN_FILE).exists())

    def test_stopping_mid_batch_does_not_rotate_unstarted_chats(self):
        self.handle.side_effect = chats.control.StopRequested()
        with patch.object(chats, "_collect", return_value=[thread(i) for i in range(10)]), \
                patch.object(chats, "_scroll_list", return_value=False):
            with self.assertRaises(chats.control.StopRequested):
                self.run_scan(max_chats_per_check=10)
        scan = chats._RobotScanState(self.root / "data" / chats.ROBOT_SCAN_FILE)
        queue = scan._load()["resumes"][scan._resume_key("selected")]["queue"]
        self.assertEqual(queue[-1], "6")
        self.assertEqual(queue[0], "0")


class ReplyDelayTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.answers = Path(self.folder.name) / "chat_answers.jsonl"
        self.cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
        self.cfg.ai.only_robot = self.cfg.ai.enabled = True
        self.cfg.watch.robot_reply_interval_seconds = 30
        self.chat = thread(1)
        self.robot = chats._Sender("Помощник", "message-1", provider_robot=True)
        self.transcript = "Ваш город?"
        self.now = 1000.0
        self.ai = Mock(notes=[])
        self.ai.decide.return_value = Decision("question", self.transcript, "Москва.")
        self.page = Mock()
        self.patches = [patch.object(chats.time, "time", side_effect=lambda: self.now),
                        patch.object(chats, "_current_sender", return_value=self.robot),
                        patch.object(chats, "_transcript", return_value=self.transcript),
                        patch.object(chats, "_buttons", return_value=[]),
                        patch.object(chats, "_wait_next_message", return_value=None),
                        patch.object(chats.control, "sleep", side_effect=self.advance)]
        (_, self.sender, _, _, _, self.sleep) = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)

    def advance(self, seconds):
        self.now += seconds

    def converse(self):
        return chats._converse(self.page, self.chat, self.cfg, self.ai, "Python", chats.ChatStats(), False,
                               self.answers, [], self.transcript, "чат")

    def test_wait_then_recheck_sender_before_pending_claim(self):
        scan = chats._RobotScanState(self.answers.parent / chats.ROBOT_SCAN_FILE)
        scan.reply_slot(30, reserve=True)
        self.now += 5
        with patch.object(chats, "_same_robot_turn", return_value=False) as guard, \
                patch.object(chats, "_send") as send:
            self.converse()
        self.assertEqual(self.now, 1030)
        self.sleep.assert_called_once_with(25)
        guard.assert_called_once()
        send.assert_not_called()
        self.assertFalse(self.answers.exists())
        self.assertEqual(scan._load()["last_reply_at"], 1000)

    def test_timestamp_is_committed_before_pending_and_outbound_even_if_delivery_is_ambiguous(self):
        observed = []
        actual_log = chats._log_answer

        def log_answer(*args, **kwargs):
            if kwargs.get("pending"):
                state = json.loads((self.answers.parent / chats.ROBOT_SCAN_FILE).read_text(encoding="utf-8"))
                observed.append(("pending", state["last_reply_at"]))
            return actual_log(*args, **kwargs)

        def send(*args, **kwargs):
            observed.append(("send", self.now))
            raise PlaywrightError("ambiguous timeout")

        with patch.object(chats, "_log_answer", side_effect=log_answer), patch.object(chats, "_send", side_effect=send):
            with self.assertRaises(PlaywrightError):
                self.converse()
        self.assertEqual(observed, [("pending", 1000), ("send", 1000)])
        fresh = chats._RobotScanState(self.answers.parent / chats.ROBOT_SCAN_FILE)
        self.now += 1
        self.assertEqual(fresh.reply_slot(30), 29)
        self.assertTrue(chats._past_answers(self.answers, self.chat.id)[1])

    def test_changed_message_id_after_wait_is_not_sent_even_for_identical_text(self):
        scan = chats._RobotScanState(self.answers.parent / chats.ROBOT_SCAN_FILE)
        scan.reply_slot(30, reserve=True)
        self.sender.side_effect = [self.robot, self.robot,
                                  chats._Sender("Помощник", "message-2", provider_robot=True)]
        with patch.object(chats, "_send") as send:
            self.converse()
        send.assert_not_called()
        self.assertFalse(self.answers.exists())


class SidebarLinkTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(search_url="")
        self.page = Mock(url="https://hh.ru/chat")
        self.page.locator.return_value.count.return_value = 0

    def test_offscreen_chat_uses_exact_observed_same_origin_link(self):
        self.page.goto.side_effect = lambda url, **kwargs: setattr(self.page, "url", url)
        with patch.object(chats.browser, "wait_read_interval") as wait, \
                patch.object(chats, "wait_captcha"), patch.object(chats, "hh_role", return_value="applicant"):
            chats._click_chat(self.page, thread(42), self.cfg)
        wait.assert_called_once_with(self.cfg)
        self.page.goto.assert_called_once_with("https://hh.ru/chat/42?hhtmFrom=app", wait_until="domcontentloaded")
        self.page.locator.return_value.click.assert_not_called()

    def test_external_or_wrong_id_link_is_never_opened(self):
        for url in ("https://evil.invalid/chat/42", "https://hh.ru/chat/420", "https://hh.ru/vacancy/42"):
            item = thread(42)
            item.url = url
            with self.subTest(url=url), self.assertRaises(PlaywrightError):
                chats._click_chat(self.page, item, self.cfg)
        self.page.goto.assert_not_called()

    def test_redirect_to_another_thread_is_rejected_before_reading_messages(self):
        self.page.goto.side_effect = lambda url, **kwargs: setattr(self.page, "url", "https://hh.ru/chat/99")
        with patch.object(chats.browser, "wait_read_interval"), patch.object(chats, "wait_captcha"), \
                patch.object(chats, "hh_role", return_value="applicant"):
            with self.assertRaisesRegex(PlaywrightError, "другой чат"):
                chats._click_chat(self.page, thread(42), self.cfg)


if __name__ == "__main__":
    unittest.main()
