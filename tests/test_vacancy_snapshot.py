"""UC snapshot reuse and identity gates; native navigation fakes, no browser/network."""

from __future__ import annotations

from html import escape
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hh_auto.core.rate_limits import WorkDeferred
from hh_auto.hh import gph, search, vacancy_snapshot as snapshots
from hh_auto.hh.search import Vacancy
from hh_auto.hh.submission_pacing import PacingStateError, PacingStatus
from hh_auto.web import browser


TITLE = "Python developer"
VACANCY = Vacancy("123", TITLE, "Fixture", "Откликнуться")


def html(vacancy_id="123", *, title=TITLE, description="Разработка REST API. Оформление по ГПХ.",
         state=True, labor=False):
    result = (f'<link rel="canonical" href="https://hh.ru/vacancy/{vacancy_id}">'
              f'<h1 data-qa="vacancy-title">{title}</h1>'
              f'<div data-qa="vacancy-description">{description}</div>')
    if state:
        metadata = {"vacancyView": {"vacancyFull": {"vacancy": {
            "vacancyId": int(vacancy_id), "description": description,
            "civilLawContracts": None, "acceptLaborContract": labor}}}}
        result += '<template id="HH-Lux-InitialState">' + escape(json.dumps(metadata)) + '</template>'
    return result


class Tab:
    def __init__(self, context):
        self.context = context
        self.url = "about:blank"
        self.main_frame = object()
        self.listeners = {}
        self.closed = False
        self.scenario = context.scenario

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.listeners[event].remove(handler)

    def goto(self, url, **kwargs):
        self.context.navigations.append((url, kwargs))
        if self.scenario.get("error"):
            raise self.scenario["error"]
        self.url = self.scenario.get("url", url)
        vacancy_id = url.rsplit("/", 1)[-1]
        self.html = self.scenario.get("html", html(vacancy_id))
        response = SimpleNamespace(status=self.scenario.get("status", 200), url=self.url,
            frame=self.main_frame, request=SimpleNamespace(is_navigation_request=lambda: True,
                redirected_from=object() if self.scenario.get("redirected") else None),
            text=lambda: self.scenario.get("raw", self.html))
        for listener in self.listeners.get("response", []):
            listener(response)
        return response

    def locator(self, _selector):
        return SimpleNamespace(first=SimpleNamespace(wait_for=lambda **_kwargs: None))

    def content(self):
        return self.html

    def close(self):
        self.closed = True


class Context:
    def __init__(self, **scenario):
        self.scenario = scenario
        self.tabs = []
        self.navigations = []
        self.listeners = {}
        self.request = SimpleNamespace(get=Mock(side_effect=AssertionError("UC must not use APIRequestContext")))

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def new_page(self):
        tab = Tab(self)
        self.tabs.append(tab)
        return tab


def main_page(context):
    return SimpleNamespace(context=context, url="https://hh.ru/search/vacancy?resume=selected",
                           selected_resume="selected", close=Mock(), goto=Mock(side_effect=AssertionError("main navigation")))


