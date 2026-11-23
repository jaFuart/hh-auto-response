"""Observed Magritte resume IDs must agree in the live option and checked radio."""

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError

from hh_auto.core.config import Config
from hh_auto.hh.responder import Responder


class Collection:
    def __init__(self, items):
        self.items = items
    def filter(self, **kwargs):
        return self
    def count(self):
        return len(self.items())
    def nth(self, index):
        return self.items()[index]
    @property
    def first(self):
        return self.nth(0)


class Radio:
    def __init__(self, page, row):
        self.page, self.row = page, row
    def get_attribute(self, name):
        return self.row.get("value", self.row["id"]) if name == "value" else None
    def is_checked(self):
        return self.row.get("checked", self.page.selected == self.row["id"])


class Option:
    def __init__(self, page, row):
        self.page, self.row = page, row
    def get_attribute(self, name):
        return {"data-magritte-select-option": self.row["id"],
                "data-qa": self.row.get("qa", "magritte-select-option-" + self.row["id"]),
                "aria-selected": self.row.get("aria", str(self.page.selected == self.row["id"]).lower()),
                "aria-disabled": self.row.get("disabled", "false")}.get(name)
    def locator(self, selector):
        assert selector == 'input[type="radio"]'
        return Collection(lambda: [Radio(self.page, self.row)])
    def click(self):
        self.page.selections.append(self.row["id"])
        self.page.selected = self.row["id"]
        self.page.closing = self.page.animated
        self.page.open = self.page.animated
    def wait_for(self, **kwargs):
        self.page.waits.append(kwargs["state"])
        if kwargs["state"] == "hidden":
            if self.page.closing:
                self.page.open, self.page.closing = False, False
            if self.page.open:
                raise PlaywrightError("menu is still open")
            return
        if not self.page.open:
            raise PlaywrightError("menu is closed")


class Title:
    def __init__(self, page):
        self.page = page
    def click(self):
        self.page.opens += 1
        self.page.open = True
    def inner_text(self):
        return "Python developer" if self.page.selected == "selected" else "Data scientist"


class Page:
    def __init__(self, selected="other"):
        self.selected = selected
        self.rows = [{"id": "selected"}, {"id": "other"}]
        self.open, self.opens, self.escapes = False, 0, 0
        self.animated, self.closing, self.waits = False, False, []
        self.selections, self.native = [], []
        self.keyboard = Mock()
        self.keyboard.press.side_effect = self.escape
    def escape(self, key):
        assert key == "Escape"
        assert self.open, "Escape after menu closure would dismiss the outer response modal"
        self.escapes += 1
        self.closing = self.animated
        self.open = self.animated
    def evaluate(self, script):
        return self.native
    def locator(self, selector):
        if selector == 'label[role="option"][data-magritte-select-option]':
            return Collection(lambda: [Option(self, row) for row in self.rows] if self.open else [])
        if selector == '[data-qa="resume-title"]':
            return Collection(lambda: [Title(self)])
        if selector.startswith('input[type="radio"]'):
            return Collection(lambda: [])
        raise AssertionError(selector)


class ModernResumeSelection(unittest.TestCase):
    def sender(self, page):
        return Responder(page, Config(search_url="", resume_url="https://hh.ru/resume/selected"),
                         Path("unused"), "Python developer")

    def test_metadata_only_live_selection_is_read_and_menu_is_closed_without_selecting(self):
        page = Page("selected")
        self.assertTrue(self.sender(page)._direct_resume_matches())
        self.assertEqual(page.selections, [])
        self.assertEqual(page.escapes, 1)
        self.assertFalse(page.open)

    def test_exact_hash_option_is_selected_then_reopened_and_checked_again(self):
        page = Page()
        self.assertTrue(self.sender(page)._select_direct_resume())
        self.assertEqual(page.selections, ["selected"])
        self.assertGreaterEqual(page.opens, 2)
        self.assertFalse(page.open)

    def test_animated_menu_is_fully_hidden_before_consecutive_id_checks(self):
        page = Page()
        page.animated = True
        sender = self.sender(page)
        self.assertTrue(sender._select_direct_resume())
        self.assertTrue(sender._direct_resume_matches())
        self.assertTrue(sender._direct_resume_matches())
        self.assertEqual(page.selections, ["selected"])
        self.assertFalse(page.open)
        self.assertFalse(page.closing)
        self.assertIn("visible", page.waits)
        self.assertGreaterEqual(page.waits.count("hidden"), 3)

    def test_inconsistent_hash_flags_duplicate_or_disabled_option_never_selects(self):
        variants = ({"value": "wrong"}, {"aria": "true", "checked": False}, {"disabled": "true"},
                    {"qa": "another-option"})
        for change in variants:
            with self.subTest(change=change):
                page = Page()
                page.rows[0].update(change)
                self.assertFalse(self.sender(page)._select_direct_resume())
                self.assertEqual(page.selections, [])
                self.assertFalse(page.open)
        page = Page()
        page.rows.append({"id": "selected"})
        self.assertFalse(self.sender(page)._select_direct_resume())
        self.assertEqual(page.selections, [])

    def test_explicit_wrong_or_ambiguous_native_id_is_not_bypassed_by_modern_title(self):
        for native in (["other"], ["selected", "other"]):
            with self.subTest(native=native):
                page = Page("selected")
                page.native = native
                self.assertFalse(self.sender(page)._direct_resume_matches())
                self.assertEqual(page.opens, 0)

    def test_normal_modal_title_mismatch_can_select_only_the_expected_id(self):
        page = Page()
        self.assertTrue(self.sender(page)._resume_matches(page))
        self.assertEqual(page.selections, ["selected"])
        self.assertEqual(page.selected, "selected")

    def test_submit_rechecks_live_selection_and_never_clicks_after_it_changes(self):
        page = Page("other")
        sender = self.sender(page)
        sender._direct_vacancy_id = "101"
        button = Mock()
        with patch.object(sender, "_wait_enabled", return_value=True), patch.object(sender, "_pause"):
            error = sender._submit(page, button, "submit")
        self.assertIn("ID", error)
        button.click.assert_not_called()
        self.assertEqual(page.selections, [])


if __name__ == "__main__":
    unittest.main()
