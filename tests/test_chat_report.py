"""Pending send claims stay in the audit log without appearing as duplicate Excel replies."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from hh_auto.reports.results import ResultsBook


class ChatReportTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.book = ResultsBook(self.root)
        self.book.chat_answers_path.parent.mkdir(parents=True)

    def answer(self, **values):
        record = {"at": "2026-11-14 12:00:00", "chat_id": "101", "title": "Разработчик",
                  "company": "Компания", "question": "Какой у вас опыт?", "answer": "Пишу на Python.",
                  "sent": False, "turn_key": "one-turn"}
        return record | values

    def write_audit(self, rows):
        original = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
        self.book.chat_answers_path.write_bytes(original)
        return original

    def test_confirmed_reply_is_shown_once_and_unsent_preview_is_retained(self):
        original = self.write_audit([
            self.answer(pending=True),
            {"turn_key": "one-turn", "sent": False, "released": True},
            self.answer(pending=True),
            self.answer(sent=True),
            self.answer(at="2026-11-14 12:01:00", turn_key="preview", answer="Пробный ответ.", sent=False),
        ])
        self.assertTrue(self.book.save_xlsx())
        workbook = load_workbook(self.book.xlsx_path, read_only=True)
        try:
            rows = list(workbook["Ответы в чатах"].iter_rows(min_row=4, values_only=True))
            self.assertCountEqual([(row[4], row[5]) for row in rows],
                                  [("Пишу на Python.", "да"), ("Пробный ответ.", "нет (пробный)")])
        finally:
            workbook.close()
        self.assertEqual(self.book.chat_answers_path.read_bytes(), original)

    def test_unconfirmed_claims_do_not_create_a_replies_sheet(self):
        original = self.write_audit([self.answer(pending=True)])
        self.assertTrue(self.book.save_xlsx())
        workbook = load_workbook(self.book.xlsx_path, read_only=True)
        try:
            self.assertNotIn("Ответы в чатах", workbook.sheetnames)
        finally:
            workbook.close()
        self.assertEqual(self.book.chat_answers_path.read_bytes(), original)

    def test_release_audit_rows_do_not_create_a_replies_sheet(self):
        original = self.write_audit([{"turn_key": "released-turn", "sent": False, "released": True}])
        self.assertTrue(self.book.save_xlsx())
        workbook = load_workbook(self.book.xlsx_path, read_only=True)
        try:
            self.assertNotIn("Ответы в чатах", workbook.sheetnames)
        finally:
            workbook.close()
        self.assertEqual(self.book.chat_answers_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
