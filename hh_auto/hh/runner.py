"""Главный цикл: страницы выдачи → вакансии → отклик → пауза → следующая."""

from __future__ import annotations

import hashlib
import random
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page

from ..core import control
from ..ai.client import AiError
from ..web.browser import FatalError
from ..core.config import Config
from ..core.delivery import ApplicationDelivery, DeliveryError
from ..core.logger import log
from ..core.rate_limits import (WorkDeferred, check_daily_limit, enabled as pacing_enabled,
                                submission_pacer)
from .pacing import FILE as PACING_FILE, Pacer
from .responder import MOVED, Responder, Result, Status
from .search import (Vacancy, collect_vacancies, describe_hh_filters, fetch_vacancy_text, next_page_info, open_results_page,
                     results_page_url, search_urls, wait_all_cards)
from ..reports.results import ResultsBook
from ..core.state import State, company_key
from .vacancy_meta import SkillStats, skip_reason, unwanted
from .gph import check_gph


RESCANS = 2  # сколько раз дособрать вакансии, которые hh подвинул на текущую страницу
RECENT = 30  # сколько последних вакансий помнить для окна приложения
AI_SKIP = "ИИ-фильтр: не подходит под ваше описание"  # одна строка в итогах; причина по каждой вакансии — в Excel


@dataclass
class RunOptions:
    dry_run: bool = False
    limit: int | None = None
    start_page: int = 0
    retry_skipped: bool = False
    retry_only: bool = False  # только сохранённая очередь; без выдачи и новых вакансий


@dataclass
class Stats:
    applied: int = 0
    with_letter: int = 0
    failed: int = 0
    already: int = 0
    dry_run: int = 0
    hidden: int = 0  # нежелательные, скрытые на hh ([filters] hide_unwanted)
    skipped: Counter = field(default_factory=Counter)
    # последние обработанные вакансии — для окошка на странице «Отклики»: {id, at, title, company, url, status, note}
    recent: list = field(default_factory=list)

    def seen(self, vac: Vacancy, status: str, note: str = "") -> None:
        """status: applied, questions, skipped, failed, dry."""
        self.recent.append({"id": vac.id, "at": datetime.now().strftime("%H:%M"), "title": vac.title,
                            "company": vac.company, "url": vac.url, "status": status, "note": note})
        del self.recent[:-RECENT]


