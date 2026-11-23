"""Строгая проверка оформления до первого клика «Откликнуться».

Фильтр hh accept_temporary включает также совместительство, поэтому не подтверждает ГПХ.
Используем публичные данные вакансии hh: civil_law_contracts и полные условия в description.
Отдельно различаем неуказанное оформление и недоступные/непонятные данные: разрешить
неуказанное можно настройкой, а ошибки проверки всегда означают пропуск.
"""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

from ..core.logger import log
from .search import Vacancy


@dataclass(frozen=True)
class GphCheck:
    allowed: bool
    reason: str
    unspecified: bool = False


_GPH = (r"(?:гпх|гпд|гражданско[\s\-‑–—]*правов\w*\s+договор\w*"
        r"|договор\w*\s+гражданско[\s\-‑–—]*правов\w*\s+характер\w*)")
# Справочник civil_law_contracts: https://api.hh.ru/openapi/redoc
_KNOWN_CONTRACTS = {"SELF_EMPLOYED", "INDIVIDUAL_ENTREPRENEUR", "INDIVIDUAL_PERSON"}
_NEGATIVE = re.compile(
    rf"\b(?:без|не)\b[^.!?\n]{{0,65}}?\b{_GPH}\b"
    rf"|\b{_GPH}\b[^.!?\n]{{0,35}}?\b(?:не\s+(?:предусмотр\w*|рассматрива\w*|оформля\w*|подход\w*|допуска\w*|работа\w*)"
    rf"|невозмож\w*|запрещ\w*)"
    r"|\b(?:только|исключительно)\s+(?:по\s+)?(?:тк\b|трудов\w*\s+договор\w*)",
    re.I,
)
_CONTRACT = rf"(?:договор(?:у)?\s+)?{_GPH}\b"
_POSITIVE = re.compile(
    rf"(?:^|[\n.!?;:])\s*(?:[-•–]\s*)?(?:возможное\s+)?оформление\s*"
    rf"(?::\s*|по\s+)(?:(?:тк(?:\s+рф)?|трудовому\s+договору)\s*(?:,|/|или)\s*(?:по\s+)?)?{_CONTRACT}"
    rf"|(?:^|[\n.!?;:])\s*(?:[-•–]\s*)?(?:работа|работаем|сотрудничество|сотрудничаем)\s+по\s+{_CONTRACT}"
    rf"|\b(?:возможно|возможен|допускается|заключаем|предлагаем)\s+(?:оформление\s+по\s+)?{_CONTRACT}"
    rf"|(?:^|[\n.!?;:])\s*(?:договор\s+)?{_GPH}\s+с\s+(?:самозанят\w*|ип\b|физлиц\w*)",
    re.I,
)
_LABOR = re.compile(
    r"\b(?:оформл\w*|трудоустройств\w*|при[её]м\s+на\s+работу)[^.!?\n]{0,70}?"
    r"\b(?:тк(?:\s+рф)?|трудов\w*\s+договор\w*)\b"
    r"|\b(?:работа|работаем)\s+по\s+(?:тк\b|трудов\w*\s+договор\w*)"
    r"|(?:^|[\n.!?;:])\s*(?:[-•–]\s*)?трудовой\s+договор\b",
    re.I,
)


class _VacancyText(HTMLParser):
    """Сохраняет границы условий, но не разрывает фразу на тегах выделения."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "li", "br", "div", "ul", "ol", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "li", "div", "ul", "ol", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


class _HtmlVacancy(HTMLParser):
    """Только состояние текущей вакансии; справочники и рекомендованные вакансии не читаем."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_state = False
        self.state_parts: list[str] = []
        self.has_description = False
        self.has_title = False
        self.blocked = False
        self.canonical = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        qa = attrs.get("data-qa", "")
        self.has_description |= qa == "vacancy-description"
        self.has_title |= qa == "vacancy-title"
        self.blocked |= qa.startswith(("account-captcha", "captcha")) or attrs.get("name") == "captchaText"
        if tag == "template" and attrs.get("id") == "HH-Lux-InitialState":
            self.in_state = True
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href", "")

    def handle_endtag(self, tag):
        if tag == "template":
            self.in_state = False

    def handle_data(self, data):
        if self.in_state:
            self.state_parts.append(data)


