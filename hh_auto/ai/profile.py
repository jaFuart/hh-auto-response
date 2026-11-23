"""Отдельный профиль подтверждённых фактов соискателя. Никаких выводов из отсутствующих данных."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from ..core.config import Ai

MAX_PROFILE_BYTES = 256 * 1024
_ROOT_KEYS = {"version", "role", "confirmed_skills", "clarifications", "projects"}
_PROJECT_KEYS = {"name", "technologies", "tasks"}


class ProfileError(Exception):
    """Настроенный профиль нельзя использовать: файл отсутствует или его структура неверна."""


@dataclass(frozen=True)
class CandidateSkill:
    name: str
    level: str
    detail: str = ""
    category: str = ""
    aliases: tuple[str, ...] = ()
    scope: str = "personal"
    proficiency: str = ""

    def mentioned_terms(self, text: str) -> tuple[str, ...]:
        return tuple(alias for alias in self.aliases or (self.name,)
                     if any(_technology_contains(text, term) for term in _TECH_ALIASES.get(alias, (alias,))))


@dataclass(frozen=True)
class CandidateMetric:
    text: str
    status: str = "confirmed"


@dataclass(frozen=True)
class CandidateAnswer:
    question: str
    answer: str
    note: str = ""
    source: str = "profile"


@dataclass(frozen=True)
class CandidateProject:
    name: str
    technologies: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    id: str = ""
    description: str = ""
    period: str = ""
    role: str = ""
    personal_technologies: tuple[str, ...] = ()
    team_technologies: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evidence: str = ""
    kind: str = "work"
    allowed_urls: tuple[str, ...] = ()
    metrics: tuple[CandidateMetric, ...] = ()
    supplementary: str = ""


@dataclass(frozen=True)
class CandidateProfile:
    version: int = 1
    role: str = ""
    confirmed_skills: tuple[str, ...] = ()
    clarifications: tuple[str, ...] = ()
    projects: tuple[CandidateProject, ...] = ()
    schema_version: str = ""
    skills: tuple[CandidateSkill, ...] = ()
    current_employer: str = ""
    former_employer: str = ""
    full_name: str = ""
    experience_claim: str = ""
    education: str = ""
    languages: tuple[tuple[str, str], ...] = ()
    experience_history: tuple[tuple[str, str, str], ...] = ()
    strengths: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    approved_urls: tuple[str, ...] = ()
    answering_examples: tuple[CandidateAnswer, ...] = ()
    updated_at: str = ""
    validation_priorities: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.role or self.confirmed_skills or self.clarifications or self.projects)

    def render(self, query: str = "") -> str:
        """Только факты; навыки не распределяются по проектам автоматически, неизвестное не дописывается."""
        if self.schema_version:
            return self._render_structured(query)
        lines = []
        if self.role:
            lines.append(f"Профессиональная роль: {self.role}.")
        if self.confirmed_skills:
            lines.extend(f"Есть практический опыт с {skill}." for skill in self.confirmed_skills)
        if self.clarifications:
            lines.append("Уточнения соискателя:")
            lines.extend(self.clarifications)
        for project in self.projects:
            lines.append(f"Проект: {project.name}.")
            if project.technologies:
                lines.append("Технологии этого проекта: " + ", ".join(project.technologies) + ".")
            lines.extend(f"Задача этого проекта: {task}" for task in project.tasks)
        return "\n".join(lines)

    @property
    def skill_levels(self) -> dict[str, str]:
        levels: dict[str, str] = {name: "hands_on" for name in self.confirmed_skills}
        rank = {"needs_confirmation": 0, "familiar": 1, "hands_on": 2}
        for skill in self.skills:
            effective = "familiar" if skill.scope == "team" and skill.level == "hands_on" else skill.level
            for alias in skill.aliases or (skill.name,):
                if alias not in levels or rank[effective] > rank[levels[alias]]:
                    levels[alias] = effective
        for canonical, level in tuple(levels.items()):
            for alias in _TECH_ALIASES.get(canonical, ()):
                if alias not in levels or rank[level] > rank[levels[alias]]:
                    levels[alias] = level
        for skill in self.skills:
            if ":" not in skill.detail:
                continue
            # An explicitly named combined subject (e.g. React / TypeScript)
            # is an alias for that declaration, not for either tool in isolation.
            label = skill.detail.split(":", 1)[0].strip()
            level = "familiar" if skill.scope == "team" and skill.level == "hands_on" else skill.level
            if label and len(label) <= 100 and (label not in levels or rank[level] > rank[levels[label]]):
                levels[label] = level
        return levels

    def skill_level(self, technology: str) -> str:
        wanted = _normalize(technology)
        for canonical, aliases in _TECH_ALIASES.items():
            if any(_normalize(alias) == wanted for alias in aliases):
                wanted = _normalize(canonical)
                break
        exact = [(name, level) for name, level in self.skill_levels.items() if _normalize(name) == wanted]
        if exact:
            return exact[0][1]
        matches = [(len(_normalize(name)), level) for name, level in self.skill_levels.items()
                   if _contains(technology, name)]
        return max(matches, default=(0, ""))[1]

    def claim_limits(self, query: str) -> tuple[CandidateSkill, ...]:
        personal = {alias for skill in self.skills if skill.level == "hands_on" and skill.scope == "personal"
                    for alias in skill.aliases or (skill.name,)}
        limits = []
        for skill in self.skills:
            if skill.level == "hands_on" and skill.scope == "personal" and not skill.proficiency:
                continue
            aliases = skill.aliases or (skill.name,)
            if skill.scope == "team" or skill.level == "familiar" or skill.level == "hands_on" and skill.scope == "scoped":
                aliases = tuple(alias for alias in aliases if alias not in personal)
            if not aliases:
                continue
            effective = replace(skill, name=aliases[0], aliases=aliases)
            if effective.mentioned_terms(query):
                limits.append(effective)
        return tuple(limits)

    def relevant_projects(self, query: str, limit: int = 2) -> tuple[CandidateProject, ...]:
        if limit < 1:
            return ()
        if not query.strip():
            return self.projects[:limit]
        ranked = sorted(enumerate(self.projects), key=lambda pair: (
            -_relevance(query, " ".join((pair[1].name, pair[1].description, *pair[1].technologies,
                                        *pair[1].tasks))), pair[0]))
        return tuple(project for _, project in ranked[:limit])

    def select_projects(self, query: str, limit: int = 2) -> tuple[CandidateProject, ...]:
        return self.relevant_projects(query, limit)

    def render_facts(self) -> str:
        """Positive guard facts only: no unfamiliar/unconfirmed tools or whole-team stack promotion."""
        if not self.schema_version:
            return self.render()
        lines = self._identity_lines()
        lines += [f"Есть практический опыт с {skill.detail or skill.name}." for skill in self.skills
                  if skill.level == "hands_on" and skill.scope != "team"]
        for project in self.projects:
            lines.append(f"Проект: {project.name}. Роль: {project.role}. Период: {project.period}.")
            if project.personal_technologies:
                lines.append("Личные подтверждённые технологии: " + ", ".join(project.personal_technologies))
            uncertain = {metric.text for metric in project.metrics if metric.status not in ("confirmed", "pilot")}
            lines.extend(task for task in project.tasks if task not in uncertain and not _UNCERTAIN.search(task))
            lines.extend(metric.text for metric in project.metrics if metric.status in ("confirmed", "pilot"))
        return "\n".join(lines)

    def facts_text(self) -> str:
        return self.render_facts()

    @property
    def answer_examples(self) -> tuple[CandidateAnswer, ...]:
        return self.answering_examples

    def answer_example(self, query: str) -> str:
        """Return an existing sourced answer only for the same general screening topic."""
        if re.search(r"сколько|стаж|лет\b|месяц|год[ау]?\b|rps|sla|нагрузк|kpi|выручк|"
                     r"конкретн.{0,25}(?:проект|кейс)|како[йм].{0,20}(?:компани|проект)|"
                     r"how (?:many|long)|years?|throughput|salary", query, re.I):
            return ""
        topics = _example_topics(query)
        if not topics:
            return ""
        q = _normalize(query)
        # These are screening examples, not an answer bank for arbitrary
        # implementation questions (Git rebase, SQL locking, Docker networking).
        if re.search(r"\b(?:почему|объясн\w*|сравн\w*|как устро\w*|чем отлич\w*|"
                     r"rebase|bisect|cherry.pick|deadlock|isolation)\b", q):
            return ""
        general = re.search(r"(?:работал|работали|опыт|знаком|использ|примен|каки[емх]|с какими|есть ли)", q)
        if not general:
            return ""
        candidates = [(index, item) for index, item in enumerate(self.answering_examples)
                      if _example_topics(item.question) == topics]
        if not candidates:
            return ""
        _, item = max(candidates, key=lambda pair: (_relevance(query, pair[1].question), pair[0]))
        return item.answer

    def to_dict(self) -> dict:
        result = asdict(self)
        result["skill_levels"] = self.skill_levels
        result["screening_examples"] = result["answering_examples"]
        return result

    def _identity_lines(self) -> list[str]:
        lines = [f"Профессиональная роль: {self.role}."] if self.role else []
        if self.current_employer:
            lines.append(f"Текущий работодатель: {self.current_employer}.")
        if self.former_employer:
            lines.append(f"Прежний работодатель: {self.former_employer}; не считать его текущим.")
        if self.experience_claim:
            lines.append("Общий профессиональный опыт: " + self.experience_claim)
        if self.education:
            lines.append("Образование: " + self.education)
        lines.extend(f"Язык {language}: {level}." for language, level in self.languages)
        return lines

    def _render_structured(self, query: str) -> str:
        lines = self._identity_lines()
        lines.append("Приоритет: актуальные уточнения кандидата > этот профиль > старое резюме. Общий стаж не является стажем по фреймворку.")
        ranked = sorted(enumerate(self.skills), key=lambda pair: (
            -_relevance(query, " ".join((pair[1].name, pair[1].detail, *pair[1].aliases))), pair[0]))
        selected = [skill for _, skill in ranked[:14]]
        if query:
            selected = list(dict.fromkeys([*self.claim_limits(query), *selected]))
        if re.search(r"\b(?:AI|ИИ|LLM|Cursor|Codex)\b|промпт|prompt", query, re.I):
            workflow_names = {_normalize(name) for name in
                              ('Cursor', 'Codex', 'Prompt engineering', 'AI-assisted development')}
            workflow = [skill for skill in self.skills
                        if skill.level == 'hands_on' and skill.scope == 'personal'
                        and any(_normalize(alias) in workflow_names for alias in skill.aliases or (skill.name,))]
            selected = list(dict.fromkeys([*selected, *workflow]))
        for skill in selected:
            label = {"hands_on": "ПРАКТИКА", "familiar": "ЗНАКОМСТВО", "needs_confirmation": "НЕ ПОДТВЕРЖДЕНО"}[skill.level]
            lines.append(f"[{label}; область={skill.scope}] {skill.detail or skill.name}")
        lines.append("Неподтверждённое и командный стек не являются личным практическим опытом.")
        for project in self.relevant_projects(query, 2):
            lines += [f"Проект [{project.id}; {project.kind}]: {project.name}.",
                      f"Период: {project.period}. Роль: {project.role}.", project.description]
            lines.extend("Личный вклад: " + task for task in project.tasks)
            if project.personal_technologies:
                lines.append("Подтверждённый личный стек: " + ", ".join(project.personal_technologies))
            if project.team_technologies:
                lines.append("Стек проекта/команды, не доказательство личного авторства: " + ", ".join(project.team_technologies))
            lines.extend("Ограничение: " + limit for limit in project.limitations)
            lines.extend(f"Метрика [{metric.status}]: {metric.text}" for metric in project.metrics)
            if project.supplementary:
                lines.append("Кейс из карты компетенций:\n" + project.supplementary[:1200])
            lines.extend("Предоставленная ссылка: " + url for url in project.allowed_urls)
        examples = sorted(self.answering_examples, key=lambda example: -_relevance(query, example.question))
        if query and examples and _relevance(query, examples[0].question) > 0:
            example = examples[0]
            lines.extend(("Пример по источнику: " + example.question, example.answer))
            if example.note:
                lines.append("Оговорка примера: " + example.note)
        lines.extend("Правило источника: " + rule for rule in self.constraints[:11])
        return "\n".join(line for line in lines if line)


def _text(value, key: str, *, empty: bool = False, limit: int = 1500) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"профиль: {key} должен быть строкой")
    value = value.strip()
    if not value and not empty:
        raise ProfileError(f"профиль: {key} не должен быть пустым")
    if len(value) > limit:
        raise ProfileError(f"профиль: {key} слишком длинный (максимум {limit} символов)")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise ProfileError(f"профиль: {key} содержит управляющие символы")
    return value


def _strings(value, key: str, *, limit: int = 1500, count: int = 100) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProfileError(f"профиль: {key} должен быть массивом строк")
    if len(value) > count:
        raise ProfileError(f"профиль: в {key} слишком много элементов (максимум {count})")
    return tuple(_text(item, f"{key}[{index}]", limit=limit) for index, item in enumerate(value, 1))


# Names are aliases for evidence already present in a source entry, never a list of
# skills granted to a candidate. Longer names prevent Django/DRF, SQL/SQLAlchemy
# and Git/GitHub from silently becoming interchangeable.
_TECH_ALIASES = {
    "Python": ("Python", "Python 3"), "JavaScript": ("JavaScript", "JS"),
    "TypeScript": ("TypeScript",), "Go": ("Go", "Golang"), "Java": ("Java",),
    "PHP": ("PHP",), "Delphi": ("Delphi", "Object Pascal", "ObjectPascal"),
    "C#": ("C#",), ".NET": (".NET",), "C++": ("C++",), "Rust": ("Rust",),
    "Django REST Framework": ("Django REST Framework", "DRF"),
    "Django": ("Django",), "FastAPI": ("FastAPI",), "Flask": ("Flask",),
    "Frappe": ("Frappe",), "ERPNext": ("ERPNext",),
    "SQLAlchemy": ("SQLAlchemy",), "SQL": ("SQL",),
    "PostgreSQL": ("PostgreSQL", "Postgres"),
    "Microsoft SQL Server": ("Microsoft SQL Server", "MS SQL Server", "MSSQL"),
    "MongoDB": ("MongoDB",), "MariaDB": ("MariaDB",), "SQLite": ("SQLite",),
    "Redis": ("Redis",), "MinIO": ("MinIO",), "S3": ("S3",),
    "REST": ("REST",), "SOAP": ("SOAP",), "HTTP": ("HTTP",), "JSON": ("JSON",),
    "asyncio": ("asyncio",), "aiohttp": ("aiohttp",), "requests": ("requests",),
    "httpx": ("httpx",), "Celery": ("Celery",), "RQ": ("RQ",),
    "Pydantic": ("Pydantic",), "Alembic": ("Alembic",),
    "GraphQL": ("GraphQL",), "gRPC": ("gRPC",), "WebSocket": ("WebSocket",),
    "Kafka": ("Kafka",), "Apache Spark": ("Apache Spark", "PySpark",),
    "RabbitMQ": ("RabbitMQ",), "DLQ": ("DLQ", "dead-letter"),
    "ETL": ("ETL",), "DWH": ("DWH",), "Selenium": ("Selenium",),
    "SeleniumBase": ("SeleniumBase",), "BeautifulSoup": ("BeautifulSoup", "BeautifulSoup4"),
    "lxml": ("lxml",), "Scrapy": ("Scrapy",), "Playwright": ("Playwright",),
    "Linux": ("Linux",), "WSL": ("WSL",), "Bash": ("Bash", "shell"),
    "SSH": ("SSH",), "SCP": ("SCP",), "Docker": ("Docker", "Dockerfile"),
    "Docker Compose": ("Docker Compose", "Docker/Compose"), "Nginx": ("Nginx",),
    "TLS": ("TLS",), "Git": ("Git",), "GitHub": ("GitHub",),
    "Gitea": ("Gitea",), "GitHub Actions": ("GitHub Actions",), "CI/CD": ("CI/CD",),
    "Kubernetes": ("Kubernetes", "K8s"), "OpenShift": ("OpenShift",),
    "Helm": ("Helm",), "Terraform": ("Terraform",), "Ansible": ("Ansible",),
    "Grafana": ("Grafana",), "Prometheus": ("Prometheus",),
    "IPFS": ("IPFS",), "Kubo": ("Kubo",), "IPFS Cluster": ("IPFS Cluster",),
    "AES-GCM": ("AES-GCM",), "Ed25519": ("Ed25519",),
    "React": ("React",), "Next.js": ("Next.js",), "Nuxt.js": ("Nuxt.js", "Nuxt"),
    "Vue": ("Vue",), "Angular": ("Angular",), "Vite": ("Vite",),
    "Babylon.js": ("Babylon.js",), "HTML": ("HTML",), "CSS": ("CSS",),
    "Pandas": ("Pandas",), "NumPy": ("NumPy",), "Matplotlib": ("Matplotlib",),
    "Seaborn": ("Seaborn",), "scikit-learn": ("scikit-learn", "sklearn"),
    "TensorFlow": ("TensorFlow",), "Keras": ("Keras",), "PyTorch": ("PyTorch",),
    "BERT": ("BERT", "BERT-mini"), "LSTM": ("LSTM",), "Prophet": ("Prophet",),
    "LightGBM": ("LightGBM",), "Neural ODE": ("Neural ODE", "DG-Neural ODE"),
    "Jupyter": ("Jupyter",), "Google Colab": ("Google Colab",),
    "Excel": ("Excel", "XLSX"), "Power BI": ("Power BI", "PowerBI"),
    "Power Query": ("Power Query", "PowerQuery"),
    "Power Query M": ("Power Query M", "PowerQuery M", "PowerQueryM", "M"),
    "Business Intelligence": ("Business Intelligence", "BI"),
    "HR Analytics": ("HR Analytics", "HR-аналитика", "HR аналитика", "анализ текучести персонала"),
    "R": ("R",), "RStudio": ("RStudio", "R Studio"),
    "OpenAPI": ("OpenAPI", "Open API"), "Swagger": ("Swagger",),
    "pytest-django": ("pytest-django", "pytest_django"), "Ruff": ("Ruff",), "Pyodide": ("Pyodide",),
    "Express.js": ("Express.js", "ExpressJS", "Express JS"),
    "WireGuard": ("WireGuard",), "Linux Networking": ("Linux Networking", "Linux network"),
    "NAT": ("NAT",), "IP Routing": ("IP Routing", "маршрутизация"),
    "nftables": ("nftables",), "TUN/TAP": ("TUN/TAP", "TUN", "TAP"), "IPAM": ("IPAM",),
    "YooKassa": ("YooKassa", "ЮKassa", "ЮКасса"),
    "TOTP": ("TOTP",), "2FA": ("2FA", "двухфакторная аутентификация"),
    "CSRF": ("CSRF",), "CSP": ("CSP",), "RBAC": ("RBAC",), "ACL": ("ACL",),
    "Idempotency": ("Idempotency", "идемпотентность", "идемпотентности"),
    "Transactional Outbox": ("Transactional Outbox", "TransactionalOutbox", "Outbox"),
    "pgvector": ("pgvector",), "RAG": ("RAG",),
    "Cursor": ("Cursor",), "Codex": ("Codex", "OpenAI Codex"),
    "Prompt engineering": ("Prompt engineering", "prompt-engineering", "промпт-инжиниринг",
                           "промпт инженеринг", "работа с промптами"),
    "AI-assisted development": ("AI-assisted development", "AI-assisted coding", "AI tools",
                                "ИИ-инструменты", "разработка с ИИ"),
}
_TEAM = re.compile(r"командн|личн.{0,35}(?:уточн|не установлен)|персональн.{0,30}не установлен|"
                   r"наличие в репозитории|по репозиторию|как механизмы продукта|архитектурная спецификация", re.I)
_UNCERTAIN = re.compile(r"потенциальн|ориентировоч|приблизительн|не подтвержд|требует уточн|"
                        r"нужно уточн|не установлен|спецификация|проверить в репозитории", re.I)
_DOMAINS = (
    ("etl", "dwh", "data engineer", "конвейер", "pipeline", "обогащ", "скрейп", "парсинг"),
    ("интеграц", "integration", "soap", "rest", "api", "провайдер", "доставк", "workflow"),
    ("devops", "linux", "docker", "инфраструктур", "on-prem", "хранени", "distributed", "ipfs"),
    ("ml", "nlp", "прогноз", "исследован", "нейрон", "pytorch", "bert", "lstm", "временн"),
    ("crm", "frappe", "erpnext", "бизнес-процесс", "права", "формы"),
)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip()


def _contains(text: str, word: str) -> bool:
    return bool(re.search(r"(?<![\w])" + re.escape(_normalize(word)) + r"(?![\w])", _normalize(text)))


def _technology_contains(text: str, word: str) -> bool:
    if word == "Go":
        # Lowercase English "go" is an ordinary verb, not evidence about Golang.
        return bool(re.search(r"(?<!\w)Go(?!\w)", text) or
                    re.search(r"(?:на|язык\w*|код\w*)\s+go\b", text, re.I))
    if word in {"R", "M"}:
        suffix = r"(?!\s*&\s*D\b|\s+Studio\b)" if word == "R" else ""
        return bool(re.search(r"(?<!\w)" + word + r"(?!\w)" + suffix, text))
    return _contains(text, word)


def _aliases(detail: str) -> tuple[str, ...]:
    # Context in a qualifier is not another asserted skill: "Redis ... outside
    # Frappe" limits Redis, while "Vue ... beyond Nuxt" does not downgrade Nuxt.
    subject = re.split(r"\s+(?:как|вне|сверх)\s+|\s+\(|;", detail, maxsplit=1, flags=re.I)[0]
    names = [name for name, aliases in _TECH_ALIASES.items() if any(_technology_contains(subject, item) for item in aliases)]
    if "Django REST Framework" in names:
        names = [name for name in names if name != "Django"]
    if "Microsoft SQL Server" in names:
        names = [name for name in names if name != "SQL"]
    if "pytest-django" in names:
        names = [name for name in names if name != "Django"]
    if "Power BI" in names and not re.search(r"Business\s+Intelligence|(?<!Power\s)\bBI\b", subject, re.I):
        names = [name for name in names if name != "Business Intelligence"]
    return tuple(names) or (detail.split(" (", 1)[0].strip(),)


def _relevance(query: str, text: str) -> int:
    if not query.strip():
        return 0
    score = sum(8 for name, aliases in _TECH_ALIASES.items()
                if any(_technology_contains(query, alias) for alias in aliases)
                and any(_technology_contains(text, alias) for alias in aliases))
    q, t = _normalize(query), _normalize(text)
    score += sum(3 for group in _DOMAINS if any(word in q for word in group) and any(word in t for word in group))
    words = set(re.findall(r"[\w+#.-]{4,}", q)) - {"опыт", "работ", "работал", "задачи", "какие", "есть", "какими", "использовали"}
    return score + sum(1 for word in words if word in t)


def _example_topics(query: str) -> frozenset[str]:
    groups = {
        "django": ("django",), "drf": ("django rest framework", "drf"), "sqlalchemy": ("sqlalchemy",),
        "asyncio": ("asyncio", "aiohttp"), "git": ("git",),
        "linux": ("linux", "терминал"), "docker": ("docker", "ci/cd", "контейнер"),
        "postgresql": ("postgresql", "postgres"),
        "queues": ("rabbitmq", "очеред", "воркер", "фоновой обработ"),
    }
    q = _normalize(query)
    topics = {name for name, aliases in groups.items() if any(_contains(q, alias) if alias.isascii() else alias in q for alias in aliases)}
    # Supporting tools in a Linux or Docker question do not change its primary topic.
    if "linux" in topics:
        topics -= {"git", "docker"}
    if "drf" in topics:
        topics.discard("django")
    return frozenset(topics)


def _dict(value, key: str) -> dict:
    if not isinstance(value, dict):
        raise ProfileError(f"профиль: {key} должен быть объектом")
    return value


def _objects(value, key: str, count: int = 60) -> list[dict]:
    if not isinstance(value, list) or len(value) > count or any(not isinstance(item, dict) for item in value):
        raise ProfileError(f"профиль: {key} должен быть массивом объектов (не больше {count})")
    return value


def _source_urls(raw: dict) -> tuple[str, ...]:
    urls = []
    for item in _objects(raw.get("sources", []), "sources"):
        reference = _text(item.get("reference", ""), "sources.reference", empty=True)
        parsed = urlparse(reference)
        if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
            urls.append(reference)
    return tuple(dict.fromkeys(urls))


def _project_urls(project: dict, approved: tuple[str, ...]) -> tuple[str, ...]:
    text = _normalize(" ".join(str(project.get(key, "")) for key in ("name", "evidence")))
    title = str(project.get("name", ""))
    result = []
    for url in approved:
        slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        distinctive = set(re.findall(r"[a-z0-9]{4,}", slug.casefold())) - {"project", "monorepo", "pipeline", "company"}
        if slug.casefold() in text or any(_contains(title, token) for token in distinctive):
            result.append(url)
    return tuple(result)


def _metric_claims(tasks: tuple[str, ...]) -> tuple[CandidateMetric, ...]:
    result = []
    for task in tasks:
        if not re.search(r"\d", task):
            continue
        # A numeric quantity with units is evidence; technology/version names are not metrics.
        if not re.search(r"\d[\d\s]*\s*(?:пользовател|клиент|регион|нод|узл|запрос|%|rps|событ)", task, re.I):
            continue
        for clause in task.split(","):
            if not re.search(r"\d", clause):
                continue
            status = "potential" if re.search(r"потенциальн", clause, re.I) else (
                "approximate" if _UNCERTAIN.search(clause) else (
                    "pilot" if re.search(r"пилот|внутренн|проверка", task, re.I) else "confirmed"))
            result.append(CandidateMetric(clause.strip(), status))
    return tuple(result)


def _parse_json_profile(raw: dict) -> CandidateProfile:
    if raw.get("schema_version") != "1.0":
        raise ProfileError('профиль JSON: требуется schema_version = "1.0"')
    candidate = _dict(raw.get("candidate"), "candidate")
    competencies = _dict(raw.get("competencies"), "competencies")
    if not competencies:
        raise ProfileError("профиль: competencies не должен быть пустым")
    skills = []
    for category, levels in competencies.items():
        category = _text(category, "competencies.category", limit=200)
        levels = _dict(levels, "competencies." + category)
        unknown = set(levels) - {"hands_on", "familiar", "needs_confirmation"}
        if unknown:
            raise ProfileError("профиль: неизвестный уровень компетенции: " + ", ".join(sorted(unknown)))
        for level, entries in levels.items():
            for detail in _strings(entries, "competencies." + category + "." + level):
                aliases = _aliases(detail)
                scope = "team" if _TEAM.search(detail) else ("scoped" if re.search(
                    r"\bв\b.{0,50}(?:проект|post service|frappe)|исследовательск|личн.{0,20}проект|упоминался", detail, re.I) else "personal")
                proficiency = "basic" if re.search(r"базов|начальн|\bbasic\b", detail, re.I) else (
                    "limited" if re.search(r"небольш|ограничен|\blimited\b", detail, re.I) else "")
                skills.append(CandidateSkill(aliases[0], level, detail, category, aliases, scope, proficiency))
    confirmed = tuple(dict.fromkeys(alias for skill in skills if skill.level == "hands_on" and skill.scope != "team"
                                    for alias in skill.aliases))
    approved = _source_urls(raw)
    projects = []
    for index, item in enumerate(_objects(raw.get("projects"), "projects", 30), 1):
        name = _text(item.get("name", ""), f"projects[{index}].name", limit=250)
        tech = _strings(item.get("technology", []), f"projects[{index}].technology", limit=500)
        tasks = _strings(item.get("personal_contribution", []), f"projects[{index}].personal_contribution", count=60)
        role = _text(item.get("role", ""), "projects.role", empty=True)
        shared = bool(re.search(r"совместн|командн|репозитории организации|масштаб личного вклада",
                                role + " " + " ".join(tasks), re.I))
        personal_tasks = " ".join(task for task in tasks if not _TEAM.search(task) and not _UNCERTAIN.search(task))
        personal, team = [], []
        for entry in tech:
            aliases = _aliases(entry)
            if _TEAM.search(entry) or _UNCERTAIN.search(entry):
                team.append(entry)
            else:
                known = [alias for alias in aliases if alias in confirmed and (not shared or _contains(personal_tasks, alias))]
                personal.extend(known)
                if not known:
                    team.append(entry)
        description = _text(item.get("description", ""), "projects.description", empty=True, limit=4000)
        kind = "research" if re.search(r"исследован|research", role + " " + name, re.I) else (
            "personal" if re.search(r"личн|экспериментальн", role + " " + item.get("restriction", ""), re.I) else "work")
        restriction = _text(item.get("restriction", ""), "projects.restriction", empty=True, limit=2500)
        projects.append(CandidateProject(
            name=name, technologies=tech, tasks=tasks,
            id=(re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_") or "project_" + str(index)), description=description,
            period=_text(item.get("period", ""), "projects.period", empty=True, limit=250), role=role,
            personal_technologies=tuple(dict.fromkeys(personal)), team_technologies=tuple(team),
            limitations=(restriction,) if restriction else (),
            evidence=_text(item.get("evidence", ""), "projects.evidence", empty=True, limit=2500),
            kind=kind, allowed_urls=_project_urls(item, approved), metrics=_metric_claims(tasks)))
    examples = tuple(CandidateAnswer(
        _text(item.get("question", ""), "screening_examples.question"),
        _text(item.get("answer", ""), "screening_examples.answer", limit=3000),
        _text(item.get("note", ""), "screening_examples.note", empty=True), "profile JSON")
        for item in _objects(raw.get("screening_examples", []), "screening_examples"))
    languages = _dict(candidate.get("languages", {}), "candidate.languages")
    history = tuple((_text(item.get("organization", ""), "experience_history.organization"),
                     _text(item.get("role", ""), "experience_history.role"),
                     _text(item.get("period", ""), "experience_history.period"))
                    for item in _objects(raw.get("experience_history", []), "experience_history"))
    return CandidateProfile(
        schema_version="1.0", role=_text(candidate.get("primary_title", ""), "candidate.primary_title", limit=250),
        confirmed_skills=confirmed, skills=tuple(skills), projects=tuple(projects),
        current_employer=_text(candidate.get("current_employer_as_of_2026_11", candidate.get("current_employer", "")),
                               "candidate.current_employer", empty=True, limit=250),
        former_employer=_text(candidate.get("former_employer", ""), "candidate.former_employer", empty=True, limit=250),
        full_name=_text(candidate.get("full_name", ""), "candidate.full_name", empty=True, limit=250),
        experience_claim=_text(candidate.get("experience_claim", ""), "candidate.experience_claim", empty=True),
        education=_text(candidate.get("education", ""), "candidate.education", empty=True),
        languages=tuple((_text(key, "language", limit=100), _text(value, "language.level", limit=200))
                        for key, value in languages.items()), experience_history=history,
        strengths=_strings(raw.get("strengths", []), "strengths"),
        constraints=_strings(raw.get("constraints_for_llm", []), "constraints_for_llm", count=60),
        clarifications=_strings(raw.get("clarifications", []), "clarifications", count=60),
        validation_priorities=_strings(raw.get("validation_priority", []), "validation_priority", count=60),
        approved_urls=approved, answering_examples=examples,
        updated_at=_text(raw.get("updated_at", ""), "updated_at", empty=True, limit=100))


def parse_candidate_profile(raw: dict) -> CandidateProfile:
    if not isinstance(raw, dict):
        raise ProfileError("профиль должен быть таблицей TOML")
    if "schema_version" in raw or "candidate" in raw or "competencies" in raw:
        return _parse_json_profile(raw)
    unknown = set(raw) - _ROOT_KEYS
    if unknown:
        raise ProfileError("профиль: неизвестные поля: " + ", ".join(sorted(unknown)))
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise ProfileError("профиль: требуется version = 1")
    role = _text(raw.get("role", ""), "role", empty=True, limit=200)
    skills = _strings(raw.get("confirmed_skills", []), "confirmed_skills", limit=100)
    clarifications = _strings(raw.get("clarifications", []), "clarifications", count=50)
    projects = raw.get("projects", [])
    if not isinstance(projects, list) or len(projects) > 30:
        raise ProfileError("профиль: projects должен быть массивом таблиц (не больше 30 проектов)")
    parsed_projects = []
    for index, project in enumerate(projects, 1):
        key = f"projects[{index}]"
        if not isinstance(project, dict):
            raise ProfileError(f"профиль: {key} должен быть таблицей")
        unknown = set(project) - _PROJECT_KEYS
        if unknown:
            raise ProfileError(f"профиль: неизвестные поля {key}: " + ", ".join(sorted(unknown)))
        parsed_projects.append(CandidateProject(
            name=_text(project.get("name", ""), key + ".name", limit=200),
            technologies=_strings(project.get("technologies", []), key + ".technologies", limit=100),
            tasks=_strings(project.get("tasks", []), key + ".tasks", count=50),
        ))
    return CandidateProfile(role=role, confirmed_skills=skills, clarifications=clarifications,
                            projects=tuple(parsed_projects))


def _read_source(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_PROFILE_BYTES:
            raise ProfileError("профиль соискателя превышает 256 КиБ")
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as error:
        raise ProfileError(f"не найден профиль соискателя: {path}") from error
    except (OSError, UnicodeError) as error:
        raise ProfileError(f"не удалось прочитать профиль соискателя: {path}") from error


def _map_cases(text: str) -> tuple[tuple[str, str], ...]:
    """Only numbered portfolio cases are supplementary evidence, not surrounding instructions."""
    matches = list(re.finditer(r"(?m)^###\s+\d+\.\s+(.+)\r?$", text))
    result = []
    for match in matches:
        end = re.search(r"(?m)^#{1,3}\s+", text[match.end():])
        body = text[match.end():match.end() + end.start()] if end else text[match.end():]
        result.append((match.group(1).strip(), body.strip()))
    return tuple(result)


def _notes_examples(text: str) -> tuple[CandidateAnswer, ...]:
    section = re.search(r"(?ms)^###\s+8\.2\.[^\n]*\n(.*?)(?=^###\s+|^##\s+|\Z)", text)
    if not section:
        return ()
    pairs = re.findall(r"\*\*Вопрос:\*\*\s*«(.*?)»\s*\*\*Ответ[^*]*:\*\*\s*«(.*?)»", section.group(1), re.S)
    return tuple(CandidateAnswer(question.strip(), answer.strip(), source="profile notes: section 8.2")
                 for question, answer in pairs)


def load_profile_file(path: Path, *, competency_map_path: Path | None = None,
                      profile_notes_path: Path | None = None) -> CandidateProfile:
    text = _read_source(path)
    try:
        raw = json.loads(text) if path.suffix.casefold() == ".json" or text.lstrip().startswith("{") else tomllib.loads(text)
    except json.JSONDecodeError as error:
        raise ProfileError(f"профиль соискателя: ошибка JSON ({error})") from error
    except tomllib.TOMLDecodeError as error:
        raise ProfileError(f"профиль соискателя: ошибка TOML ({error})") from error
    profile = parse_candidate_profile(raw)
    if competency_map_path:
        cases = _map_cases(_read_source(competency_map_path))
        projects = []
        for project in profile.projects:
            # Exact source heading matching prevents a generic keyword from
            # attaching another project's claims to this candidate project.
            match = next((body for title, body in cases if _normalize(title) == _normalize(project.name)), "")
            projects.append(replace(project, supplementary=match))
        profile = replace(profile, projects=tuple(projects))
    if profile_notes_path:
        examples = _notes_examples(_read_source(profile_notes_path))
        # Later, explicitly provided examples supersede a same-topic old example.
        # Unrelated examples from JSON stay available.
        topics = {_example_topics(item.question) for item in examples}
        previous = tuple(item for item in profile.answering_examples if _example_topics(item.question) not in topics)
        profile = replace(profile, answering_examples=previous + examples)
    return profile


def load_candidate_profile(cfg: Ai) -> CandidateProfile:
    configured = cfg.profile_path
    if not isinstance(configured, str):
        raise ProfileError("[ai] profile_path должен быть строкой с путём к TOML или JSON")
    if not configured.strip():
        return CandidateProfile()
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path(getattr(cfg, "_profile_base_dir", Path.cwd())) / path
    extra = {}
    for key in ("competency_map_path", "profile_notes_path"):
        value = getattr(cfg, key, "")
        if not isinstance(value, str):
            raise ProfileError(f"[ai] {key} должен быть строкой с путём")
        if value.strip():
            extra_path = Path(value).expanduser()
            if not extra_path.is_absolute():
                extra_path = Path(getattr(cfg, "_profile_base_dir", Path.cwd())) / extra_path
            extra[key] = extra_path.resolve()
    return load_profile_file(path.resolve(), **extra)