def run(
    page: Page,
    cfg: Config,
    opts: RunOptions,
    state: State,
    results: ResultsBook,
    stats: Stats,
    resume_title: str | None,
    snapshots_dir: Path,
    ai=None,
    resume_text: str = "",
    searches: list[tuple[str, str]] | None = None,
    skills: SkillStats | None = None,
    market=None,
) -> None:
    """searches — свои выдачи (подпись, ссылка) вместо выдач из настроек (так проверяются «компании мечты»);
    skills — куда считать ключевые навыки из выдачи (советы к резюме, [stats] skill_tips);
    market — куда записывать вакансии выдачи для анализа резюме (analysis.market.MarketStore)."""
    limits = cfg.limits
    max_responses = opts.limit or limits.max_responses
    log.info("")
    log.info("Шаг 3/3: отклики на вакансии" + ("  [DRY-RUN: только список, без кликов]" if opts.dry_run else ""))
    log.info(
        f"  лимит на этот запуск: {max_responses} откликов, пауза между вакансиями "
        f"{limits.delay_min:g}–{limits.delay_max:g} с, страниц выдачи максимум {limits.max_pages}"
    )
    searches = [] if opts.retry_only else (searches or search_urls(cfg))
    if searches:
        log.info(f"  фильтры hh: {describe_hh_filters(searches[0][1])}")
    else:
        log.info("  только ранее сохранённые попытки; поиск новых вакансий отключён")
    if limits.per_company:
        log.info(f"  в одну компанию — не больше {limits.per_company} откл. за {limits.per_company_days} дн. "
                 "([limits] per_company)")
    # Нейросеть для писем и анкет — только если вы их включали. Включённый [ai_filter] тоже создаёт
    # нейросеть, но письма от этого писать ей нельзя: резюме для фильтра даже не загружается.
    responder_ai = ai if (cfg.ai.enabled or cfg.letter.mode == "ai") else None
    responder = Responder(page, cfg, snapshots_dir, resume_title, responder_ai, resume_text,
                          recent_letters=results.recent_letters(10))
    resume_id = cfg.resume_id or "(auto)"
    try:
        delivery = None if opts.dry_run else ApplicationDelivery(state.path.parent / "application-delivery.json")
    except DeliveryError as error:
        raise FatalError(str(error)) from error
    # после капчи — серии откликов с перерывами; пробный прогон ничего не отправляет, ему они не нужны
    durable_pacing = pacing_enabled(cfg)
    pacer = Pacer(limits if not opts.dry_run and not durable_pacing else replace(limits, captcha_rest=0),
                  state.path.parent / PACING_FILE)
    errors_in_row = 0
    filters_warned = False
    criteria_key = _criteria_key(cfg)
    handled: set[str] = set()  # вакансии этого запуска: в выдаче другого запроса их второй раз не смотрим
    planned: Counter = Counter()  # пробный прогон: «откликнулись бы» по компаниям — для лимита per_company

    def cancel_queued(vac: Vacancy, reason: str) -> None:
        if delivery:
            row = delivery.get(resume_id, vac.id)
            if row and row["status"] not in {"confirmed", "cancelled", "exhausted"}:
                delivery.cancel(resume_id, vac.id, reason)

    def recover(vac: Vacancy, row: dict) -> None:
        """A previous delivery was found; it is history, not a new send in this run."""
        state.mark_applied(vac, False)
        stats.already += 1
        stats.seen(vac, "applied", "ранее отправленный отклик подтверждён на hh; письмо неизвестно")
        records = results.records()
        recorded = isinstance(records, list) and any(str(record.get("id")) == vac.id and
            record.get("category") == "applied" for record in records if isinstance(record, dict))
        if not recorded:
            results.add(vac, Result(Status.APPLIED,
                "ранее отправленный отклик подтверждён на hh; наличие письма неизвестно",
                delivery={"status": "confirmed", "reconciled": True, "outbound_attempted": False,
                          "idempotency_key": row["idempotency_key"], "queue_status": row["status"],
                          "retry_count": max(0, row["attempts"] - 1)}))

    def queued_ready(vac: Vacancy, *, retry: bool) -> bool:
        """An ambiguous claim may only be reconciled, never resent on the same check."""
        if not delivery:
            return True
        row = delivery.get(resume_id, vac.id)
        if row is None:
            return not retry
        if row["status"] == "confirmed":
            recover(vac, row)
            return False
        if row["status"] in {"cancelled", "exhausted"}:
            log.info(f"  очередь: {row['status']} — повтор запрещён ({row.get('reason', '')})")
            return False
        due = {item["idempotency_key"] for item in delivery.due()}
        if row["idempotency_key"] not in due:
            log.info("  очередь: следующая попытка ещё не наступила")
            return False
        if retry or row["status"] in {"unknown", "in_flight"}:
            origin = page.url
            status = responder.application_status(vac)
            if status == "applied":
                recover(vac, delivery.reconcile(resume_id, vac.id, status))
                return False
            if status == "unknown" or row["status"] in {"unknown", "in_flight"}:
                delivery.reconcile(resume_id, vac.id, status,
                    reason="проверка hh перед повтором: " + status)
                log.info("  очередь: проверка сохранена; возможный повтор после паузы, без клика сейчас")
                return False
            # A safe pending attempt was explicitly verified as not_applied. It can now reach the filters/claim.
            if not retry:
                responder.back_to_results(origin)
        return True

    def hide(vac: Vacancy) -> None:
        """Нежелательную вакансию ещё и скрыть на hh, чтобы она больше не попадалась в выдаче."""
        if not cfg.filters.hide_unwanted or opts.dry_run:
            return
        if responder.hide(vac):
            stats.hidden += 1
            log.info("  скрыл на hh — в выдаче больше не появится")
        else:
            log.info("  скрыть на hh не получилось — просто пропущена")

    def handle(vac: Vacancy, tag: str, url: str, label: str) -> bool:
        """Одна вакансия: фильтры → отклик → итог. False — запуск пора заканчивать."""
        nonlocal errors_in_row
        if vac.id in handled:
            log.debug(f"{tag} «{vac.title}» — уже была в этом запуске")
            return True
        handled.add(vac.id)
        if delivery:
            previous = delivery.get(resume_id, vac.id)
            if previous and previous['status'] in {'cancelled', 'exhausted'}:
                stats.already += 1
                log.debug(f"{tag} очередь: {previous['status']} — повтор запрещён, без чтения вакансии")
                return True
        seen = state.seen(vac.id, opts.retry_skipped)
        if seen and _ai_skip_outdated(state, vac.id, cfg, criteria_key):
            seen = None  # отказ ИИ-фильтра по старому описанию (или фильтр выключили) — смотрим заново
        if seen:
            stats.already += 1
            log.debug(f"{tag} «{vac.title}» — {seen}")
            if delivery and delivery.get(resume_id, vac.id):
                if vac.id in state.applied:
                    delivery.reconcile(resume_id, vac.id, "applied", reason=seen)
                else:
                    cancel_queued(vac, seen)
            return True
        if not vac.can_respond:
            stats.already += 1
            log.info(f"{tag} «{vac.title}» — кнопки «Откликнуться» нет ({vac.button_text or 'уже откликались'}), пропускаю")
            if delivery and delivery.get(resume_id, vac.id):
                queued_ready(vac, retry=opts.retry_only)
            return True

        excluded = _find_word(vac.title, cfg.filters.exclude_title_words)
        if excluded:
            stats.skipped[f"в названии «{excluded}»"] += 1
            stats.seen(vac, "skipped", f"в названии «{excluded}»")
            log.info(f"{tag} «{vac.title}» — в названии «{excluded}» ([filters]), пропускаю")
            hide(vac)
            cancel_queued(vac, f"в названии «{excluded}»")
            return True
        company_word = _find_word(vac.company, cfg.filters.exclude_company_words)
        if company_word:
            stats.skipped[f"компания: «{company_word}»"] += 1
            stats.seen(vac, "skipped", f"компания: «{company_word}»")
            log.info(f"{tag} «{vac.title}» — компания «{vac.company}» содержит «{company_word}» ([filters]), пропускаю")
            hide(vac)
            cancel_queued(vac, f"компания: «{company_word}»")
            return True
        if cfg.filters.include_company_words and not _find_word(vac.company, cfg.filters.include_company_words):
            stats.skipped["компания не из списка ([filters] include_company_words)"] += 1
            stats.seen(vac, "skipped", "компания не из списка")
            log.info(f"{tag} «{vac.title}» — компания «{vac.company}» не из списка include_company_words, пропускаю")
            hide(vac)
            cancel_queued(vac, "компания не из текущего списка")
            return True
        if cfg.filters.include_words:
            where = f"{vac.title} {vac.snippet}" if cfg.filters.include_in_snippet else vac.title
            if not _find_word(where, cfg.filters.include_words):
                stats.skipped["нет нужных слов ([filters] include_words)"] += 1
                stats.seen(vac, "skipped", "нет нужных слов")
                log.info(f"{tag} «{vac.title}» — нет ни одного слова из [filters] include_words, пропускаю")
                hide(vac)
                cancel_queued(vac, "нет нужных слов текущего фильтра")
                return True
        skip = skip_reason(vac.title, vac.snippet, vac.meta, cfg)  # уровень, данные hh, вопросы при отклике
        if skip:
            stats.skipped[skip[0]] += 1
            stats.seen(vac, "skipped", skip[1])
            log.info(f"{tag} «{vac.title}» — {skip[1]}, пропускаю")
            if unwanted(skip[0]):
                hide(vac)
            cancel_queued(vac, skip[1])
            return True
        if limits.per_company and vac.company:
            sent = state.company_applied(vac.company, limits.per_company_days) + planned[company_key(vac.company)]
            if delivery:
                sent += sum(1 for row in delivery.snapshot() if row.get('delivery_uncertain')
                            and row['vacancy_id'] != vac.id and row['vacancy_id'] not in state.applied
                            and company_key(row.get('vacancy', {}).get('company', '')) == company_key(vac.company))
            if sent >= limits.per_company:
                stats.skipped["лимит откликов в одну компанию ([limits] per_company)"] += 1
                stats.seen(vac, "skipped", "лимит откликов в компанию")
                log.info(f"{tag} «{vac.title}» — в «{vac.company}» уже {sent} откл. за {limits.per_company_days} дн. "
                         "([limits] per_company), пропускаю")
                cancel_queued(vac, "лимит откликов в одну компанию")
                return True
        if cfg.filters.exclude_description_words:
            excluded, checked = _description_exclusion(page, vac, cfg.filters.exclude_description_words)
            if excluded or not checked:
                reason = (f"в тексте вакансии «{excluded}»" if excluded
                          else "не удалось проверить исключения: полное описание вакансии недоступно")
                stats.skipped[reason] += 1
                stats.seen(vac, "skipped", reason)
                log.info(f"{tag} «{vac.title}» — {reason} ([filters] exclude_description_words), пропускаю")
                if excluded:
                    hide(vac)
                    cancel_queued(vac, reason)
                return True
        if cfg.filters.require_gph:
            contract = check_gph(page, vac)
            if not (contract.allowed or cfg.filters.gph_allow_unspecified and contract.unspecified):
                reason = "ГПХ не подтверждён ([filters] require_gph)"
                stats.skipped[reason] += 1
                stats.seen(vac, "skipped", contract.reason)
                log.info(f"{tag} «{vac.title}» — {contract.reason}, пропускаю")
                # Не запоминаем и не скрываем: hh может временно не отдать условия оформления.
                cancel_queued(vac, contract.reason)
                return True
            log.info(f"{tag} оформление прошло фильтр: {contract.reason}")
        if cfg.ai_filter.enabled:
            ai_reason, rejected = _ai_skip_reason(cfg, ai, vac, tag)
            if ai_reason:
                stats.skipped[AI_SKIP if rejected else ai_reason] += 1
                stats.seen(vac, "skipped", ai_reason)
                log.info(f"{tag} «{vac.title}» — {ai_reason}, пропускаю")
                if rejected and not opts.dry_run:
                    # запоминаем: в следующих запусках не спрашиваем нейросеть снова (токены и время), а в
                    # таблице на листе «Пропущенные» видно, что и почему отсеяно, — по ней удобно править описание
                    state.mark_skipped(vac, f"{AI_SKIP} [{criteria_key}]: {ai_reason}")
                    results.add(vac, Result(Status.SKIPPED, ai_reason, remember=True))
                if rejected:  # сбой нейросети — не повод скрывать
                    hide(vac)
                    cancel_queued(vac, ai_reason)
                return True

        log.info("")
        log.info(f"{tag} ▶ «{vac.title}» — {vac.company}")
        log.info(f"  {vac.url}")
        if opts.dry_run:
            stats.dry_run += 1
            stats.seen(vac, "dry")
            planned[company_key(vac.company)] += 1
            log.info("  [dry-run] здесь был бы отклик")
            return True

        if durable_pacing:
            status = submission_pacer(cfg, state.path.parent.parent).status()
            if status.wait_seconds:
                raise WorkDeferred(status)
        check_daily_limit(cfg, state.path.parent.parent)
        if not queued_ready(vac, retry=opts.retry_only):
            if not opts.retry_only and page.url != url:
                responder.back_to_results(url)
            return True
        if delivery and not delivery.claim(resume_id, vac.id, vacancy=_saved_vacancy(vac)):
            log.info("  очередь: попытка уже началась или повтор запрещён — без клика")
            return True
        try:
            result = responder.apply_on_vacancy(vac) if opts.retry_only or durable_pacing else responder.apply(vac)
        except WorkDeferred:
            if delivery:
                delivery.record_result(resume_id, vac.id, confirmed=False, outbound_attempted=responder.clicked,
                                       reason='отправка отложена ограничителем частоты')
            raise
        if delivery:
            attempted = bool((result.delivery or {}).get("outbound_attempted", responder.clicked))
            row = delivery.record_result(resume_id, vac.id, confirmed=result.status is Status.APPLIED,
                                         outbound_attempted=attempted, reason=result.reason)
            if result.status is Status.SKIPPED and result.reason != MOVED:
                row = delivery.cancel(resume_id, vac.id, result.reason)
            result.delivery = {**(result.delivery or {}), "idempotency_key": row["idempotency_key"],
                               "retry_count": max(0, row["attempts"] - 1), "queue_status": row["status"]}
        if result.reason != MOVED:  # вакансию ещё не трогали — в таблицу её не пишем
            # для воронки на вкладке «Статистика»: из какого поиска, какое письмо, совпадение по навыкам
            results.add(vac, result, {"query": label, "letter_kind": responder.letter_kind if result.letter else "none",
                                      "match": vac.meta.match if vac.meta else None})
            stats.seen(vac, {Status.APPLIED: "applied", Status.SKIPPED: "questions" if result.questions else "skipped"}
                       .get(result.status, "failed"),
                       "с письмом" if result.status is Status.APPLIED and result.letter else
                       "лимит hh" if result.status is Status.LIMIT else result.reason)
        else:
            handled.discard(vac.id)  # hh увёз её на другую страницу — встретим там и обработаем
        if result.status is Status.APPLIED:
            reconciled = bool((result.delivery or {}).get("reconciled"))
            if reconciled:
                stats.already += 1
            else:
                pacer.sent()
                stats.applied += 1
                stats.with_letter += result.letter
            state.mark_applied(vac, result.letter)
            if result.reason:
                (log.info if result.letter else log.warning)(f"  {'' if result.letter else '! '}{result.reason}")
            log.info(f"  итого отправлено откликов: {stats.applied} из {max_responses}")
        elif result.status is Status.SKIPPED:
            stats.skipped[result.reason] += 1
            if result.remember:
                state.mark_skipped(vac, result.reason)
            (log.info if result.reason == MOVED else log.warning)(f"  ⤼ вакансия пропущена: {result.reason}")
        elif result.status is Status.FAILED:
            stats.failed += 1
            log.error(f"  ✗ не удалось откликнуться: {result.reason}")
            if durable_pacing and (result.delivery or {}).get('outbound_attempted'):
                raise FatalError('статус отправки неизвестен; требуется отдельная проверка hh перед продолжением')
        else:  # Status.LIMIT
            log.error("hh сообщил, что лимит откликов исчерпан — останавливаюсь. Попробуйте через сутки.")
            return False

        errors_in_row = errors_in_row + 1 if result.status is Status.FAILED else 0
        if errors_in_row >= limits.max_consecutive_errors:
            raise FatalError(
                f"{errors_in_row} ошибок подряд — похоже, hh поменял вёрстку или слетела сессия. "
                + (f"Скриншоты и HTML страниц: {snapshots_dir}" if cfg.logs.snapshots else
                   "Включите снимки страниц ([logs] snapshots), чтобы было по чему разобраться")
            )
        if stats.applied >= max_responses:
            log.success(f"Достигнут лимит этого запуска: {max_responses} откликов")
            return False

        if not opts.retry_only:
            responder.back_to_results(url)
        if pacer.rest_if_due():  # была капча или серия откликов набрана — перерыв вместо обычной паузы
            pass
        elif responder.clicked:
            _sleep(limits.delay_min, limits.delay_max, "перед следующей вакансией")
        else:  # в hh ничего не нажимали (вакансия уехала, кнопки уже нет) — паузе 6–15 с нечего разделять
            log.debug("  в hh ничего не нажимали — без паузы")
        return True

    def walk(label: str, search_url: str, start_page: int) -> bool:
        """Одна выдача по страницам. False — запуск закончен (лимит откликов, лимит hh)."""
        nonlocal filters_warned
        for page_num in range(start_page, limits.max_pages):
            url = results_page_url(search_url, page_num)
            log.info("")
            log.info(f"━━━━━━━━━━ Страница выдачи №{page_num + 1} ━━━━━━━━━━")
            vacancies = open_results_page(page, url)
            if not vacancies:
                log.info("  вакансий на странице нет — выдача закончилась")
                return True
            if skills is not None:
                skills.record([v.meta for v in vacancies if v.meta])
            if market is not None:
                try:
                    market.record([v.meta for v in vacancies if v.meta], label)
                except OSError as e:  # анализ резюме не важнее откликов
                    log.debug(f"  вакансии для анализа резюме не записались: {e}")
            can_respond = sum(v.can_respond for v in vacancies)
            done_before = sum(1 for v in vacancies if state.seen(v.id, opts.retry_skipped))
            log.success(
                f"  ✓ подтянуто вакансий: {len(vacancies)}, с кнопкой «Откликнуться»: {can_respond}, "
                f"обработаны в прошлых запусках: {done_before}"
            )

            skipped_before = sum(stats.skipped.values())
            on_page = {v.id for v in vacancies}
            batch = vacancies
            for rescan in range(RESCANS + 1):
                for i, vac in enumerate(batch, 1):
                    if not handle(vac, f"[стр. {page_num + 1}, {i}/{len(batch)}{'+' if rescan else ''}]", url, label):
                        return False
                # После перезагрузки выдачи hh заново сортирует «подходящие к резюме»: часть вакансий уезжает
                # на другие страницы, а на эту приезжают новые. Их проходим сразу, не дожидаясь следующего запуска.
                if opts.dry_run or rescan == RESCANS or not responder.on_search_page():
                    break
                wait_all_cards(page)
                batch = [v for v in collect_vacancies(page) if v.id not in on_page]
                on_page.update(v.id for v in batch)
                fresh = [v for v in batch if v.can_respond and not state.seen(v.id, opts.retry_skipped)]
                if not fresh:
                    break
                log.info("")
                log.info(f"  hh пересортировал выдачу: на этой странице появилось ещё {len(fresh)} новых вакансий — прохожу их")

            skipped_on_page = sum(stats.skipped.values()) - skipped_before
            if skipped_on_page >= len(on_page) * 0.8 and not filters_warned:
                filters_warned = True  # одного раза за запуск достаточно
                log.warning(f"  ! на этой странице фильтры отсеяли {skipped_on_page} из {len(on_page)} вакансий — "
                            "если откликов мало, проверьте [filters] (особенно «только со словами»)")
            has_next, why = next_page_info(page, page_num)
            if not has_next:
                log.info(f"  выдача закончилась: {why}")
                return True
            log.info(f"  дальше: {why}")
            _sleep(limits.page_delay_min, limits.page_delay_max, "перед следующей страницей выдачи")

        log.info(f"Пройдено максимальное число страниц ([limits] max_pages = {limits.max_pages})")
        return True

    if opts.retry_only:
        rows = [] if delivery is None else [row for row in delivery.due() if row["resume_id"] == resume_id]
        for index, row in enumerate(rows, 1):
            control.check()
            vac = _restored_vacancy(row)
            if not handle(vac, f"[повтор {index}/{len(rows)}]", vac.url, "очередь технических повторов"):
                return
        return

    for number, (label, search_url) in enumerate(searches, 1):
        if len(searches) > 1:
            log.info("")
            log.info(f"══════════ Поиск {number} из {len(searches)}: {label} ══════════")
        if not walk(label, search_url, opts.start_page if number == 1 else 0):
            return
        if number < len(searches):
            _sleep(limits.page_delay_min, limits.page_delay_max, "перед следующим запросом")


