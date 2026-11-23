"""Исключения читают полный текст до отклика; все страницы и отправки подменены."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hh_auto.core.config import Config, ConfigError, Filters, validate_config
from hh_auto.hh import runner
from hh_auto.hh.search import Vacancy, fetch_vacancy_text


EXCLUSIONS = ["Битрикс24", "amoCRM"]


class DescriptionExclusions(unittest.TestCase):
    def test_crm_aliases_match_spelling_and_word_boundaries(self):
        for text in ("Интеграция с Битрикс24", "Bitrix24 REST", "битрикс 24", "BITRIX-24",
                     "Битрикс‑24", "API amoCRM", "AMO CRM", "амоCRM", "амо-ЦРМ", "amo_crm"):
            with self.subTest(text=text):
                self.assertIsNotNone(runner._find_description_word(text, EXCLUSIONS))
        for text in ("Разработка собственной CRM", "1С-Битрикс: Управление сайтом", "Битрикс CMS",
                     "Python, PostgreSQL, Power BI", "bitrix240", "someamocrmtool"):
            with self.subTest(text=text):
                self.assertIsNone(runner._find_description_word(text, EXCLUSIONS))

    def test_other_exclusions_remain_case_insensitive_substrings(self):
        self.assertEqual(runner._find_description_word("Работа с TELEPHONY API", ["telephony"]), "telephony")

    def test_card_match_does_not_load_a_page(self):
        vacancy = Vacancy("101", "Python developer", "Example", "Откликнуться", snippet="API amo CRM")
        with patch.object(runner, "fetch_vacancy_text") as fetch:
            self.assertEqual(runner._description_exclusion(Mock(), vacancy, EXCLUSIONS), ("amoCRM", True))
        fetch.assert_not_called()

    def test_full_text_has_no_four_thousand_or_hundred_thousand_character_cutoff(self):
        vacancy = Vacancy("101", "Python developer", "Example", "Откликнуться", snippet="Python APIs")
        full = "Разработка API. " * 8_000 + "Интеграция с Битрикс24."
        with patch.object(runner, "fetch_vacancy_text", return_value=full) as fetch:
            self.assertEqual(runner._description_exclusion(Mock(), vacancy, EXCLUSIONS), ("Битрикс24", True))
        fetch.assert_called_once_with(unittest.mock.ANY, vacancy, limit=None, require_description=True)

    def test_missing_full_description_does_not_pass_on_a_clean_snippet(self):
        vacancy = Vacancy("101", "Python developer", "Example", "Откликнуться", snippet="Python APIs")
        for full in ("", " \n "):
            with self.subTest(full=full), patch.object(runner, "fetch_vacancy_text", return_value=full):
                self.assertEqual(runner._description_exclusion(Mock(), vacancy, EXCLUSIONS), (None, False))
        with patch.object(runner, "fetch_vacancy_text", return_value="Python backend and PostgreSQL."):
            self.assertEqual(runner._description_exclusion(Mock(), vacancy, EXCLUSIONS), (None, True))

    def test_config_requires_a_list_of_nonempty_strings(self):
        self.assertEqual(Filters().exclude_description_words, [])
        self.assertEqual(Filters().exclude_title_words, [])
        for value in ("amoCRM", [""], ["   "], [42], None):
            cfg = Config("https://hh.ru/search/vacancy")
            cfg.filters.exclude_description_words = value
            with self.subTest(value=value), self.assertRaises(ConfigError):
                validate_config(cfg)


class FullDescriptionRequest(unittest.TestCase):
    def setUp(self):
        self.vacancy = Vacancy("101", "Python developer", "Example", "Откликнуться")

    def page(self, html, *, ok=True, url=None):
        response = Mock(ok=ok, url=url or self.vacancy.url)
        response.text.return_value = html
        page = Mock()
        page.context.request.get.return_value = response
        return page, response

    def test_full_description_keeps_tail_and_inline_tags(self):
        full = "Python backend. " * 8_000 + "amo<span>CRM</span> integration."
        page, response = self.page('<div data-qa="vacancy-description">' + full + '</div>')
        text = fetch_vacancy_text(page, self.vacancy, limit=None, require_description=True)
        self.assertGreater(len(text), 100_000)
        self.assertTrue(text.endswith("amoCRM integration."))
        response.dispose.assert_called_once_with()

    def test_missing_description_wrong_vacancy_login_and_captcha_stay_unknown(self):
        for html, url in (
            ('<span data-qa="skills-element">Python</span>', self.vacancy.url),
            ('<div data-qa="vacancy-description"> </div><span data-qa="skills-element">Python</span>', self.vacancy.url),
            ('<div data-qa="vacancy-description">Python</div>', "https://hh.ru/account/login"),
            ('<div data-qa="vacancy-description">Python</div>', "https://hh.ru/vacancy/999"),
            ('<div data-qa="vacancy-description">Python</div>', "https://unrelated.example/vacancy/101"),
            ('<input name="captchaText"><div data-qa="vacancy-description">Python</div>', self.vacancy.url),
        ):
            with self.subTest(url=url, html=html):
                page, response = self.page(html, url=url)
                self.assertEqual(fetch_vacancy_text(page, self.vacancy, limit=None, require_description=True), "")
                response.dispose.assert_called_once_with()


class ExclusionRunner(unittest.TestCase):
    def run_bot(self, vacancy, *, full="Python backend.", dry=True, excluded_words=None):
        cfg = Config("https://hh.ru/search/vacancy")
        cfg.filters.exclude_title_words = ["менеджер по продаж", "менеджер продаж", "sales manager"]
        cfg.filters.exclude_description_words = EXCLUSIONS if excluded_words is None else excluded_words
        cfg.filters.hide_unwanted = True
        cfg.filters.require_gph = True
        cfg.ai_filter.enabled = True
        cfg.ai_filter.criteria = "Python"
        cfg.limits.max_pages = 1
        state = Mock(path=Path("unused/state.json"))
        state.seen.return_value = None
        stats = runner.Stats()
        with patch.object(runner, "Responder") as responder, \
                patch.object(runner, "ApplicationDelivery") as queue, \
                patch.object(runner, "Pacer"), \
                patch.object(runner, "_sleep"), \
                patch.object(runner, "open_results_page", return_value=[vacancy]), \
                patch.object(runner, "collect_vacancies", return_value=[]), \
                patch.object(runner, "wait_all_cards"), \
                patch.object(runner, "next_page_info", return_value=(False, "end")), \
                patch.object(runner, "fetch_vacancy_text", return_value=full) as fetch, \
                patch.object(runner, "check_gph") as gph, \
                patch.object(runner, "_ai_skip_reason", return_value=("", False)) as ai_check:
            gph.return_value.allowed = True
            responder.return_value.on_search_page.return_value = True
            queue.return_value.get.return_value = None
            runner.run(Mock(), cfg, runner.RunOptions(dry_run=dry), state, Mock(), stats, None, Path("unused"))
        return stats, responder.return_value, state, fetch, gph, ai_check, queue.return_value

    def test_excluded_full_description_blocks_before_ai_contract_claim_or_send(self):
        vacancy = Vacancy("101", "Python developer", "Example", "Откликнуться", snippet="Clean short snippet")
        for dry in (True, False):
            with self.subTest(dry=dry):
                stats, responder, state, fetch, gph, ai_check, queue = self.run_bot(
                    vacancy, full="Python APIs. " * 500 + "Работа с amoCRM.", dry=dry)
                self.assertEqual(sum(stats.skipped.values()), 1)
                self.assertEqual(stats.dry_run, 0)
                gph.assert_not_called()
                ai_check.assert_not_called()
                queue.claim.assert_not_called()
                responder.apply.assert_not_called()
                state.mark_skipped.assert_not_called()

    def test_missing_full_description_is_not_hidden_or_marked_permanently_skipped(self):
        vacancy = Vacancy("101", "Python developer", "Example", "Откликнуться", snippet="Clean snippet")
        stats, responder, state, fetch, gph, ai_check, queue = self.run_bot(vacancy, full="", dry=False)
        self.assertIn("полное описание вакансии недоступно", stats.recent[0]["note"])
        responder.hide.assert_not_called()
        state.mark_skipped.assert_not_called()
        queue.cancel.assert_not_called()
        queue.claim.assert_not_called()
        gph.assert_not_called()
        ai_check.assert_not_called()

    def test_title_filter_does_not_exclude_a_developer_for_sales_duties_in_the_body(self):
        for title, count in (("Менеджер по продажам B2B", 0), ("Sales manager", 0),
                             ("Python developer", 1)):
            with self.subTest(title=title):
                vacancy = Vacancy("101", title, "Example", "Откликнуться")
                stats, _, _, fetch, _, _, _ = self.run_bot(vacancy, full="Продукт для менеджеров по продажам.")
                self.assertEqual(stats.dry_run, count)
                if count == 0:
                    fetch.assert_not_called()

    def test_empty_description_exclusions_do_not_request_full_text(self):
        vacancy = Vacancy("101", "Python developer", "Example", "Откликнуться")
        stats, _, _, fetch, _, _, _ = self.run_bot(vacancy, excluded_words=[])
        self.assertEqual(stats.dry_run, 1)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