class VacancySnapshotTest(unittest.TestCase):
    def setUp(self):
        snapshots._CACHES.clear()
        self.addCleanup(snapshots._CACHES.clear)
        uc = patch.object(browser, "is_uc_context", return_value=True)
        interval = patch.object(browser, "wait_read_interval")
        captcha = patch.object(browser, "wait_captcha", return_value=False)
        self.is_uc = uc.start()
        self.read_gate = interval.start()
        self.captcha = captcha.start()
        self.addCleanup(uc.stop)
        self.addCleanup(interval.stop)
        self.addCleanup(captcha.stop)

    def test_gph_description_and_letter_reuse_one_native_get_without_touching_main_page(self):
        context = Context()
        page = main_page(context)
        self.assertTrue(gph.check_gph(page, VACANCY).allowed)
        short = search.fetch_vacancy_text(page, VACANCY, limit=12, require_description=True)
        full = search.fetch_vacancy_text(page, VACANCY, limit=None, require_description=True)
        self.assertEqual(short, full[:12])
        self.assertIn("REST API", full)
        self.assertEqual(len(context.navigations), 1)
        self.assertEqual(context.navigations[0][0], VACANCY.url)
        self.assertEqual(context.navigations[0][1]["wait_until"], "domcontentloaded")
        self.read_gate.assert_called_once_with()
        context.request.get.assert_not_called()
        self.assertTrue(context.tabs[0].closed)
        self.assertEqual((page.url, page.selected_resume),
                         ("https://hh.ru/search/vacancy?resume=selected", "selected"))
        page.close.assert_not_called()
        page.goto.assert_not_called()

    def test_description_first_is_reused_by_gph_too(self):
        context = Context()
        page = main_page(context)
        self.assertIn("REST API", search.fetch_vacancy_text(page, VACANCY))
        self.assertTrue(gph.check_gph(page, VACANCY).allowed)
        self.assertEqual(len(context.navigations), 1)

    def test_hydrated_dom_without_state_uses_the_same_native_response_for_gph(self):
        context = Context(html=html(state=False), raw=html())
        self.assertTrue(gph.check_gph(main_page(context), VACANCY).allowed)
        self.assertEqual(len(context.navigations), 1)
        context.request.get.assert_not_called()

    def test_raw_fallback_cannot_replace_explicit_rendered_labor_restriction(self):
        context = Context(html=html(description="Только по ТК РФ.", labor=True), raw=html())
        self.assertFalse(gph.check_gph(main_page(context), VACANCY).allowed)

    def test_wrong_page_or_incomplete_content_is_never_cached(self):
        scenarios = [
            {"url": "https://hh.ru/vacancy/999"},
            {"url": "https://hh.ru/account/login"},
            {"url": "https://hh.ru.other.invalid/vacancy/123"},
            {"status": 403}, {"redirected": True},
            {"html": html(title="")}, {"html": html(title="Another role")},
            {"html": html(description="")},
            {"html": '<h1 data-qa="vacancy-title">Python developer</h1>'},
            {"html": html("999")},
            {"raw": html("999")},
            {"html": html().replace('&quot;vacancyId&quot;: 123', '&quot;vacancyId&quot;: 999')},
        ]
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                context = Context(**scenario)
                page = main_page(context)
                self.assertEqual(search.fetch_vacancy_text(page, VACANCY), "")
                verdict = gph.check_gph(page, VACANCY)
                self.assertFalse(verdict.allowed)
                self.assertFalse(verdict.unspecified)
                self.assertEqual(len(context.navigations), 2)
                self.assertTrue(all(tab.closed for tab in context.tabs))
                context.request.get.assert_not_called()

    def test_captcha_markup_is_fatal_and_is_never_cached(self):
        context = Context(html=html() + '<input name="captchaText">')
        for _ in range(2):
            with self.assertRaises(browser.FatalError):
                search.fetch_vacancy_text(main_page(context), VACANCY)
        self.assertEqual(len(context.navigations), 2)
        self.assertTrue(all(tab.closed for tab in context.tabs))

    def test_pacing_and_captcha_exceptions_propagate_instead_of_empty_context(self):
        errors = [WorkDeferred(PacingStatus(20_000, 100, "captcha_cooldown")),
                  PacingStateError("synthetic state error"), browser.FatalError("synthetic CAPTCHA")]
        for error in errors:
            for fetch in (search.fetch_vacancy_text, gph.check_gph):
                with self.subTest(error=type(error), fetch=fetch.__name__):
                    context = Context()
                    with patch.object(browser, "wait_captcha", side_effect=error):
                        with self.assertRaises(type(error)):
                            fetch(main_page(context), VACANCY)
                    self.assertTrue(context.tabs[0].closed)
                    self.assertFalse(snapshots._CACHES[context].items)

    def test_global_read_gate_blocks_before_any_navigation(self):
        context = Context()
        error = WorkDeferred(PacingStatus(20_000, 100, "captcha_cooldown"))
        with patch.object(browser, "wait_read_interval", side_effect=error):
            with self.assertRaises(WorkDeferred):
                search.fetch_vacancy_text(main_page(context), VACANCY)
        self.assertEqual(context.navigations, [])
        self.assertTrue(context.tabs[0].closed)

    def test_navigation_failures_are_not_cached_and_only_owned_tabs_are_closed(self):
        context = Context(error=RuntimeError("synthetic network failure"))
        page = main_page(context)
        self.assertIsNone(snapshots.get_snapshot(page, VACANCY))
        self.assertIsNone(snapshots.get_snapshot(page, VACANCY))
        self.assertEqual(len(context.navigations), 2)
        page.close.assert_not_called()
        self.assertTrue(all(tab.closed for tab in context.tabs))

    def test_cache_is_context_scoped_bounded_and_cleared_on_context_close(self):
        context, other = Context(), Context()
        with patch.object(snapshots, "MAX_SNAPSHOTS", 2):
            for identifier in ("1", "2", "3", "2", "1"):
                vacancy = Vacancy(identifier, TITLE, "Fixture", "Откликнуться")
                self.assertIsNotNone(snapshots.get_snapshot(main_page(context), vacancy))
        self.assertEqual(len(context.navigations), 4)
        snapshots.get_snapshot(main_page(other), Vacancy("1", TITLE, "Fixture", "Откликнуться"))
        self.assertEqual(len(other.navigations), 1)
        for handler in context.listeners["close"]:
            handler()
        self.assertEqual(snapshots._CACHES[context].bytes, 0)
        self.assertFalse(snapshots._CACHES[context].items)

    def test_oversized_valid_snapshot_is_returned_but_does_not_expand_cache(self):
        context = Context()
        with patch.object(snapshots, "MAX_CACHE_BYTES", 1):
            for _ in range(2):
                self.assertIsNotNone(snapshots.get_snapshot(main_page(context), VACANCY))
        self.assertEqual(len(context.navigations), 2)
        self.assertEqual(snapshots._CACHES[context].bytes, 0)

    def test_default_playwright_still_uses_the_existing_request_path(self):
        context = Context()
        response = Mock(ok=True, url=VACANCY.url)
        response.text.return_value = html()
        context.request.get.side_effect = None
        context.request.get.return_value = response
        with patch.object(browser, "is_uc_context", return_value=False):
            self.assertIn("REST API", search.fetch_vacancy_text(main_page(context), VACANCY))
        self.assertEqual(context.tabs, [])
        self.read_gate.assert_called_once_with()
        response.dispose.assert_called_once()

    def test_default_playwright_read_gate_defers_before_api_requests_too(self):
        for fetch in (search.fetch_vacancy_text, gph.check_gph):
            with self.subTest(fetch=fetch.__name__):
                context = Context()
                with patch.object(browser, "is_uc_context", return_value=False), \
                        patch.object(browser, "wait_read_interval", side_effect=WorkDeferred(
                            PacingStatus(20_000, 100, "captcha_cooldown"))):
                    with self.assertRaises(WorkDeferred):
                        fetch(main_page(context), VACANCY)
                context.request.get.assert_not_called()
                self.assertEqual(context.tabs, [])

    def test_default_playwright_gph_api_and_html_fallback_each_pass_the_read_gate(self):
        context = Context()
        api = Mock(ok=False)
        response = Mock(ok=True, url=VACANCY.url)
        response.text.return_value = html()
        context.request.get.side_effect = [api, response]
        with patch.object(browser, "is_uc_context", return_value=False):
            self.assertTrue(gph.check_gph(main_page(context), VACANCY).allowed)
        self.assertEqual(self.read_gate.call_count, 2)
        self.assertEqual(context.request.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
