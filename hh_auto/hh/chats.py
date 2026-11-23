"""Чаты с работодателями: отказы читаем, анкеты «Робота-рекрутера» заполняем через ИИ.

Обычно идём по непрочитанным чатам; include_read=True проверяет также прочитанные чаты с вопросами:
  • последнее сообщение «Отказ» → открываем, чтобы hh отметил его прочитанным;
  • если включён ИИ ([ai] enabled) — открываем чат и отдаём переписку нейросети:
      - вопрос анкеты робота → отправляем короткий ответ, ждём следующий вопрос и так по кругу;
      - уведомление без вопроса («мы рассмотрим ваше резюме») → просто прочитано;
      - живой человек, приглашение, документы и т.п. → оставляем вам (список в итогах);
  • без ИИ остальные непрочитанные не трогаем — только выводим списком.
Заодно по списку чатов узнаём, что стало с откликами (приглашение, отказ), — для воронки на вкладке
«Статистика» и уведомления о приглашении (negotiations.py). Раз в сутки для воронки просматриваются
и прочитанные чаты.

Список чатов hh догружает порциями, когда его докрутили до конца, а новые чаты появляются сверху: дойдя
до конца, ждём догрузку, а в конце один раз проходим список заново. Чат, где робот прислал следующий
вопрос уже после того, как мы из него ушли, открываем снова в этом же запуске.
"""

from __future__ import annotations

import io
import json
import hashlib
import math
import os
import random
import re
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Page
from playwright.sync_api import Error as PlaywrightError

from ..core import control
from ..web import selectors as S
from ..web import browser
from ..ai.client import AiClient, AiError, is_done_message, norm, ours_is_last, review
from ..web.auth import hh_role, is_auth_url
from ..web.browser import FatalError, slowdown, wait_captcha
from ..core.config import Config
from ..core.logger import log
from .negotiations import Negotiations
from .resume import fetch_resume_text
from .chat_dates import ROBOT_CHAT_DAYS, chat_date_status

_COLLECT_JS = """
(s) => Array.from(document.querySelectorAll(s.cell)).map((cell) => {
  const text = (el) => (el?.textContent || '').replace(/\\s+/g, ' ').trim();
  const last = cell.querySelector(s.last);
  const date = cell.querySelector('[data-qa="chat-cell-creation-time"]');
  return {
    id: cell.getAttribute('data-qa').replace('chatik-open-chat-', ''),
    url: cell.href || '',
    title: text(cell.querySelector(s.title)),
    company: text(cell.querySelector(s.subtitle)),
    last: text(last),
    unread: !!cell.querySelector(s.badge),
    color: last ? String(last.className || '') : '',  // «Отказ» hh выделяет красным, приглашение — зелёным
    latest_at: date ? (date.getAttribute('datetime') || date.getAttribute('data-timestamp') ||
      date.getAttribute('title') || text(date)) : '',
  };
})
"""

# Список чатов — в начало (после просмотра всех чатов для воронки)
_SCROLL_TOP_JS = """
(cellSelector) => {
  let el = document.querySelector(cellSelector);
  while (el && el !== document.body) {
    const style = getComputedStyle(el);
    if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight) break;
    el = el.parentElement;
  }
  (el && el !== document.body ? el : document.scrollingElement).scrollTop = 0;
}
"""

# Прокручивает ближайший прокручиваемый контейнер списка чатов (список виртуальный:
# в DOM только видимые строки). Возвращает false, когда дальше крутить некуда.
_SCROLL_JS = """
(cellSelector) => {
  let el = document.querySelector(cellSelector);
  while (el && el !== document.body) {
    const style = getComputedStyle(el);
    if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight) break;
    el = el.parentElement;
  }
  const target = el && el !== document.body ? el : document.scrollingElement;
  const before = target.scrollTop;
  target.scrollTop += target.clientHeight * 0.8;
  return target.scrollTop !== before;
}
"""

# Текст открытого чата: поднимаемся от поля ввода до самого большого блока, в котором ещё
# нет списка чатов, — это панель переписки (шапка с вакансией + сообщения). Блок ввода
# (поле + кнопки «Отправить», вложения) вырезаем, чтобы нейросеть видела только сообщения.
_TRANSCRIPT_JS = """
([inputSelector, cellSelector]) => {
  const input = Array.from(document.querySelectorAll(inputSelector)).find((el) => el.offsetParent !== null);
  if (!input) return null;
  let composer = input;
  while (composer.parentElement && !composer.parentElement.querySelector('button')) composer = composer.parentElement;
  if (composer.parentElement) composer = composer.parentElement;
  let el = input, best = input;
  while (el.parentElement && el.parentElement !== document.body) {
    el = el.parentElement;
    if (el.querySelector(cellSelector)) break;
    best = el;
  }
  let text = best.innerText;
  const tail = composer !== best ? composer.innerText.trim() : '';
  if (tail) {
    const at = text.lastIndexOf(tail);
    if (at >= 0) text = text.slice(0, at) + text.slice(at + tail.length);
  }
  // и на всякий случай срезаем с конца строки, совпадающие с подписями кнопок и полей
  const ui = new Set(Array.from(best.querySelectorAll('button, [role="button"], label'))
    .map((el) => el.innerText.trim()).filter(Boolean));
  const lines = text.split(String.fromCharCode(10));
  while (lines.length && (!lines[lines.length - 1].trim() || ui.has(lines[lines.length - 1].trim()))) lines.pop();
  return lines.join(String.fromCharCode(10)).trim();
}
"""


# Только подпись отправителя/участника из интерфейса hh. Текст сообщения и старая переписка не являются
# доказательством, что сейчас пишет робот: человек может упомянуть «Робота-рекрутера» в своём сообщении.
_SENDER_JS = """
([inputSelector, cellSelector]) => {
  const visible = (el) => el && el.offsetParent !== null;
  const input = Array.from(document.querySelectorAll(inputSelector)).find(visible);
  if (!input) return null;
  let el = input, panel = input;
  while (el.parentElement && el.parentElement !== document.body) {
    el = el.parentElement;
    if (el.querySelector(cellSelector)) break;
    panel = el;
  }
  const authorSelector = '[data-qa="chatik-message-author"], [data-qa="chatik-message-sender"], ' +
    '[data-qa="chat-message-author"], [data-qa="chat-message-sender"], ' +
    '[data-qa="message-author"], [data-qa="message-sender"], [data-qa="chat-bubble-label"]';
  const messageSelector = '[data-qa="chatik-message"], [data-qa="chatik-message-container"], ' +
    '[data-qa="chat-message"], [data-qa="chat-message-container"], [data-message-id]';
  // Текущая вёрстка hh: обёртка сообщения с числовым ID, внутри — bubble (его за сообщение не считаем).
  let messages = Array.from(panel.querySelectorAll('[data-qa^="chatik-chat-message-"]')).filter(
    (node) => visible(node) && /^chatik-chat-message-[0-9]+$/.test(node.getAttribute('data-qa') || ''));
  if (!messages.length) messages = Array.from(panel.querySelectorAll(messageSelector)).filter(visible);
  const last = messages[messages.length - 1];
  if (!last) return null;
  const ownSelector = '[class*="message-my--"], [class*="message-text-outgoing--"]';
  const botSelector = '[class*="magritte-bot___"]';
  const outgoing = last.matches(ownSelector) || !!last.querySelector(ownSelector);
  const robot = !outgoing && (last.matches(botSelector) || !!last.querySelector(botSelector));
  const author = Array.from(last.querySelectorAll(authorSelector)).filter(visible).pop();
  const name = (author && (author.innerText || author.getAttribute('aria-label')) ||
    (outgoing ? 'Вы' : robot ? 'Робот hh' : 'работодатель')).trim();
  if (!name || name.length > 160) return null;
  const qa = last.getAttribute('data-qa') || '';
  return {author: name, provider_robot: robot, outgoing,
    message_id: last.getAttribute('data-message-id') || last.id ||
      (/^chatik-chat-message-[0-9]+$/.test(qa) ? qa : '')};
}
"""


