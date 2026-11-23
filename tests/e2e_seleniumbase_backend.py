"""Offline UC/CDP integration: resume selection, exact letter, persistence and owned cleanup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright
from hh_auto.core.config import Config
from hh_auto.hh import responder as response_module
from hh_auto.hh.responder import Responder, Status
from hh_auto.hh.search import Vacancy
from hh_auto.hh.search import fetch_vacancy_text
from hh_auto.hh.gph import check_gph
from hh_auto.core.rate_limits import submission_pacer
from hh_auto.web import browser as browser_module

VACANCY = '''<meta charset="utf-8"><h1 data-qa="vacancy-title">Python</h1>
<div data-qa="vacancy-description">Разработка REST API. Оформление по ГПХ.</div>
<template id="HH-Lux-InitialState">{"vacancyView":{"vacancyFull":{"vacancy":{
"vacancyId":101,"description":"Разработка REST API. Оформление по ГПХ.",
"civilLawContracts":["INDIVIDUAL_PERSON"],"acceptLaborContract":false}}}}</template>
<a data-qa="vacancy-response-link-top" href="/applicant/vacancy_response?vacancyId=101"
onclick="event.preventDefault();throw Error('Quick submit must never run')">Откликнуться</a>'''
FORM = '''<meta charset="utf-8"><script>window.sent=[]</script><form>
<input type="radio" name="resumeId" value="other" checked>
<input type="radio" name="resumeId" value="selected">
<textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
<button type="button" data-qa="vacancy-response-submit-popup" onclick="
window.sent.push({resume:document.querySelector('input:checked').value,letter:document.querySelector('textarea').value});
document.querySelector('form').remove();document.body.insertAdjacentHTML('beforeend',
'<p data-qa=vacancy-response-link-view-topic>Вы откликнулись</p>');">Откликнуться</button></form>'''


class LocalApi(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = json.dumps({'cookie': self.headers.get('Cookie', '')}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def owned_group_processes(group):
    if os.name != 'posix':
        return []
    result = []
    for item in Path('/proc').iterdir():
        if item.name.isdigit():
            try:
                if os.getpgid(int(item.name)) == group:
                    result.append(int(item.name))
            except (ProcessLookupError, PermissionError):
                pass
    return result


def assert_native_window_geometry(page):
    metrics = page.evaluate('''() => ({innerWidth, innerHeight, outerWidth, outerHeight,
        screenWidth: screen.width, screenHeight: screen.height})''')
    assert 0 < metrics['innerWidth'] <= metrics['outerWidth'] <= metrics['screenWidth'], metrics
    assert 0 < metrics['innerHeight'] <= metrics['outerHeight'] <= metrics['screenHeight'], metrics


def main():
    api = ThreadingHTTPServer(('127.0.0.1', 0), LocalApi)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='hh-uc-e2e-') as folder:
            root = Path(folder)
            os.environ['HH_AUTO_HOME'] = str(root)
            cfg = Config(search_url='', resume_url='https://hh.ru/resume/selected')
            cfg.browser.backend = 'seleniumbase_uc'
            cfg.browser.headless = True
            cfg.browser.profile_dir = str(root / 'profile')
            cfg.letter.mode = 'template'
            cfg.letter.template_text = 'Разрабатывал REST API на Python и проверял ответы внешних сервисов.'
            cfg.logs.snapshots = False
            cfg.limits.submission_interval_seconds = 180
            cfg.limits.submission_batch_size = 2
            cfg.limits.submission_batch_pause_minutes = 25
            cfg.limits.captcha_cooldown_minutes = 120
            cfg.limits.max_responses_per_day = 10
            with sync_playwright() as pw:
                context = browser_module.launch(pw, cfg)
                owner = browser_module._uc_owners[id(context)]
                worker_pid = owner.process.pid
                try:
                    extra_page = context.new_page()
                    extra_page.wait_for_function('''([locale, timezone]) => {
                        const current = Intl.DateTimeFormat().resolvedOptions();
                        const expanded = value => new Intl.Locale(value).maximize().toString();
                        return expanded(current.locale) === expanded(locale) && current.timeZone === timezone;
                    }''', arg=[cfg.geo.locale, cfg.geo.timezone])
                    assert_native_window_geometry(extra_page)
                    extra_page.close()
                    context.add_cookies([{'name': 'hhrole', 'value': 'applicant', 'url': 'https://hh.ru'},
                        {'name': 'uc_fixture', 'value': 'fixture-only', 'url': f'http://127.0.0.1:{api.server_port}',
                         'expires': time.time() + 3600}])
                    requests = []

                    def intercept(route):
                        request = route.request
                        assert request.method == 'GET', 'No real submission is permitted in this fixture'
                        path = urlparse(request.url).path
                        requests.append(path)
                        if path == '/vacancy/101':
                            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=VACANCY)
                        elif path == '/applicant/vacancy_response':
                            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=FORM)
                        else:
                            route.abort()

                    context.route('**/*', intercept)
                    page = context.pages[0]
                    assert_native_window_geometry(page)
                    page_errors = []
                    page.on('pageerror', lambda error: page_errors.append(str(error)))
                    vacancy = Vacancy('101', 'Python', 'Fixture', 'Откликнуться')
                    before_url, before_pages = page.url, len(context.pages)
                    with patch.object(context.request, 'get', side_effect=AssertionError('UC reads must use Chrome')):
                        description = fetch_vacancy_text(page, vacancy, limit=None, require_description=True)
                        assert 'Разработка REST API.' in description, description
                        contract = check_gph(page, vacancy)
                        assert contract.allowed, contract
                        assert fetch_vacancy_text(page, vacancy, limit=None, require_description=True) == description
                    assert requests.count('/vacancy/101') == 1, requests
                    assert page.url == before_url and len(context.pages) == before_pages
                    assert not (root / 'data/submission-pacing.json').exists(), 'Reads consumed an application attempt'
                    with patch.object(response_module.control, 'sleep'), patch.object(Responder, '_pause'):
                        sender = Responder(page, cfg, root / 'snapshots', 'Fixture')
                        result = sender.apply_on_vacancy(vacancy)
                    assert result.status is Status.APPLIED and result.letter
                    assert result.letter_text == cfg.letter.template_text
                    assert page.evaluate('window.sent') == [{'resume': 'selected', 'letter': cfg.letter.template_text}]
                    assert not page_errors, 'The quick-submit handler was invoked'
                    pace = submission_pacer(cfg, root).peek_status()
                    assert pace.batch_count == 1 and pace.submit_until - pace.last_attempt_at == 180
                    assert pace.wait_seconds > 0
                    assert_native_window_geometry(page)
                    assert page.evaluate('''desired => new Intl.Locale(Intl.DateTimeFormat().resolvedOptions().locale)
                        .maximize().toString() === new Intl.Locale(desired).maximize().toString()''', cfg.geo.locale)
                    assert page.evaluate('Intl.DateTimeFormat().resolvedOptions().timeZone') == cfg.geo.timezone
                    page.evaluate("localStorage.setItem('uc-fixture', 'saved')")
                    reply = context.request.get(f'http://127.0.0.1:{api.server_port}/api').json()
                    assert 'uc_fixture=fixture-only' in reply['cookie']
                finally:
                    browser_module.detach()
                    browser_module.close_context(context)
                assert owner.process.poll() is not None, 'UC worker did not stop'
                deadline = time.monotonic() + 8
                while owned_group_processes(worker_pid) and time.monotonic() < deadline:
                    time.sleep(0.1)
                assert not owned_group_processes(worker_pid), 'Owned UC/Chrome/Xvfb processes remained'

                context = browser_module.launch(pw, cfg)
                try:
                    assert any(item['name'] == 'uc_fixture' for item in context.cookies())
                    context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<h1>Saved profile</h1>'))
                    page = context.pages[0]
                    page.goto('https://hh.ru/vacancy/101')
                    assert page.evaluate("localStorage.getItem('uc-fixture')") == 'saved'
                finally:
                    browser_module.detach()
                    browser_module.close_context(context)
            print('UC offline integration passed: native cached vacancy/GPH reads, durable pre-submit pacing, CDP, native window geometry, exact resume/letter, request cookies, timezone, persistent profile, owned cleanup; no hh/API requests')
    finally:
        api.shutdown()
        api.server_close()


if __name__ == '__main__':
    main()
