"""Requested application style, descriptive cases and evidence-backed short confirmations."""

from dataclasses import replace
import unittest

from hh_auto.ai.application import (RUSSIAN_LETTER_ASSESSMENT, RUSSIAN_LETTER_GREETING,
                                     analyze_vacancy, make_letter_plan, validate_letter)
from hh_auto.ai.claims import personal_claim_issues
from hh_auto.ai.client import clean_letter
from hh_auto.ai.letters import _requested_style, _case_description
from hh_auto.ai.profile import CandidateSkill, CandidateProject
from tests.test_active_generation import DESCRIPTION, GOOD_EN, GOOD_RU, RESUME, StubModel


class RequestedLetterStyle(unittest.TestCase):
    def normalize(self, text, description=DESCRIPTION):
        profile = StubModel().profile
        analysis = analyze_vacancy("Python Integration Developer", "Тестовая компания", description)
        plan = make_letter_plan(analysis, profile)
        return _requested_style(clean_letter(text, 1800), analysis, profile, plan, "Тестовая компания", 1800)

    def write(self, model, description=DESCRIPTION, title="Python Integration Developer"):
        return model.write_letter(RESUME, "", title, "Тестовая компания", description, 1800)

    def assert_default_style(self, text):
        self.assertTrue(text.startswith(RUSSIAN_LETTER_GREETING + "\n\n" + RUSSIAN_LETTER_ASSESSMENT))
        self.assertNotIn("В работе использую", text)
        self.assertNotRegex(text, r"прикладно[ем][^ ]* приложени|Практический опыт связан|Моя часть работы:")
        self.assertNotRegex(text, r"Courier Desk|ETL Workbench|Тестовая компания|Новая компания")
        self.assertNotRegex(text, r"буду рад|готов обсудить|ГПХ|В я\b|не могу подтвердить")

    def test_real_generation_preserves_the_case_without_a_forced_skill_ending(self):
        model = StubModel([GOOD_RU + " Буду рад обсудить детали. Мне подходит ГПХ."])
        # The unwanted GPH sentence needs a model correction; the requested case remains intact.
        model.replies.append(GOOD_RU)
        result = self.write(model)
        self.assert_default_style(result)
        self.assertIn("В сервисе интеграций доставки реализовал", result)
        self.assertIn("провайдерные адаптеры доставки", result)
        self.assertIn("обработку ошибок API", result)
        self.assertEqual(personal_claim_issues(result, model.profile), [])
        self.assertLessEqual(model.last_letter_record["revision_count"], 2)

    def test_start_word_is_its_own_first_line_in_generation_and_outage_fallback(self):
        variants = (
            "Начните сопроводительное письмо со слова «Зэд.».",
            "Сопроводительное должно начинаться со слова «Зэд.».",
            "Нужно начать письмо словом «Зэд.».",
            'Begin the cover letter with the word "Зэд.".',
        )
        for instruction in variants:
            for unavailable in (False, True):
                with self.subTest(instruction=instruction, unavailable=unavailable):
                    model = StubModel([GOOD_RU], unavailable=unavailable)
                    result = self.write(model, instruction + "\n" + DESCRIPTION)
                    self.assertEqual(result.splitlines()[0], "Зэд.")
                    self.assert_default_style(result.split("\n\n", 1)[1])
                    self.assertIn("Зэд.", model.last_letter_record["analysis"]["required_keywords"])
                    self.assertNotIn("objective:keyword", " ".join(model.last_letter_record["validation_issues"]))

    def test_cleaner_split_greeting_does_not_leave_a_second_interest_paragraph(self):
        experience = ("Разрабатывал контейнеризированный конвейер сбора и обработки данных о компаниях: "
                      "автоматизировал извлечение и нормализацию данных, выделял этапы ETL, работал с API, "
                      "очередями и воркерами. Собирал Docker-образы и настраивал Docker Compose для "
                      "многокомпонентных приложений с API и базой данных.")
        draft = (RUSSIAN_LETTER_GREETING + "\n\n" + RUSSIAN_LETTER_ASSESSMENT + "\n\n" + experience +
                 "\n\nВ работе использую Python, PostgreSQL, Docker, Flask, FastAPI.")
        result = self.normalize(draft)
        self.assertEqual(result.count("очень заинтересовала данная вакансия!"), 1)
        self.assertEqual(result.count(RUSSIAN_LETTER_ASSESSMENT), 1)
        self.assertIn(experience, result)
        self.assertEqual(self.normalize(result), result)

    def test_saved_backend_with_two_bare_interest_tails_normalizes_until_stable(self):
        experience = ("Разрабатывал контейнеризированный конвейер сбора и обработки данных о компаниях: "
                      "автоматизировал извлечение и нормализацию данных, выделял этапы ETL, работал с API, "
                      "очередями и воркерами. Собирал Docker-образы и настраивал Docker Compose для "
                      "многокомпонентных приложений с API и базой данных.")
        saved = (RUSSIAN_LETTER_GREETING + "\n\n" + RUSSIAN_LETTER_ASSESSMENT +
                 "\n\nочень заинтересовала данная вакансия!\n\n" + experience +
                 "\n\nВ работе использую Python, PostgreSQL, Docker, Flask, FastAPI.")
        # clean_letter splits the first greeting, so this produces two bare tails.
        self.assertEqual(clean_letter(saved, 1800).count("очень заинтересовала данная вакансия!"), 2)
        result = self.normalize(saved)
        self.assertEqual(result.count("очень заинтересовала данная вакансия!"), 1)
        self.assertEqual(result.count(RUSSIAN_LETTER_ASSESSMENT), 1)
        self.assertIn(experience, result)
        self.assertEqual(self.normalize(result), result)

    def test_crm_preview_has_one_skill_paragraph_without_losing_the_zed_case(self):
        experience = ("Разрабатывал приложение для согласования почтовых и курьерских отправлений: "
                      "настраивал поля, проверки, роли и права, реализовывал многошаговые сценарии согласования. "
                      "Интегрировал службы доставки: работал с расчётом тарифов, созданием и отменой заказов, "
                      "отслеживанием и обработкой ошибок.")
        inventory = "Использую Python, REST API и SQL. Знаком с Frappe и JavaScript."
        description = "Начните сопроводительное письмо со слова «Зэд.».\n" + DESCRIPTION
        draft = ("Зэд.\n\n" + RUSSIAN_LETTER_GREETING + "\n\n" + RUSSIAN_LETTER_ASSESSMENT +
                 "\n\n" + experience + "\n\n" + inventory +
                 "\n\nВ работе использую Python, SQL, Frappe, JavaScript, REST.")
        result = self.normalize(draft, description)
        self.assertEqual(result.splitlines()[0], "Зэд.")
        self.assertEqual(result.count(RUSSIAN_LETTER_ASSESSMENT), 1)
        self.assertIn(experience, result)
        self.assertNotIn("В работе использую", result)
        self.assertEqual(result.count("Frappe"), 1)
        self.assertEqual(self.normalize(result, description), result)

    def test_skill_cleanup_preserves_a_concrete_use_of_a_tool(self):
        fact = "Использую Python для асинхронного сбора данных, проверяю ответы API и нормализую записи."
        result = self.normalize(fact + "\n\nИспользую Python, REST API и SQL.")
        self.assertIn(fact, result)
        self.assertNotIn("В работе использую", result)

    def test_saved_mixed_paragraph_does_not_gain_a_second_stack_inventory(self):
        facts = ("Разрабатывал REST API на Django REST Framework. "
                 "В работе использую Celery для фоновых задач и Redis для кэширования.")
        result = self.normalize(facts + "\n\nВ работе использую Django, Celery, Redis.")
        self.assertIn("Разрабатывал REST API на Django REST Framework.", result)
        self.assertIn("Использую Celery для фоновых задач и Redis для кэширования.", result)
        self.assertNotIn("В работе использую", result)
        self.assertEqual(result.count("Celery"), 1)
        self.assertEqual(result.count("Redis"), 1)
        self.assertEqual(self.normalize(result), result)

    def test_unknown_project_domain_is_omitted_instead_of_inventing_a_label(self):
        model = StubModel()
        case = CandidateProject(name="Backend Demo", description="REST API и фоновые задачи.",
                                tasks=("Разрабатывал REST API на Django REST Framework (DRF).",
                                       "Работал с Celery для фоновых задач."), technologies=("Django", "Celery"))
        self.assertEqual(_case_description(case), ("", ""))
        model.profile = replace(model.profile, projects=(case,))
        analysis = analyze_vacancy("Django backend", "Тестовая компания", "Django, REST API, Celery: фоновые задачи.")
        plan = make_letter_plan(analysis, model.profile)
        text = _requested_style("В Backend Demo разрабатывал REST API на Django REST Framework.", analysis,
                                model.profile, plan, "Тестовая компания", 1800)
        self.assertIn("Разрабатывал REST API на Django REST Framework.", text)
        self.assertNotRegex(text, r"Backend Demo|прикладн|В разрабатывал|Моя часть работы")

    def test_outage_fallback_describes_confirmed_tasks_directly(self):
        model = StubModel(unavailable=True)
        case = CandidateProject(name="Backend Demo", description="REST API и фоновые задачи.",
                                tasks=("Разрабатывал REST API на Django REST Framework (DRF).",
                                       "Работал с Celery для фоновых задач."), technologies=("Django", "Celery"))
        model.profile = replace(model.profile, projects=(case,),
                                confirmed_skills=(*model.profile.confirmed_skills, "Django", "Celery"))
        result = self.write(model, "Django, REST API, Celery: фоновые задачи.", "Django backend")
        self.assertIn("Разрабатывал REST API на Django REST Framework (DRF).", result)
        self.assertIn("Работал с Celery для фоновых задач.", result)
        self.assertNotRegex(result, r"Backend Demo|прикладн|Практический опыт связан|Моя часть работы|В работе использую")
        self.assertTrue(model.last_letter_record["fallback_used"])

    def test_inventory_cleanup_preserves_custom_skills_unknown_to_the_catalog(self):
        for body in ("Разрабатывал API на Python.\n\nИспользую Python и QueryForge.\n\nПроверял HTTP-ответы.",
                     "Разрабатывал API на Python.\n\nИспользую QueryForge."):
            with self.subTest(body=body):
                result = self.normalize(body)
                self.assertIn("QueryForge", result)
                self.assertIn("Разрабатывал API на Python.", result)

    def test_concrete_task_survives_removal_of_a_vacuous_preface(self):
        result = self.normalize("Практический опыт связан с задачей: автоматизировал сверку заказов и обработку webhook.")
        self.assertIn("Автоматизировал сверку заказов и обработку webhook.", result)
        self.assertNotIn("Практический опыт связан", result)

    def test_required_nonopening_keyword_survives_a_skill_paragraph(self):
        description = "Обязательно включите в сопроводительное письмо слово «КВАЗАР».\n" + DESCRIPTION
        result = self.normalize("Разрабатывал API на Python.\n\nИспользую Python и КВАЗАР.", description)
        self.assertIn("КВАЗАР", result)

    def test_unknown_project_name_removal_keeps_an_english_sentence_grammatical(self):
        model = StubModel()
        case = CandidateProject(name="Backend Demo", description="REST API.", tasks=("Built a Django API.",))
        model.profile = replace(model.profile, projects=(case,))
        analysis = analyze_vacancy("Django backend", "Company", "Write the cover letter in English. Django API.")
        result = _requested_style("In the project Backend Demo, I built a Django API.", analysis, model.profile,
                                  make_letter_plan(analysis, model.profile), "Company", 1800)
        self.assertEqual(result, "I built a Django API.")

    def test_postal_case_uses_a_grammatical_domain_description_instead_of_deleting_its_name(self):
        model = StubModel([GOOD_RU])
        postal = replace(model.profile.projects[0], description="Согласование почтовых и курьерских отправлений, роли и workflow.")
        model.profile = replace(model.profile, projects=(postal, *model.profile.projects[1:]))
        result = self.write(model)
        self.assertIn("В приложении согласования почтовых и курьерских отправлений реализовал", result)
        self.assertNotIn("Courier Desk", result)
        self.assertNotRegex(result, r"\bВ\s+я\b|\bВ\s*[,.;]")

    def test_descriptive_concrete_case_passes_without_a_registry_name(self):
        model = StubModel()
        text = (RUSSIAN_LETTER_GREETING + "\n\n" + RUSSIAN_LETTER_ASSESSMENT +
                "\n\nВ сервисе интеграций доставки реализовал провайдерные адаптеры и обработку ошибок API. "
                "Добавил валидацию заявок и согласование статусов.\n\nВ работе использую Python, Frappe и PostgreSQL.")
        issues = validate_letter(text, analyze_vacancy("Python backend", "Тестовая компания", DESCRIPTION), model.profile)
        self.assertFalse(any(issue.startswith("style:missing_case") for issue in issues), issues)

    def test_identical_requested_intro_is_not_a_repetition_error_for_different_cases(self):
        model = StubModel()
        prefix = RUSSIAN_LETTER_GREETING + "\n\n" + RUSSIAN_LETTER_ASSESSMENT + "\n\n"
        previous = prefix + "В аналитическом конвейере собирал данные и проверял SQL-запросы."
        text = prefix + "В сервисе доставки реализовал провайдерные адаптеры, валидацию заявок и обработку ошибок API."
        issues = validate_letter(text, analyze_vacancy("Python backend", "Тестовая компания", DESCRIPTION), model.profile,
                                 recent_letters=[previous])
        self.assertFalse(any(issue.startswith("style:repeated") for issue in issues), issues)

    def test_english_and_required_opening_answers_keep_the_employers_format(self):
        model = StubModel([GOOD_EN])
        result = self.write(model, "Write the cover letter in English. Build Python data-processing pipelines.", "AI Engineer")
        self.assertNotIn(RUSSIAN_LETTER_GREETING, result)
        self.assertNotIn("ETL Workbench", result)
        self.assertIn("asynchronous data collection", result)
        answer = "1. В Courier Desk реализовал адаптеры доставки и обработку ошибок API.\n\n2. Добавил валидацию заявок и статусов."
        model = StubModel([answer, answer, answer])
        description = ("Начните письмо с ответов на вопросы:\n1. Какие интеграции разрабатывали?\n"
                       "2. Как проверяли заявки?\n" + DESCRIPTION)
        result = self.write(model, description)
        self.assertTrue(result.startswith("1."))
        self.assertRegex(result, r"(?m)^2\.")
        self.assertNotIn(RUSSIAN_LETTER_GREETING, result)
        self.assertNotIn("Courier Desk", result)


