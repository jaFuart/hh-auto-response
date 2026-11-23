"""Страницы выдачи: переход по страницам и сбор карточек вакансий."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..core import control
from . import filters as hh_filters
from ..web import selectors as S
from ..web.auth import hh_role, is_auth_url
from ..web.browser import FatalError, open_url, wait_captcha
from ..core.logger import log
from .vacancy_meta import VacancyMeta, read_meta


@dataclass
class Vacancy:
    id: str
    title: str
    company: str
    button_text: str  # текст кнопки отклика в карточке ("" — кнопки нет)
    remote: bool = False  # в карточке есть «Можно удалённо»
    snippet: str = ""  # обязанности и требования из карточки (коротко)
    meta: VacancyMeta | None = field(default=None, repr=False)  # данные hh из выдачи (vacancy_meta.py)

    @property
    def url(self) -> str:
        return f"https://hh.ru/vacancy/{self.id}"

    @property
    def can_respond(self) -> bool:
        return "Откликнуться" in self.button_text


_COLLECT_JS = """
(s) => Array.from(document.querySelectorAll(s.card)).map((card) => {
  const text = (sel) => (card.querySelector(sel)?.textContent || '').replace(/\\s+/g, ' ').trim();
  const link = card.querySelector(s.link);
  const match = link && link.href.match(/\\/vacancy\\/(\\d+)/);
  return {
    id: match ? match[1] : '', title: text(s.title), company: text(s.employer),
    button_text: text(s.button), remote: !!card.querySelector(s.remote),
    snippet: Array.from(card.querySelectorAll(s.snippet)).map((el) => el.textContent).join(' ').replace(/\\s+/g, ' ').trim(),
  };
}).filter((v) => v.id)
"""


SEARCH_PAGE = "https://hh.ru/search/vacancy"


def build_search_url(cfg, text: str | None = None) -> str:
    """Ссылка выдачи для запуска: ссылка из настроек или общий поиск по запросу, плюс фильтры hh.
    text — один из запросов (по умолчанию первый). Выбранное резюме привязывается и к общему поиску."""
    if cfg.search.mode == "query":
        query = [("text", text if text is not None else (cfg.search.queries or [""])[0])]
        if cfg.search.area:
            query.append(("area", cfg.search.area))
        if cfg.search.title_only:
            query.append(("search_field", "name"))
        query += [("enable_snippets", "true"), ("hhtmFrom", "vacancy_search_list")]
        url = f"{SEARCH_PAGE}?{urlencode(query)}"
    else:
        url = cfg.search_url
    url = hh_filters.apply(url, cfg.hh_filters)
    resume_id = cfg.resume_id
    allow_unspecified = cfg.filters.require_gph and cfg.filters.gph_allow_unspecified
    if resume_id or allow_unspecified:
        parsed = urlparse(url)
        # Ссылка и настройки могли остаться от другого резюме. Письмо и отклик должны использовать одно.
        # «ГПХ или не указано» проверяется программой: серверный ГПХ/совместительство отрежет неуказанное.
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                 if not (resume_id and key == "resume")
                 and not (allow_unspecified and key == "accept_temporary")]
        if resume_id:
            query.append(("resume", resume_id))
        url = urlunparse(parsed._replace(query=urlencode(query)))
    return url


def search_urls(cfg) -> list[tuple[str, str]]:
    """Все выдачи одного запуска: (подпись, ссылка). По запросу — своя выдача на каждую строку query."""
    if cfg.search.mode == "query":
        return [(f"запрос «{text}»", build_search_url(cfg, text)) for text in cfg.search.queries]
    return [("ссылка из настроек", build_search_url(cfg))]


def describe_search(cfg) -> str:
    if cfg.search.mode == "query":
        from ..core.config import AREAS

        area = AREAS.get(cfg.search.area, f"регион {cfg.search.area}")
        where = "в названии" if cfg.search.title_only else "везде"
        queries = cfg.search.queries or [""]
        if len(queries) == 1:
            return f"запрос «{queries[0]}» ({area}, искать {where})"
        shown = ", ".join(f"«{q}»" for q in queries[:3]) + ("…" if len(queries) > 3 else "")
        return f"{_plural(len(queries), 'запрос', 'запроса', 'запросов')}: {shown} ({area}, искать {where})"
    return f"ссылка: {cfg.search_url}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    word = one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many
    return f"{n} {word}"


def apply_hh_filters(search_url: str, filters: hh_filters.HhFilters) -> str:
    """Подставляет в ссылку поиска фильтры самого hh ([hh_filters]). Незаданные не трогаем."""
    return hh_filters.apply(search_url, filters)


def describe_hh_filters(search_url: str) -> str:
    """Человекочитаемое описание фильтров hh в ссылке — для лога."""
    return hh_filters.describe(search_url)


def results_page_url(search_url: str, page_num: int) -> str:
    """Ссылка на страницу выдачи page_num (нумерация hh с нуля)."""
    u = urlparse(search_url)
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "page"]
    if page_num:
        query.append(("page", str(page_num)))
    return urlunparse(u._replace(query=urlencode(query)))


class _DescriptionParser(HTMLParser):
    """Текст блоков страницы вакансии с нужным data-qa (описание, ключевые навыки)."""

    VOID = {"br", "img", "hr", "input", "meta", "link", "wbr", "source"}
    BREAKS = {"p", "li", "br", "div", "ul", "ol", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self, wanted: tuple[str, ...]):
        super().__init__()
        self.wanted, self.depth, self.parts = wanted, 0, []
        self.has_description = False
        self.has_description_text = False
        self.in_description = False
        self.blocked = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        qa = attributes.get("data-qa") or ""
        self.has_description |= qa == "vacancy-description"
        self.blocked |= qa.startswith(("account-captcha", "captcha")) or attributes.get("name") == "captchaText"
        if self.depth:
            if tag in self.BREAKS:
                self.parts.append("\n")
            if tag not in self.VOID:
                self.depth += 1
        elif any(v and v.startswith(self.wanted) for k, v in attrs if k == "data-qa") and tag not in self.VOID:
            self.depth = 1
            self.in_description = qa == "vacancy-description"
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.depth and tag not in self.VOID:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)
            self.has_description_text |= self.in_description and bool(data.strip())


def fetch_vacancy_text(page: Page, vac: Vacancy, limit: int | None = 4000, *, require_description: bool = False) -> str:
    """Описание и навыки; UC читает проверенный снимок в отдельной собственной вкладке Chrome."""
    from ..web import browser
    from ..core.rate_limits import WorkDeferred
    from .submission_pacing import PacingStateError

    response = None
    try:
        if browser.is_uc_context(page.context):
            from .vacancy_snapshot import get_snapshot

            snapshot = get_snapshot(page, vac)
            if snapshot is None:
                return ""
            html = snapshot.html
        else:
            browser.wait_read_interval()
            response = page.context.request.get(vac.url, timeout=20_000)
            if not response.ok:
                return ""
            if require_description:
                resolved = urlparse(response.url)
                host = resolved.hostname or ""
                if not ((host == "hh.ru" or host.endswith(".hh.ru"))
                        and resolved.path.rstrip("/") == f"/vacancy/{vac.id}"):
                    return ""
            html = response.text()
        wanted = ("vacancy-description", "skills-element") if require_description else (
            "vacancy-description", "skills-element", "bloko-tag__text")
        parser = _DescriptionParser(wanted)
        parser.feed(html)
        if require_description and (not parser.has_description or not parser.has_description_text or parser.blocked):
            return ""
    except (WorkDeferred, FatalError, PacingStateError):
        raise
    except Exception as e:
        log.debug(f"  описание вакансии не загрузилось: {e}")
        return ""
    finally:
        if response is not None:
            try:
                response.dispose()
            except Exception:
                pass
    text = re.sub(r"[ \t ]+", " ", "".join(parser.parts))
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text if limit is None else text[:limit]


def open_results_page(page: Page, url: str) -> list[Vacancy]:
    log.info(f"  открываю выдачу: {url}")
    open_url(page, url)
    wait_captcha(page)
    if is_auth_url(page.url) or hh_role(page) == "anonymous":
        raise FatalError("сессия hh истекла (вы больше не авторизованы), перезапустите скрипт и войдите заново")
    try:
        page.locator(S.VACANCY_CARD).first.wait_for(timeout=20_000)
    except PlaywrightTimeout:
        log.warning("  ! карточки вакансий не появились за 20 с")
        return []
    wait_all_cards(page)
    return collect_vacancies(page)


def collect_vacancies(page: Page) -> list[Vacancy]:
    """Вакансии с уже открытой страницы выдачи (без перехода)."""
    selectors = {
        "card": S.VACANCY_CARD,
        "link": S.VACANCY_TITLE_LINK,
        "title": S.VACANCY_TITLE_TEXT,
        "employer": S.VACANCY_EMPLOYER,
        "button": S.RESPONSE_BUTTON,
        "remote": S.REMOTE_LABEL,
        "snippet": S.SNIPPET,
    }
    vacancies = [Vacancy(**raw) for raw in page.evaluate(_COLLECT_JS, selectors)]
    metas = read_meta(page)
    for v in vacancies:
        v.meta = metas.get(v.id)
    if vacancies:
        log.debug(f"  данные hh по вакансиям (проверка работодателя, отклики, навыки): {len(metas)} из {len(vacancies)}")
    for v in vacancies:
        log.debug(f"    • {v.id} «{v.title}» — {v.company} [кнопка: {v.button_text or 'нет'}{', удалённо' if v.remote else ''}]")
    return vacancies


def wait_all_cards(page: Page, timeout: float = 12, settle: float = 1.5) -> None:
    """hh сначала рисует 20 карточек, а остальные (до 50 — «50 вакансий» в выдаче) догружает через пару секунд.
    Без ожидания программа брала только первые 20 и теряла остальные 30 на каждой странице."""
    cards = page.locator(S.VACANCY_CARD)
    count, stable_since, deadline = cards.count(), time.monotonic(), time.monotonic() + timeout
    while time.monotonic() < deadline and time.monotonic() - stable_since < settle:
        page.mouse.wheel(0, 4000)  # на случай, если догрузка завязана на прокрутку
        control.sleep(0.4)  # «Остановить» — сразу, а не через 12 с
        now = cards.count()
        if now != count:
            count, stable_since = now, time.monotonic()
    page.evaluate("window.scrollTo(0, 0)")


_PAGER_JS = """
(s) => ({
  next: document.querySelectorAll(s.next).length,
  pages: Array.from(document.querySelectorAll(s.page)).map((a) => parseInt(a.textContent, 10)).filter((n) => !isNaN(n)),
  cards: document.querySelectorAll(s.card).length,
  header: (document.querySelector(s.header) || {}).innerText || '',
})
"""


def next_page_info(page: Page, page_num: int) -> tuple[bool, str]:
    """Есть ли страница после page_num (с нуля) и почему так решили — для понятного лога.

    hh то показывает стрелку «дальше», то только номера страниц (стрелки нет, а страниц 9),
    поэтому смотрим на всё: стрелку, номера страниц и «Найдено N вакансий» в шапке.
    """
    try:
        info = page.evaluate(_PAGER_JS, {"next": S.PAGER_NEXT, "page": S.PAGER_PAGE, "card": S.VACANCY_CARD,
                                         "header": S.SEARCH_HEADER})
    except Exception:
        return False, "не удалось прочитать переключатель страниц"
    current = page_num + 1
    found = re.search(r"Найден\w*\s+([\d\s ]+)", info["header"])
    total = int(re.sub(r"\D", "", found.group(1))) if found else None
    total_note = f"найдено {total} вакансий, " if total else ""
    if info["next"]:
        return True, "есть кнопка «дальше»"
    if info["pages"]:  # номера страниц видны — решают они (на последней странице карточек меньше)
        if max(info["pages"]) > current:
            return True, f"{total_note}страниц не меньше {max(info['pages'])}"
        return False, f"{total_note}это последняя страница ({current} из {max(info['pages'])})"
    if total and info["cards"] and total > current * info["cards"]:
        return True, f"{total_note}по {info['cards']} на странице"
    if total is not None:
        return False, f"{total_note}это последняя страница ({current})"
    return False, "переключателя страниц нет — выдача на одной странице"