def _matches_vacancy_url(url: str, vacancy_id: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return (host == "hh.ru" or host.endswith(".hh.ru")) and parsed.path.rstrip("/") == f"/vacancy/{vacancy_id}"


def evaluate_gph_html(html: str, vacancy_id: str) -> GphCheck:
    """Проверить нормальную страницу вакансии и её собственные условия из HH-Lux-InitialState."""
    parser = _HtmlVacancy()
    parser.feed(html)
    if parser.blocked or not parser.has_description or not parser.has_title:
        return GphCheck(False, "hh не вернул обычную страницу вакансии для проверки оформления")
    if parser.canonical and not _matches_vacancy_url(parser.canonical, vacancy_id):
        return GphCheck(False, "страница hh относится к другой вакансии")
    try:
        state = json.loads("".join(parser.state_parts))
        vacancy = state["vacancyView"]["vacancyFull"]["vacancy"]
        if str(vacancy.get("vacancyId", "")) != vacancy_id:
            return GphCheck(False, "данные hh относятся к другой вакансии")
        description = vacancy.get("description")
        if not isinstance(description, str) or not description.strip():
            return GphCheck(False, "hh не отдал полное описание вакансии для проверки оформления")
        contracts = vacancy.get("civilLawContracts")
        if isinstance(contracts, list):
            contracts = [{"id": item} if isinstance(item, str) else item for item in contracts]
        return evaluate_gph({"description": description, "civil_law_contracts": contracts,
                             "accept_labor_contract": vacancy.get("acceptLaborContract")})
    except (ValueError, TypeError, KeyError, AttributeError):
        return GphCheck(False, "не удалось прочитать полные данные вакансии со страницы hh")


def _check_gph_html(page, vacancy: Vacancy) -> GphCheck:
    from ..web import browser
    from ..web.browser import FatalError
    from ..core.rate_limits import WorkDeferred
    from .submission_pacing import PacingStateError

    response = None
    try:
        browser.wait_read_interval()
        response = page.context.request.get(vacancy.url, timeout=20_000)
        if not response.ok or not _matches_vacancy_url(response.url, vacancy.id):
            return GphCheck(False, "не удалось загрузить страницу вакансии для проверки оформления")
        return evaluate_gph_html(response.text(), vacancy.id)
    except (WorkDeferred, FatalError, PacingStateError):
        raise
    except Exception as error:
        log.debug(f"  проверка оформления по странице hh не удалась: {error}")
        return GphCheck(False, "не удалось проверить оформление: страница вакансии недоступна")
    finally:
        if response is not None:
            try:
                response.dispose()
            except Exception:
                pass


def evaluate_gph(data: dict) -> GphCheck:
    """Оценить полный ответ GET /vacancies/{id}; никаких предположений по частичной занятости."""
    if data.get("description") is not None and not isinstance(data["description"], str):
        return GphCheck(False, "неизвестный формат описания вакансии от hh")
    parser = _VacancyText()
    parser.feed(str(data.get("description") or ""))
    description = unescape("".join(parser.parts))
    description = re.sub(r"[^\S\n]+", " ", description)
    if _NEGATIVE.search(description):
        return GphCheck(False, "в условиях есть отказ от ГПХ или указано оформление только по ТК")
    contracts = data.get("civil_law_contracts")
    if contracts is not None and not isinstance(contracts, list):
        return GphCheck(False, "неизвестный формат условий оформления ГПХ от hh")
    if isinstance(contracts, list):
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
               or item["id"] not in _KNOWN_CONTRACTS for item in contracts):
            return GphCheck(False, "неизвестный тип договора в условиях оформления hh")
        if contracts:
            return GphCheck(True, "hh указал гражданско-правовой договор в условиях оформления")
    labor = data.get("accept_labor_contract")
    if labor is not None and not isinstance(labor, bool):
        return GphCheck(False, "неизвестный формат сведений о трудовом договоре от hh")
    if labor is True:
        return GphCheck(False, "hh указал трудовой договор без подтверждённого ГПХ")
    positive = _POSITIVE.search(description)
    if positive and isinstance(contracts, list):
        return GphCheck(False, "условия ГПХ в описании противоречат структурированным данным hh")
    if positive:
        return GphCheck(True, "в полном описании явно предлагается оформление по ГПХ")
    if _LABOR.search(description):
        return GphCheck(False, "в описании указан трудовой договор без варианта ГПХ")
    return GphCheck(False, "формат оформления в вакансии не указан", unspecified=True)


def check_gph(page, vacancy: Vacancy) -> GphCheck:
    """API hh, при его недоступности — страница в вашей сессии. Без кликов и отправки отклика."""
    if not vacancy.id.isdigit():
        return GphCheck(False, "не удалось проверить ГПХ: некорректный номер вакансии")
    from ..web import browser
    from ..core.rate_limits import WorkDeferred
    from .submission_pacing import PacingStateError

    if browser.is_uc_context(page.context):
        from .vacancy_snapshot import get_snapshot

        snapshot = get_snapshot(page, vacancy)
        if snapshot is None:
            return GphCheck(False, "не удалось проверить оформление по браузерному снимку вакансии")
        rendered = _HtmlVacancy()
        rendered.feed(snapshot.html)
        html = snapshot.response_html if snapshot.response_html and not rendered.state_parts else snapshot.html
        return evaluate_gph_html(html, vacancy.id)
    response = None
    try:
        browser.wait_read_interval()
        response = page.context.request.get(
            f"https://api.hh.ru/vacancies/{vacancy.id}",
            headers={"User-Agent": "HH-Auto-Response/1.0 (personal job search)"},
            timeout=20_000,
        )
        if not response.ok:
            log.debug("  API hh недоступен — проверяю оформление по странице вакансии")
        else:
            data = response.json()
            if (isinstance(data, dict) and str(data.get("id", "")) == vacancy.id
                    and isinstance(data.get("description"), str)):
                return evaluate_gph(data)
            log.debug("  API hh не вернул полные данные — проверяю страницу вакансии")
    except (WorkDeferred, browser.FatalError, PacingStateError):
        raise
    except Exception as error:  # сеть, лимит hh или изменившийся формат данных — без отклика
        log.debug(f"  проверка ГПХ не удалась: {error}")
    finally:
        if response is not None:
            try:
                response.dispose()
            except Exception:
                pass  # закрывшийся браузер не должен отменять результат строгой проверки
    return _check_gph_html(page, vacancy)