def log_summary(stats: Stats, elapsed: float) -> None:
    minutes, seconds = divmod(int(elapsed), 60)
    log.info("")
    log.info("══════════════ Итоги запуска ══════════════")
    log.success(f"  откликов отправлено:        {stats.applied} (из них с письмом: {stats.with_letter})")
    if stats.dry_run:
        log.info(f"  [dry-run] вакансий к отклику: {stats.dry_run}")
    log.info(f"  пропущено в этом запуске:   {sum(stats.skipped.values())}" + (f" (из них скрыто на hh: {stats.hidden})" if stats.hidden else ""))
    for reason, count in stats.skipped.most_common():
        log.info(f"      {count} × {reason}")
    log.info(f"  уже откликались / ранее:    {stats.already}")
    (log.error if stats.failed else log.info)(f"  ошибок:                     {stats.failed}")
    log.info(f"  время работы:               {minutes} мин {seconds} с")


def _find_word(title: str, words: list[str]) -> str | None:
    lowered = title.lower()
    return next((w for w in words if w.lower() in lowered), None)


_CRM_PATTERNS = {
    "битрикс24": r"(?<!\w)(?:битрикс|bitrix)[\s.\-‐‑–—_]*24(?!\w)",
    "bitrix24": r"(?<!\w)(?:битрикс|bitrix)[\s.\-‐‑–—_]*24(?!\w)",
    "amocrm": r"(?<!\w)(?:amo|амо)[\s.\-‐‑–—_]*(?:crm|црм)(?!\w)",
    "амоcrm": r"(?<!\w)(?:amo|амо)[\s.\-‐‑–—_]*(?:crm|црм)(?!\w)",
    "амоцрм": r"(?<!\w)(?:amo|амо)[\s.\-‐‑–—_]*(?:crm|црм)(?!\w)",
}


