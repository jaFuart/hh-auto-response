"""Offline Chromium fixture: observed GET route, exact resume selection and actual submitted letter.

Every browser request is intercepted and fulfilled locally. No hh connection or API is used.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import Error as PlaywrightError, sync_playwright
from hh_auto.core.config import Config
from hh_auto.hh import responder as module
from hh_auto.hh.responder import Responder, Status
from hh_auto.hh.search import Vacancy


VACANCY = """<!doctype html><meta charset="utf-8"><h1>Python fixture</h1>
<a data-qa="vacancy-response-link-top"
href="/applicant/vacancy_response?vacancyId=101&employerId=999"
onclick="throw Error('Never click the quick-apply handler')">Откликнуться</a>"""
FORM = """<!doctype html><meta charset="utf-8"><script>window.sent=[];</script>
<form id="RESPONSE_MODAL_FORM_ID">{resume}
<textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
<button type="button" data-qa="vacancy-response-submit-popup" onclick="
 const selected=document.querySelector('input[name=resumeId]:checked') || document.querySelector('input[name=resumeId]');
 window.sent.push({{resume:selected ? selected.value : '',letter:document.querySelector('textarea').value}});
 document.querySelector('form').remove();
 document.body.insertAdjacentHTML('beforeend','<p data-qa=vacancy-response-link-view-topic>Вы откликнулись</p>');
">Откликнуться</button></form>"""


def main():
    cfg = Config(search_url="", resume_url="https://hh.ru/resume/selected")
    cfg.letter.mode = "template"
    cfg.letter.template_text = "Разрабатываю Python API. Подтверждённый опыт помогает решать задачи команды."
    cfg.logs.snapshots = False
    vacancy = Vacancy("101", "Python", "Fixture", "Откликнуться")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True, chromium_sandbox=False)
        except PlaywrightError as error:
            if sys.platform != "win32" or "executable doesn't exist" not in str(error).casefold():
                raise
            browser = pw.chromium.launch(channel="chrome", headless=True, chromium_sandbox=False)
        context = browser.new_context()
        context.add_cookies([{"name": "hhrole", "value": "applicant", "url": "https://hh.ru"}])
        requests = []
        fixture = {"resume": '<input type="hidden" name="resumeId" value="selected">'}

        def intercept(route):
            request = route.request
            requests.append((request.method, urlparse(request.url).path))
            if request.method != "GET":
                raise AssertionError("The fixture must never make a real outbound request")
            path = urlparse(request.url).path
            if path == "/vacancy/101":
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=VACANCY)
            elif path == "/applicant/vacancy_response":
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FORM.format(resume=fixture["resume"]))
            else:
                route.abort()

        context.route("**/*", intercept)
        page = context.new_page()
        with patch.object(module.control, "sleep"), patch.object(Responder, "_pause"):
            responder = Responder(page, cfg, Path("unused"), "Fixture resume")
            result = responder.apply_on_vacancy(vacancy)
            assert result.status is Status.APPLIED and result.letter
            assert result.letter_text == cfg.letter.template_text
            assert page.evaluate("window.sent") == [{"resume": "selected", "letter": cfg.letter.template_text}]
            assert result.delivery["outbound_attempted"]

            fixture["resume"] = ('<label><input type="radio" name="resumeId" value="wrong" checked>Wrong</label>'
                                 '<label><input type="radio" name="resumeId" value="selected">Selected</label>')
            result = responder.apply_on_vacancy(vacancy)
            assert result.status is Status.APPLIED
            assert page.evaluate("window.sent[0].resume") == "selected"

            fixture["resume"] = '<span data-qa="resume-title">Same title without verified ID</span>'
            result = responder.apply_on_vacancy(vacancy)
            assert result.status is Status.FAILED and not result.delivery["outbound_attempted"]
            assert page.evaluate("window.sent") == []

        assert all(method == "GET" for method, _ in requests)
        context.close()
        browser.close()
    print("Offline targeted retry passed: GET only, exact selected ID, actual letter readback, ambiguous resume no send.")


if __name__ == "__main__":
    main()