# Кнопки быстрого ответа в открытом чате («Да» / «Нет», «Интересно», ответ на приглашение hh…).
# Берём видимые кнопки с текстом в области переписки, кроме шапки чата.
_BUTTONS_JS = """
([inputSelector, cellSelector, sendSelector]) => {
  const input = Array.from(document.querySelectorAll(inputSelector)).find((el) => el.offsetParent !== null);
  if (!input) return [];
  let el = input, best = input;
  while (el.parentElement && el.parentElement !== document.body) {
    el = el.parentElement;
    if (el.querySelector(cellSelector)) break;
    best = el;
  }
  // строка ввода: ближайший к полю блок с кнопками — «Отправить», «Прикрепить» — это не ответы
  let row = input;
  while (row.parentElement && !row.parentElement.querySelector('button')) row = row.parentElement;
  row = row.parentElement || row;
  const tools = /^(отправить|прикрепить|send|attach|загрузить|файл|фото|эмодзи|emoji)$/i;
  const top = best.getBoundingClientRect().top;
  const seen = new Set(), out = [];
  for (const b of best.querySelectorAll('button, [role="button"]')) {
    if (!b.offsetParent || b.contains(input) || row.contains(b)) continue;
    if (b.matches(sendSelector) || tools.test((b.innerText || '').trim())) continue;
    const text = (b.innerText || '').trim();
    if (!text || text.length > 80 || text.includes(String.fromCharCode(10)) || seen.has(text)) continue;
    if (b.getBoundingClientRect().top < top + 90) continue;  // шапка чата: вакансия, меню
    seen.add(text);
    out.push(text);
  }
  return out;
}
"""


@dataclass
class ChatStats:
    opened: int = 0  # прочитано отказов
    info_read: int = 0  # прочитано уведомлений без вопроса (с ИИ)
    answered: int = 0  # отправлено ответов ИИ
    questionnaires: int = 0  # чатов, где ИИ отвечал на анкету
    failed: int = 0
    rejections_read: int = 0  # отказы, прочитанные ещё до запуска
    other_unread: list[str] = field(default_factory=list)  # нужен ваш ответ
    invitations: list[str] = field(default_factory=list)  # новые приглашения: «вакансия» — компания
    checked: int = 0  # попытки открыть чат при ограниченном фоновом обходе
    deferred: int = 0  # подходящие чаты, оставленные в очереди следующего обхода


@dataclass
class _Chat:
    id: str
    title: str
    company: str
    last: str
    unread: bool
    color: str = ""
    badge: bool = False  # у чата счётчик непрочитанных (unread в списке «Только непрочитанные» — у всех)
    latest_at: str = ""  # дата последнего сообщения из списка hh, а не дата отклика
    url: str = ""  # ссылка, полученная из строки hh; нужна для виртуального списка


def _chat_age_reason(chat: _Chat) -> str:
    status = chat_date_status(chat.latest_at, days=ROBOT_CHAT_DAYS)
    if status == "recent":
        return ""
    if status == "old":
        return f"последнее сообщение старше {ROBOT_CHAT_DAYS} дней"
    return "не удалось определить дату последнего сообщения"


@dataclass(frozen=True)
class _Sender:
    author: str
    message_id: str = ""
    provider_robot: bool = False
    outgoing: bool = False


class _SendNotAttempted(Exception):
    """Не дошли до кнопки/Enter отправки: после исправления поля ответ можно безопасно попробовать снова."""


def _current_sender(page: Page) -> _Sender | None:
    try:
        found = page.evaluate(_SENDER_JS, [S.CHAT_INPUT, S.CHAT_CELL])
    except PlaywrightError:
        return None
    if not isinstance(found, dict) or not isinstance(found.get("author"), str):
        return None
    author = found["author"].strip()
    return _Sender(author, str(found.get("message_id") or ""), found.get("provider_robot") is True,
                   found.get("outgoing") is True) if author else None


def _robot_sender(sender: _Sender | None, markers: list[str]) -> bool:
    return sender is not None and not sender.outgoing and (sender.provider_robot or any(
        norm(marker) in norm(sender.author) for marker in markers if marker.strip()))


LIST_LOAD_WAIT = 3.0  # с: докрутили список чатов до конца — столько ждём, не догрузит ли hh ещё
ROBOT_SCAN_FILE = "robot-chat-scan.json"
_SCAN_LOCKS: dict[str, threading.RLock] = {}


