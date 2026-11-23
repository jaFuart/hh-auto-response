"""Synthetic vacancy/portfolio fixtures only: no account, API, personal candidate dataset or sends."""

import json
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_auto.ai.application import (analyze_vacancy, deduplicate_vacancies, make_letter_plan,
                                    normalize_vacancy_text, validate_letter)


PROFILE = {
    "role": "Python engineer", "current_employer": "New Company", "former_employer": "Old Company",
    "confirmed_skills": ["Python", "FastAPI", "REST API", "SQL", "PostgreSQL", "Docker", "RabbitMQ", "ETL"],
    "skills": [{"name": "SQLAlchemy", "level": "familiar", "detail": "Знакомство с архитектурой"}],
    "approved_urls": ["https://example.test/work"],
    "projects": [
        {"id": "files", "name": "File Worker Service", "description": "API и контейнерный pipeline обработки файлов",
         "technologies": ["Python", "FastAPI", "PostgreSQL", "RabbitMQ", "Docker", "TypeScript"],
         "personal_technologies": ["Python", "FastAPI", "Docker"], "team_technologies": ["TypeScript"],
         "tasks": ["Разрабатывал API приёма файлов", "Разделял фоновые задачи и статусы обработки"],
         "limitations": ["RabbitMQ не означает опыт с Kafka; интерфейс TypeScript создавал другой участник"],
         "allowed_urls": ["https://example.test/work"], "kind": "personal", "evidence": "Синтетический пример"},
        {"id": "parcel", "name": "Parcel Workflow", "description": "Корпоративные CRM и интеграции доставки",
         "technologies": ["Frappe", "REST API", "SOAP", "Redis"],
         "tasks": ["Подключал провайдеров доставки через HTTP API", "Синхронизировал статусы и роли workflow"],
         "kind": "commercial"},
        {"id": "data", "name": "Data Quality", "description": "ETL и DWH, качество и дедупликация данных",
         "technologies": ["Python", "SQL", "ETL", "PostgreSQL"],
         "tasks": ["Нормализовал данные", "Писал SQL с оконными функциями"], "kind": "commercial"},
        {"id": "commerce", "name": "Commerce Metrics", "description": "marketplace analytics e-commerce прогнозирование",
         "technologies": ["Python", "Prophet"], "tasks": ["Автоматизировал отчёты и прогнозирование продаж"],
         "kind": "personal"},
        {"id": "ml", "name": "Forecast Experiment", "description": "Исследовательские ML-модели и классификация",
         "technologies": ["Python", "BERT", "LSTM"], "tasks": ["Проверял гипотезы и качество прогнозирования"],
         "limitations": ["Исследование не является production ML"], "kind": "research"},
    ],
}


def analysis(description, title="Backend engineer", vacancy_id="synthetic"):
    return analyze_vacancy(title, "Synthetic company", description, vacancy_id)


class VacancyAnalysisTest(unittest.TestCase):
    def test_full_text_instruction_after_4000_characters_is_not_lost(self):
        prefix = "Описание продукта и командной работы. " * 180
        result = analysis(prefix + "\nОБЯЗАТЕЛЬНО:\nPython и FFmpeg.\nКАК ОТКЛИКНУТЬСЯ:\n"
                          "Пришлите короткое сопроводительное письмо на английском.\nВ отклике приложите:\n"
                          "— Примеры законченных AI-видео.\n— Описание пайплайна и личного вклада.\n"
                          "— Краткий разбор дефекта и исправления.\n— Информацию о стоимости и повторных попытках.",
                          "AI Video Engineer")
        self.assertGreater(len(result.full_text), 4000)
        self.assertEqual(result.language, "en")
        self.assertTrue(result.requires_video_samples)
        self.assertIn("FFmpeg", result.critical_requirements)
        materials = " ".join(item["text"] for item in result.application_instructions)
        self.assertIn("стоимости", materials)
        self.assertIn("личного вклада", materials)

    def test_two_opening_questions_are_complete_and_in_order(self):
        result = analysis("Обязательное условие для отклика: начните сопроводительное письмо с ответов на 2 вопроса:\n"
                          "1. С какими источниками работали, сколько потоков запускали\nодновременно?\n"
                          "2. Какой сложный сбой разбирали и что исправили?\nPython\nDocker", "R&D engineer")
        self.assertEqual(len(result.opening_questions), 2)
        self.assertIn("потоков запускали одновременно?", result.opening_questions[0])
        self.assertTrue(result.opening_questions[1].endswith("что исправили?"))
        self.assertNotIn("Docker", result.opening_questions[1])
        self.assertIn("без приветствия", result.render_guidance())

    def test_english_skill_requirement_is_not_an_english_letter_instruction(self):
        self.assertEqual(analysis("Требования:\nPython.\nАнглийский B2.").language, "ru")

    def test_corporate_results_and_documents_do_not_imply_informal_address(self):
        result = analysis("Успешному кандидату мы предлагаем обучение.\n"
                          "Требования:\nОтветственность за результаты и документы.")
        self.assertEqual(result.vacancy_tone, "formal")

    def test_production_ml_requirement_is_distinct_from_research(self):
        result = analysis("Что ждем от кандидата:\nОпыт вывода ML-решений в продакшн и их поддержки.",
                          "Data Scientist")
        self.assertTrue(any("продакшн" in requirement for requirement in result.critical_requirements))

    def test_optional_specialized_skill_does_not_become_critical(self):
        result = analysis("Требования:\nTalend Studio на экспертном уровне.\nБудет плюсом:\nSpark и Iceberg.",
                          "Data Engineer")
        self.assertIn("Talend Studio", result.critical_requirements)
        self.assertNotIn("Spark", result.critical_requirements)
        self.assertIn("Spark", result.preferred_skills)

    def test_html_normalization_preserves_lists_and_tail(self):
        result = normalize_vacancy_text("<h2>Требования</h2><ul><li>Python</li><li>SQL</li></ul>"
                                        "<p>Напишите письмо на английском.</p><script>secret()</script>")
        self.assertRegex(result, r"Python\s+SQL")
        self.assertIn("на английском", result)
        self.assertNotIn("secret()", result)


