"""Pure vacancy analysis, factual case selection and advisory letter diagnostics.

No account, model, delivery or filesystem access belongs here. A low fit or a quality issue
never means skip/review: callers automatically improve and send the best factual draft.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
import json
import re
from typing import Any, Iterable, Mapping

from .profile import _TECH_ALIASES, _technology_contains


_SKILLS = {
    "Python": ("python",), "FastAPI": ("fastapi",), "Flask": ("flask",), "Django": ("django",),
    "DRF": ("django rest framework", "drf"), "Frappe": ("frappe",), "ERPNext": ("erpnext",),
    "REST API": ("rest api", "rest/soap", "http/rest"), "SOAP": ("soap",), "OAuth": ("oauth",),
    "PostgreSQL": ("postgresql", "postgres",), "SQL": ("sql",), "MS SQL Server": ("ms sql", "sql server"),
    "MySQL": ("mysql",), "MongoDB": ("mongodb",), "SQLAlchemy": ("sqlalchemy",), "Alembic": ("alembic",),
    "Redis": ("redis",), "RabbitMQ": ("rabbitmq",), "Celery": ("celery",), "APScheduler": ("apscheduler",),
    "asyncio": ("asyncio",), "aiohttp": ("aiohttp",), "httpx": ("httpx",), "requests": ("requests",),
    "Aiogram": ("aiogram",), "Pytest": ("pytest",), "Docker": ("docker",),
    "Docker Compose": ("docker compose", "docker-compose", "docker/compose"),
    "Git": ("git",), "GitLab CI/CD": ("gitlab ci/cd", "gitlab"),
    "GitHub Actions": ("github actions",), "CI/CD": ("ci/cd", "ci-cd"), "Linux": ("linux",),
    "Nginx": ("nginx",), "MinIO": ("minio",), "S3": ("s3",), "IPFS": ("ipfs", "kubo"),
    "JavaScript": ("javascript",), "TypeScript": ("typescript",), "React": ("react",), "Next.js": ("next.js",),
    "PHP": ("php",), "Laravel": ("laravel",), "Talend": ("talend",),
    "Talend Studio": ("talend studio",), "Talend Data Quality": ("talend data quality",),
    "ClickHouse": ("clickhouse",), "dbt": ("dbt",), "Kafka": ("kafka",), "Flink": ("flink",),
    "Iceberg": ("iceberg",), "Spark": ("spark",), "PySpark": ("pyspark",), "Hadoop": ("hadoop",),
    "Hive": ("hive",), "Airflow": ("airflow",), "Kubernetes": ("kubernetes",), "Debezium": ("debezium",),
    "ETL": ("etl", "elt"), "DWH": ("dwh",), "Pandas": ("pandas",), "NumPy": ("numpy",),
    "BERT": ("bert", "bert-mini"), "LSTM": ("lstm",), "Prophet": ("prophet",),
    "PyTorch": ("pytorch",), "TensorFlow": ("tensorflow", "keras"), "scikit-learn": ("scikit-learn",),
    "Sherpa RPA": ("sherpa",), "UiPath": ("uipath",), "n8n": ("n8n",), "LangChain": ("langchain",),
    "Битрикс24": ("битрикс24", "bitrix24"), "Claude Code": ("claude code",),
    "FFmpeg": ("ffmpeg",), "TTS": ("tts",), "Whisper": ("whisper", "faster-whisper"),
    "ComfyUI": ("comfyui",), "ADB": ("adb",), "Appium": ("appium",),
    "UiAutomator2": ("uiautomator2",), "Multilogin": ("multilogin",), "AdsPower": ("adspower",),
    "GeeLark": ("geelark",), "Playwright": ("playwright",), "Puppeteer": ("puppeteer",),
    "tls-client": ("tls-client",), "curl_cffi": ("curl_cffi",), "Prometheus": ("prometheus",),
}
for _name in (
    "Power BI", "Power Query", "Power Query M", "Business Intelligence", "HR Analytics", "R", "RStudio",
    "Go", "C++", "Delphi", "Object Pascal", "Java", "OpenAPI", "Swagger", "pytest-django", "Ruff", "Pyodide",
    "Express.js", "WireGuard", "Linux Networking", "NAT", "IP Routing", "nftables", "TUN/TAP", "IPAM",
    "YooKassa", "TOTP", "2FA", "CSRF", "CSP", "RBAC", "ACL", "Idempotency", "Transactional Outbox",
    "Cursor", "Codex", "Prompt engineering", "AI-assisted development",
):
    if _name in _TECH_ALIASES:
        _SKILLS.setdefault(_name, _TECH_ALIASES[_name])

_SPECIALIZED = frozenset({"PHP", "Laravel", "Talend", "Talend Studio", "Talend Data Quality", "ClickHouse",
    "dbt", "Kafka", "Flink", "Iceberg", "Spark", "PySpark", "Hadoop", "Hive", "Airflow", "Kubernetes",
    "Debezium", "Sherpa RPA", "UiPath", "n8n", "LangChain", "Битрикс24", "Claude Code", "FFmpeg",
    "TTS", "Whisper", "ComfyUI", "ADB", "Appium", "UiAutomator2", "Multilogin", "AdsPower", "GeeLark",
    "Playwright", "Puppeteer", "tls-client", "curl_cffi"})
_HEADINGS = (
    ("preferred", r"будет (?:плюсом|преимуществом)|будет преимуществ|желательн|дополнительно|nice.to.have|preferred"),
    ("required", r"обязательно|требования|что (?:для нас важно|ждем|ждём|мы ожидаем)|наши ожидания|"
                 r"чего мы ждем|чего мы ждём|мы ожидаем|тебе подходит|requirements|must.have|what we expect"),
    ("responsibilities", r"чем.{0,25}заниматься|что.{0,25}делать|предстоит|результаты.{0,30}отвечаешь|"
                         r"задача|responsibilities|what you.ll do"),
    ("instructions", r"как откликнуться|условие для отклика|как подать|how to apply|application instructions"),
    ("conditions", r"условия|что (?:мы|я) (?:предлага|даю)|мы предлагаем|что предлагаем|benefits|we offer|наши офисы"),
)
_EXPERIENCE = re.compile(r"(?:от|не менее|минимум|at least)\s+(?:\d+|тр[её]х|двух|четыр[её]х|пяти)"
                         r"[-\s]*(?:х|ех)?\s*(?:лет|год|years?)|production[- ](?:опыт|проект)|"
                         r"промышленн\w*\s+(?:опыт|эксплуатац)|\b(?:в|до)\s+прод(?:акшн|акшен|а)?\b|"
                         r"экспертном уровне", re.I)
_URL = re.compile(r"https?://[^\s<>\"«»]+", re.I)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    result, seen = [], set()
    for value in values:
        value = value.strip()
        if value and value.casefold() not in seen:
            result.append(value)
            seen.add(value.casefold())
    return tuple(result)


def _norm(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "section", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "section", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def normalize_vacancy_text(text: str) -> str:
    """Keep the complete description, including instructions at its very end."""
    if re.search(r"</?(?:p|div|li|br|h[1-6]|strong|ul)\b", text, re.I):
        parser = _PlainText()
        parser.feed(text)
        text = "".join(parser.parts)
    lines = [re.sub(r"[\t\xa0 ]+", " ", line).strip() for line in text.replace("\r", "").splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _has_skill(text: str, name: str) -> bool:
    return any(_technology_contains(text, alias) for alias in _SKILLS.get(name, (name,)))


def _skills(text: str) -> tuple[str, ...]:
    return tuple(name for name in _SKILLS if _has_skill(text, name))


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {key: [] for key in ("intro", "required", "preferred", "responsibilities",
                                                        "instructions", "conditions")}
    current = "intro"
    for line in text.splitlines():
        if not line.strip():
            continue
        header = re.sub(r"^[^\w]+|[\s:!]+$", "", line.casefold())
        detected = next((kind for kind, pattern in _HEADINGS
                         if len(header) < 160 and re.search(pattern, header, re.I)), None)
        if detected and not line.lstrip().startswith(("—", "●", "-", "•")):
            current = detected
        else:
            sections[current].append(re.sub(r"^[—●•*\-]\s*", "", line))
    return sections


def _question_block(text: str) -> tuple[tuple[str, ...], bool, str]:
    marker = re.search(r"(?:начните|начни|начать|в начале|start|begin|ответьте|ответить).{0,230}?"
                       r"(?:вопрос[а-я]*|questions?)\s*[:.]", text, re.I | re.S)
    if not marker:
        return (), False, ""
    block = text[marker.end():]
    questions = []
    markers = list(re.finditer(r"(?m)^\s*(\d{1,2})[.)]\s+", block))
    for index, match in enumerate(markers):
        body = block[match.end():markers[index + 1].start() if index + 1 < len(markers) else len(block)].strip()
        if "?" in body:
            body = body.split("?", 1)[0] + "?"
        else:
            body = body.split("\n\n", 1)[0]
        if body:
            questions.append(" ".join(body.split()))
    opening = bool(re.search(r"начни|начать|в начале|start|begin", marker[0], re.I))
    return tuple(questions[:12]), opening, " ".join(marker[0].split())


@dataclass(frozen=True)
class ApplicationAnalysis:
    vacancy_id: str
    title: str
    company: str
    full_text: str
    role_family: str = "software_engineering"
    product_or_domain: str = ""
    business_problem: str = ""
    responsibilities: tuple[str, ...] = ()
    required_skills: tuple[str, ...] = ()
    preferred_skills: tuple[str, ...] = ()
    critical_requirements: tuple[str, ...] = ()
    employment_type: tuple[str, ...] = ()
    contract_terms: tuple[str, ...] = ()
    work_format: tuple[str, ...] = ()
    location_constraints: tuple[str, ...] = ()
    application_instructions: tuple[dict, ...] = ()
    required_questions: tuple[str, ...] = ()
    opening_questions: tuple[str, ...] = ()
    language: str = "ru"
    word_limit: int | None = None
    required_keywords: tuple[str, ...] = ()
    requires_portfolio: bool = False
    requires_video_samples: bool = False
    vacancy_tone: str = "neutral"
    evidence: tuple[str, ...] = ()
    matching_is_advisory_only: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    def render_guidance(self) -> str:
        parts = [f"Язык письма: {self.language}. Тон: {self.vacancy_tone}.",
                 f"Задача работодателя: {self.business_problem}",
                 "Инструкции объявления важнее стандартного приветствия, концовки и обычной длины."]
        if not self.full_text:
            parts.append("Описание объявления недоступно. Опирайся на известную должность и факты кандидата, "
                         "не придумывай задачи работодателя.")
        if self.opening_questions:
            parts.append("НАЧНИ С ОТВЕТОВ ПО ПОРЯДКУ, без приветствия перед ними:")
            parts.extend(f"{index}. {question}" for index, question in enumerate(self.opening_questions, 1))
        elif self.required_questions:
            parts.append("Ответь на вопросы по порядку: " + " | ".join(self.required_questions))
        parts.extend("Условие отклика: " + item["text"] for item in self.application_instructions
                     if item["kind"] not in {"questions", "language"})
        if self.word_limit:
            parts.append(f"Ограничение работодателя: не более {self.word_limit} слов.")
        if self.critical_requirements:
            parts.append("Критичные требования, которые нельзя подменять смежными технологиями: "
                         + "; ".join(self.critical_requirements))
        parts.append("Не добавляй стандартную фразу о ГПХ. Условия сотрудничества упоминай лишь по прямому вопросу.")
        return "\n".join(parts)

    @property
    def prompt_guidance(self) -> str:
        return self.render_guidance()


def analyze_vacancy(title: str, company: str, description: str, vacancy_id: str = "") -> ApplicationAnalysis:
    text = normalize_vacancy_text(description or "")
    sections = _sections(text)
    main = "\n".join(sections["required"] + sections["responsibilities"])
    if not main:
        main = text
    preferred = _skills("\n".join(sections["preferred"]))
    required = _skills(main)
    all_skills = _skills(f"{title}\n{text}")
    critical = [skill for skill in required if skill in _SPECIALIZED]
    critical.extend(line for line in sections["required"] if _EXPERIENCE.search(line) or re.search(
        r"практический опыт.{0,50}(?:видео|арбитраж|регер)|личный опыт.{0,50}(?:видео|пайплайн)|"
        r"можете показать.{0,35}(?:ролик|работу)|повторяемого выпуска", line, re.I))
    instructions: list[dict] = []
    evidence: list[str] = []
    questions, start_with_questions, question_marker = _question_block(text)
    if questions:
        instructions.append({"kind": "questions", "text": " | ".join(questions), "required": True,
                             "position": "opening" if start_with_questions else "body", "evidence": question_marker})
        evidence.append(question_marker)
    english = re.search(r"(?:сопроводительн[а-я]*.{0,35}?письм[а-я]*|cover\s+letter)[^.\n]{0,150}?"
                        r"(?:на английск[а-я]*|in English)", text, re.I)
    reverse_english = re.search(r"(?:напишите|пишите|write|submit|send)[^.\n]{0,100}"
                                r"(?:английск[а-я]*|English)[^.\n]{0,70}(?:письм|letter)", text, re.I)
    english = english or reverse_english
    latin = len(re.findall(r"[a-z]", text, re.I))
    cyrillic = len(re.findall(r"[а-яё]", text, re.I))
    language = "en" if english or latin > 100 and cyrillic / max(latin + cyrillic, 1) < .12 else "ru"
    if english:
        instructions.append({"kind": "language", "text": "Письмо на английском", "required": True,
                             "evidence": english[0]})
        evidence.append(english[0])
    portfolio = bool(re.search(r"портфолио|примеры\s+(?:законченных\s+|готовых\s+)?(?:работ|AI[- ]?видео|видео)|"
                              r"portfolio|completed.{0,20}video", text, re.I))
    video = portfolio and bool(re.search(r"(?:AI[- ]?видео|видеопайплайн|video pipeline|completed.{0,20}video)",
                                        text, re.I))
    for line in sections["instructions"]:
        if re.search(r"прилож|пример|описание|разбор|информаци|личный вклад|NDA|attach|include|portfolio", line, re.I):
            instructions.append({"kind": "material", "text": line, "required": True, "evidence": line})
            evidence.append(line)
    if portfolio and not any(item["kind"] == "material" for item in instructions):
        line = next((line for line in text.splitlines() if re.search(r"портфолио|примеры.{0,40}(?:работ|видео)|portfolio",
                                                                   line, re.I)), "Запрошены примеры работ")
        instructions.append({"kind": "portfolio", "text": line, "required": True, "evidence": line})
    keywords = []
    for match in re.finditer(r"\b(?:начните|начни|начать|начинаться|начаться|укажите|добавьте|include|start|begin)\b"
                             r".{0,65}(?:слов[а-я]*|фраз[а-я]*|word)"
                             r"\s*[«\"“]([^»\"”\n]{1,80})[»\"”]", text, re.I):
        word = match[1].strip()
        if not re.search(r"парол|ключ|token|secret|ignore|инструкц|https?", word, re.I):
            keywords.append(word)
            instructions.append({"kind": "keyword", "text": word, "required": True, "evidence": match[0]})
    word_match = re.search(r"(?:до|не более|max(?:imum)?|up to)\s*(\d{2,4})\s*(?:слов|words)", text, re.I)
    word_limit = int(word_match[1]) if word_match else None
    employee = bool(re.search(r"по ТК|ТК РФ|в штат|официальное оформление", text, re.I))
    contractor = bool(re.search(r"ГПХ|независимым подрядчиком|самозанят|contractor", text, re.I))
    employment = tuple(label for flag, label in ((employee, "employee"), (contractor, "contractor")) if flag)
    contract_lines = tuple(line for line in text.splitlines()
                           if re.search(r"по ТК|ТК РФ|в штат|ГПХ|официальное оформление|подрядчик|самозанят", line, re.I))
    work = tuple(label for pattern, label in ((r"удален|удалён|remote", "remote"), (r"гибрид|hybrid", "hybrid"),
                                              (r"(?:работ[а-я]*|формат)\s+в офисе|офисный", "office"))
                 if re.search(pattern, text, re.I))
    location = tuple(line for line in text.splitlines() if re.search(
        r"за пределами|часовой пояс|МСК\s*[±+-]|пересечение с командой|прожива[а-я]*\s+(?:в|за)", line, re.I))
    query = f"{title}\n{text}"
    family = "software_engineering"
    domains = ((r"AI[- ]?Video|генеративного видео|видеопайплайн", "ai_video", "AI-видеопроизводство"),
               (r"\b(?:AI|ИИ)[\s/-]*Product[\s/-]*Engineer\b|\bProduct[\s/-]*Engineer[^\n]{0,35}\b(?:AI|ИИ)\b",
                "ai_product", "разработка AI-продукта"),
               (r"\bProduct[\s/-]*Engineer\b|продуктов[а-я]*\s+инженер", "product_engineering", "продуктовая разработка"),
               (r"\bRPA\b|роботизац", "rpa", "автоматизация бизнес-процессов"),
               (r"Data Scientist|Data Analyst|ML-модел", "data_science", "ML и аналитика"),
               (r"Data Engineer|datahub|\bTalend\b|dbt-модел", "data_engineering", "платформа данных / ETL"),
               (r"Anti.Fraud|антифрод|ферм[аы]", "automation", "R&D-автоматизация"),
               (r"интеграции|интеграций", "integrations", "внешние API и бизнес-процессы"),
               (r"Claude Code|AI-engineer|AI-разработчик", "ai_automation", "AI и бизнес-автоматизация"),
               (r"backend|FastAPI|VPN", "python_backend", "backend-сервисы"))
    domain = ""
    # Title determines specialization first, then the complete body supplies additional context.
    for source in (title, query):
        found = next(((kind, label) for pattern, kind, label in domains if re.search(pattern, source, re.I)), None)
        if found:
            family, domain = found
            break
    for pattern, label in ((r"VPN-сервис", "VPN-сервис"), (r"iGaming", "iGaming / аналитическая платформа"),
                           (r"\bWMS\b|фулфилмент", "WMS / логистические интеграции"),
                           (r"Wildberries.{0,15}Ozon|маркетплейс", "маркетплейсы и бизнес-процессы"),
                           (r"Битрикс24|CRM", "корпоративная CRM")):
        if re.search(pattern, text, re.I):
            domain = label
            break
    business = next(iter(sections["responsibilities"]), "") or next(iter(sections["intro"]), "")
    evidence.extend(line for line in text.splitlines()
                    if any(_has_skill(line, skill) for skill in critical if skill in _SKILLS))
    tone = "informal" if re.search(r"Привет\.|Меня зовут|без.{0,15}бюрократи|фаундер|\bты\b", text, re.I) else \
        "formal" if re.search(r"Успешному кандидату|Мы ожидаем, что Вы|Оформление по ТК", text, re.I) else "neutral"
    return ApplicationAnalysis(str(vacancy_id), title, company, text, family, domain, business,
        tuple(sections["responsibilities"]), required or all_skills, preferred, _unique(critical), employment,
        contract_lines, work, location, tuple(instructions), questions, questions if start_with_questions else (),
        language, word_limit, _unique(keywords), portfolio, video, tone, _unique(evidence))


def _mapping(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if callable(getattr(value, "to_dict", None)):
        return value.to_dict()
    return asdict(value) if is_dataclass(value) else {}


def _profile_data(profile: Any) -> dict:
    return _mapping(profile) if profile is not None else {}


def _level(profile: Any, data: dict, skill: str) -> str:
    lookup = getattr(profile, "skill_level", None)
    if callable(lookup):
        return lookup(skill) or "unknown"
    for item in data.get("skills", ()):
        item = _mapping(item)
        if _norm(item.get("name", "")) == _norm(skill) or any(_norm(alias) == _norm(skill) for alias in item.get("aliases", ())):
            return item.get("level", "unknown")
    if any(_has_skill(str(item), skill) for item in data.get("confirmed_skills", ())):
        return "hands_on"
    competencies = data.get("competencies", {})
    for category in competencies.values() if isinstance(competencies, Mapping) else ():
        for level in ("needs_confirmation", "familiar", "hands_on"):
            if any(_has_skill(str(item), skill) for item in category.get(level, ())):
                return level
    return "unknown"


def _project_dict(project: Any) -> dict:
    raw = _mapping(project)
    result = {key: raw.get(key, "") for key in ("id", "name", "description", "period", "role", "evidence", "kind")}
    result["technologies"] = list(raw.get("technologies", raw.get("technology", ())))
    result["personal_technologies"] = list(raw.get("personal_technologies", ()))
    result["team_technologies"] = list(raw.get("team_technologies", ()))
    tasks = raw.get("tasks", raw.get("personal_contribution", ()))
    result["tasks"] = [str(task) for task in tasks if not re.search(
        r"ориентировоч|потенциальн|\d+\s*(?:пользовател|клиент|регион|rps)", str(task), re.I)]
    limits = raw.get("limitations", raw.get("restriction", ()))
    result["limitations"] = [limits] if isinstance(limits, str) and limits else list(limits or ())
    result["allowed_urls"] = list(raw.get("allowed_urls", ()))
    result["metrics"] = [metric for metric in raw.get("metrics", ())
                         if _mapping(metric).get("status") in {"confirmed", "verified"}]
    return result


def _tokens(text: str) -> set[str]:
    return {word[:8] for word in re.findall(r"[a-zа-яё][\w.+/-]{3,}", _norm(text))
            if word not in {"работать", "работал", "работы", "разработка", "проект", "опыт", "система"}}


def _recent_texts(recent_letters: Iterable[Any]) -> list[str]:
    texts = []
    for item in recent_letters or ():
        text = (item.get("final_text") or item.get("letter_text") or item.get("text") or "") \
            if isinstance(item, Mapping) else str(item)
        if text:
            texts.append(text)
    return texts[-20:]


_PRODUCT_FAMILIES = {"ai_product", "product_engineering"}
_PRODUCT_TASKS = {
    "interface": r"\bUI\b|\bUX\b|front[- ]?end|фронтенд|интерфейс|пользовательск\w*\s+сценари|user.flow",
    "backend": r"back[- ]?end|б[эе]кенд|серверн|\bAPI\b|full[- ]?stack",
    "prototype": r"прототип|\bMVP\b|proof.of.concept|prototype",
    "hypothesis": r"гипотез|эксперимент|обратн\w*\s+связ|feedback|user.research|исследован\w*\s+пользовател",
    "iteration": r"релиз|итерац|доработ\w*.{0,25}продукт|запуск\w*.{0,25}продукт|end.to.end",
    "ai_feature": r"\bLLM\b|\bRAG\b|языков\w*\s+модел|(?:AI|ИИ)[- ]?(?:функц|ассистент|интеграц)|"
                  r"(?:встро|интегр)[^.!?\n]{0,60}(?:\bAI\b|\bИИ\b|OpenAI|модел)|OpenAI\s+API",
}


def _product_signals(text: str) -> set[str]:
    return {key for key, pattern in _PRODUCT_TASKS.items() if re.search(pattern, text, re.I)}


def _personal_project_text(project: dict) -> str:
    # The registry's full project stack can include another team member's tools.
    return "\n".join([*project["tasks"], *project["personal_technologies"]])


def _product_evidence(project: dict, analysis: ApplicationAnalysis) -> tuple[set[str], set[str], bool]:
    personal = _personal_project_text(project)
    signals = _product_signals(personal)
    if any(_has_skill(" ".join(project["personal_technologies"]), skill)
           for skill in ("React", "Next.js")):
        signals.add("interface")
    if any(_has_skill(" ".join(project["personal_technologies"]), skill)
           for skill in ("FastAPI", "Django", "Flask", "REST API")):
        signals.add("backend")
    target = "\n".join(analysis.responsibilities) or analysis.business_problem or analysis.full_text
    wanted = _product_signals(target)
    sections = _sections(analysis.full_text)
    # A required LLM feature can be stated under requirements, rather than repeated among daily tasks.
    if "ai_feature" in _product_signals("\n".join(sections["required"] + sections["intro"])):
        wanted.add("ai_feature")
    product_work = bool(("interface" in signals and "backend" in signals)
                        or ("prototype" in signals and signals & {"interface", "hypothesis", "iteration"})
                        or {"hypothesis", "iteration"} <= signals)
    direct = product_work and bool(signals & wanted or not wanted)
    if analysis.role_family == "ai_product" and "ai_feature" in wanted:
        direct = direct and "ai_feature" in signals
    return signals, wanted, direct


def _project_relation(project: dict, analysis: ApplicationAnalysis) -> tuple[str, str]:
    if analysis.role_family in _PRODUCT_FAMILIES:
        _, wanted, direct = _product_evidence(project, analysis)
        if direct:
            reason = "Личные задачи кейса совпадают с продуктовыми задачами вакансии."
        elif analysis.role_family == "ai_product" and "ai_feature" in wanted:
            reason = "Есть переносимые инженерные задачи, но личный AI-продуктовый кейс с требуемой LLM-функцией не подтверждён."
        else:
            reason = "Есть переносимые инженерные задачи, но личный продуктовый цикл/интерфейс/прототип не подтверждён."
        return ("direct" if direct else "adjacent"), reason
    specialty = [skill for skill in analysis.critical_requirements if skill in _SPECIALIZED]
    direct = not specialty or any(_has_skill(_personal_project_text(project), skill) for skill in specialty)
    return ("direct" if direct else "adjacent"), (
        "Совпадают подтверждённые личные задачи и технологии." if direct else
        "Личные задачи переносимы, но требуемая специализация в этом кейсе не подтверждена.")


def _target_tasks(analysis: ApplicationAnalysis, project: dict) -> list[str]:
    tasks = list(analysis.responsibilities)
    if not tasks:
        tasks = [analysis.business_problem] if analysis.business_problem else []
    personal = _personal_project_text(project)
    tokens, signals = _tokens(personal), _product_signals(personal)
    return sorted(tasks, key=lambda item: (len(signals & _product_signals(item)),
                                          len(tokens & _tokens(item))), reverse=True)[:2]


@dataclass(frozen=True)
class LetterPlan:
    analysis: ApplicationAnalysis
    selected_projects: tuple[dict, ...] = ()
    strongest_matches: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    critical_gaps: tuple[str, ...] = ()
    evidence_levels: dict[str, str] = field(default_factory=dict)
    fit_score: float = 0.0
    missing_materials: tuple[str, ...] = ()
    approved_urls: tuple[str, ...] = ()
    variation_guidance: tuple[str, ...] = ()
    matching_is_advisory_only: bool = True
    automatic: bool = True
    requires_approval: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def render_guidance(self) -> str:
        parts = [self.analysis.render_guidance(),
                 "Подбор соответствия — только диагностика и выбор аргументов. Отклик автоматический при любом fit.",
                 "Письмо от первого лица: обычно 60–110 слов, допустимо 40–160; специальные инструкции важнее.",
                 "Свяжи одну задачу работодателя с одним конкретным подтверждённым кейсом, описав его без собственных имён "
                 "работодателей и проектов; не пересказывай стек. Реестровые названия ниже нужны для проверки источников."]
        if self.analysis.language == "ru" and not self.analysis.opening_questions:
            parts.append("Обычное русское письмо: «" + RUSSIAN_LETTER_GREETING + "», затем «" +
                         RUSSIAN_LETTER_ASSESSMENT + "». Далее 1–2 конкретных абзаца о реальных задачах: "
                         "навыки и технологии показывай в контексте действий. Отдельный список навыков допустим "
                         "только если он полезен и не дублирует опыт; шаблонный финал не нужен. "
                         "Не подменяй предметную область канцелярским ярлыком «прикладное приложение». "
                         "Без вывода и приглашения к общению. "
                         "Начальное кодовое слово вакансии поставь отдельной первой строкой. "
                         "Явные вопросы, язык и ограничение формата вакансии имеют приоритет.")
        if self.strongest_matches:
            parts.append("Подтверждённые совпадения: " + ", ".join(self.strongest_matches))
        for project in self.selected_projects:
            parts.append("Релевантный кейс (" + project["relation"] + "): " + json.dumps(project, ensure_ascii=False))
        if self.selected_projects:
            parts.append("Свяжи личное действие выбранного кейса с 1–2 конкретными задачами вакансии из target_tasks. "
                         "Покажи, какая часть задачи переносима; не приписывай кейсу новый продукт или внедрение.")
            if self.selected_projects[0]["relation"] == "adjacent":
                parts.append("Перед описанием смежного кейса в русском письме поставь точную фразу «Из смежного опыта:». "
                             "Не заменяй её синонимом и не оставляй без следующего за ней реального кейса. "
                             "Обязательный язык и начальные ответы на вопросы вакансии сохраняют приоритет.")
        if self.analysis.role_family in _PRODUCT_FAMILIES:
            parts.append("Для Product Engineer опирайся на реальные задачи продукта: интерфейс и backend, "
                         "прототип, проверку гипотез, обратную связь или итерации — только подтверждённые в личном вкладе. "
                         "Cursor, Codex и prompt engineering могут описывать рабочий процесс разработки, "
                         "но сами по себе не доказывают личную интеграцию LLM, AI-функции или выпуск AI-продукта. "
                         "Не приписывай эти инструменты старому проекту, если такой связи нет в источнике.")
        if self.critical_gaps:
            parts.append("Не заявляй как свой production-опыт неподтверждённые требования: " + "; ".join(self.critical_gaps))
        if self.missing_materials:
            parts.append("В профиле нет подтверждённых материалов: " + "; ".join(self.missing_materials)
                         + ". Не выдумывай работы/ссылки/вложения и не обещай их показать; раскрой смежную практику. "
                         "Не объясняй отсутствие примеров вымышленным NDA.")
        parts.append("Не вставляй названия кейсов и работодателей в письмо: передай предметную область, свою задачу и решение. "
                     "Стек командного проекта не равен личному опыту каждого компонента. Сохрани ограничения кейса, "
                     "различай исследование, прототип, пилот и production; не добавляй метрики или сроки от себя.")
        parts.extend(self.variation_guidance)
        return "\n".join(parts)

    @property
    def prompt_guidance(self) -> str:
        return self.render_guidance()


def make_letter_plan(analysis: ApplicationAnalysis, profile: Any, *, resume: str = "", context: str = "",
                     recent_letters: Iterable[Any] = ()) -> LetterPlan:
    data = _profile_data(profile)
    levels = {skill: _level(profile, data, skill) for skill in analysis.required_skills}
    strong = tuple(skill for skill, level in levels.items() if level == "hands_on")
    gaps = tuple(f"{skill}: {level}" for skill, level in levels.items() if level != "hands_on")
    critical = tuple(requirement for requirement in analysis.critical_requirements
                     if requirement not in levels or levels[requirement] != "hands_on")
    weights = {skill: 3 if skill in analysis.critical_requirements else 1 for skill in levels}
    total = sum(weights.values()) + 2 * sum(requirement not in levels for requirement in analysis.critical_requirements)
    score = sum(weights[skill] * (1 if level == "hands_on" else .35 if level == "familiar" else 0)
                for skill, level in levels.items()) / total if total else 0.0
    query = f"{analysis.title}\n{analysis.product_or_domain}\n{analysis.business_problem}\n{analysis.full_text}"
    query_tokens = _tokens(query)
    projects = [_project_dict(item) for item in data.get("projects", ())]
    if not projects and callable(getattr(profile, "relevant_projects", None)):
        projects = [_project_dict(item) for item in profile.relevant_projects(query, limit=8)]
    family_terms = {
        "python_backend": r"api|распредел|доступ|docker|fastapi|микросервис|токен",
        "integrations": r"достав|курьер|почтов|интеграц|статус|провайдер|workflow",
        "ai_automation": r"маркетплейс|автоматизац|бизнес|workflow|данн|etl",
        "rpa": r"workflow|бизнес|заявк|валидац|импорт|роль|автоматизац",
        "data_engineering": r"etl|dwh|качество|дедуп|данн|конвейер|pipeline",
        "data_science": r"bert|lstm|prophet|прогноз|классификац|исследова|модел|ml",
        "ai_video": r"конвейер|pipeline|очеред|воркер|статус|обработк|api|ml",
        "automation": r"асинхрон|сбор|скрейп|автоматизац|очеред|отказ|разделен",
    }.get(analysis.role_family, r"api|бизнес|данн")
    recent = _recent_texts(recent_letters)
    ranked = []
    for index, project in enumerate(projects):
        body = "\n".join([str(project.get("name", "")), str(project.get("description", "")), *project["tasks"]])
        overlap = len(_tokens(body) & query_tokens)
        tech_overlap = sum(2 for skill in analysis.required_skills if _has_skill(" ".join(project["technologies"]), skill))
        domain = len(re.findall(family_terms, body, re.I))
        penalty = min(sum(project["name"] in old for old in recent[-5:]), 3)
        specialty_bonus = 0
        if analysis.role_family == "ai_automation":
            if re.search(r"Wildberries|Ozon|маркетплейс", query, re.I) and re.search(
                    r"marketplace|маркетплейс|e-commerce", body, re.I):
                specialty_bonus += 18
            if re.search(r"CRM|Битрикс24|Bitrix24", query, re.I) and re.search(
                    r"CRM|корпоратив|workflow|роли", body, re.I):
                specialty_bonus += 12
        if analysis.role_family in _PRODUCT_FAMILIES:
            personal = _personal_project_text(project)
            signals, wanted, direct = _product_evidence(project, analysis)
            # Count distinct personal capabilities, not repetitions in a long corporate description.
            overlap = min(len(_tokens(personal) & query_tokens), 12)
            tech_overlap = sum(2 for skill in analysis.required_skills if _has_skill(personal, skill))
            domain = 0
            specialty_bonus += 8 * len(signals & wanted) + (20 if direct else 0)
            specialty_bonus += 4 * len(signals & {"interface", "prototype", "hypothesis", "iteration"})
            if "ai_feature" in wanted and "ai_feature" in signals:
                specialty_bonus += 12
        unfinished = " ".join([str(project.get("period", "")), str(project.get("kind", "")),
                               *project["limitations"]])
        if re.search(r"в разработке|in.progress|планируется|planned|детали реализации не подтверждены", unfinished, re.I):
            penalty += 10
        ranked.append((overlap + tech_overlap + min(domain, 14) * 2 + specialty_bonus - penalty, -index, project))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = []
    for project_score, _, project in ranked[:2]:
        if project_score <= 0 or not project.get("name"):
            continue
        project = dict(project)
        project["selection_score"] = project_score
        project["relation"], project["relation_reason"] = _project_relation(project, analysis)
        project["target_tasks"] = _target_tasks(analysis, project)
        project["tasks"] = sorted(project["tasks"], key=lambda task: len(_tokens(task) & query_tokens), reverse=True)[:4]
        selected.append(project)
    approved = _unique([*data.get("approved_urls", ()),
                        *(url for project in projects for url in project.get("allowed_urls", ()))])
    video_urls = [url for project in projects if re.search(r"video|видео|ролик", " ".join(
                    str(project.get(key, "")) for key in ("kind", "name", "description")), re.I)
                  for url in project.get("allowed_urls", ())]
    missing = []
    if analysis.requires_video_samples and not video_urls:
        missing.append("законченные AI-видео и подтверждённое личное видеопортфолио")
    elif analysis.requires_portfolio and not approved:
        missing.append("доступные ссылки/вложения портфолио")
    variations = []
    if recent:
        openings = [re.split(r"[.!?\n]", text, 1)[0].strip() for text in recent[-5:]]
        endings = [text.strip().splitlines()[-1][-160:] for text in recent[-3:]]
        variations.append("Сохрани заданное вступление; меняй конкретный релевантный опыт и аргументы относительно прошлых писем: "
                          + " | ".join(_unique(openings)))
        variations.append("Не копируй финальные перечисления; связывай навыки с конкретными задачами, без универсального вывода: "
                          + " | ".join(_unique(endings)))
    return LetterPlan(analysis, tuple(selected), strong, gaps, critical, levels, round(score, 3), tuple(missing),
                      approved, tuple(variations))


def _structure(text: str) -> tuple[str, ...]:
    norm = _norm(text)
    tags = []
    if re.match(r"(?:здравствуйте|добрый день|привет|hello|hi)\b", norm):
        tags.append("greeting")
    for label, pattern in (("developing", r"разрабатываю|занимаюсь|пишу|i (?:develop|build)"),
                           ("experience_helps", r"этот опыт поможет|опыт.{0,20}будет полезен|experience.{0,20}help"),
                           ("interested", r"интересны.{0,20}задач|мне интересна|interested in"),
                           ("gph", r"подходит гпх|предпочитаю гпх"),
                           ("discuss", r"(?:готов|буду рад|предлагаю).{0,16}обсудить|happy to discuss")):
        if re.search(pattern, norm):
            tags.append(label)
    return tuple(tags)


def _positive_facts(profile: Any, data: dict, resume: str, context: str) -> str:
    render = getattr(profile, "render_facts", None)
    if callable(render):
        return "\n".join((resume, context, render()))
    parts = [resume, context, str(data.get("experience_claim", "")), *data.get("confirmed_skills", ())]
    for item in data.get("skills", ()):
        skill = _mapping(item)
        if skill.get("level") == "hands_on" and skill.get("scope") != "team":
            parts.append(str(skill.get("detail") or skill.get("name") or ""))
    for item in data.get("projects", ()):
        project = _project_dict(item)
        parts.extend((project["name"], project["period"], project["role"], *project["tasks"]))
        parts.extend(str(_mapping(metric).get("text", "")) for metric in project["metrics"])
    return "\n".join(str(part) for part in parts)


def _project_aliases(name: str) -> tuple[str, ...]:
    """A descriptive suffix is not a mandatory part of an otherwise real project name."""
    primary = re.split(r"\s*:\s+|\s+/\s+|\s*\(", name, maxsplit=1)[0].strip()
    aliases = [name, primary]
    for parenthesis in re.findall(r"\(([^()]*)\)", name):
        # Repository identifiers are useful aliases; generic '(distributed storage)' is not a name.
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{3,}", parenthesis.strip()):
            aliases.append(parenthesis.strip())
    return _unique(alias for alias in aliases if len(alias.strip()) >= 4)


def _case_mentioned(text: str, project_names: Iterable[str], projects: Iterable[Mapping[str, Any]] = ()) -> bool:
    normal = re.sub(r"[^\w]+", " ", _norm(text))
    for name in project_names:
        for alias in _project_aliases(name):
            alias = re.sub(r"[^\w]+", " ", _norm(alias)).strip()
            if alias and re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", normal):
                return True
    contribution = re.search(r"разрабатывал|внедрял|создавал|реализовал|разработал|автоматизировал|настроил|"
                             r"implemented|developed|built|created|automated", text, re.I)
    if contribution:
        ignored = {"python", "данных", "данные", "работа", "проект", "сервис", "система", "задачи", "backend"}
        def case_tokens(value):
            return {word[:6] for word in re.findall(r"[a-zа-яё][\w]{3,}", _norm(value)) if word not in ignored}
        current = case_tokens(text)
        for project in projects:
            evidence = " ".join((str(project.get("description", "")), *project.get("tasks", ())))
            if re.search(r"маркетплейс|marketplace", text, re.I) and not re.search(
                    r"маркетплейс|marketplace", evidence + " " + str(project.get("name", "")), re.I):
                continue
            if len(current & case_tokens(evidence)) >= 3:
                return True
            themes = (
                r"почтов|курьер|отправлен|доставк|delivery|courier|shipment",
                r"согласован|workflow|заявк|валидац|approval|validation|status|статус",
                r"etl|dwh|нормализац|конвейер|pipeline|data.collection|normaliz",
                r"ipfs|распредел[её]н.{0,20}хран|distributed.storage|cluster",
                r"прогноз|временн.{0,15}ряд|forecast|time.series|bert|lstm",
            )
            if any(re.search(theme, evidence, re.I) and re.search(theme, text, re.I) for theme in themes):
                return True
    # A real case may be described by its domain instead of its registry heading.
    # Require the domain in the source AND a concrete past contribution in the letter;
    # a generic SQL/API mention or interest in the employer's product is insufficient.
    marketplace_case = any(re.search(
        r"(?:marketplace|маркетплейс).{0,60}(?:analyt|аналит|прогноз)|"
        r"(?:analyt|аналит|прогноз).{0,60}(?:marketplace|маркетплейс)",
        _norm(str(project.get("name", "")) + " " + str(project.get("description", ""))), re.I)
        for project in projects)
    if marketplace_case:
        case = re.search(r"(?:проект[а-я]*|сервис[а-я]*|приложени[а-я]*)[^.!?\n]{0,100}"
                         r"аналитик[а-я]*[^.!?\n]{0,35}маркетплейс[а-я]*|"
                         r"(?:project|service|application)[^.!?\n]{0,80}marketplace analytics", text, re.I)
        if case and re.search(r"разрабатывал|внедрял|создавал|реализовал|разработал|автоматизировал|"
                              r"worked|built|developed|implemented", text[max(0, case.start() - 60):case.end() + 160], re.I):
            return True
    return False


def _language_body(text: str, analysis: ApplicationAnalysis, project_names: Iterable[str]) -> str:
    """Known proper names can retain their original script in an otherwise English letter."""
    names = [analysis.title, analysis.company]
    names.extend(alias for name in project_names for alias in _project_aliases(name))
    body = text
    for name in sorted(_unique(names), key=len, reverse=True):
        if not re.search(r"[а-яё]", name, re.I):
            continue
        pattern = r"\s+".join(re.escape(part) for part in name.split())
        body = re.sub(pattern, " ", body, flags=re.I)
    return body


_CLAIM_QUANTITY = re.compile(r"\b\d+(?:[.,]\d+)?(?:\s*[–-]\s*\d+(?:[.,]\d+)?)?\s*"
                             r"(?:%|rps\b|пользовател[а-я]*|клиент[а-я]*|поток[а-я]*|аккаунт[а-я]*|"
                             r"узл[а-я]*|нод[а-я]*|ролик[а-я]*|videos?\b|лет\b|год(?:а|ов)?\b|месяц[а-я]*)", re.I)


def validate_letter(text: str, analysis: ApplicationAnalysis, profile: Any = None, *, plan: LetterPlan | None = None,
                    recent_letters: Iterable[Any] = (), resume: str = "", context: str = "") -> list[str]:
    """Objective/style diagnostics only; the result never authorizes blocking delivery."""
    issues = []
    stripped = text.strip()
    if not stripped:
        return ["objective:empty — нужен непустой правдивый текст для автоматического отклика"]
    plan = plan or make_letter_plan(analysis, profile, resume=resume, context=context, recent_letters=recent_letters)
    data = _profile_data(profile)
    known_project_items = [_mapping(item) for item in (*data.get("projects", ()), *plan.selected_projects)]
    known_projects = [str(item.get("name", "")) for item in known_project_items]
    words = re.findall(r"[\w]+(?:[-’'][\w]+)*", text, re.U)
    language_body = _language_body(text, analysis, known_projects)
    cyrillic = len(re.findall(r"[а-яё]", language_body, re.I))
    latin = len(re.findall(r"[a-z]", language_body, re.I))
    if analysis.language == "en" and cyrillic > max(8, (cyrillic + latin) * .08):
        issues.append("objective:language — работодатель просит письмо на английском")
    if analysis.opening_questions:
        if re.match(r"(?:Здравствуйте|Добрый|Привет|Hello|Hi\b|Dear)", stripped, re.I):
            issues.append("objective:opening — ответы на обязательные вопросы должны идти до приветствия")
        answers = re.findall(r"(?m)^\s*(?:\d{1,2}[.)]|(?:Ответ|Answer)\s+\d{1,2})\s*", text, re.I)
        if len(answers) < len(analysis.opening_questions):
            issues.append("objective:questions — нужны отдельные пронумерованные ответы по порядку на все вопросы")
    for word in analysis.required_keywords:
        if _norm(word) not in _norm(text):
            issues.append(f"objective:keyword — отсутствует обязательное слово «{word}»")
    if analysis.word_limit and len(words) > analysis.word_limit:
        issues.append(f"objective:length — превышен лимит работодателя {analysis.word_limit} слов")
    elif not analysis.word_limit and not analysis.required_questions and not analysis.requires_portfolio and not 40 <= len(words) <= 160:
        issues.append("style:length — обычное письмо должно содержать 40–160 содержательных слов")
    if re.search(r"(?:мне|предпочитаю|подходит|рассматриваю).{0,30}ГПХ|civil.{0,15}contract", text, re.I):
        kind = "objective:contract_contradiction" if "employee" in analysis.employment_type else "style:gph_boilerplate"
        issues.append(kind + " — убери универсальную фразу о ГПХ; условия вакансии не подменяются предпочтением")
    for skill in plan.critical_gaps:
        if skill not in _SKILLS or not _has_skill(text, skill):
            continue
        for alias in _SKILLS[skill]:
            claim = re.search(r"(?:работал[а-я]*\s+(?:с|на)|использовал[а-я]*|внедрял[а-я]*|настраивал[а-я]*|"
                              r"опыт\s+(?:с|в)|worked\s+(?:with|on)|used|implemented|hands.on(?: experience)?\s+with)"
                              r"\s+(?:\w+\s+){0,3}" + re.escape(alias) + r"(?!\w)", text, re.I)
            if claim and not re.search(r"\bне\s+$|\bnot\s+$", text[max(0, claim.start() - 8):claim.start()], re.I):
                issues.append(f"objective:unbacked_skill — {skill} не подтверждён как личный hands-on опыт")
                break
    approved = {_norm(url.rstrip(".,);")) for url in plan.approved_urls}
    vacancy_url = f"https://hh.ru/vacancy/{analysis.vacancy_id}" if analysis.vacancy_id else ""
    for url in _URL.findall(text):
        if _norm(url.rstrip(".,);")) not in approved and url.rstrip(".,);") != vacancy_url:
            issues.append("objective:unapproved_url — ссылка не подтверждена доступными материалами кандидата")
    if analysis.requires_video_samples and plan.missing_materials and re.search(
        r"(?:прикладываю|готов показать|мои готовые|attached|here are|my completed|I produced).{0,90}"
        r"(?:видео|ролик|videos?|films?)", text, re.I | re.S):
        issues.append("objective:video_evidence — нельзя обещать или приписывать неподтверждённые видео")
    known_video = any(re.search(r"video|видео|ролик", " ".join(str(_mapping(project).get(key, ""))
                          for key in ("kind", "name", "description")), re.I) for project in data.get("projects", ()))
    if analysis.role_family == "ai_video" and not known_video:
        video_claims = re.finditer(
            r"(?:built|created|developed|produced|delivered)\s+(?:(?:AI|generative|multi[- ]scene|production|advertising)\s+){0,4}"
            r"(?:videos?|video pipelines?)|(?:создал|создавал|разработал|выпускал|собрал).{0,25}"
            r"(?:AI[- ]?видео|видеопайплайн|ролик)", text, re.I)
        if any(not re.search(r"\b(?:не|not|never)\b", text[max(0, match.start() - 14):match.start()], re.I)
               for match in video_claims):
            issues.append("objective:video_experience — выпуск видео/видеопайплайн не подтверждён личными кейсами")
    if re.search(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,}", text):
        issues.append("objective:secret — текст содержит похожие на секрет значения")
    former = data.get("former_employer", "")
    current = str(data.get("current_employer", "")).split("(", 1)[0].strip()
    if former and _norm(former) in _norm(text) and current and _norm(current) not in _norm(text) and re.search(
        r"сейчас работаю|текущий работодатель|currently work|current employer", text, re.I):
        issues.append("objective:current_employer — используй актуального работодателя либо не называй его")
    facts = _positive_facts(profile, data, resume, context)
    identity_facts = "\n".join((facts, str(data.get("current_employer", "")), str(data.get("former_employer", ""))))
    if re.search(r"\bNDA\b", text, re.I) and not re.search(r"\bNDA\b", facts, re.I):
        issues.append("objective:unsupported_nda — соглашение NDA не подтверждено фактами кандидата")
    for employer in re.finditer(r"(?i:(?:работал[а-я]*|работаю|worked|working)\s+(?:в|at|for)\s+"
                                r"(?:компании\s+)?)([A-ZА-ЯЁ][\w.-]*(?:\s+[A-ZА-ЯЁ][\w.-]*){0,3})", text):
        name = employer[1]
        if _norm(name) not in _norm(identity_facts) and name not in _SKILLS:
            issues.append("objective:unverified_employer — работодатель «" + name + "» не подтверждён")
    facts_normal = re.sub(r"[\s+]", "", _norm(facts))
    for quantity in _CLAIM_QUANTITY.finditer(text):
        value = re.sub(r"[\s+]", "", _norm(quantity[0]))
        if value not in facts_normal:
            issues.append("objective:unverified_quantity — не подтверждена цифра «" + quantity[0] + "»")
            continue
        if re.search(r"лет|год|месяц", quantity[0], re.I):
            sentence_start = max(text.rfind(mark, 0, quantity.start()) for mark in (".", "!", "?", "\n")) + 1
            sentence_end = re.search(r"[.!?\n]", text[quantity.end():])
            end = quantity.end() + sentence_end.start() if sentence_end else len(text)
            scope_skills = tuple(skill for skill in _skills(text[sentence_start:end])
                                 if skill not in {"Python", "JavaScript", "TypeScript", "SQL", "REST API", "CI/CD", "Git"}
                                 and not (not profile and skill in {"Go", "C++", "Java", "Delphi", "R"}))
            # Resume-only language tenure retains the existing numeric guard;
            # a structured profile requires explicit per-technology evidence.
            # General seniority does not establish the same duration in each named framework.
            for skill in scope_skills:
                if not any(_has_skill(line, skill) and value in re.sub(r"[\s+]", "", _norm(line))
                           for line in facts.splitlines()):
                    issues.append(f"objective:scoped_duration — стаж именно с {skill} отдельно не подтверждён")
    for project in re.findall(r"(?:проект[а-я]*|project)\s+[«\"“]([^»\"”]+)[»\"”]", text, re.I):
        if not any(_norm(project) in _norm(name) for name in known_projects):
            issues.append("objective:project — указан проект, отсутствующий в подтверждённых кейсах")
    if plan.selected_projects and not _case_mentioned(text, known_projects, known_project_items):
        issues.append("style:missing_case — опиши одну конкретную задачу подтверждённого релевантного кейса без собственных имён")
    if len(_skills(text)) > 7 and len(words) < 100:
        issues.append("style:stack_list — замени перечень технологий конкретным кейсом и его пользой")
    if re.search(r"готов быстро освоить|знаком базово|опыта.{0,15}не было", text, re.I):
        issues.append("style:self_deprecation — не делай неподтверждённых выводов о пробелах, опиши смежную практику")
    recent = _recent_texts(recent_letters)
    def varied_body(value):
        for fixed in (RUSSIAN_LETTER_GREETING, RUSSIAN_LETTER_ASSESSMENT):
            value = value.replace(fixed, "")
        return re.sub(r"\W+", " ", _norm(value)).strip()
    normalized = varied_body(text)
    if normalized and any(SequenceMatcher(None, normalized, varied_body(old)).ratio() > .78 for old in recent):
        issues.append("style:repeated_wording — письмо слишком похоже на один из последних откликов")
    structure = set(_structure(text))
    repeated = sum(len(structure & set(_structure(old))) >= 3 for old in recent[-5:])
    if len(structure) >= 3 and repeated >= 3:
        issues.append("style:repeated_structure — повторяются вступление, аргументация и универсальное завершение")
    return list(_unique(issues))


def deduplicate_vacancies(records: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Stable ID-based deduplication for regression exports; never initiates an application."""
    result, seen = [], set()
    for record in records:
        identity = str(record.get("id") or record.get("vacancy_id") or record.get("url") or "")
        if not identity:
            identity = json.dumps(dict(record), sort_keys=True, ensure_ascii=False, default=str)
        if identity not in seen:
            result.append(dict(record))
            seen.add(identity)
    return result
RUSSIAN_LETTER_GREETING = "Добрый день, очень заинтересовала данная вакансия!"
RUSSIAN_LETTER_ASSESSMENT = "Считаю, что мои навыки и компетенции соответствуют описанию вакансии."

