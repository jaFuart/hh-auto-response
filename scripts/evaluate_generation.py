"""Replay exported applications and robot questions without opening hh or sending messages.

Default: validate fixtures and report the actual old texts, with network disabled.
Add --live to call the configured model for all 10 letters and 5 source questions.
Only the configured model origin is allowed; hh requests and browser use are absent.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto.ai.profile import load_candidate_profile  # noqa: E402
from hh_auto.ai.application import _case_mentioned, analyze_vacancy, make_letter_plan, validate_letter  # noqa: E402
from hh_auto.core.config import AI_PROVIDERS, load_config  # noqa: E402


EXPECTED_LETTERS = 10
EXPECTED_CHATS = 5
_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_WORDS = re.compile(r"[\w]+(?:[+.#/-][\w]+)*", re.UNICODE)
_HH_HOST = re.compile(r"(?:^|\.)(?:hh\.ru|headhunter\.ru|hh\.kz|hh\.uz|hh\.by)$", re.I)


class EvaluationError(Exception):
    pass


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise EvaluationError(f"Не удалось прочитать JSON {path.name}: {type(error).__name__}") from error


def _normal(text: str) -> str:
    return " ".join(_WORDS.findall(text.casefold()))


def load_cases(exports_dir: Path) -> tuple[list[dict], list[dict]]:
    """Use source IDs and confirmed old answers; never interpret the final bot summary as a question."""
    pages = _read_json(exports_dir / "vacancy_pages.json")
    old_path = exports_dir / "applications_source.json"
    old = _read_json(old_path) if old_path.exists() else pages
    chat = _read_json(exports_dir / "robot_chat.json")
    if not isinstance(pages, dict) or not isinstance(old, dict) or not isinstance(chat, dict):
        raise EvaluationError("Экспорты должны содержать JSON-объекты")
    old_by_id = {str(row.get("id", "")): row for row in old.get("items", []) if isinstance(row, dict)}
    letters, seen = [], set()
    for item in pages.get("items", []):
        if not isinstance(item, dict):
            raise EvaluationError("Вакансия должна быть JSON-объектом")
        vacancy_id = str(item.get("id", "")).strip()
        if not vacancy_id or vacancy_id in seen:
            continue
        seen.add(vacancy_id)
        if not str(item.get("vacancy_text", "")).strip():
            raise EvaluationError(f"Нет текста вакансии {vacancy_id}")
        source = old_by_id.get(vacancy_id, item)
        letters.append({"id": vacancy_id, "title": str(item.get("title", "")),
                        "company": str(item.get("company", "")), "url": str(item.get("url", "")),
                        "description": str(item["vacancy_text"]),
                        "old_text": str(source.get("letter_text", "")),
                        "old_attached": bool(source.get("letter_attached", False)),
                        "old_source": str(source.get("letter_text_source", "export"))})
    messages = [m for m in chat.get("messages", []) if isinstance(m, dict)]
    chats, seen = [], set()
    for row in chat.get("confirmed_sent_log", []):
        if not isinstance(row, dict) or not row.get("sent", True):
            continue
        question = str(row.get("question", "")).strip()
        if not question or _normal(question) in seen:
            continue
        seen.add(_normal(question))
        index = next((i for i, m in enumerate(messages)
                      if m.get("robot") and not m.get("outgoing")
                      and _normal(question) in _normal(str(m.get("text", "")))), None)
        if index is None:
            raise EvaluationError("Вопрос из журнала не найден среди входящих сообщений робота")
        prefix = messages[:index + 1]
        transcript = "\n\n".join(str(m.get("text", "")) for m in prefix)
        sent = [str(m.get("text", "")) for m in prefix if m.get("outgoing")]
        chats.append({"id": str(messages[index].get("id") or f"question-{len(chats) + 1}"),
                      "chat_id": str(chat.get("chat_id", "")), "question": question,
                      "transcript": transcript, "sent": sent, "old_text": str(row.get("answer", ""))})
    if len(letters) != EXPECTED_LETTERS or len(chats) != EXPECTED_CHATS:
        raise EvaluationError(f"Нужны все {EXPECTED_LETTERS} уникальных вакансий и {EXPECTED_CHATS} вопросов; "
                              f"получено {len(letters)} и {len(chats)}")
    return letters, chats


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


@contextmanager
def model_network_only(base_url: str, *, live: bool):
    """Fail closed offline; online allow only the configured model origin, including redirects."""
    expected = _origin(base_url)
    if live and (expected[0] not in ("http", "https") or not expected[1] or _HH_HOST.search(expected[1])):
        raise EvaluationError("Адрес модели недопустим для изолированной проверки")
    class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, newurl):
            if _origin(newurl) != expected:
                raise EvaluationError("Переадресация модели на другой сервер запрещена")
            return super().redirect_request(request, response, code, message, headers, newurl)

    opener = urllib.request.build_opener(RestrictedRedirect())

    def guarded(request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        if not live or _origin(url) != expected or _HH_HOST.search(_origin(url)[1]):
            raise EvaluationError("Сетевой запрос запрещён режимом проверки")
        # A private opener prevents the default urllib opener from following an unvalidated redirect.
        return opener.open(request, *args, **kwargs)

    with patch.object(urllib.request, "urlopen", guarded):
        yield


def text_metrics(text: str, *, vacancy_id: str = "", projects=()) -> dict:
    words = _WORDS.findall(text)
    letters = [character for character in text if character.isalpha()]
    latin_share = sum("a" <= c.casefold() <= "z" for c in letters) / max(1, len(letters))
    names = [str(p.get("name", "") if isinstance(p, dict) else getattr(p, "name", "")) for p in projects]
    checks = {}
    if vacancy_id == "139438300":
        greeting = re.search(r"(?im)^\s*(?:здравствуй\w*|добрый\s+(?:день|вечер)|привет\b|dear\b|hello\b|hi\b)", text)
        head = text[:greeting.start()] if greeting else text
        answers = re.findall(r"(?m)^\s*(?:(\d{1,2})[.)]|(?:Ответ|Answer)\s+(\d{1,2}))\s*", head, re.I)
        numbers = [first or second for first, second in answers]
        checks["two_opening_answers_before_greeting"] = numbers[:2] == ["1", "2"]
    if vacancy_id == "139086991":
        checks["english_letter_script_check"] = bool(letters) and latin_share >= 0.8
    return {"characters": len(text), "words": len(words),
            "broad_40_160_words": 40 <= len(words) <= 160,
            "latin_letter_share": round(latin_share, 3),
            "opening": _normal(" ".join(words[:8])), "ending": _normal(" ".join(words[-8:])),
            "explicit_project_name_mentions": [name for name in names if name and _case_mentioned(text, [name])],
            "git_commands": sorted(set(re.findall(
                r"(?i)\bgit\s+(?:status|add|commit|push|pull|fetch|checkout|switch|merge|rebase|diff|log|branch|clone|stash|restore)\b",
                text))), "instruction_checks": checks}


def _redact(value, secrets=()):
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if str(k).lower() in
                        {"api_key", "authorization", "access_token", "cookie", "set-cookie"}
                        else _redact(v, secrets)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        value = _TOKEN.sub("[REDACTED]", value)
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def _summary(records: list[dict], side: str) -> dict:
    successful = [r[side] for r in records if r[side].get("text")]
    openings = [r["metrics"]["opening"] for r in successful]
    endings = [r["metrics"]["ending"] for r in successful]
    checks = [v for r in successful for v in r["metrics"]["instruction_checks"].values()]
    return {"texts": len(successful), "repeated_openings": len(openings) - len(set(openings)),
            "repeated_endings": len(endings) - len(set(endings)),
            "broad_word_range_count": sum(r["metrics"]["broad_40_160_words"] for r in successful),
            "required_instruction_checks": {"passed": sum(checks), "total": len(checks)},
            "validator_objective_issue_count": sum(sum(issue.startswith("objective:") for issue in r["metrics"].get("validation_issues", [])) for r in successful),
            "validator_style_issue_count": sum(sum(issue.startswith("style:") for issue in r["metrics"].get("validation_issues", [])) for r in successful),
            "planned_case_reference_checks": {
                "passed": sum(r["metrics"].get("planned_case_referenced") is True for r in successful),
                "total": sum(r["metrics"].get("planned_case_referenced") is not None for r in successful)},
            "explicit_project_names": sorted({p for r in successful
                                               for p in r["metrics"]["explicit_project_name_mentions"]})}


def evaluate(cfg, letters: list[dict], chats: list[dict], resume: str, *, live: bool = False,
             client_factory=None) -> dict:
    """Evaluate every case even when an individual model call fails. No delivery code is imported."""
    profile = load_candidate_profile(cfg.ai)
    report = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
              "mode": "live_model_dry_run" if live else "offline_fixtures", "dry_run": True,
              "hh_requests_allowed": False, "outbound_messages": 0,
              "expected": {"letters": EXPECTED_LETTERS, "chats": EXPECTED_CHATS},
              "model": {"provider": cfg.ai.provider, "model": cfg.ai.model},
              "limitations": ["Проверяются тексты и объективные признаки; конверсия в найм не измеряется.",
                              "Проверка языка по алфавиту и упоминаний проектов не заменяет оценку смысла."],
              "letters": [], "chats": []}
    client, startup_error = None, ""
    if live:
        if not resume.strip():
            raise EvaluationError("Для генерации требуется непустое выбранное резюме")
        try:
            if client_factory is None:
                from hh_auto.ai.client import AiClient
                client_factory = AiClient
            client = client_factory(cfg.ai)
        except Exception as error:
            startup_error = f"{type(error).__name__}: {error}"
    recent, old_recent = [], []
    for case in letters:
        record = {k: case[k] for k in ("id", "title", "company", "url")}
        record["source_sha256"] = hashlib.sha256(case["description"].encode()).hexdigest()
        old_text = case["old_text"]
        analysis = analyze_vacancy(case["title"], case["company"], case["description"], case["id"])
        plan = make_letter_plan(analysis, profile, resume=resume, context=cfg.ai.context, recent_letters=recent)
        record["analysis"] = analysis.to_dict()
        record["letter_plan"] = plan.to_dict()
        record["old"] = {"text": old_text, "actually_attached": case["old_attached"],
                         "source": case["old_source"], "metrics": text_metrics(old_text, vacancy_id=case["id"], projects=profile.projects)}
        old_issues = validate_letter(old_text, analysis, profile, plan=plan, recent_letters=old_recent,
                                     resume=resume, context=cfg.ai.context)
        record["old"]["metrics"].update(validation_issues=old_issues,
            planned_case_referenced=(not any(issue.startswith("style:missing_case") for issue in old_issues))
                                     if plan.selected_projects and old_text else None)
        if old_text:
            old_recent.append(old_text)
        new = {"status": "not_generated_offline", "text": "", "metadata": {}}
        if live:
            try:
                if startup_error:
                    raise EvaluationError(startup_error)
                client.last_letter_record = {}
                text = client.write_letter(resume, cfg.ai.context, case["title"], case["company"],
                                           case["description"], cfg.letter.ai_max_chars,
                                           experience_first=cfg.letter.experience_first,
                                           ai_prompt=cfg.letter.ai_prompt, vacancy_id=case["id"],
                                           recent_letters=list(recent))
                new.update(status="generated" if text.strip() else "empty", text=text,
                           metadata=copy.deepcopy(getattr(client, "last_letter_record", {})))
            except Exception as error:
                new.update(status="error", error=f"{type(error).__name__}: {error}",
                           metadata=copy.deepcopy(getattr(client, "last_letter_record", {})))
        new["metrics"] = text_metrics(new["text"], vacancy_id=case["id"], projects=profile.projects)
        new_issues = validate_letter(new["text"], analysis, profile, plan=plan, recent_letters=recent,
                                     resume=resume, context=cfg.ai.context) if live else []
        new["metrics"].update(validation_issues=new_issues,
            planned_case_referenced=(not any(issue.startswith("style:missing_case") for issue in new_issues))
                                     if plan.selected_projects and new["text"] else None)
        if new["text"].strip():
            recent.append(new["text"])
        record["new"] = new
        report["letters"].append(record)
    for case in chats:
        record = {k: case[k] for k in ("id", "chat_id", "question")}
        record["old"] = {"text": case["old_text"], "metrics": text_metrics(case["old_text"], projects=profile.projects)}
        new = {"status": "not_generated_offline", "text": "", "metadata": {}}
        if live:
            try:
                if startup_error:
                    raise EvaluationError(startup_error)
                client.last_chat_record = {}
                decision = client.decide(resume, cfg.ai.context, case["transcript"], list(case["sent"]), buttons=[])
                new.update(status="generated" if decision.reply.strip() else "no_text", text=decision.reply,
                           kind=decision.kind, button=decision.button,
                           metadata=copy.deepcopy(getattr(client, "last_chat_record", {})),
                           notes=list(getattr(client, "notes", [])))
            except Exception as error:
                new.update(status="error", error=f"{type(error).__name__}: {error}")
        new["metrics"] = text_metrics(new["text"], projects=profile.projects)
        record["new"] = new
        report["chats"].append(record)
    report["coverage"] = {"letters": len(report["letters"]), "chats": len(report["chats"]),
                          "all_cases_present": len(letters) == EXPECTED_LETTERS and len(chats) == EXPECTED_CHATS,
                          "new_letters_generated": sum(r["new"]["status"] == "generated" for r in report["letters"]),
                          "new_chat_answers_generated": sum(r["new"]["status"] == "generated" for r in report["chats"])}
    report["comparison"] = {"old_letters": _summary(report["letters"], "old"),
                            "new_letters": _summary(report["letters"], "new")}
    secrets = (cfg.ai.api_key, os.environ.get("OPENAI_API_KEY", ""), os.environ.get("HH_AI_API_KEY", ""))
    return _redact(report, secrets)


def readable_report(report: dict) -> str:
    lines = ["Проверка генерации: 10 вакансий + 5 вопросов робота", f"Режим: {report['mode']}",
             "Отправки в hh: 0. Старые тексты взяты из экспорта реальных отправок.",
             json.dumps(report["coverage"], ensure_ascii=False), "", *report["limitations"], "",
             "Объективное сравнение:", json.dumps(report["comparison"], ensure_ascii=False, indent=2)]
    for collection, label in (("letters", "Письмо"), ("chats", "Вопрос")):
        for record in report[collection]:
            lines.extend(["", "=" * 70, f"{label} {record['id']}: {record.get('title', record.get('question', ''))}",
                          "", "РАНЬШЕ:", record["old"]["text"], "", f"ТЕПЕРЬ ({record['new']['status']}):",
                          record["new"].get("text") or record["new"].get("error", "Генерация отключена")])
            if record["new"].get("metadata"):
                lines.extend(["", "Метаданные:", json.dumps(record["new"]["metadata"], ensure_ascii=False, indent=2)])
    return "\n".join(lines) + "\n"


def refresh_metrics(report: dict, cfg, resume: str) -> dict:
    """Re-score saved texts without regeneration or changes to original generation evidence.

    Returns a deep copy. Only per-side metrics and the derived comparison are replaced;
    generated texts, drafts, model metadata, analysis and generation plans stay identical.
    Full vacancy source comes from the saved analysis, not from hh.
    """
    refreshed = copy.deepcopy(report)
    with model_network_only("", live=False):
        profile = load_candidate_profile(cfg.ai)
        recent = {"old": [], "new": []}
        for record in refreshed.get("letters", []):
            source = record.get("analysis", {}).get("full_text")
            if source is None:
                source = record.get("letter_plan", {}).get("analysis", {}).get("full_text")
            if source is None:
                raise EvaluationError(f"Нет сохранённого исходного текста вакансии {record.get('id', '')}")
            analysis = analyze_vacancy(str(record.get("title", "")), str(record.get("company", "")),
                                       str(source), str(record.get("id", "")))
            for side in ("old", "new"):
                item = record[side]
                text = item.get("text", "")
                plan = make_letter_plan(analysis, profile, resume=resume, context=cfg.ai.context,
                                        recent_letters=recent[side])
                metrics = text_metrics(text, vacancy_id=str(record.get("id", "")), projects=profile.projects)
                issues = validate_letter(text, analysis, profile, plan=plan, recent_letters=recent[side],
                                         resume=resume, context=cfg.ai.context) if (
                    side == "old" or item.get("status") != "not_generated_offline") else []
                metrics.update(validation_issues=issues,
                    planned_case_referenced=(not any(issue.startswith("style:missing_case") for issue in issues))
                                             if plan.selected_projects and text else None)
                item["metrics"] = metrics
                if text.strip():
                    recent[side].append(text)
        for record in refreshed.get("chats", []):
            for side in ("old", "new"):
                record[side]["metrics"] = text_metrics(record[side].get("text", ""), projects=profile.projects)
        refreshed["comparison"] = {"old_letters": _summary(refreshed.get("letters", []), "old"),
                                    "new_letters": _summary(refreshed.get("letters", []), "new")}
    return refreshed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--exports-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume", type=Path, help="По умолчанию <config-dir>/data/resume.txt с проверкой resume.txt.id")
    parser.add_argument("--live", action="store_true", help="Разрешить только API выбранной модели; отправки в hh отсутствуют")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        cfg = load_config(args.config.resolve(), check_search_url=False, upgrade=False)
        letters, chats = load_cases(args.exports_dir)
        resume = ""
        if args.live:
            resume_path = args.resume or args.config.resolve().parent / "data" / "resume.txt"
            if args.resume is None:
                identity = Path(str(resume_path) + ".id").read_text(encoding="utf-8").strip()
                if not cfg.resume_id or identity != cfg.resume_id:
                    raise EvaluationError("Кэш резюме не соответствует выбранному resume_id")
            resume = resume_path.read_text(encoding="utf-8")
        base_url = cfg.ai.base_url or AI_PROVIDERS[cfg.ai.provider][0]
        with model_network_only(base_url, live=args.live):
            report = evaluate(cfg, letters, chats, resume, live=args.live)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        json_path, text_path = args.output_dir / "generation-evaluation.json", args.output_dir / "generation-evaluation.txt"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        text_path.write_text(readable_report(report), encoding="utf-8")
        print(f"Проверено: {len(letters)} вакансий, {len(chats)} вопросов. Отправок в hh: 0.")
        print(json_path.resolve())
        print(text_path.resolve())
        if args.live and (report["coverage"]["new_letters_generated"] != EXPECTED_LETTERS or
                          report["coverage"]["new_chat_answers_generated"] != EXPECTED_CHATS):
            return 2
        return 0
    except Exception as error:
        print(_redact(f"Ошибка проверки: {type(error).__name__}: {error}",
                      (os.environ.get("OPENAI_API_KEY", ""), os.environ.get("HH_AI_API_KEY", ""))), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