class LetterPlanTest(unittest.TestCase):
    def test_vendor_technologies_are_not_inferred_from_adjacent_skills(self):
        result = analysis("Что для нас важно:\nClickHouse, dbt и Kafka в production.\n"
                          "Требуется опыт Talend Studio, PHP и Laravel.", "Data Engineer")
        plan = make_letter_plan(result, PROFILE)
        for name in ("ClickHouse", "dbt", "Kafka", "Talend Studio", "PHP", "Laravel"):
            self.assertIn(name, plan.critical_gaps)
        self.assertNotIn("Kafka", plan.strongest_matches)
        self.assertNotIn("Talend Studio", plan.strongest_matches)
        self.assertTrue(plan.automatic)
        self.assertFalse(plan.requires_approval)
        self.assertTrue(plan.matching_is_advisory_only)

    def test_familiarity_is_preserved_without_promoting_to_hands_on(self):
        plan = make_letter_plan(analysis("Обязательно:\nPython и SQLAlchemy."), PROFILE)
        self.assertEqual(plan.evidence_levels["SQLAlchemy"], "familiar")
        self.assertNotIn("SQLAlchemy", plan.strongest_matches)
        self.assertIn("SQLAlchemy: familiar", plan.gaps)

    def test_project_stack_is_not_promoted_to_personal_skill(self):
        plan = make_letter_plan(analysis("Обязательно:\nTypeScript и FastAPI."), PROFILE)
        self.assertNotIn("TypeScript", plan.strongest_matches)
        project = next(item for item in plan.selected_projects if item["id"] == "files")
        self.assertIn("TypeScript", project["team_technologies"])
        self.assertNotIn("TypeScript", project["personal_technologies"])
        self.assertIn("не равен личному опыту", plan.render_guidance())

    def test_requested_video_portfolio_is_not_replaced_with_a_software_url(self):
        result = analysis("КАК ОТКЛИКНУТЬСЯ:\nПришлите сопроводительное письмо на английском.\n"
                          "Приложите примеры законченных AI-видео.", "AI Video Engineer")
        plan = make_letter_plan(result, PROFILE)
        self.assertTrue(plan.missing_materials)
        self.assertTrue(plan.automatic)
        self.assertIn("не обещай их показать", plan.render_guidance())

    def test_case_selection_changes_with_business_problem(self):
        api = make_letter_plan(analysis("Обязательно:\nFastAPI, PostgreSQL, Docker.\n"
                                        "Что будешь делать:\nРазвивать API и обработку файлов."), PROFILE)
        logistics = make_letter_plan(analysis("Чем предстоит заниматься:\nИнтеграции провайдеров доставки, "
                                              "статусы и workflow.\nЧто для нас важно:\nPHP и Laravel.",
                                              "Backend — интеграции"), PROFILE)
        commerce = make_letter_plan(analysis("Привет. Автоматизируем бизнес на Wildberries и Ozon.\n"
                                             "Тебе подходит, если:\nРаботаешь с Claude Code.",
                                             "AI-разработчик"), PROFILE)
        self.assertEqual(api.selected_projects[0]["id"], "files")
        self.assertEqual(logistics.selected_projects[0]["id"], "parcel")
        self.assertIn("commerce", [item["id"] for item in commerce.selected_projects])

    def test_zero_fit_still_has_automatic_delivery_policy(self):
        plan = make_letter_plan(analysis("Обязательно:\nTalend Studio и Laravel."), {})
        self.assertEqual(plan.fit_score, 0)
        self.assertTrue(plan.automatic)
        self.assertFalse(plan.requires_approval)
        self.assertIn("при любом fit", plan.render_guidance())