class ConfirmedShortAnswers(unittest.TestCase):
    def model(self):
        model = StubModel(unavailable=True)
        skill = CandidateSkill("Power BI", "hands_on", "Power BI: практика подтверждена", aliases=("Power BI",))
        model.profile = replace(model.profile, confirmed_skills=(*model.profile.confirmed_skills, "Power BI"),
                                skills=(*model.profile.skills, skill))
        return model

    def test_confirmed_power_bi_has_a_short_yes_without_a_model_call(self):
        for question in ("Работали ли вы с Power BI?", "Есть ли у вас опыт с PowerBI?", "Владеете ли вы Power BI?"):
            with self.subTest(question=question):
                model = self.model()
                result = model.decide(RESUME, "", "Работодатель: " + question, [])
                self.assertEqual(result.kind, "question")
                self.assertEqual(result.reply, "Да.")
                self.assertEqual(model.calls, [])

    def test_unknown_or_extra_conditions_never_become_an_invented_yes(self):
        for question in ("Работали ли вы с Tableau?", "Работали ли вы с Power BI и Tableau?",
                         "Есть ли у вас 5 лет опыта с Power BI?", "Какие задачи решали с Power BI?"):
            with self.subTest(question=question):
                model = self.model()
                result = model.decide(RESUME, "", "Работодатель: " + question, [])
                self.assertEqual(result.kind, "question")
                self.assertNotEqual(result.reply, "Да.")
                self.assertTrue(result.reply)
                self.assertNotRegex(result.reply, r"5\s+лет|(?:использовал|работал).{0,20}Tableau")
                self.assertEqual(personal_claim_issues(result.reply, model.profile), [])


if __name__ == "__main__":
    unittest.main()
