"""The active cover-letter pipeline: evidence, instructions, bounded repair and an automatic fallback."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import re

from .application import (RUSSIAN_LETTER_ASSESSMENT, RUSSIAN_LETTER_GREETING, _project_aliases, _SKILLS,
                          _case_mentioned, _skills, analyze_vacancy, make_letter_plan, validate_letter)
from .claims import fact_judge_messages, personal_claim_issues
from .language import candidate_gender_rule, normalize_candidate_text


def _data(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return asdict(value) if is_dataclass(value) else value


def _case_description(project, *, english=False):
    data = _data(project)
    body = " ".join((str(data.get("description", "")), *data.get("tasks", ())))
    if re.search(r"почтов|отправлен|shipment", body, re.I):
        if not re.search(r"согласован|workflow|approval", body, re.I):
            return ("a shipment processing application", "a shipment processing application") if english else (
                "приложение обработки отправлений", "приложении обработки отправлений")
        return ("a postal and courier workflow application", "a postal and courier workflow application") if english else (
            "приложение согласования почтовых и курьерских отправлений", "приложении согласования почтовых и курьерских отправлений")
    if re.search(r"доставк|delivery", body, re.I):
        return ("a delivery integration service", "a delivery integration service") if english else (
            "сервис интеграций доставки", "сервисе интеграций доставки")
    if re.search(r"SIM|сим.карт", body, re.I):
        return ("a SIM request management application", "a SIM request management application") if english else (
            "приложение управления заявками на SIM-карты", "приложении управления заявками на SIM-карты")
    if re.search(r"CRM|ERPNext", body, re.I):
        return ("a corporate CRM application", "a corporate CRM application") if english else ("корпоративная CRM", "корпоративной CRM")
    if re.search(r"etl|dwh|конвейер|нормализац|pipeline", body, re.I):
        return ("a data collection and processing pipeline", "a data collection and processing pipeline") if english else (
            "конвейер сбора и обработки данных", "конвейере сбора и обработки данных")
    if re.search(r"ipfs|распредел[её]н.{0,25}хран|distributed.storage", body, re.I):
        return ("a distributed storage product", "a distributed storage product") if english else (
            "продукт распределённого хранения данных", "продукте распределённого хранения данных")
    if re.search(r"прогноз|временн.{0,15}ряд|forecast|time.series", body, re.I):
        return ("a forecasting research project", "a forecasting research project") if english else (
            "исследовательский проект прогнозирования", "исследовательском проекте прогнозирования")
    return "", ""


def _describe_names(text, profile, company, *, english=False):
    """Replace a source registry name with its sourced domain, preserving prepositions."""
    if not profile:
        return text
    links = []
    def keep_link(match):
        links.append(match.group(0))
        return f"\x00{len(links) - 1}\x00"
    text = re.sub(r"https?://[^\s<>\"«»]+", keep_link, text)
    for project in sorted(profile.projects, key=lambda item: len(item.name), reverse=True):
        nominal, locative = _case_description(project, english=english)
        for name in sorted(_project_aliases(project.name), key=len, reverse=True):
            quoted = r"[«\"“]?" + re.escape(name) + r"[»\"”]?"
            if not nominal:
                # Keep the sourced action when the project has no known domain description.
                text = re.sub(r"\b(?:в|на|для)\s+(?:(?:проекте|приложении|сервисе)\s+)?"
                              + quoted + r"\s*[,;:]?", "", text, flags=re.I)
                text = re.sub(r"\b(?:in|at|on)\s+(?:(?:the|a)\s+)?(?:project\s+)?"
                              + quoted + r"\s*[,;:]?", "", text, flags=re.I)
                text = re.sub(r"\b(?:проект|приложение|сервис|project)\s+" + quoted + r"\s*[—:-]?", "", text, flags=re.I)
                text = re.sub(r"(?<!\w)" + quoted + r"(?!\w)\s*[—:-]?", "", text, flags=re.I)
                continue
            text = re.sub(r"\b(?:в|на)\s+(?:(?:проекте|приложении|сервисе)\s+)?" + quoted,
                          "В " + locative, text, flags=re.I)
            text = re.sub(r"\b(?:in|at|on)\s+(?:(?:the|a)\s+)?(?:project\s+)?" + quoted,
                          "In " + locative, text, flags=re.I)
            text = re.sub(r"(?<!\w)" + quoted + r"(?!\w)", nominal, text, flags=re.I)
    names = [company, profile.current_employer.split("(", 1)[0].strip(), profile.former_employer]
    names.extend(item[0] for item in profile.experience_history)
    for name in sorted(set(filter(None, names)), key=len, reverse=True):
        text = re.sub(r"\b(?:в|на)\s+(?:компании\s+)?[«\"“]?" + re.escape(name) + r"[»\"”]?",
                      "в компании", text, flags=re.I)
        text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", "a company" if english else "компания", text, flags=re.I)
    return re.sub(r"\x00(\d+)\x00", lambda match: links[int(match.group(1))], text)


def _opening_keywords(analysis):
    return tuple(item["text"] for item in analysis.application_instructions if item.get("kind") == "keyword"
                 and re.search(r"\b(?:начните|начни|начать|начинаться|начаться|start|begin)\b",
                               item.get("evidence", ""), re.I))


def _relevant_stack(profile, analysis, plan):
    if not profile:
        return ()
    choices = [*analysis.required_skills]
    choices.extend(tool for project in plan.selected_projects for tool in project.get("personal_technologies", ()))
    choices.extend(profile.confirmed_skills)
    result = []
    for tool in choices:
        if tool in result or len(tool) > 35 or profile.skill_level(tool) != "hands_on":
            continue
        personal = [item for item in profile.skills if tool in item.aliases and item.level == "hands_on" and item.scope == "personal"]
        if profile.skills and not personal:
            continue
        result.append(tool)
        if len(result) == 5:
            break
    return tuple(result)


_GENERIC_ENDING = re.compile(r"^\s*(?:я\s+)?(?:буду\s+(?:очень\s+)?рад|готов\s+обсудить|предлагаю\s+обсудить|"
                             r"могу\s+(?:подробно\s+)?(?:обсудить|разобрать|рассказать)|"
                             r"не\s+могу\s+подтвердить|благодарю|спасибо|таким\s+образом|(?:этот|такой)\s+опыт.{0,45}"
                             r"(?:поможет|полезен|применим|позволит)|I\s+(?:can|would|am\s+happy\s+to)\s+discuss|"
                             r"thank\s+you|I\s+look\s+forward)", re.I)


def _skill_inventory(paragraph):
    """A bare technology inventory can be replaced; a concrete use of a tool must stay."""
    starter = (r"^\s*(?:я\s+)?(?:(?:в\s+работе\s+)?(?:использую|применяю|владею)\b|"
               r"(?:знаком|работаю)\s+с\b|(?:мои\s+)?(?:навыки|стек|компетенции)\s*[:—-])")
    detail = r"\b(?:для|чтобы|при|через|где|проект\w*|приложени\w*|разработ\w*|реализ\w*|интегр\w*|настро\w*)\b"
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", paragraph) if sentence.strip()]
    return bool(sentences) and all(re.search(starter, sentence, re.I) and not re.search(detail, sentence, re.I)
                                   for sentence in sentences)


def _task_prose(tasks):
    return " ".join(str(task).strip().rstrip(".; ") + "." for task in tasks[:2] if str(task).strip())


def _known_inventory(sentence, tools):
    """Only deduplicate complete tool names; unknown names and extra facts must survive."""
    remaining = re.sub(r"^\s*(?:я\s+)?(?:(?:в\s+работе\s+)?(?:использую|применяю|владею)\b|"
                       r"(?:знаком|работаю)\s+с\b|(?:мои\s+)?(?:навыки|стек|компетенции)\s*[:—-])",
                       "", sentence, flags=re.I)
    aliases = {alias for tool in tools for alias in _SKILLS.get(tool, (tool,))}
    if "REST API" in tools:
        aliases.add("REST")
    for alias in sorted(aliases, key=len, reverse=True):
        remaining = re.sub(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", " ", remaining, flags=re.I)
    return not re.sub(r"\b(?:и|или|and|or)\b|[\s,;/&().!?]+", "", remaining, flags=re.I)


def _inventory_skills(text):
    tools = set(_skills(text))
    if re.search(r"(?<!\w)REST(?!\w)", text, re.I):
        tools.add("REST API")
    return tools


def _natural_paragraphs(body, protected_keywords=()):
    protected = lambda text: any(re.search(re.escape(word), text, re.I) for word in protected_keywords if word)
    paragraphs = []
    for part in re.split(r"\n\s*\n", body):
        sentences = []
        for sentence in re.split(r"(?<=[.!?])\s+", part.strip()):
            if not sentence.strip():
                continue
            original = sentence
            sentence = re.sub(r"^Практический опыт связан с (?:такой )?задачей\s*:\s*", "", sentence, flags=re.I)
            if re.fullmatch(r"прикладно[её] приложение[.!]?", sentence, re.I):
                sentence = ""
            sentence = re.sub(r"^Моя часть работы\s*:\s*", "", sentence, flags=re.I)
            sentence = re.sub(r"^В работе использую\s+", "Использую ", sentence, flags=re.I)
            sentence = re.sub(r";\s+(?=[А-ЯЁ][а-яё]+(?:вал|выл|л)\b)", ". ", sentence)
            if any(re.search(re.escape(word), original, re.I) and not re.search(re.escape(word), sentence, re.I)
                   for word in protected_keywords if word):
                sentence = original
            sentence = sentence.strip()
            if sentence:
                sentences.append(sentence[0].upper() + sentence[1:])
        if sentences:
            paragraphs.append(sentences)
    # A technical task already carries its stack; a repeated inventory adds no information.
    covered = _inventory_skills(" ".join(sentence for part in paragraphs for sentence in part
                                         if not _skill_inventory(sentence)))
    result = []
    for part in paragraphs:
        kept = []
        for sentence in part:
            tools = _inventory_skills(sentence)
            if _skill_inventory(sentence):
                if tools and tools <= covered and _known_inventory(sentence, tools) and not protected(sentence):
                    continue
                covered.update(tools)
            kept.append(sentence)
        if kept:
            result.append(" ".join(kept))
    return result


ADJACENT_EXPERIENCE_PREFIX = "Из смежного опыта:"


def _adjacent_case_paragraphs(paragraphs, plan):
    """Label a sourced adjacent case once; never manufacture an empty experience heading."""
    adjacent = [project for project in plan.selected_projects if project.get('relation') == 'adjacent']
    if not adjacent:
        return paragraphs
    marker = re.compile(r"\bИз\s+смежного\s+опыта\s*[:—–-]?\s*", re.I)
    result, labelled = [], False
    for paragraph in paragraphs:
        matches_case = any(_case_mentioned(paragraph, (), (project,)) for project in adjacent)
        if matches_case:
            paragraph = marker.sub('', paragraph).strip()
            if not labelled:
                paragraph = re.sub(r'^[А-ЯЁ]', lambda match: match[0].lower(), paragraph)
                paragraph = ADJACENT_EXPERIENCE_PREFIX + ' ' + paragraph
                labelled = True
        result.append(paragraph)
    return result


def _requested_style(text, analysis, profile, plan, company, max_chars, *, hide_vacancy=False):
    if not profile:
        return text
    body = _describe_names(text, profile, company, english=analysis.language == "en")
    body = "\n\n".join(" ".join(sentence for sentence in re.split(r"(?<=[.!?])\s+", paragraph)
                                  if not _GENERIC_ENDING.search(sentence))
                            for paragraph in re.split(r"\n\s*\n", body))
    keywords = _opening_keywords(analysis)
    for word in keywords:
        body = re.sub(r"^\s*" + re.escape(word) + r"\s*", "", body, flags=re.I)
    prefix = "\n".join(keywords)
    if (analysis.language != "ru" or analysis.opening_questions or hide_vacancy
            or analysis.word_limit and analysis.word_limit < 35):
        return ((prefix + "\n\n") if prefix else "") + body.strip()
    for fixed in (RUSSIAN_LETTER_GREETING, RUSSIAN_LETTER_ASSESSMENT):
        body = body.replace(fixed, "")
    body = re.sub(r"\bСчитаю\s*,?\s*что\s+мои\s+навыки\s+и\s+компетенции\s+соответствуют\s+"
                  r"описанию\s+вакансии\s*[.!]?", "", body, flags=re.I)
    while True:
        previous = body
        body = re.sub(r"^\s*(?:Здравствуйте|Добрый\s+день|Привет)[^.!?\n]*[.!?]?\s*", "", body, flags=re.I)
        body = re.sub(r"^\s*(?:очень\s+)?заинтересовала\s+данная\s+вакансия\s*[!.]?\s*", "", body, flags=re.I)
        body = re.sub(r"^\s*(?:Меня\s+заинтересовал[ао]?|Мне\s+интересн[ао])[^.!?]*[.!?]\s*", "", body, flags=re.I)
        if body == previous:
            break
    paragraphs = _adjacent_case_paragraphs(_natural_paragraphs(body, analysis.required_keywords), plan)
    opening = RUSSIAN_LETTER_GREETING + "\n\n" + RUSSIAN_LETTER_ASSESSMENT
    opening = ((prefix + "\n\n") if prefix else "") + opening
    experience = "\n\n".join(paragraphs).strip()
    if not experience:
        stack = _relevant_stack(profile, analysis, plan)
        experience = "Работаю с " + ", ".join(stack) + "." if stack else ""
    available = max_chars - len(opening) - 4
    if len(experience) > available > 0:
        clipped = experience[:available].rsplit(" ", 1)[0]
        end = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
        experience = clipped[:end + 1] if end > available // 2 else clipped.rstrip(" ,;:") + "."
    return "\n\n".join(part for part in (opening, experience) if part)


def truthful_letter(client, analysis, plan, resume: str, context: str) -> str:
    """Use source facts even if the model is unavailable; never invent an attachment or a missing case."""
    profile = getattr(client, "profile", None)
    selected = getattr(plan, "selected_projects", ()) or ()
    project = _data(selected[0]) if selected else {}
    name = project.get("name", "") if isinstance(project, dict) else ""
    tasks = project.get("tasks", project.get("personal_contribution", ())) if isinstance(project, dict) else ()
    if isinstance(tasks, str):
        tasks = [tasks]
    skills = list(getattr(profile, "confirmed_skills", ()) or ())
    relevant = [skill for skill in analysis.required_skills
                if profile and profile.skill_level(skill) == "hands_on"]
    if not relevant:
        relevant = [skill for skill in skills if re.search(re.escape(skill), analysis.title + " " + analysis.full_text, re.I)]
    stack = ", ".join((relevant or skills)[:4])
    if analysis.language == "en":
        parts = ["I develop backend services, integrations and data-processing workflows."
                 if "Python" in stack or "Python" in resume else "I am interested in discussing the engineering tasks."]
        if stack:
            parts.append(f"My hands-on background includes {stack}.")
        domain = _case_description(project, english=True)[0] if name else ""
        if domain:
            parts.append("My contribution includes work on " + domain + ".")
    else:
        parts = []
        if tasks:
            parts.append(_task_prose(tasks))
        elif stack:
            parts.append(f"В разработке использую {stack}.")
        if not parts and stack:
            parts.append("Работаю с " + stack + ".")
    body = " ".join(parts)
    if analysis.opening_questions:
        answers = []
        for index, question in enumerate(analysis.opening_questions, 1):
            example = getattr(profile, "answer_example", lambda _: "")(question) if profile else ""
            answer = example or body
            if profile and re.search(r"аккаунт|поток|масштаб|источник", question, re.I):
                answer = (profile.answer_example("Использовали ли вы asyncio?") or body)
                answer += " Точные показатели одновременных аккаунтов и потоков в этом отклике не указываю."
            elif profile and re.search(r"антифрод|защит|обош|обход|бан", question, re.I):
                projects = profile.relevant_projects("ESM Post Service интеграции API обработка ошибок", limit=1)
                if projects:
                    case = projects[0]
                    answer = _task_prose(case.tasks)
            answers.append(f"{index}. {answer}")
        body = "\n\n".join(answers)
    return body


def generate_letter(client, resume: str, context: str, title: str, company: str, description: str,
                    max_chars: int, experience_first: bool, ai_prompt: str, *, vacancy_id: str = "",
                    recent_letters=()) -> str:
    # Imported here because client exposes the legacy public helpers used by existing callers/tests.
    from .client import (AiError, LETTER_EXPERIENCE_FIRST, LETTER_INTEREST_FIRST, LETTER_NAMING, LETTER_PROMPT,
                         LETTER_USER_RULES, PROMPT_VERSION, _WANTS_SIGNATURE, clean_letter, drop_sentences_if,
                         forbidden_mentions, letter_invented_numbers, letter_invented_tech, letter_mentions,
                         letter_vacancy)

    profile = getattr(client, "profile", None)
    analysis = analyze_vacancy(title, company, description, vacancy_id=vacancy_id)
    raw_context = client._raw_context(context)
    context = client._candidate_context(raw_context, query=title + "\n" + description)
    plan = make_letter_plan(analysis, profile, resume=resume, context=raw_context, recent_letters=recent_letters)
    # Special application questions can require more room than a normal short letter.
    limit = max(max_chars, 1800) if analysis.opening_questions else max_chars
    signature = bool(ai_prompt and _WANTS_SIGNATURE.search(ai_prompt))
    hide_vacancy, hide_company = forbidden_mentions(ai_prompt + "\n" + raw_context)
    structure = LETTER_EXPERIENCE_FIRST if experience_first else LETTER_INTEREST_FIRST
    prompt = (LETTER_PROMPT.replace("{naming}", LETTER_NAMING[hide_vacancy, hide_company])
              .replace("{structure}", structure).replace("{max_chars}", str(limit)))
    if ai_prompt.strip():
        prompt += LETTER_USER_RULES.replace("{rules}", ai_prompt.strip())
    prompt += "\n\n" + candidate_gender_rule(client.cfg)
    user = "\n\n".join([
        f"АКТУАЛЬНЫЕ ДАННЫЕ КАНДИДАТА:\n{context}",
        f"РЕЗЮМЕ (при расхождении актуальный профиль выше важнее):\n{resume or '(нет)'}",
        letter_vacancy(title, company, description, hide_vacancy, hide_company),
        "СТРУКТУРИРОВАННЫЙ ПЛАН ЗАЯВКИ:\n" + plan.render_guidance(),
    ])
    if recent_letters:
        user += ("\n\nПОСЛЕДНИЕ ПИСЬМА: заданное вступление сохраняется; меняй конкретный опыт и релевантные навыки "
                 "под новую задачу, не копируй тело прошлых писем:\n")
        user += "\n\n".join(str(letter)[:1200] for letter in list(recent_letters)[-4:])
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": user}]
    facts = client._guard_facts(resume, raw_context)
    validation_context = raw_context
    if not profile and ai_prompt.strip():
        # Older configurations stored explicit candidate achievements in this field.
        facts += "\n" + ai_prompt.strip()
        validation_context += "\n" + ai_prompt.strip()
    judge_facts = (context + "\n\nПОДТВЕРЖДЁННЫЕ ВЫБРАННЫЕ КЕЙСЫ (личный вклад и ограничения сохранены):\n"
                   + json.dumps(_data(plan).get("selected_projects", []), ensure_ascii=False)
                   + "\n\nРЕЗЮМЕ (вторичный источник):\n" + resume)
    judge_question = f"ВАКАНСИЯ: {title}. КОМПАНИЯ: {company}. Это адресат отклика, не прежняя работа.\n{description}"

    def objective(text):
        problems = personal_claim_issues(text, profile, question=description, source_facts=raw_context)
        prose = re.sub(r"https?://[^\s<>\"«»]+", "", text)
        if analysis.language != "en":
            invented = letter_invented_tech(prose, facts + "\n" + company, title)
            if invented:
                problems.append("В источниках НЕТ " + ", ".join(invented) + "; не заявляй это как свой опыт")
        numbered_body = re.sub(r"(?m)^\s*\d+[.)]\s+", "", prose)
        numbers = letter_invented_numbers(numbered_body, facts, title, company)
        if numbers:
            problems.append("Неподтверждённые цифры: " + ", ".join(numbers))
        mentions = letter_mentions(text, title, company, hide_vacancy, hide_company, resume)
        if mentions:
            problems.append("Нежелательные упоминания: " + ", ".join(mentions))
        return list(dict.fromkeys(problems))

    candidates, revisions, technical = [], [], []
    format_codes = ("objective:language", "objective:opening", "objective:questions", "objective:keyword", "objective:length")
    for attempt in range(3):
        try:
            client.last_raw = client.complete(messages, max_tokens=max(1000, min(2200, limit)))
            text = normalize_candidate_text(clean_letter(client.last_raw, limit, signature), client.cfg)
            text = _requested_style(text, analysis, profile, plan, company, limit, hide_vacancy=hide_vacancy)
        except AiError as error:
            technical.append(str(error)[:300])
            break
        facts_issues = objective(text)
        quality = validate_letter(text, analysis, profile, plan=plan, recent_letters=recent_letters,
                                  resume=resume, context=validation_context)
        facts_issues.extend(issue for issue in quality if issue.startswith("objective:")
                            and not issue.startswith(format_codes))
        # Independent language-neutral fact check; standard English words are not technologies.
        if profile and text and not facts_issues:
            try:
                judge_messages = fact_judge_messages(text, judge_facts, judge_question)
                judge_messages[0]["content"] += (" Имена работодателей и проектов намеренно заменены описанием предметной "
                                                "области. Сопоставляй личные задачи с кейсами источников по содержанию; "
                                                "отсутствие реестрового имени само по себе не является ошибкой факта.")
                raw = client.complete(judge_messages, max_tokens=400, json_mode=True)
                match = re.search(r"\{.*\}", raw, re.S)
                judged = json.loads(match.group(0)) if match else {}
                faults = judged.get("unsupported_claims", [])
                if isinstance(faults, list):
                    facts_issues.extend(str(fault)[:350] for fault in faults if isinstance(fault, str) and fault.strip())
            except (AiError, ValueError, TypeError):
                technical.append("Независимая проверка фактов недоступна; выполнены локальные проверки")
        candidates.append({"text": text, "fact_issues": facts_issues, "quality_issues": quality})
        repair_quality = quality if profile else [issue for issue in quality if issue.startswith("objective:")]
        if text and not facts_issues and not repair_quality:
            break
        if attempt == 2 or attempt == 1 and not profile:
            break
        issues = facts_issues + quality
        revisions.append(issues)
        messages += [{"role": "assistant", "content": text}, {"role": "user", "content": (
            "Исправь конкретные недостатки:\n- " + "\n- ".join(issues) +
            "\nСохрани реальные кейсы и обязательные инструкции заявки. Не дополняй факты догадками. "
            "Верни только новое письмо. Если требуемого кейса нет, раскрой смежный подтверждённый, не выдумывая его."
        )}]
    valid = [row for row in candidates if row["text"] and not row["fact_issues"]]
    if not valid and candidates and not profile:
        stripped = drop_sentences_if(candidates[-1]["text"], lambda sentence: bool(objective(sentence)))
        if stripped:
            valid.append({"text": stripped, "quality_issues": []})
    fallback = not valid
    if valid:
        chosen = min(valid, key=lambda row: (sum(issue.startswith("objective:") for issue in row["quality_issues"]),
                                             len(row["quality_issues"])))
        final = chosen["text"]
    else:
        final = normalize_candidate_text(clean_letter(truthful_letter(client, analysis, plan, resume, raw_context),
                                                     limit, signature), client.cfg)
        final = _requested_style(final, analysis, profile, plan, company, limit, hide_vacancy=hide_vacancy)
        final = drop_sentences_if(final, lambda sentence: bool(objective(sentence)))
        if not final:
            final = _requested_style("", analysis, profile, plan, company, limit, hide_vacancy=hide_vacancy)
            if not final:
                final = "Relevant experience: " + ", ".join(_relevant_stack(profile, analysis, plan)) + "."
    # Diagnostics never decide whether a selected vacancy should be sent.
    final = normalize_candidate_text(final, client.cfg)
    final_issues = validate_letter(final, analysis, profile, plan=plan, recent_letters=recent_letters,
                                  resume=resume, context=validation_context)
    critical = [issue for issue in final_issues if issue.startswith("objective:") and not issue.startswith(format_codes)]
    if critical:
        final = _requested_style("", analysis, profile, plan, company, limit, hide_vacancy=hide_vacancy)
        if not final:
            final = "Relevant experience: " + ", ".join(_relevant_stack(profile, analysis, plan)) + "."
        fallback = True
        final_issues = validate_letter(final, analysis, profile, plan=plan, recent_letters=recent_letters,
                                      resume=resume, context=validation_context)
    client.last_letter_record = {
        "vacancy_id": vacancy_id, "title": title, "company": company, "prompt_version": PROMPT_VERSION,
        "model": client.model, "generated_at": datetime.now(timezone.utc).isoformat(),
        "analysis": _data(analysis), "plan": _data(plan), "draft": candidates[0]["text"] if candidates else "",
        "final_text": final, "validation_issues": final_issues, "revision_count": len(revisions),
        "revisions": revisions, "fallback_used": fallback, "technical_notes": technical,
        "matching_is_advisory_only": True, "requires_approval": False,
        "profile_schema_version": getattr(profile, "schema_version", ""),
        "profile_updated_at": getattr(profile, "updated_at", ""),
    }
    return final
