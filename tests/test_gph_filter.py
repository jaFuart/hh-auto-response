"""Строгий ГПХ: реальные форматы данных hh, отрицания и запрет клика до проверки. Без сети."""

from __future__ import annotations

import sys
import json
import unittest
from html import escape
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto.core.config import Config  # noqa: E402
from hh_auto.hh import runner  # noqa: E402
from hh_auto.hh.gph import GphCheck, check_gph, evaluate_gph, evaluate_gph_html  # noqa: E402
from hh_auto.hh.search import Vacancy  # noqa: E402


VACANCY = Vacancy("123", "Разработчик", "Компания", "Откликнуться")


def vacancy_html(description="Разработка приложений.", contracts=None, labor=False, vacancy_id="123"):
    state = {"vacancyView": {"vacancyFull": {"vacancy": {
        "vacancyId": int(vacancy_id), "description": description,
        "civilLawContracts": contracts, "acceptLaborContract": labor,
    }}}, "translations": {"unrelated": "Оформление по ГПХ."}}
    return (f'<link rel="canonical" href="https://hh.ru/vacancy/{vacancy_id}">'
            '<h1 data-qa="vacancy-title">Разработчик</h1>'
            f'<div data-qa="vacancy-description">{description}</div>'
            f'<template id="HH-Lux-InitialState">{escape(json.dumps(state, ensure_ascii=False))}</template>')


class GphDataTest(unittest.TestCase):
    def test_structured_contracts_and_mixed_contracts(self):
        for identifier in ("INDIVIDUAL_PERSON", "SELF_EMPLOYED", "INDIVIDUAL_ENTREPRENEUR"):
            with self.subTest(identifier=identifier):
                data = {"civil_law_contracts": [{"id": identifier}], "accept_labor_contract": True}
                self.assertTrue(evaluate_gph(data).allowed)

    def test_explicit_legacy_conditions(self):
        for description in (
            "Оформление: Договор ГПХ с самозанятым, с ИП, с физлицом",
            "Оформление по ГПХ.",
            "Условия: оформление по ТК РФ или ГПХ.",
            "<p>Оформление по <strong>ГПХ</strong> с физлицом.</p>",
            "Работа по договору ГПХ.",
            "Возможен гражданско-правовой договор.",
            "ГПХ с самозанятым, с ИП, с физлицом",
        ):
            with self.subTest(description=description):
                self.assertTrue(evaluate_gph({"description": description}).allowed)

    def test_unknown_or_unrelated_work_is_not_gph(self):
        for data in (
            {}, {"accept_temporary": True}, {"employment_form": {"id": "PART"}},
            {"description": "Работа по совместительству. Удалённо."},
            {"description": "Опыт работы с договорами ГПХ будет преимуществом."},
            {"description": "Обязанности: оформление договоров ГПХ для подрядчиков."},
            {"description": "Работа с самозанятыми и ИП."},
            {"civil_law_contracts": []}, {"civil_law_contracts": [None, {}]},
            {"civil_law_contracts": "ГПХ"},
            {"civil_law_contracts": [{"id": "UNKNOWN_CONTRACT"}], "description": "Оформление по ГПХ."},
            {"civil_law_contracts": [{"id": ["SELF_EMPLOYED"]}]},
            {"civil_law_contracts": "ГПХ", "description": "Оформление по ГПХ."},
        ):
            with self.subTest(data=data):
                self.assertFalse(evaluate_gph(data).allowed)

    def test_empty_structured_contracts_cannot_be_overridden(self):
        verdict = evaluate_gph({"civil_law_contracts": [], "description": "Оформление по ГПХ."})
        self.assertFalse(verdict.allowed)
        self.assertFalse(verdict.unspecified)

    def test_successful_vacancy_with_no_contract_is_distinguished_from_rejection(self):
        for data in (
            {}, {"civil_law_contracts": None}, {"civil_law_contracts": []},
            {"civil_law_contracts": [], "accept_labor_contract": False},
            {"description": "Работа по совместительству. Удалённо.", "accept_temporary": True},
            {"description": "Опыт работы с договорами ГПХ будет преимуществом."},
        ):
            with self.subTest(data=data):
                verdict = evaluate_gph(data)
                self.assertFalse(verdict.allowed)
                self.assertTrue(verdict.unspecified)

    def test_labor_contract_without_gph_is_never_unspecified(self):
        for data in (
            {"accept_labor_contract": True},
            {"civil_law_contracts": [], "accept_labor_contract": True},
            {"description": "Оформление по ТК РФ."},
            {"description": "<p>Трудовой договор</p>"},
            {"description": "Работа по трудовому договору."},
            {"description": "ГПХ не предусмотрен."},
        ):
            with self.subTest(data=data):
                verdict = evaluate_gph(data)
                self.assertFalse(verdict.allowed)
                self.assertFalse(verdict.unspecified)

    def test_malformed_or_unknown_contracts_cannot_be_treated_as_unspecified(self):
        for data in (
            {"civil_law_contracts": [None, {}]},
            {"civil_law_contracts": "ГПХ"},
            {"civil_law_contracts": [{"id": "UNKNOWN_CONTRACT"}]},
            {"civil_law_contracts": [{"id": "SELF_EMPLOYED"}, {"id": "UNKNOWN_CONTRACT"}]},
            {"civil_law_contracts": [{"id": ["SELF_EMPLOYED"]}]},
            {"accept_labor_contract": "false"},
            {"description": {"error": "неполные данные"}},
        ):
            with self.subTest(data=data):
                verdict = evaluate_gph(data)
                self.assertFalse(verdict.allowed)
                self.assertFalse(verdict.unspecified)

    def test_negation_overrides_other_positive_signals(self):
        for description in (
            "Не оформляем по ГПХ.", "ГПХ не предусмотрен.", "Без ГПХ, только ТК РФ.",
            "Рассматриваем только трудовой договор.", "ГПХ не рассматриваем.",
        ):
            with self.subTest(description=description):
                self.assertFalse(evaluate_gph({"description": description,
                                              "civil_law_contracts": [{"id": "SELF_EMPLOYED"}]}).allowed)