def _find_description_word(text: str, words: list[str]) -> str | None:
    """Названия CRM учитывают варианты написания; остальные исключения — подстроки без учёта регистра."""
    lowered = text.casefold()
    for word in words:
        key = re.sub(r"[\s.\-‐‑–—_]+", "", word.casefold())
        pattern = _CRM_PATTERNS.get(key)
        if (re.search(pattern, lowered) if pattern else word.casefold() in lowered):
            return word
    return None


def _description_exclusion(page: Page, vac: Vacancy, words: list[str]) -> tuple[str | None, bool]:
    """Возвращает совпавшее исключение и признак полной проверки; пустой сниппет не заменяет описание."""
    excluded = _find_description_word(f"{vac.title}\n{vac.snippet}", words)
    if excluded:
        return excluded, True
    description = fetch_vacancy_text(page, vac, limit=None, require_description=True)
    if not description.strip():
        return None, False
    return _find_description_word(description, words), True


def _saved_vacancy(vac: Vacancy) -> dict:
    return {"id": vac.id, "title": vac.title, "company": vac.company, "button_text": vac.button_text,
            "remote": vac.remote, "snippet": vac.snippet}


def _restored_vacancy(row: dict) -> Vacancy:
    raw = row.get("vacancy", {})
    return Vacancy(str(row["vacancy_id"]), str(raw.get("title", "")), str(raw.get("company", "")),
                   str(raw.get("button_text", "Откликнуться")), bool(raw.get("remote", False)),
                   str(raw.get("snippet", "")))