class QualityDiagnosticsTest(unittest.TestCase):
    def test_required_questions_must_precede_greeting(self):
        result = analysis("Начните сопроводительное письмо с ответов на 2 вопроса:\n"
                          "1. Какой ваш стек?\n2. Какой проверяемый результат?")
        bad = validate_letter("Здравствуйте! Работаю на Python, готов обсудить задачи.", result, PROFILE)
        good = validate_letter("1. Разрабатываю API на Python.\n2. В File Worker Service разделял фоновые задачи.",
                               result, PROFILE)
        self.assertTrue(any(issue.startswith("objective:opening") for issue in bad))
        self.assertTrue(any(issue.startswith("objective:questions") for issue in bad))
        self.assertFalse(any(issue.startswith(("objective:opening", "objective:questions")) for issue in good))

    def test_language_and_fabricated_video_links_are_objective_issues(self):
        result = analysis("КАК ОТКЛИКНУТЬСЯ:\nПришлите сопроводительное письмо на английском.\n"
                          "Приложите примеры законченных AI-видео.", "AI Video Engineer")
        issues = validate_letter("Здравствуйте! Прикладываю готовое видео https://invented.test/video.", result, PROFILE)
        for prefix in ("objective:language", "objective:unapproved_url", "objective:video_evidence"):
            self.assertTrue(any(issue.startswith(prefix) for issue in issues), issues)

    def test_english_body_can_retain_known_russian_title_and_company(self):
        result = analyze_vacancy("AI Video Engineer / Python-разработчик генеративного видео",
                                 "Синтетическая Компания", "Сопроводительное письмо на английском.")
        english = ("I built reliable backend services and integrations in File Worker Service. "
                   "This experience is relevant to the «AI Video Engineer / Python-разработчик генеративного видео» "
                   "role at Синтетическая Компания, especially reliable execution and error handling.")
        issues = validate_letter(english, result, PROFILE)
        self.assertFalse(any(issue.startswith("objective:language") for issue in issues), issues)
        russian = english + " Мой опыт разработки помогает решать задачи интеграций и обработки данных."
        self.assertTrue(any(issue.startswith("objective:language") for issue in validate_letter(russian, result, PROFILE)))

    def test_short_real_project_name_is_not_required_to_include_descriptive_suffix(self):
        profile = {**PROFILE, "projects": [*PROFILE["projects"],
                   {"id": "data_studio", "name": "Example Data Studio: synthetic data example",
                    "tasks": ["Синтетический пример проверки данных"], "technologies": ["Python"]}]}
        result = analysis("Обязательно:\nFastAPI и Docker.")
        plan = make_letter_plan(result, profile)
        plan = replace(plan, selected_projects=(next(project for project in plan.selected_projects if project["id"] == "files"),))
        issues = validate_letter("В Example Data Studio автоматизировал проверки данных.", result, profile, plan=plan)
        self.assertFalse(any(issue.startswith("style:missing_case") for issue in issues), issues)
        invented = validate_letter("В проекте «Example Data Systems» автоматизировал проверки.", result, profile, plan=plan)
        self.assertTrue(any(issue.startswith("style:missing_case") for issue in invented))
        self.assertTrue(any(issue.startswith("objective:project") for issue in invented))

    def test_another_confirmed_case_is_valid_even_if_not_selected_by_ranker(self):
        result = analysis("Обязательно:\nFastAPI и Docker.")
        plan = make_letter_plan(result, PROFILE)
        plan = replace(plan, selected_projects=(next(project for project in plan.selected_projects if project["id"] == "files"),))
        issues = validate_letter("В Forecast Experiment проверял качество моделей и гипотезы.", result, PROFILE, plan=plan)
        self.assertFalse(any(issue.startswith("style:missing_case") for issue in issues), issues)

    def test_descriptive_marketplace_analytics_case_matches_its_source_domain(self):
        result = analysis("Автоматизация бизнеса на маркетплейсах.", "AI-разработчик")
        text = ("В проекте сервиса аналитики маркетплейсов я разрабатывал и внедрял модели прогнозирования "
                "продаж с использованием временных рядов.")
        issues = validate_letter(text, result, PROFILE)
        self.assertFalse(any(issue.startswith("style:missing_case") for issue in issues), issues)
        unrelated = {**PROFILE, "projects": [project for project in PROFILE["projects"] if project["id"] != "commerce"]}
        unsupported = validate_letter(text, result, unrelated)
        self.assertTrue(any(issue.startswith("style:missing_case") for issue in unsupported))
        fake = validate_letter('В проекте «Fictional Marketplace Wizard» создавал сервис аналитики маркетплейсов.',
                               result, PROFILE)
        self.assertTrue(any(issue.startswith("objective:project") for issue in fake))

    def test_generic_sql_or_interest_in_marketplace_service_is_not_a_personal_case(self):
        result = analysis("Автоматизация бизнес-процессов.", "AI-разработчик")
        for text in ("Работал с SQL и Python.", "Мне интересен проект сервиса аналитики маркетплейсов."):
            with self.subTest(text=text):
                issues = validate_letter(text, result, PROFILE)
                self.assertTrue(any(issue.startswith("style:missing_case") for issue in issues))

    def test_gph_boilerplate_conflicts_with_employee_terms(self):
        result = analysis("Условия:\nОформление по ТК РФ с первого дня.")
        issues = validate_letter("Здравствуйте! Мне подходит ГПХ, готов обсудить задачи.", result, PROFILE)
        self.assertTrue(any(issue.startswith("objective:contract_contradiction") for issue in issues))
        neutral = validate_letter("Опишу опыт интеграций в Parcel Workflow.", result, PROFILE)
        self.assertFalse(any("contract" in issue or "gph" in issue for issue in neutral))

    def test_video_production_cannot_be_invented_without_a_link_either(self):
        result = analysis("КАК ОТКЛИКНУТЬСЯ:\nСопроводительное письмо на английском.\n"
                          "Приложите примеры законченных AI-видео.", "AI Video Engineer")
        issues = validate_letter("I built production video pipelines and created multi-scene AI videos.", result, PROFILE)
        self.assertTrue(any(issue.startswith("objective:video_experience") for issue in issues))

    def test_missing_portfolio_is_not_hidden_behind_fabricated_nda(self):
        result = analysis("Примеры законченных AI-видео. Сопроводительное письмо на английском.", "AI Video Engineer")
        issues = validate_letter("My videos are covered by an NDA.", result, PROFILE)
        self.assertTrue(any(issue.startswith("objective:unsupported_nda") for issue in issues))

    def test_unknown_employer_is_detected_without_a_fabricated_number(self):
        issues = validate_letter("I worked at Invented Corp on Python services.", analysis("Python backend"), PROFILE)
        self.assertTrue(any(issue.startswith("objective:unverified_employer") for issue in issues))

    def test_unsupported_vendor_experience_is_flagged_but_not_a_delivery_decision(self):
        result = analysis("Обязательно:\nTalend Studio, Kafka и Laravel.")
        issues = validate_letter("Работал с Kafka и использовал Talend Studio.", result, PROFILE)
        self.assertTrue(any(issue.startswith("objective:unbacked_skill") for issue in issues))
        self.assertIsInstance(issues, list)

    def test_repeated_structure_is_detected_beyond_literal_copy(self):
        recent = ["Здравствуйте! Разрабатываю сервисы на Python. Мне интересны задачи API. Готов обсудить задачи.",
                  "Здравствуйте! Пишу интеграции. Мне интересны задачи данных. Буду рад обсудить задачи.",
                  "Привет! Занимаюсь обработкой файлов. Интересны ваши задачи. Предлагаю обсудить детали."]
        text = "Здравствуйте! Разрабатываю модели. Мне интересны задачи платформы. Готов обсудить задачи."
        issues = validate_letter(text, analysis("Python backend"), PROFILE, recent_letters=recent)
        self.assertTrue(any(issue.startswith("style:repeated_structure") for issue in issues))

    def test_employer_word_limit_overrides_default_minimum_length(self):
        result = analysis("В сопроводительном письме не более 20 слов.")
        issues = validate_letter("Разрабатываю API на Python и обработку файлов в File Worker Service.", result, PROFILE)
        self.assertFalse(any("length" in issue for issue in issues), issues)

    def test_unknown_project_and_outdated_current_employer_are_flagged(self):
        issues = validate_letter('Сейчас работаю в Old Company. В проекте «Invented System» создавал сервис.',
                                 analysis("Backend"), PROFILE)
        self.assertTrue(any(issue.startswith("objective:current_employer") for issue in issues))
        self.assertTrue(any(issue.startswith("objective:project") for issue in issues))

    def test_metrics_and_framework_seniority_need_their_own_evidence(self):
        result = analysis("Python backend")
        issues = validate_letter("С Django работал 5 лет, ускорил систему на 40% для 500 пользователей.",
                                 result, {**PROFILE, "experience_claim": "5+ лет общего опыта"})
        self.assertTrue(any(issue.startswith("objective:unverified_quantity") for issue in issues))
        self.assertTrue(any(issue.startswith("objective:scoped_duration") for issue in issues))

    def test_general_backend_seniority_is_not_a_claim_about_framework_duration(self):
        issues = validate_letter("Python backend engineer, 5 лет общего опыта разработки.", analysis("Python backend"),
                                 {**PROFILE, "experience_claim": "5+ лет общего опыта разработки"})
        self.assertFalse(any(issue.startswith("objective:scoped_duration") for issue in issues))

    def test_unverified_account_and_concurrent_thread_counts_are_flagged(self):
        issues = validate_letter("Запускал 200 аккаунтов и 100 потоков одновременно.",
                                 analysis("R&D automation"), PROFILE)
        self.assertEqual(sum(issue.startswith("objective:unverified_quantity") for issue in issues), 2)

    def test_missing_description_is_diagnostic_without_blocking_policy(self):
        result = analysis("", "Backend engineer")
        plan = make_letter_plan(result, PROFILE)
        self.assertIn("недоступно", result.render_guidance())
        self.assertTrue(plan.automatic)
        self.assertFalse(plan.requires_approval)

    def test_analysis_and_plan_are_json_serializable(self):
        result = analysis("Обязательно:\nFastAPI и Docker.")
        plan = make_letter_plan(result, PROFILE)
        json.dumps(result.to_dict(), ensure_ascii=False)
        json.dumps(plan.to_dict(), ensure_ascii=False)