class GphLookupTest(unittest.TestCase):
    def page(self, payload=None, ok=True):
        page = mock.Mock()
        response = page.context.request.get.return_value
        response.ok, response.json.return_value = ok, payload
        return page

    def test_uses_exact_vacancy_and_no_credentials(self):
        page = self.page({"id": "123", "description": "Разработка.",
                          "civil_law_contracts": [{"id": "SELF_EMPLOYED"}]})
        self.assertTrue(check_gph(page, VACANCY).allowed)
        args, kwargs = page.context.request.get.call_args
        self.assertEqual(args, ("https://api.hh.ru/vacancies/123",))
        self.assertNotIn("Authorization", kwargs["headers"])
        page.context.request.get.return_value.dispose.assert_called_once_with()

    def test_http_errors_network_errors_and_wrong_payload_fail_closed(self):
        for payload, ok in ((None, False), (None, True), ({"id": "999"}, True), ({}, True), ([], True),
                            ({"id": "123"}, True), ({"id": "123", "description": {}}, True)):
            with self.subTest(payload=payload, ok=ok):
                verdict = check_gph(self.page(payload, ok), VACANCY)
                self.assertFalse(verdict.allowed)
                self.assertFalse(verdict.unspecified)
        page = self.page()
        page.context.request.get.side_effect = TimeoutError("network")
        verdict = check_gph(page, VACANCY)
        self.assertFalse(verdict.allowed)
        self.assertFalse(verdict.unspecified)

    def test_valid_payload_with_no_contract_is_unspecified(self):
        verdict = check_gph(self.page({"id": "123", "description": "Разработка.",
                                      "civil_law_contracts": [], "accept_labor_contract": False}), VACANCY)
        self.assertTrue(verdict.unspecified)
        self.assertFalse(verdict.allowed)


class GphHtmlFallbackTest(unittest.TestCase):
    def test_exact_current_vacancy_metadata_controls_contract(self):
        for contracts in (["SELF_EMPLOYED"], [{"id": "INDIVIDUAL_PERSON"}]):
            with self.subTest(contracts=contracts):
                self.assertTrue(evaluate_gph_html(vacancy_html(contracts=contracts, labor=True), "123").allowed)
        labor_only = evaluate_gph_html(vacancy_html(contracts=[], labor=True), "123")
        self.assertFalse(labor_only.allowed)
        self.assertFalse(labor_only.unspecified)
        unspecified = evaluate_gph_html(vacancy_html(contracts=[]), "123")
        self.assertFalse(unspecified.allowed)
        self.assertTrue(unspecified.unspecified)  # перевод с ГПХ вне вакансии не влияет

    def test_captcha_login_missing_state_and_other_vacancy_never_become_unspecified(self):
        for html in (
            '<h1>Войти</h1>',
            vacancy_html() + '<input data-qa="account-captcha-input">',
            vacancy_html(vacancy_id="999"),
            '<h1 data-qa="vacancy-title">Разработчик</h1><div data-qa="vacancy-description">Работа.</div>',
            vacancy_html(description=""),
            vacancy_html(contracts=["UNKNOWN_CONTRACT"]),
        ):
            with self.subTest(html=html[:90]):
                verdict = evaluate_gph_html(html, "123")
                self.assertFalse(verdict.allowed)
                self.assertFalse(verdict.unspecified)

    def test_api_403_uses_verified_authenticated_page_and_disposes_both_responses(self):
        page = mock.Mock()
        api_response = mock.Mock(ok=False)
        html_response = mock.Mock(ok=True, url=VACANCY.url)
        html_response.text.return_value = vacancy_html(contracts=[])
        page.context.request.get.side_effect = [api_response, html_response]
        verdict = check_gph(page, VACANCY)
        self.assertTrue(verdict.unspecified)
        self.assertEqual(page.context.request.get.call_args_list[1].args, (VACANCY.url,))
        api_response.dispose.assert_called_once_with()
        html_response.dispose.assert_called_once_with()

    def test_failed_html_and_login_redirect_stay_unavailable_after_api_403(self):
        for ok, url in ((False, VACANCY.url), (True, "https://hh.ru/account/login"),
                        (True, "https://hh.ru/vacancy/999")):
            with self.subTest(ok=ok, url=url):
                page = mock.Mock()
                page.context.request.get.side_effect = [mock.Mock(ok=False), mock.Mock(ok=ok, url=url)]
                verdict = check_gph(page, VACANCY)
                self.assertFalse(verdict.allowed)
                self.assertFalse(verdict.unspecified)