def _criteria_key(cfg: Config) -> str:
    """Отпечаток описания из [ai_filter]: поменяли описание — старые отказы фильтра пересматриваются."""
    text = " ".join(cfg.ai_filter.criteria.lower().split())
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _ai_skip_outdated(state: State, vacancy_id: str, cfg: Config, key: str) -> bool:
    """Вакансию раньше отсеял ИИ-фильтр, но фильтр с тех пор выключили или поменяли описание."""
    entry = state.skipped.get(vacancy_id)
    if not entry or not str(entry.get("reason", "")).startswith(AI_SKIP):
        return False
    return not cfg.ai_filter.enabled or f"[{key}]" not in entry["reason"]


def _ai_skip_reason(cfg: Config, ai, vac: Vacancy, tag: str) -> tuple[str, bool]:
    """Спросить нейросеть, интересна ли вакансия ([ai_filter]). Возвращает (причина пропуска или '',
    отказ ли это нейросети). '' — вакансию обрабатываем как обычно. При сбое ИИ решает [ai_filter] skip_on_error;
    такой пропуск не запоминается — в следующий раз спросим снова."""
    if ai is None:
        log.warning(f"{tag} [ai_filter] включён, но нейросеть недоступна — пропускаю проверку")
        return ("ИИ: не удалось проверить (нейросеть недоступна)" if cfg.ai_filter.skip_on_error else ""), False
    log.info(f"{tag} нейросеть оценивает вакансию…")
    try:
        verdict = ai.evaluate_vacancy(cfg.ai_filter.criteria, vac.title, vac.company, vac.snippet,
                                      cfg.ai_filter.max_snippet_chars, remote=vac.remote)
    except AiError as e:
        log.warning(f"{tag} нейросеть не оценила вакансию: {e}")
        return ("ИИ: не удалось проверить (ошибка нейросети)" if cfg.ai_filter.skip_on_error else ""), False
    if verdict.interesting:
        log.success(f"{tag} нейросеть: интересна" + (f" — {verdict.reason}" if verdict.reason else ""))
        return "", False
    return "ИИ: не интересна" + (f" — {verdict.reason}" if verdict.reason else ""), True


def _sleep(low: float, high: float, what: str) -> None:
    from ..web.browser import slowdown

    delay = random.uniform(low, high) * slowdown()  # после капчи — дольше
    log.info(f"  ⏸ пауза {delay:.1f} с {what}")
    control.sleep(delay)
