"""Verified native-browser vacancy reads shared by UC filters and letter context."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from html.parser import HTMLParser
import json
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

from ..core.logger import log

MAX_SNAPSHOTS = 32
MAX_CACHE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class VacancySnapshot:
    vacancy_id: str
    url: str
    title: str
    html: str
    response_html: str = ""


class _Cache:
    def __init__(self):
        self.items = OrderedDict()
        self.bytes = 0

    def clear(self, *_args):
        self.items.clear()
        self.bytes = 0

    def discard(self, vacancy_id):
        entry = self.items.pop(vacancy_id, None)
        if entry:
            self.bytes -= entry[1]

    def put(self, snapshot):
        self.discard(snapshot.vacancy_id)
        size = len(snapshot.html.encode("utf-8")) + len(snapshot.response_html.encode("utf-8"))
        if size > MAX_CACHE_BYTES:
            return
        self.items[snapshot.vacancy_id] = (snapshot, size)
        self.bytes += size
        while self.items and (len(self.items) > MAX_SNAPSHOTS or self.bytes > MAX_CACHE_BYTES):
            _, (_, removed) = self.items.popitem(last=False)
            self.bytes -= removed


_CACHES = WeakKeyDictionary()


def _cache_for(context):
    cache = _CACHES.get(context)
    if cache is None:
        cache = _Cache()
        _CACHES[context] = cache
        context.on("close", cache.clear)
    return cache


def _matches_url(value: str, vacancy_id: str) -> bool:
    try:
        url = urlparse(value)
        host = url.hostname or ""
        return (url.scheme == "https" and (host == "hh.ru" or host.endswith(".hh.ru"))
                and not url.username and not url.password and url.port in (None, 443)
                and url.path.rstrip("/") == f"/vacancy/{vacancy_id}")
    except (TypeError, ValueError):
        return False


def _title(value: str) -> str:
    return " ".join(value.split()).casefold()


class _Identity(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.title_depth = self.description_depth = self.state_depth = None
        self.title_parts, self.description_parts, self.state_parts = [], [], []
        self.canonical = ""
        self.blocked = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in self.VOID:
            self.depth += 1
        qa = attrs.get("data-qa", "")
        self.blocked |= qa.startswith(("account-captcha", "captcha", "account-login")) or attrs.get("name") == "captchaText"
        if qa == "vacancy-title":
            self.title_depth = self.depth
        if qa == "vacancy-description":
            self.description_depth = self.depth
        if tag in ("template", "script") and attrs.get("id") == "HH-Lux-InitialState":
            self.state_depth = self.depth
        if tag == "link" and "canonical" in attrs.get("rel", "").split():
            self.canonical = attrs.get("href", "")

    def handle_endtag(self, tag):
        if tag not in self.VOID:
            self.depth = max(0, self.depth - 1)
        for name in ("title_depth", "description_depth", "state_depth"):
            marker = getattr(self, name)
            if marker is not None and self.depth < marker:
                setattr(self, name, None)

    def handle_data(self, text):
        if self.title_depth is not None:
            self.title_parts.append(text)
        if self.description_depth is not None:
            self.description_parts.append(text)
        if self.state_depth is not None:
            self.state_parts.append(text)

    @property
    def title(self):
        return " ".join("".join(self.title_parts).split())

    def metadata_matches(self, vacancy_id):
        if self.blocked or (self.canonical and not _matches_url(self.canonical, vacancy_id)):
            return False
        if self.state_parts:
            try:
                state = json.loads("".join(self.state_parts))
                vacancy = state["vacancyView"]["vacancyFull"]["vacancy"]
                if str(vacancy["vacancyId"]) != vacancy_id:
                    return False
            except (ValueError, KeyError, TypeError):
                return False
        return True

    def matches(self, vacancy_id, expected_title):
        if (not self.title or not "".join(self.description_parts).strip()
                or (expected_title and _title(self.title) != _title(expected_title))
                or not self.metadata_matches(vacancy_id)):
            return False
        return True


def get_snapshot(page, vacancy) -> VacancySnapshot | None:
    """One verified GET in an owned tab; never navigate or close the caller's main page."""
    from ..web import browser
    from ..core.rate_limits import WorkDeferred
    from .submission_pacing import PacingStateError

    vacancy_id = str(vacancy.id)
    if not vacancy_id.isdigit() or not _matches_url(vacancy.url, vacancy_id):
        return None
    cache = _cache_for(page.context)
    cached = cache.items.get(vacancy_id)
    if cached and (not vacancy.title or _title(cached[0].title) == _title(vacancy.title)):
        cache.items.move_to_end(vacancy_id)
        return cached[0]
    if cached:
        cache.discard(vacancy_id)
    tab = None
    responses = []

    def main_response(response):
        with suppress(Exception):
            if response.request.is_navigation_request() and response.frame == tab.main_frame:
                responses.append(response)

    try:
        tab = page.context.new_page()
        tab.on("response", main_response)
        browser.open_url(tab, vacancy.url, attempts=1, timeout=30)
        browser.wait_captcha(tab)
        if not _matches_url(tab.url, vacancy_id) or not responses:
            return None
        response = responses[-1]
        if (not 200 <= response.status < 300 or not _matches_url(response.url, vacancy_id)
                or any(300 <= item.status < 400 for item in responses)
                or response.request.redirected_from is not None):
            return None
        tab.locator('[data-qa="vacancy-description"]').first.wait_for(state="attached", timeout=10_000)
        browser.wait_captcha(tab)
        html = tab.content()
        identity = _Identity(html)
        if identity.blocked:
            raise browser.FatalError("hh.ru вернул проверку или страницу входа вместо вакансии")
        if not identity.matches(vacancy_id, vacancy.title):
            return None
        response_html = response.text()
        raw_identity = _Identity(response_html)
        if raw_identity.blocked:
            raise browser.FatalError("hh.ru вернул проверку или страницу входа вместо вакансии")
        if not raw_identity.metadata_matches(vacancy_id):
            return None
        if response_html == html:
            response_html = ""
        snapshot = VacancySnapshot(vacancy_id, tab.url, identity.title, html, response_html)
        cache.put(snapshot)
        return snapshot
    except (WorkDeferred, browser.FatalError, PacingStateError):
        raise
    except Exception as error:
        if tab is not None:
            browser.wait_captcha(tab)
        log.debug("  браузерный снимок вакансии недоступен: %s", type(error).__name__)
        return None
    finally:
        if tab is not None:
            with suppress(Exception):
                tab.remove_listener("response", main_response)
            with suppress(Exception):
                tab.close()