class GphRunnerTest(unittest.TestCase):
    def test_unspecified_policy_changes_eligibility_but_never_sends_in_dry_run(self):
        for allow_unspecified in (False, True):
            with self.subTest(allow_unspecified=allow_unspecified):
                cfg = Config("https://hh.ru/search/vacancy")
                cfg.filters.require_gph = True
                cfg.filters.gph_allow_unspecified = allow_unspecified
                cfg.limits.max_pages = 1
                state = mock.Mock(path=Path("unused/state.json"))
                state.seen.return_value = None
                stats = runner.Stats()
                with mock.patch.object(runner, "Responder") as responder, \
                        mock.patch.object(runner, "Pacer"), \
                        mock.patch.object(runner, "open_results_page", return_value=[VACANCY]), \
                        mock.patch.object(runner, "next_page_info", return_value=(False, "последняя")), \
                        mock.patch.object(runner, "check_gph", return_value=GphCheck(False, "не указано", unspecified=True)):
                    runner.run(mock.Mock(), cfg, runner.RunOptions(dry_run=True), state, mock.Mock(), stats,
                               None, Path("unused"))
                    responder.return_value.apply.assert_not_called()
                self.assertEqual(stats.dry_run, int(allow_unspecified))
                self.assertEqual(sum(stats.skipped.values()), int(not allow_unspecified))

    def test_confirmed_gph_reaches_dry_run_without_applying(self):
        cfg = Config("https://hh.ru/search/vacancy")
        cfg.filters.require_gph = True
        cfg.limits.max_pages = 1
        state = mock.Mock(path=Path("unused/state.json"))
        state.seen.return_value = None
        stats = runner.Stats()
        with mock.patch.object(runner, "Responder") as responder, \
                mock.patch.object(runner, "Pacer"), \
                mock.patch.object(runner, "open_results_page", return_value=[VACANCY]), \
                mock.patch.object(runner, "next_page_info", return_value=(False, "последняя")), \
                mock.patch.object(runner, "check_gph", return_value=GphCheck(True, "ГПХ подтверждён")):
            runner.run(mock.Mock(), cfg, runner.RunOptions(dry_run=True), state, mock.Mock(), stats,
                       None, Path("unused"))
            responder.return_value.apply.assert_not_called()
        self.assertEqual(stats.dry_run, 1)
        self.assertEqual(sum(stats.skipped.values()), 0)

    def test_gate_blocks_live_and_dry_run_before_ai_and_apply(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                cfg = Config("https://hh.ru/search/vacancy")
                cfg.filters.require_gph = True
                cfg.filters.gph_allow_unspecified = True
                cfg.filters.hide_unwanted = True
                cfg.ai_filter.enabled = True
                cfg.ai_filter.criteria = "Все вакансии подходят"
                cfg.limits.max_pages = 1
                state = mock.Mock(path=Path("unused/state.json"))
                state.seen.return_value = None
                stats = runner.Stats()
                with mock.patch.object(runner, "Responder") as responder, \
                        mock.patch.object(runner, "Pacer"), \
                        mock.patch.object(runner, "open_results_page", return_value=[VACANCY]), \
                        mock.patch.object(runner, "collect_vacancies", return_value=[]), \
                        mock.patch.object(runner, "wait_all_cards"), \
                        mock.patch.object(runner, "next_page_info", return_value=(False, "последняя")), \
                        mock.patch.object(runner, "check_gph", return_value=GphCheck(False, "ГПХ не подтверждён")), \
                        mock.patch.object(runner, "_ai_skip_reason") as ai_check:
                    runner.run(mock.Mock(), cfg, runner.RunOptions(dry_run=dry_run), state, mock.Mock(), stats,
                               None, Path("unused"), ai=mock.Mock())
                    responder.return_value.apply.assert_not_called()
                    responder.return_value.hide.assert_not_called()
                    ai_check.assert_not_called()
                    state.mark_skipped.assert_not_called()
                self.assertEqual(stats.applied, 0)
                self.assertEqual(stats.dry_run, 0)
                self.assertEqual(sum(stats.skipped.values()), 1)


if __name__ == "__main__":
    unittest.main()