class _RobotScanState:
    """Очередь осмотров, а не кэш ответов: даже неизменившаяся строка снова дойдёт до своей очереди."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._thread_lock = _SCAN_LOCKS.setdefault(str(self.path.resolve()), threading.RLock())

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            stream, locked = None, False
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(self.path.with_name(self.path.name + ".lock"),
                                     os.O_CREAT | os.O_RDWR, 0o600)
                stream = os.fdopen(descriptor, "r+b")
                if os.name == "nt":
                    import msvcrt

                    if not stream.read(1):
                        stream.write(b"\0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                yield
            except OSError as error:
                raise FatalError("не удалось заблокировать или сохранить очередь чатов — обход остановлен") from error
            finally:
                if stream is not None:
                    if locked:
                        with suppress(OSError):
                            if os.name == "nt":
                                stream.seek(0)
                                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                            else:
                                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    stream.close()

    def _load(self) -> dict:
        try:
            if self.path.stat().st_size > 4_000_000:
                raise ValueError("oversized scan state")
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1:
                raise ValueError("unknown scan state")
            last = state["last_reply_at"]
            if isinstance(last, bool) or not isinstance(last, (float, int)) or not math.isfinite(last) or last < 0:
                raise ValueError("invalid reply timestamp")
            if not isinstance(state["resumes"], dict):
                raise ValueError("invalid queues")
            for key, value in state["resumes"].items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    raise ValueError("invalid resume queue")
                queue, signatures = value["queue"], value["signatures"]
                if (not isinstance(queue, list) or not all(isinstance(item, str) and item for item in queue)
                        or len(queue) != len(set(queue)) or not isinstance(signatures, dict)
                        or not all(isinstance(k, str) and isinstance(v, str) for k, v in signatures.items())):
                    raise ValueError("invalid queue entries")
            return state
        except FileNotFoundError:
            return {"version": 1, "last_reply_at": 0.0, "resumes": {}}
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
            raise FatalError("не удалось прочитать очередь и интервал ответов чатов — обход остановлен") from error

    def _save(self, state: dict) -> None:
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".robot-chat-scan-", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                if os.name == "posix":
                    os.fchmod(stream.fileno(), 0o600)
                json.dump(state, stream, ensure_ascii=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError) as error:
            raise FatalError("не удалось сохранить очередь и интервал ответов чатов — обход остановлен") from error
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink()

    @staticmethod
    def _resume_key(resume_id: str) -> str:
        return hashlib.sha256(resume_id.encode("utf-8")).hexdigest()

    def select(self, chats: list[_Chat], resume_id: str, budget: int) -> list[_Chat]:
        by_id = {chat.id: chat for chat in chats}
        with self._locked():
            state = self._load()
            entry = state["resumes"].setdefault(self._resume_key(resume_id), {"queue": [], "signatures": {}})
            old = entry["signatures"]
            queue = [item for item in entry["queue"] if item in by_id]
            queued = set(queue)
            queue.extend(item for item in by_id if item not in queued)
            signatures = {item: hashlib.sha256(json.dumps(
                [chat.last, chat.latest_at, chat.unread], ensure_ascii=True).encode("utf-8")).hexdigest()
                for item, chat in by_id.items()}
            priority = [item for item, chat in by_id.items()
                        if item not in old or old[item] != signatures[item] or chat.unread]
            count = min(budget, len(queue))
            # Старейшие осмотры получают не менее 60% мест, даже при постоянном потоке новых сообщений.
            fifo = queue[:count - min(count * 2 // 5, len(priority))]
            urgent = [item for item in priority if item not in fifo][:count * 2 // 5]
            chosen = urgent + fifo
            chosen += [item for item in queue if item not in chosen][:count - len(chosen)]
            entry.update(queue=queue, signatures=signatures)
            self._save(state)
        return [by_id[item] for item in chosen]

    def checked(self, resume_id: str, chat_id: str) -> None:
        with self._locked():
            state = self._load()
            entry = state["resumes"].get(self._resume_key(resume_id))
            if entry and chat_id in entry["queue"]:
                entry["queue"].remove(chat_id)
                entry["queue"].append(chat_id)
                self._save(state)

    def reply_slot(self, interval: float, *, reserve: bool = False) -> float:
        """Остаток паузы; ноль с reserve=True означает, что время записано до возможной отправки."""
        with self._locked():
            state = self._load()
            now = time.time()
            if not math.isfinite(now) or now < 0:
                raise FatalError("не удалось определить время для интервала ответов роботу")
            remaining = max(0.0, state["last_reply_at"] + interval - now)
            if not remaining and reserve:
                state["last_reply_at"] = now
                self._save(state)
            return remaining


def read_rejections(
    page: Page, cfg: Config, dry_run: bool = False, stats: ChatStats | None = None, root: Path | None = None,
    *, require_selected_resume: bool = False, include_read: bool = False,
    max_chats_per_check: int | None = None,
) -> ChatStats:
    if max_chats_per_check is not None and (type(max_chats_per_check) is not int or max_chats_per_check < 0):
        raise ValueError("max_chats_per_check must be a nonnegative integer or None")
    rules = cfg.chats
    stats = stats if stats is not None else ChatStats()
    only_rejections = rules.mode == "rejections"
    ai = None if only_rejections else _make_ai(cfg)  # «только отказы»: нейросеть и резюме не нужны
    log.info("")
    title = ("чаты: только отказы (остальные чаты не трогаю)" if only_rejections
             else "чаты: отказы" + (" + ИИ-ответы на анкеты" if ai else ""))
    log.info(f"Шаг 3/3: {title}" + ("  [DRY-RUN: ничего не открываю и не отправляю]" if dry_run else ""))
    resume = ""
    if require_selected_resume and ai is None:
        raise FatalError("автоответы роботу включены, но ИИ недоступен — сообщения не отправлены")
    if ai:
        log.info(f"  ИИ: {ai.describe()}, отвечать {'только роботу-рекрутеру' if cfg.ai.only_robot else 'всем'}")
        strict_resume = require_selected_resume or cfg.ai.only_robot
        if strict_resume and not cfg.resume_id:
            raise FatalError("для автоответов роботу выберите резюме (resume_url) — сообщения не отправлены")
        resume = fetch_resume_text(page, cfg, (root or Path(".")) / "data" / "resume.txt",
                                   require_selected=strict_resume)
        if strict_resume and not resume.strip():
            raise FatalError("нет текста выбранного резюме для автоответов роботу — сообщения не отправлены")
    answers_log = (root or Path(".")) / "data" / "chat_answers.jsonl"
    scan_read = include_read and ai is not None
    bounded = bool(scan_read and cfg.ai.only_robot and max_chats_per_check)

    log.info(f"  открываю чаты: {rules.url}")
    browser.wait_read_interval(cfg)
    page.goto(rules.url, wait_until="domcontentloaded")
    wait_captcha(page)
    if is_auth_url(page.url) or hh_role(page) == "anonymous":
        raise FatalError("hh перекинул на страницу входа — сессия истекла, войдите заново")
    try:  # при уже включённом фильтре список может быть пуст — тогда ждём сам переключатель
        page.locator(f"{S.CHAT_CELL}, {S.CHAT_ONLY_UNREAD}").first.wait_for(state="attached", timeout=20_000)
    except PlaywrightError:
        log.warning("  ! список чатов не появился за 20 с (чатов нет или hh поменял вёрстку)")
        return stats

    # статусы откликов для воронки и уведомление о приглашении
    tracker = Negotiations(root) if root is not None and (cfg.stats.funnel or cfg.notify.invitations) else None
    if tracker is not None and cfg.stats.funnel and tracker.full_sync_due() and not bounded:
        _announce(cfg, _full_sync(page, tracker, rules, cfg=cfg), stats, quiet=dry_run)

    # Вопрос может остаться без ответа, даже если пользователь уже открыл чат. В этом режиме счётчик
    # непрочитанных не участвует в отборе: решение принимает последний отправитель и ID сообщения.
    if scan_read:
        log.info("  проверяю также прочитанные чаты: ищу последние вопросы робота без ответа")
        if not _disable_only_unread(page):
            raise FatalError("не удалось выключить «Только непрочитанные» — полный просмотр чатов остановлен")
        only_unread = False
    else:
        # Если включить фильтр не вышло — отличаем непрочитанные по счётчику сообщений у чата.
        only_unread = _enable_only_unread(page)
    if only_unread:
        try:
            page.locator(S.CHAT_CELL).first.wait_for(timeout=5_000)
        except PlaywrightError:
            log.success("  ✓ непрочитанных чатов нет")
            return stats

    if bounded:
        return _read_robot_batch(page, cfg, ai, resume, stats, dry_run, answers_log, tracker,
                                 max_chats_per_check)

    rejection = rules.rejection_text.strip().lower()
    seen: set[str] = set()
    missing: Counter = Counter()  # чат → сколько раз подряд его строки не было в списке, когда дошла очередь
    answered: dict[str, list[str]] = {}  # чат → наши ответы в нём за этот запуск
    opened_at: dict[str, str] = {}  # чат → его последнее сообщение в списке, когда мы его открывали
    previous, previous_id = "", ""  # переписка прошлого чата — чтобы не принять её за переписку следующего
    second_pass = False
    found = 0
    for _ in range(500):  # защита от бесконечного цикла
        if scan_read and _filter_checked(page):
            log.info("  hh включил «Только непрочитанные» — выключаю для полного просмотра")
            if not _disable_only_unread(page):
                raise FatalError("hh включил фильтр непрочитанных; полный просмотр чатов остановлен")
        # hh сбрасывает «Только непрочитанные» (например, при открытии чата) — проверяем каждый раз,
        # иначе прочитанные чаты из полного списка были бы приняты за непрочитанные
        if only_unread and not _filter_checked(page):
            log.warning("  ! hh снял галочку «Только непрочитанные» — включаю снова")
            only_unread = _enable_only_unread(page)
        chats = _collect(page, only_unread)
        if tracker is not None:
            _announce(cfg, tracker.update(chats, rules.rejection_text), stats, quiet=dry_run)
        for chat in chats:
            if ai is not None and cfg.ai.only_robot:
                age_reason = _chat_age_reason(chat)
                if age_reason:
                    if chat.id not in seen:
                        log.info(f"  чат пропущен: {age_reason}")
                    seen.add(chat.id)
                    continue
            if chat.id in seen:
                if _robot_wrote_again(chat, answered.get(chat.id), opened_at.get(chat.id), cfg.ai.max_answers_per_chat):
                    seen.discard(chat.id)  # робот ответил, когда мы уже ушли из чата, — вернёмся к нему
                continue
            is_rejection = chat.last.lower().startswith(rejection)
            if is_rejection and not chat.unread:
                seen.add(chat.id)
                stats.rejections_read += 1
                log.debug(f"  • уже прочитан отказ: «{chat.title}» — {chat.company}")
            elif not is_rejection and ((not chat.unread and not scan_read) or not ai):
                seen.add(chat.id)
                if chat.unread:
                    stats.other_unread.append(f"«{chat.title}» — {chat.company}: {chat.last[:80]}")
                    log.info(f"  • непрочитанный чат НЕ с отказом (не трогаю): «{chat.title}» — {chat.company}: «{chat.last[:80]}»")

        target = next((c for c in chats if c.id not in seen and (c.unread or scan_read)), None)
        if target is None:
            browser.wait_read_interval(cfg)
            if _scroll_list(page):
                continue
            if second_pass or not only_unread:
                break
            # Пока мы шли вниз, наверху могли появиться новые чаты (отказ пришёл во время запуска), а робот —
            # ответить в чате, из которого мы ушли. Короткий список непрочитанных проходим ещё раз с начала
            second_pass = True
            browser.wait_read_interval(cfg)
            page.evaluate(_SCROLL_TOP_JS, S.CHAT_CELL)
            control.sleep(0.5)
            continue
        if not page.locator(_cell_selector(target.id)).count():
            # Строка пропала: список перерисовался (пришёл новый чат, hh убрал прочитанный). Раньше чат
            # всё равно открывали — щелчок ждал 20 с и чат считался ошибкой. Соберём список заново
            missing[target.id] += 1
            if missing[target.id] < 3:
                control.sleep(0.5)
                continue
            seen.add(target.id)
            log.warning(f"  ! чат «{target.title}» пропал из списка — пропускаю")
            continue

        seen.add(target.id)
        found += 1
        is_rejection = target.last.lower().startswith(rejection)
        again = bool(answered.get(target.id))
        log.info("")
        log.info(f"[{found}] {'✉' if is_rejection else '✎'} «{target.title}» — {target.company}: «{target.last[:100]}»"
                 + ("  (робот ответил после нашего ответа — возвращаюсь)" if again else ""))
        if is_rejection:
            if dry_run:
                log.info("  [dry-run] отказ — здесь чат был бы открыт и прочитан")
                stats.opened += 1
            elif _open_chat(page, target, cfg):
                stats.opened += 1
            else:
                stats.failed += 1
        else:
            opened_at[target.id] = target.last
            sent = answered.setdefault(target.id, [])
            other = previous if previous_id != target.id else ""  # вернулись в тот же чат — его переписка не чужая
            transcript = _handle_with_ai(page, target, cfg, ai, resume, stats, dry_run, answers_log, sent, other)
            if transcript:
                previous, previous_id = transcript, target.id
    return stats


def _read_robot_batch(page: Page, cfg: Config, ai: AiClient, resume: str, stats: ChatStats,
                      dry_run: bool, answers_log: Path, tracker, budget: int) -> ChatStats:
    """Полный сбор строк sidebar и ограниченное число осмотров переписки по устойчивой очереди."""
    inventory: dict[str, _Chat] = {}
    browser.wait_read_interval(cfg)
    page.evaluate(_SCROLL_TOP_JS, S.CHAT_CELL)
    control.sleep(0.5)
    while True:
        control.check()
        if _filter_checked(page) and not _disable_only_unread(page):
            raise FatalError("hh включил фильтр непрочитанных; полный просмотр чатов остановлен")
        chats = _collect(page, only_unread=False)
        if tracker is not None:
            _announce(cfg, tracker.update(chats, cfg.chats.rejection_text), stats, quiet=dry_run)
        inventory.update((chat.id, chat) for chat in chats)
        browser.wait_read_interval(cfg)
        if not _scroll_list(page):
            break
    if tracker is not None and cfg.stats.funnel and tracker.full_sync_due():
        tracker.mark_full_sync()

    candidates = []
    skipped: Counter = Counter()
    rejection = cfg.chats.rejection_text.strip().lower()
    for chat in inventory.values():
        age_reason = _chat_age_reason(chat)
        if age_reason:
            skipped[age_reason] += 1
        elif chat.last.lower().startswith(rejection) and not chat.unread:
            stats.rejections_read += 1
        else:
            candidates.append(chat)
    for reason, count in skipped.items():
        log.info(f"  чатов пропущено: {count} — {reason}")
    scan = _RobotScanState(answers_log.parent / ROBOT_SCAN_FILE)
    targets = scan.select(candidates, cfg.resume_id, budget)
    stats.deferred += len(candidates) - len(targets)
    log.info(f"  очередь чатов за {ROBOT_CHAT_DAYS} дней: {len(candidates)}; "
             f"открываю не более {budget}, осталось после этого прохода: {len(candidates) - len(targets)}")
    previous = ""
    for index, target in enumerate(targets, 1):
        control.check()
        log.info(f"[{index}/{len(targets)}] «{target.title}» — {target.company}")
        stats.checked += 1
        try:
            if target.last.lower().startswith(rejection):
                if dry_run:
                    log.info("  [dry-run] отказ — здесь чат был бы открыт и прочитан")
                    stats.opened += 1
                elif _open_chat(page, target, cfg):
                    stats.opened += 1
                else:
                    stats.failed += 1
            else:
                transcript = _handle_with_ai(page, target, cfg, ai, resume, stats, dry_run, answers_log, [], previous)
                if transcript:
                    previous = transcript
        finally:
            # Не переносим ещё не начатые осмотры: остановленный процесс продолжит с их места.
            scan.checked(cfg.resume_id, target.id)
    return stats


def check_invitations(page: Page, cfg: Config, root: Path) -> list[str]:
    """Фоновая проверка ([watch] invitations): нет ли в непрочитанных чатах приглашения. Чаты не открываются —
    остаются непрочитанными, как были. Возвращает новые приглашения («вакансия» — компания)."""
    rules = cfg.chats
    stats = ChatStats()
    tracker = Negotiations(root)
    log.info("Чаты: нет ли приглашения")
    browser.wait_read_interval(cfg)
    page.goto(rules.url, wait_until="domcontentloaded")
    wait_captcha(page)
    if is_auth_url(page.url) or hh_role(page) == "anonymous":
        raise FatalError("hh перекинул на страницу входа — сессия истекла, войдите заново")
    try:
        page.locator(f"{S.CHAT_CELL}, {S.CHAT_ONLY_UNREAD}").first.wait_for(state="attached", timeout=20_000)
    except PlaywrightError:
        log.warning("  ! список чатов не появился за 20 с (чатов нет или hh поменял вёрстку)")
        return []
    if cfg.stats.funnel and tracker.full_sync_due():
        _announce(cfg, _full_sync(page, tracker, rules, cfg=cfg), stats)
    only_unread = _enable_only_unread(page)
    if only_unread:
        try:
            page.locator(S.CHAT_CELL).first.wait_for(timeout=5_000)
        except PlaywrightError:
            log.success("  ✓ непрочитанных чатов нет")
            return stats.invitations
    seen: set[str] = set()
    for _ in range(100):
        chats = _collect(page, only_unread)
        _announce(cfg, tracker.update([c for c in chats if c.id not in seen], rules.rejection_text), stats)
        seen.update(c.id for c in chats)
        if not _scroll_list(page, cfg=cfg):
            break
    if not stats.invitations:
        log.info(f"  новых приглашений нет (непрочитанных чатов: {len(seen) if only_unread else 'см. список'})")
    return stats.invitations


def _announce(cfg: Config, fresh: list[dict], stats: ChatStats, quiet: bool = False) -> None:
    """Новые приглашения: в журнал (окно приложения покажет их сразу) и в уведомления ([notify] invitations)."""
    if not fresh:
        return
    for invitation in fresh:
        text = f"«{invitation['title']}» — {invitation['company']}"
        stats.invitations.append(text)
        log.success(f"  ★ Приглашение: {text}")
    if cfg.notify.invitations and not quiet and (cfg.notify.desktop or cfg.notify.telegram):
        from ..core import notify

        title = "HH-Auto-Response — приглашение" + (f" ({len(fresh)})" if len(fresh) > 1 else "")
        notify.send(cfg.notify, title, "\n".join(stats.invitations[-len(fresh):]))


def _full_sync(page: Page, tracker: Negotiations, rules, *, cfg: Config | None = None) -> list[dict]:
    """Раз в сутки — весь список чатов, и прочитанные: что стало с откликами (для воронки). Возвращает
    приглашения из непрочитанных чатов, о которых ещё не сообщали."""
    if _filter_checked(page) and not _disable_only_unread(page):
        log.debug("  «Только непрочитанные» не снимается — воронка обновится по непрочитанным чатам")
        return []
    log.info("  смотрю статусы всех чатов — для воронки откликов (раз в сутки)")
    seen: set[str] = set()
    fresh: list[dict] = []
    for _ in range(300):  # до нескольких тысяч чатов
        chats = _collect(page, only_unread=False)
        fresh += tracker.update([c for c in chats if c.id not in seen], rules.rejection_text)
        seen.update(c.id for c in chats)
        if not _scroll_list(page, pause=0.4, cfg=cfg):
            break
    if cfg is not None:
        browser.wait_read_interval(cfg)
    page.evaluate(_SCROLL_TOP_JS, S.CHAT_CELL)
    tracker.mark_full_sync()
    log.info(f"  просмотрено чатов: {len(seen)}")
    return fresh


def _collect(page: Page, only_unread: bool) -> list[_Chat]:
    """Чаты, которые сейчас видны в списке. Пустые строки (hh их ещё не дорисовал) не берём: по ним не понять,
    отказ ли это, — возьмём, когда дорисуются."""
    chats = []
    for raw in page.evaluate(_COLLECT_JS, _selectors()):
        chat = _Chat(**raw)
        if not (chat.title or chat.company or chat.last):
            continue
        chat.badge = chat.unread
        if only_unread:  # в отфильтрованном списке непрочитанные все, даже без счётчика
            chat.unread = True
        chats.append(chat)
    return chats


def _scroll_list(page: Page, pause: float = 0.5, *, cfg: Config | None = None) -> bool:
    """Прокрутить список чатов дальше. False — дальше ничего нет. Докрутив до конца, hh догружает следующую
    порцию чатов не сразу: раньше программа в этот момент решала, что чатов больше нет, и чаты ниже оставались
    необработанными. Теперь догрузку ждём до LIST_LOAD_WAIT с."""
    deadline = time.monotonic() + LIST_LOAD_WAIT
    while True:
        if cfg is not None:
            browser.wait_read_interval(cfg)
        if page.evaluate(_SCROLL_JS, S.CHAT_CELL):
            break
        if time.monotonic() >= deadline:
            return False
        control.sleep(0.5)
    control.sleep(pause)  # даём виртуальному списку дорисовать строки
    return True


def _cell_selector(chat_id: str) -> str:
    return f'[data-qa="chatik-open-chat-{chat_id}"]'


def _robot_wrote_again(chat: _Chat, sent: list[str] | None, opened_with: str | None, limit: int) -> bool:
    """Чат, где мы в этом запуске отвечали, снова с непрочитанным сообщением — и оно не наше: робот прислал
    следующий вопрос, когда мы уже ушли из чата (не дождались или отвечали в другом). Раньше такой чат
    до следующего запуска не открывался, и на анкету приходилась лишь часть ответов."""
    return (bool(sent) and len(sent) < limit and chat.badge and chat.last != opened_with
            and not _is_ours(chat.last, sent[-1]))


def _is_ours(last: str, answer: str) -> bool:
    """Последнее сообщение в списке чатов — наш ответ (hh может подписать его «Вы:» и обрезать)."""
    mine, text = norm(answer), re.sub(r"^вы:\s*", "", norm(last))
    if not mine or not text:
        return False
    if len(mine) <= 40:  # короткий ответ («Да», кнопка) — целиком, а не начало чужого «Давайте…»
        return text == mine or text.startswith(mine) and not text[len(mine)].isalnum()
    return text[:40] == mine[:40]


def _disable_only_unread(page: Page) -> bool:
    """Снимает «Только непрочитанные» — так же щелчком по подписи, как и включение. True — снят."""
    label = page.get_by_text("Только непрочитанные", exact=True).first
    _wait_list_settled(page)
    for _attempt in range(3):
        if not _filter_checked(page):
            break
        try:
            label.click()
        except PlaywrightError as e:
            log.debug(f"  щелчок не прошёл: {(str(e).splitlines() or [''])[0]}")
        for _ in range(20):
            control.sleep(0.3)
            if not _filter_checked(page):
                break
    control.sleep(1.0)  # список перезагружается
    return not _filter_checked(page)


def _make_ai(cfg: Config) -> AiClient | None:
    if not cfg.ai.enabled:
        return None
    try:
        return AiClient(cfg.ai)
    except AiError as e:
        log.warning(f"  ! ИИ выключен: {e}")
        return None


def _enable_only_unread(page: Page) -> bool:
    """Включает в списке чатов фильтр «Только непрочитанные». True — фильтр включён."""
    box = page.locator(S.CHAT_ONLY_UNREAD)
    if not box.count():
        log.warning("  ! переключателя «Только непрочитанные» нет — ищу непрочитанные по счётчикам")
        return False
    box = box.first
    if _filter_checked(page):
        log.info("  фильтр «Только непрочитанные» уже включён")
        return True

    log.info("  → включаю фильтр «Только непрочитанные»")
    # Щелчок сразу после загрузки hh иногда «глотает»: страница ещё не ожила. Ждём, пока список дорисуется.
    _wait_list_settled(page)
    # Переключатель у hh — две вложенные label, у каждой свой input. Щёлкаем только по подписи: прямой
    # щелчок по скрытому input мог переключить соседний и вернуть галочку обратно.
    label = page.get_by_text("Только непрочитанные", exact=True).first
    for attempt in range(3):
        if _filter_checked(page):
            break
        try:
            label.click()
        except PlaywrightError as e:
            log.debug(f"  щелчок не прошёл: {(str(e).splitlines() or [''])[0]}")
        for _ in range(20):  # до 6 с: на медленном hh список перезагружается долго
            control.sleep(0.3)
            if _filter_checked(page):
                break
        if not _filter_checked(page):
            log.debug(f"  попытка {attempt + 1}: галочка не встала, состояние: {_filter_state(page)}")
            control.sleep(2)
    if _filter_checked(page):
        # Список перезагружается: ждём, пока он перестанет меняться. Раньше ждали 1 с, и если hh не успевал,
        # прочитанные чаты из старого списка принимались за непрочитанные
        _wait_list_settled(page, timeout=6)
        if _filter_checked(page):  # и после перезагрузки галочка всё ещё стоит
            log.success("  ✓ фильтр включён — в списке только непрочитанные чаты")
            return True
    log.warning(f"  ! не удалось включить фильтр ({_filter_state(page)}) — отличаю непрочитанные по счётчику")
    return False


def _wait_list_settled(page: Page, timeout: float = 15) -> None:
    """Ждём, пока список чатов перестанет меняться (страница hh догрузилась и ожила)."""
    cells = page.locator(S.CHAT_CELL)
    last, stable_since, deadline = -1, time.monotonic(), time.monotonic() + timeout
    while time.monotonic() < deadline and time.monotonic() - stable_since < 1.5:
        control.sleep(0.3)
        now = cells.count()
        if now != last:
            last, stable_since = now, time.monotonic()


def _filter_state(page: Page) -> str:
    """Для журнала: что на самом деле с обоими input переключателя."""
    try:
        return page.evaluate("""() => {
          const inner = document.querySelector('[data-qa="chatik-checkbox-only-unread"]');
          if (!inner) return 'переключателя нет';
          const outer = inner.closest('label[data-interactive]')?.querySelector(':scope > input[type=checkbox]');
          return `input hh: ${inner.checked ? 'вкл' : 'выкл'}, класс: ${/unchecked/i.test(inner.className) ? 'unchecked' : 'checked'}`
               + `, внешний input: ${outer ? (outer.checked ? 'вкл' : 'выкл') : 'нет'}`;
        }""")
    except PlaywrightError:
        return "не прочитать"


def _filter_checked(page: Page) -> bool:
    """Галочка «Только непрочитанные» стоит: и свойство checked, и класс hh без «unchecked»."""
    box = page.locator(S.CHAT_ONLY_UNREAD)
    try:
        return box.count() > 0 and box.first.evaluate(
            "el => el.checked && !/unchecked/i.test(el.className)")
    except PlaywrightError:
        return False


def _click_chat(page: Page, chat: _Chat, cfg: Config) -> None:
    cell = page.locator(_cell_selector(chat.id))
    if not cell.count() and chat.url:
        # В виртуальном списке строка уже может быть вне DOM. Используем только фактическую ссылку
        # hh той же страницы, с точным ID, а не строим произвольный маршрут или внешний адрес.
        target = urlsplit(urljoin(cfg.chats.url, chat.url))
        origin = urlsplit(cfg.chats.url)
        if (target.scheme not in ("http", "https") or target.scheme != origin.scheme
                or target.netloc != origin.netloc or target.path.rstrip("/") != f"/chat/{chat.id}"
                or target.username or target.password):
            raise PlaywrightError("ссылка чата не совпадает с адресом и ID в списке hh")
        browser.wait_read_interval(cfg)
        log.info("  → открываю чат по ссылке из списка")
        page.goto(target.geturl(), wait_until="domcontentloaded")
        wait_captcha(page)
        if is_auth_url(page.url) or hh_role(page) == "anonymous":
            raise FatalError("hh перекинул на страницу входа — сессия истекла, войдите заново")
        actual = urlsplit(page.url)
        if (actual.scheme != origin.scheme or actual.netloc != origin.netloc
                or actual.path.rstrip("/") != f"/chat/{chat.id}"):
            raise PlaywrightError("после перехода hh открыл другой чат — ответ не подготовлен")
        return
    browser.wait_read_interval(cfg)
    for attempt in range(3):
        try:
            cell.scroll_into_view_if_needed()
            break
        except PlaywrightError as e:
            # список перерисовался (пришёл новый чат) и строку создали заново — найдём её ещё раз
            if attempt == 2 or "not attached" not in str(e):
                raise
            control.sleep(0.3)
    control.sleep(random.uniform(cfg.limits.action_delay_min, cfg.limits.action_delay_max) / 2 * slowdown())
    log.info("  → открываю чат")
    cell.click()
    try:
        page.wait_for_url(re.compile(rf"/chat/{re.escape(chat.id)}(?:[/?#]|$)"), timeout=10_000)
    except PlaywrightError:
        log.debug(f"  адрес не сменился на /chat/{chat.id}: {page.url}")


def _back_to_list(page: Page, cfg: Config | None = None) -> None:
    """Узкая вёрстка: чат открывается вместо списка — возвращаемся назад."""
    if not page.locator(S.CHAT_CELL).count():
        log.debug("  список чатов скрыт — возвращаюсь назад")
        if cfg is not None:
            browser.wait_read_interval(cfg)
        page.go_back(wait_until="domcontentloaded")
        page.locator(S.CHAT_CELL).first.wait_for(timeout=15_000)


def _leave_chat(page: Page, url: str, cfg: Config | None = None) -> None:
    """Назад к списку чатов — и после ошибки в чате: в узкой вёрстке, пока открыт чат, списка не видно,
    и раньше после ошибки программа не находила остальных чатов и заканчивала работу."""
    try:
        _back_to_list(page, cfg)
    except PlaywrightError as e:
        log.debug(f"  к списку чатов назад не вернуться ({(str(e).splitlines() or [''])[0]}) — открываю его заново")
        try:
            if cfg is not None:
                browser.wait_read_interval(cfg)
            page.goto(url, wait_until="domcontentloaded")
            page.locator(S.CHAT_CELL).first.wait_for(timeout=15_000)
        except PlaywrightError:
            pass  # браузер закрыли или чатов нет — это увидит основной цикл


def _open_chat(page: Page, chat: _Chat, cfg: Config) -> bool:
    cell = page.locator(_cell_selector(chat.id))
    try:
        _click_chat(page, chat, cfg)
        delay = random.uniform(cfg.chats.delay_min, cfg.chats.delay_max) * slowdown()  # после капчи — дольше
        log.info(f"  ⏸ {delay:.1f} с — жду, пока hh отметит чат прочитанным")
        control.sleep(delay)
        _back_to_list(page, cfg)
        if cell.count() == 0 or cell.locator(S.CHAT_UNREAD_BADGE).count() == 0:
            log.success("  ✓ прочитан")
        else:
            log.warning("  ! счётчик непрочитанных у чата не исчез")
        return True
    except PlaywrightError as e:
        log.error(f"  ✗ не удалось открыть чат: {(str(e).splitlines() or [''])[0]}")
        _leave_chat(page, cfg.chats.url, cfg)
        return False


# ─────────────────────────── ИИ-ответы ───────────────────────────


def _handle_with_ai(page, chat: _Chat, cfg: Config, ai: AiClient, resume: str, stats: ChatStats,
                    dry_run: bool, answers_log: Path, sent: list[str], previous: str = "") -> str:
    """Переписка с роботом в одном чате. sent — наши ответы в этом чате за запуск (дополняется: в чат, где
    робот ответил позже, возвращаемся с ними). previous — переписка прошлого открытого чата. Возвращает
    последнюю увиденную переписку этого чата ("" — не прочитали)."""
    needs_you = f"«{chat.title}» — {chat.company}"
    transcript = None
    try:
        _click_chat(page, chat, cfg)
        transcript = _wait_transcript(page, chat, previous)
        if transcript is None:
            log.info("  в чате нет поля для ответа (чат закрыт работодателем) — просто прочитан")
            stats.info_read += 1
        else:
            transcript = _converse(page, chat, cfg, ai, resume, stats, dry_run, answers_log, sent, transcript,
                                   needs_you)
    except AiError as e:
        log.error(f"  ✗ ошибка ИИ: {e} — оставляю чат вам")
        stats.failed += 1
        _leave_to_you(stats, needs_you, chat.last)
    except PlaywrightError as e:
        log.error(f"  ✗ ошибка в чате: {(str(e).splitlines() or [''])[0]}")
        stats.failed += 1
    _leave_chat(page, cfg.chats.url, cfg)
    return transcript or ""


def _leave_to_you(stats: ChatStats, who: str, last: str) -> None:
    """В список «ждут вашего ответа» — один раз на чат: в чат могли вернуться, тогда строка — с новым сообщением."""
    stats.other_unread[:] = [line for line in stats.other_unread if not line.startswith(f"{who}: ")]
    stats.other_unread.append(f"{who}: {last[:80]}")


def _past_answers(path: Path, chat_id: str) -> tuple[list[str], set[str]]:
    """Наши подтверждённые ответы и уже начатые отправки в этом чате — в том числе до перезапуска."""
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], set()
    except (OSError, UnicodeError) as e:
        raise FatalError("не удалось прочитать историю автоответов — сообщения не отправлены") from e
    sent, turns, confirmed_turns = [], set(), set()
    for row in rows:
        try:
            record = json.loads(row)
        except ValueError:
            continue
        if not isinstance(record, dict) or str(record.get("chat_id")) != chat_id:
            continue
        if record.get("sent") is True:
            answer = str(record.get("answer") or "").removeprefix("[кнопка] ").strip()
            if answer:
                sent.append(answer)
        if record.get("turn_key"):
            key = str(record["turn_key"])
            if record.get("sent") is True or record.get("pending") is True:
                turns.add(key)
                if record.get("sent") is True:
                    confirmed_turns.add(key)
            elif record.get("released") is True and key not in confirmed_turns:
                turns.discard(key)
    return sent, turns


MAX_SEND_ATTEMPTS = 3  # proven pre-send failures may retry on later monitor checks, never forever


def _send_attempts(path: Path, chat_id: str, turn_key: str) -> int:
    """A release permits retry but does not erase the historical attempt budget."""
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    except (OSError, UnicodeError) as error:
        raise FatalError("не удалось прочитать попытки автоответов — сообщения не отправлены") from error
    attempts = 0
    for row in rows:
        try:
            record = json.loads(row)
        except ValueError:
            continue
        if (isinstance(record, dict) and str(record.get("chat_id")) == chat_id
                and record.get("turn_key") == turn_key and record.get("pending") is True
                and record.get("dry_run") is not True):
            attempts += 1
    return attempts


def _generation_record(value) -> dict:
    """Only JSON metadata, with credentials excluded even if an adapter accidentally includes them."""
    def clean(item):
        if isinstance(item, dict):
            return {str(key): ("[REDACTED]" if str(key).casefold() in
                              {"api_key", "authorization", "access_token", "cookie", "set-cookie"}
                              else clean(val)) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(val) for val in item]
        if isinstance(item, str):
            return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", item)
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return str(item)
    return clean(value) if isinstance(value, dict) else {}


def _turn_key(chat: _Chat, transcript: str, sender: _Sender | None) -> str:
    """ID входящего сообщения hh; при старой вёрстке — содержательная переписка без статусов и времени."""
    identity = sender.message_id if sender is not None else ""
    if not identity:
        identity = "\n".join(norm(line) for line in transcript.splitlines()
                             if line.strip() and not _STATUS_LINE.fullmatch(line.strip()))
    return hashlib.sha256(f"{chat.id}\0{identity}".encode("utf-8")).hexdigest()


def _same_robot_turn(page: Page, cfg: Config, chat: _Chat, turn_key: str) -> bool:
    if _chat_age_reason(chat):
        return False
    sender = _current_sender(page)
    transcript = _transcript(page)
    return bool(_robot_sender(sender, cfg.ai.robot_markers) and transcript
                and _turn_key(chat, transcript, sender) == turn_key)


def _answer_context(history: list[str], sent: list[str], limit: int) -> list[str]:
    """История для ИИ отдельно от счётчика этого запуска; уже записанный хвост не дублируется."""
    overlap = min(len(history), len(sent))
    while overlap and history[-overlap:] != sent[:overlap]:
        overlap -= 1
    return (history + sent[overlap:])[-max(limit, 1):]


def _wait_robot_reply_slot(page: Page, cfg: Config, chat: _Chat, turn_key: str, answers_log: Path) -> bool:
    interval = cfg.watch.robot_reply_interval_seconds
    if not interval:
        return True
    scan = _RobotScanState(answers_log.parent / ROBOT_SCAN_FILE)
    announced = False
    while True:
        control.check()
        remaining = scan.reply_slot(interval)
        if remaining:
            if not announced:
                log.info(f"  ⏸ интервал между ответами роботу: ещё {remaining:.0f} с")
                announced = True
            control.sleep(min(remaining, 30))
            continue
        # Отправитель мог смениться во время паузы; резервируем время только для того же вопроса.
        from ..core.rate_limits import check_captcha_cooldown

        check_captcha_cooldown(cfg, answers_log.parent.parent)
        if not _same_robot_turn(page, cfg, chat, turn_key):
            return False
        if not scan.reply_slot(interval, reserve=True):
            return True


def _converse(page, chat: _Chat, cfg: Config, ai: AiClient, resume: str, stats: ChatStats, dry_run: bool,
              answers_log: Path, sent: list[str], transcript: str, needs_you: str) -> str:
    """Вопрос робота → ответ ИИ → ждём следующий вопрос, и так по кругу. Возвращает последнюю переписку."""
    if cfg.ai.only_robot and (not cfg.resume_id or not resume.strip()):
        log.warning("  ! нет выбранного резюме для автоответов роботу — оставляю чат вам")
        _leave_to_you(stats, needs_you, chat.last)
        return transcript
    if cfg.ai.only_robot:
        age_reason = _chat_age_reason(chat)
        if age_reason:
            log.info(f"  чат пропущен: {age_reason}")
            return transcript
    history, handled_turns = _past_answers(answers_log, chat.id)
    # sent разделяется с обходом списка чатов и содержит ТОЛЬКО ответы текущего запуска.
    # Записанная история нужна модели и проверке последнего сообщения, но не расходует новый лимит.
    context_sent = _answer_context(history, sent, cfg.ai.max_answers_per_chat)
    for _ in range(cfg.ai.max_answers_per_chat - len(sent)):
        control.check()
        # Страховка от повторного ответа: переписка заканчивается нашим же ответом — ждём работодателя
        if context_sent and ours_is_last(transcript, context_sent[-1]) and not _question_after(transcript, context_sent[-1]):
            log.info("  последнее сообщение — ваше, ждём работодателя")
            break
        sender = _current_sender(page)
        if sender is not None and sender.outgoing:
            log.info("  последнее сообщение — ваше, ждём работодателя")
            break
        is_robot = _robot_sender(sender, cfg.ai.robot_markers)
        log.info(f"  пишет: {'Робот-рекрутер' if is_robot else 'работодатель (робот не подтверждён)'}")
        if cfg.ai.only_robot and not is_robot:
            log.info("  текущий отправитель не подтверждён как робот hh — оставляю чат вам")
            _leave_to_you(stats, needs_you, chat.last)
            break
        turn_key = _turn_key(chat, transcript, sender)
        if turn_key in handled_turns:
            log.info("  на это сообщение уже отвечали или отправка ранее началась — повторно не отправляю")
            _leave_to_you(stats, needs_you, chat.last)
            break
        attempts = _send_attempts(answers_log, chat.id, turn_key)
        if attempts >= MAX_SEND_ATTEMPTS:
            log.warning(f"  ! исчерпаны {MAX_SEND_ATTEMPTS} технические попытки ответа на это сообщение — "
                        "новых отправок и вызовов ИИ не будет")
            break
        log.info("  … спрашиваю ИИ, что ответить")
        buttons = _buttons(page)
        if buttons:
            log.debug(f"  кнопки в чате: {buttons}")
        decision = ai.decide(resume, cfg.ai.context, transcript[-6000:], list(context_sent), buttons)
        generation = _generation_record(getattr(ai, "last_chat_record", {}))
        for fix in ai.notes:
            log.warning(f"  ! проверка: {fix}")
        # Страховки: документы, деньги, контакты, ссылки и повторы — что бы ни сказала модель
        # Одинаковый текст ответа допустим на новый вопрос робота с новым ID (например, он повторно
        # спрашивает город). Повторы старого сообщения уже отсеяны по ID, остальные страховки review нужны.
        review_sent = [] if cfg.ai.only_robot and is_robot and sender and sender.message_id else context_sent
        note = review(decision, transcript, review_sent)
        if note:
            log.warning(f"  ! {note}")
        if decision.kind == "info":
            if is_done_message(transcript):
                log.success("  ✓ робот завершил анкету — перехожу к следующему чату")
            else:
                log.success("  ✓ ответ не нужен (уведомление) — прочитано" if not context_sent else "  ✓ анкета завершена")
            if not context_sent:
                stats.info_read += 1
            break
        if decision.kind == "wait":
            log.info("  последнее сообщение — ваше, ждём работодателя")
            break
        if decision.kind == "human" or (cfg.ai.only_robot and not is_robot):
            why = "нужен ваш ответ" if decision.kind == "human" else "пишет не робот, а человек"
            log.warning(f"  ! {why} — оставляю вам" + (f": «{decision.question[:120]}»" if decision.question else ""))
            _leave_to_you(stats, needs_you, chat.last)
            break

        log.info(f"  вопрос: «{decision.question[:200]}»")
        if decision.button:
            log.success(f"  ↳ ИИ выбрал кнопку: «{decision.button}»")
        else:
            log.success(f"  ↳ ответ ИИ: «{decision.reply}»")
        answer = f"[кнопка] {decision.button}" if decision.button else decision.reply
        if dry_run:
            log.info("  [dry-run] ответ не отправлен")
            _log_answer(answers_log, chat, decision.question, answer, sent=False, generation=generation)
            break
        if cfg.ai.only_robot:
            # Пока отвечала нейросеть, разговор мог продолжить человек. Проверяем отправителя и сообщение
            # заново до кнопки отправки, а не доверяем прежней отметке робота на весь диалог.
            age_reason = _chat_age_reason(chat)
            if age_reason:
                log.info(f"  отправка отменена: {age_reason}")
                break
            live_sender = _current_sender(page)
            live_transcript = _transcript(page)
            if (not _robot_sender(live_sender, cfg.ai.robot_markers) or not live_transcript
                    or _turn_key(chat, live_transcript, live_sender) != turn_key):
                log.info("  сообщение или отправитель сменились во время подготовки ответа — оставляю чат вам")
                _leave_to_you(stats, needs_you, chat.last)
                break
            if not _wait_robot_reply_slot(page, cfg, chat, turn_key, answers_log):
                log.info("  сообщение или отправитель сменились во время паузы — ответ не отправлен")
                _leave_to_you(stats, needs_you, chat.last)
                break
        # Запись ДО отправки защищает и от сбоя подтверждения: неоднозначную доставку автоматически не повторяем.
        _log_answer(answers_log, chat, decision.question, answer, sent=False, turn_key=turn_key, pending=True,
                    generation=generation)
        handled_turns.add(turn_key)
        guard = (lambda: _same_robot_turn(page, cfg, chat, turn_key)) if cfg.ai.only_robot else None
        try:
            if cfg.ai.only_robot:
                delivered = (_press(page, decision.button, guard=guard) if decision.button
                             else _send(page, decision.reply, guard=guard))
            else:
                delivered = _press(page, decision.button) if decision.button else _send(page, decision.reply)
        except _SendNotAttempted as e:
            _log_answer(answers_log, chat, decision.question, answer, sent=False, turn_key=turn_key, released=True,
                        generation=generation)
            handled_turns.discard(turn_key)
            log.warning(f"  ! отправка не начиналась ({e}) — ответ можно повторить после исправления поля")
            stats.failed += 1
            _leave_to_you(stats, needs_you, chat.last)
            break
        if not delivered:
            log.error("  ✗ не удалось отправить ответ — оставляю чат вам")
            stats.failed += 1
            _leave_to_you(stats, needs_you, chat.last)
            break
        sent.append(decision.button or decision.reply)
        context_sent.append(decision.button or decision.reply)
        context_sent[:] = context_sent[-max(cfg.ai.max_answers_per_chat, 1):]
        stats.answered += 1
        if len(sent) == 1:
            stats.questionnaires += 1
        _log_answer(answers_log, chat, decision.question, answer, sent=True, turn_key=turn_key,
                    generation=generation)
        reply = _wait_next_message(page, cfg.ai.reply_wait, sent[-1])
        if reply is None:
            log.info(f"  робот не прислал новый вопрос за {cfg.ai.reply_wait:.0f} с — перехожу дальше "
                     "(пришлёт позже — вернусь к чату в этом же запуске)")
            break
        transcript = reply
    else:
        log.warning(f"  ! достигнут лимит ответов в одном чате ({cfg.ai.max_answers_per_chat})")
    return transcript


def _transcript(page: Page) -> str | None:
    try:
        return page.evaluate(_TRANSCRIPT_JS, [S.CHAT_INPUT, S.CHAT_CELL])
    except PlaywrightError:
        return None


def _wait_transcript(page: Page, chat: _Chat, previous: str = "", timeout: float = 8) -> str | None:
    """Переписка только что открытого чата. Сразу после щелчка панель ещё может показывать прошлый чат,
    а сообщения — догружаться: раньше бралось то, что было на экране через 0,8 с, и ИИ мог отвечать по чужой
    переписке. Ждём, пока текст перестанет меняться и будет этим чатом: его вакансия в шапке (или прошло
    3 с) и не переписка прошлого чата. Поля для ответа нет — None (чат закрыт работодателем)."""
    title = norm(chat.title)[:40]
    started = time.monotonic()
    last, stable_since = None, started
    while time.monotonic() - started < timeout:
        text = _transcript(page)
        now = time.monotonic()
        if not text or not text.strip():
            last = None
        elif text != last:
            last, stable_since = text, now
        elif (now - stable_since >= 0.6 and text != previous
              and (not title or title in norm(text) or now - started >= 3)):
            return text
        control.sleep(0.3)
    return last if last and last != previous else None


def _wait_next_message(page: Page, timeout: float, last_sent: str) -> str | None:
    """После нашего ответа ждём новое сообщение работодателя (следующий вопрос робота). Отметка «прочитано»,
    время или «печатает…» под нашим ответом — ещё не оно: раньше программа принимала их за новое сообщение,
    видела последним свой ответ и уходила из чата, а вопрос робота, пришедший следом, оставался без ответа."""
    # Робот бывает быстрее, чем мы убедились, что наш ответ ушёл: его вопрос уже на экране. Раньше он попадал
    # в «было до ответа», перемен программа не видела и уходила, а счётчика непрочитанных у открытого чата нет —
    # вернуться к нему было не по чему, и вопрос оставался без ответа
    baseline = _transcript(page) or ""
    if _question_after(baseline, last_sent):
        return _settled(page, baseline)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        control.sleep(1.0)
        text = _transcript(page) or ""
        # короткий вопрос («Ваш возраст?») ours_is_last не отличает от подписей — его видно по новой строке
        if text != baseline and (not ours_is_last(text, last_sent) or _new_lines(baseline, text)):
            return _settled(page, text)
    return None


def _question_after(transcript: str, answer: str) -> bool:
    """После нашего ответа уже есть вопрос. Короткий («Ваш город?») ours_is_last принимает за подписи под
    ответом (время, «прочитано») — и раньше такой вопрос оставался без ответа. В подписях «?» не бывает."""
    text, mine = norm(transcript), norm(answer)
    at = text.rfind(mine[:60])
    return bool(mine) and at >= 0 and "?" in text[at + len(mine):]


# Строки-подписи под сообщениями: время, «Вы», «прочитано», «печатает…» — это не новое сообщение
_STATUS_LINE = re.compile(r"\d{1,2}:\d{2}|вы|прочитано|доставлено|отправлено|сегодня|вчера|печатает\W*", re.I)


def _new_lines(before: str, after: str) -> bool:
    """В переписке появилась строка, которой не было (не время и не подпись вроде «прочитано»)."""
    old = Counter(line.strip() for line in before.splitlines())
    for line in after.splitlines():
        line = line.strip()
        if not line or _STATUS_LINE.fullmatch(line):
            continue
        if old[line]:
            old[line] -= 1
        else:
            return True
    return False


def _settled(page: Page, text: str, quiet: float = 1.5, limit: float = 6) -> str:
    """Робот часто шлёт несколько сообщений подряд («Спасибо!» и следующий вопрос) — ждём, пока переписка
    перестанет меняться (до limit с), чтобы ответить на всё сразу, а не на первое из них."""
    deadline, stable_since = time.monotonic() + limit, time.monotonic()
    while time.monotonic() < deadline and time.monotonic() - stable_since < quiet:
        control.sleep(0.5)
        now = _transcript(page) or text
        if now != text:
            text, stable_since = now, time.monotonic()
    return text


def _buttons(page: Page) -> list[str]:
    try:
        return page.evaluate(_BUTTONS_JS, [S.CHAT_INPUT, S.CHAT_CELL, S.CHAT_SEND]) or []
    except PlaywrightError:
        return []


def _press(page: Page, text: str, *, guard: Callable[[], bool] | None = None) -> bool:
    """Нажать кнопку быстрого ответа с ровно таким текстом и дождаться, что переписка изменилась."""
    before = _transcript(page) or ""
    button = page.locator('button, [role="button"]').filter(
        has_text=re.compile(rf"^\s*{re.escape(text)}\s*$")).filter(visible=True)
    if not button.count():
        log.warning(f"  ! кнопки «{text}» уже нет на странице")
        raise _SendNotAttempted("кнопка быстрого ответа исчезла до нажатия")
    control.sleep(random.uniform(0.4, 0.9))
    if guard is not None and not guard():
        raise _SendNotAttempted("сообщение или отправитель сменились перед нажатием ответа")
    log.info(f"  → нажимаю кнопку «{text}»")
    button.last.click()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        control.sleep(0.4)
        if (_transcript(page) or "") != before:
            log.success("  ✓ ответ кнопкой отправлен")
            return True
    return False


def _composer_input(page: Page):
    """Единственное поле настоящего composer hh; широкая старая вёрстка допустима только без неоднозначности."""
    composer = page.locator(S.CHAT_COMPOSER).filter(visible=True)
    if composer.count() > 1:
        raise _SendNotAttempted("найдено несколько панелей отправки hh")
    if composer.count() == 1:
        scope = composer.first
        boxes = scope.locator(S.CHAT_INPUT).filter(visible=True)
    else:
        boxes = page.locator(S.CHAT_INPUT).filter(visible=True)
        if boxes.count() != 1:
            raise _SendNotAttempted("не удалось однозначно выбрать поле сообщения hh")
        scope = boxes.first.locator("xpath=..")
    if boxes.count() != 1:
        raise _SendNotAttempted("в панели hh нет единственного доступного поля сообщения")
    return boxes.first, scope


def _send(page: Page, text: str, *, guard: Callable[[], bool] | None = None) -> bool:
    try:
        box, scope = _composer_input(page)
        # fill фокусирует поле сам. Предварительный координатный click на текущем textarea hh зависал.
        box.fill(text, timeout=5_000)
        control.sleep(random.uniform(0.4, 0.9))
        native_composer = scope.get_attribute("data-qa") == "chatik-message-input"
        send, send_count = None, 0
        if not native_composer:
            allowed = (f':is({S.CHAT_SEND}):not([data-qa="record-complete"])'
                       ':not([aria-label*="запись"]):not([title*="запись"])')
            selector = f'button{allowed}, [role="button"]{allowed}'
            send = scope.locator(selector).filter(visible=True)
            send_count = send.count()
            if send_count > 1:
                raise _SendNotAttempted("в панели hh несколько кнопок отправки")
        # Проверяем ПОСЛЕ fill и задержки: за это время мог ответить пользователь или подключиться рекрутер.
        if guard is not None and not guard():
            try:
                value = box.input_value() if box.evaluate("el => 'value' in el") else box.inner_text()
                if value == text:
                    box.fill("", timeout=1_000)
            except PlaywrightError:
                pass
            raise _SendNotAttempted("сообщение или отправитель сменились перед отправкой")
    except PlaywrightError as e:
        raise _SendNotAttempted("не удалось подготовить поле сообщения hh") from e
    # С этого момента ошибка неоднозначна: после click/Enter hh мог уже принять сообщение.
    try:
        if native_composer or not send_count:
            # Нативная панель hh: Enter отправляет текст и работает у спрятанного/свёрнутого браузера,
            # где координатный click по появившейся кнопке отправки может не пройти.
            box.press("Enter", timeout=8_000)
        else:
            send.first.click(timeout=8_000)
    except PlaywrightError:
        log.debug("  ошибка на границе отправки сообщения (подробный call log):", exc_info=True)
        raise
    probe = text[:40]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        control.sleep(0.4)
        value = box.input_value() if box.evaluate("el => 'value' in el") else box.inner_text()
        if not value.strip() and probe in (_transcript(page) or ""):
            log.success("  ✓ ответ отправлен")
            return True
    return False


def _log_answer(path: Path, chat: _Chat, question: str, answer: str, sent: bool, *,
                turn_key: str = "", pending: bool = False, released: bool = False,
                generation: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "chat_id": chat.id, "title": chat.title,
        "company": chat.company, "question": question, "answer": answer, "sent": sent,
    }
    if turn_key:
        record["turn_key"] = turn_key
    if pending:
        record["pending"] = True
    if released:
        record["released"] = True
    if generation:
        record["generation"] = _generation_record(generation)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        try:
            descriptor = f.fileno()
        except (AttributeError, io.UnsupportedOperation):  # in-memory append streams used by offline tests
            pass
        else:
            os.fsync(descriptor)  # pending claim must reach disk before the irreversible send


def log_chat_summary(stats: ChatStats, elapsed: float) -> None:
    minutes, seconds = divmod(int(elapsed), 60)
    log.info("")
    log.info("══════════════ Итоги ══════════════")
    if stats.checked or stats.deferred:
        log.info(f"  осмотрено чатов:                {stats.checked}; в очереди: {stats.deferred}")
    log.success(f"  прочитано отказов:              {stats.opened}")
    if stats.answered or stats.questionnaires or stats.info_read:
        log.success(f"  ИИ ответил на анкеты:           {stats.questionnaires} чатов, {stats.answered} ответов")
        log.info(f"  прочитано уведомлений:          {stats.info_read}")
    log.info(f"  отказы, прочитанные ранее:      {stats.rejections_read}")
    (log.error if stats.failed else log.info)(f"  ошибок:                         {stats.failed}")
    if stats.other_unread:
        log.warning(f"  ждут ВАШЕГО ответа ({len(stats.other_unread)}):")
        for line in stats.other_unread:
            log.warning(f"      {line}")
    log.info(f"  время работы:                   {minutes} мин {seconds} с")


def _selectors() -> dict[str, str]:
    return {
        "cell": S.CHAT_CELL,
        "title": S.CHAT_TITLE,
        "subtitle": S.CHAT_SUBTITLE,
        "last": S.CHAT_LAST_MESSAGE,
        "badge": S.CHAT_UNREAD_BADGE,
    }