class TenSyntheticVacanciesTest(unittest.TestCase):
    def test_ten_unique_cases_keep_distinct_critical_instructions(self):
        fixtures = [
            ("Backend FastAPI", "Обязательно:\nFastAPI и Docker.\nРазвиваем VPN-сервис."),
            ("AI-разработчик", "Привет. Бизнес Wildberries и Ozon.\nТебе подходит, если:\nClaude Code каждый день."),
            ("AI-engineer", "Наши ожидания:\nn8n, LangChain и Битрикс24.\nУсловия:\nОформление по ТК РФ."),
            ("R&D Python", "Начните письмо с ответов на 2 вопроса:\n1. Какой стек?\n2. Какой результат?"),
            ("AI Video Engineer", "КАК ОТКЛИКНУТЬСЯ:\nСопроводительное письмо на английском.\nПримеры законченных AI-видео."),
            ("Data Engineer", "Обязательно:\nTalend Studio и Talend Data Quality."),
            ("Разработчик RPA", "Чем предстоит заниматься:\nБизнес-процессы на Sherpa RPA."),
            ("Data Engineer", "Что для нас важно:\nClickHouse, dbt, Kafka и Python."),
            ("Backend интеграции", "Что для нас важно:\nОт трёх лет PHP и Laravel.\nИнтеграции доставки."),
            ("Data Scientist", "Что ждем от кандидата:\nPySpark, Hadoop, Kafka, Airflow и production ML."),
        ]
        records = [{"id": str(index), "title": title, "vacancy_text": text}
                   for index, (title, text) in enumerate(fixtures, 1)]
        unique = deduplicate_vacancies(records + records[:4])
        self.assertEqual(len(unique), 10)
        analyses = [analyze_vacancy(item["title"], "Synthetic", item["vacancy_text"], item["id"]) for item in unique]
        self.assertEqual(len(analyses[3].opening_questions), 2)
        self.assertEqual(analyses[4].language, "en")
        self.assertTrue(analyses[4].requires_video_samples)
        for index, skill in ((1, "Claude Code"), (2, "n8n"), (5, "Talend Studio"), (6, "Sherpa RPA"),
                             (7, "Kafka"), (8, "Laravel"), (9, "PySpark")):
            self.assertIn(skill, analyses[index].critical_requirements)
        for item in analyses:
            plan = make_letter_plan(item, PROFILE)
            self.assertTrue(plan.matching_is_advisory_only)
            self.assertTrue(plan.automatic)
            self.assertFalse(plan.requires_approval)


if __name__ == "__main__":
    unittest.main()
