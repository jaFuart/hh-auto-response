"""Граница месяца и реальные краткие подписи дат hh — без браузера и сети."""

from datetime import date, datetime, timedelta, timezone
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto.hh.chat_dates import chat_date_status, parse_chat_date  # noqa: E402

MOSCOW = timezone(timedelta(hours=3))
NOW = datetime(2026, 11, 14, 16, 40, tzinfo=MOSCOW)


class ChatDatesTest(unittest.TestCase):
    def test_observed_hh_labels_are_recent(self):
        for label, expected in (("14:20", date(2026, 11, 14)), ("вчера", date(2026, 11, 13)),
                                ("чт", date(2026, 11, 12)), ("Сегодня, 12:40", date(2026, 11, 14))):
            with self.subTest(label=label):
                self.assertEqual(parse_chat_date(label, now=NOW), expected)
                self.assertEqual(chat_date_status(label, now=NOW), "recent")

    def test_30_day_boundary_includes_whole_day(self):
        self.assertEqual(chat_date_status("15 окт.", now=NOW), "recent")
        self.assertEqual(chat_date_status("15.10.2026 00:01", now=NOW), "recent")
        self.assertEqual(chat_date_status("14 октября 2026, 23:59", now=NOW), "old")

    def test_yearless_dates_mean_nearest_past_occurrence(self):
        january = datetime(2027, 1, 10, tzinfo=MOSCOW)
        self.assertEqual(parse_chat_date("25 дек", now=january), date(2026, 12, 25))
        self.assertEqual(chat_date_status("25 дек", now=january), "recent")
        self.assertEqual(parse_chat_date("15 ноя", now=NOW), date(2025, 11, 15))
        self.assertEqual(chat_date_status("15 ноя", now=NOW), "old")
        self.assertEqual(parse_chat_date("29 фев", now=NOW), date(2024, 2, 29))

    def test_machine_dates_convert_to_local_calendar_day(self):
        self.assertEqual(parse_chat_date("2026-10-14T22:10:00Z", now=NOW), date(2026, 10, 15))
        self.assertEqual(chat_date_status("2026-10-14T22:10:00Z", now=NOW), "recent")
        stamp = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
        for value in (stamp, int(stamp * 1000), str(int(stamp)), "01.11.26", "1 ноября 2026"):
            with self.subTest(value=value):
                self.assertEqual(parse_chat_date(value, now=NOW), date(2026, 11, 1))

    def test_unknown_or_future_values_do_not_bypass_cutoff(self):
        for value in ("", None, {}, True, "31 ноября", "в прошлом месяце", "2027-01-01", float("nan"), "25:80"):
            with self.subTest(value=value):
                self.assertEqual(chat_date_status(value, now=NOW), "unknown")


if __name__ == "__main__":
    unittest.main()
