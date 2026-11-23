"""The real generation pipeline labels sourced adjacent cases and keeps AI workflow separate."""
from dataclasses import replace
import unittest

from hh_auto.ai.application import analyze_vacancy, make_letter_plan, validate_letter
from hh_auto.ai.client import LETTER_PROMPT
from hh_auto.ai.letters import ADJACENT_EXPERIENCE_PREFIX, _requested_style
from hh_auto.ai.profile import CandidateSkill
from tests.test_active_generation import RESUME, StubModel, professional_profile


DESCRIPTION = (
    "Задачи:\nПроверять продуктовые гипотезы, создавать прототипы интерфейсов и backend API.\n"
    "Применять ИИ-инструменты для разработки и проверки кода.\n"
    "Требования:\nPython, Cursor, Codex, prompt engineering и работа с пользовательскими сценариями."
)
CASE = ("Разрабатывал согласование курьерских отправлений: реализовал провайдерные адаптеры доставки, "
        "валидацию заявок, синхронизацию статусов и обработку ошибок API. "
        "Работал с серверной логикой на Python и Frappe и пользовательскими сценариями согласования.")
WORKFLOW = ("Для разработки и проверки кода применяю Cursor и Codex, составляю и уточняю промпты "
            "под конкретные задачи.")


def ai_profile():
    profile = professional_profile()
    tools = [CandidateSkill(name, 'hands_on', detail, 'AI tools', (name,)) for name, detail in (
        ('Cursor', 'Cursor: практическая работа с ИИ-ассистентом при разработке кода.'),
        ('Codex', 'Codex: практическая работа с ИИ-ассистентом при разработке кода.'),
        ('Prompt engineering', 'Prompt engineering: составление и уточнение промптов под задачу.'),
        ('AI-assisted development', 'AI-assisted development: разработка и проверка кода с ИИ-инструментами.'),
    )]
    return replace(profile, skills=profile.skills + tuple(tools))


class AdjacentExperienceLetters(unittest.TestCase):
    def setUp(self):
        self.profile = ai_profile()
        self.analysis = analyze_vacancy('Product Engineer / AI Product Engineer', 'Тестовая компания', DESCRIPTION)
        plan = make_letter_plan(self.analysis, self.profile)
        # Exercise formatting separately from the project-ranking contract.
        projects = tuple({**project, 'relation': 'adjacent'} for project in plan.selected_projects)
        self.plan = replace(plan, selected_projects=projects)

    def normalize(self, text, **kwargs):
        return _requested_style(text, self.analysis, self.profile, self.plan, 'Тестовая компания', 1600, **kwargs)

    def test_exact_phrase_precedes_the_case_and_keeps_current_ai_practice_separate(self):
        result = self.normalize(CASE + '\n\n' + WORKFLOW)
        self.assertIn(ADJACENT_EXPERIENCE_PREFIX + ' разрабатывал согласование', result)
        self.assertEqual(result.count(ADJACENT_EXPERIENCE_PREFIX), 1)
        self.assertIn('\n\n' + WORKFLOW, result)
        self.assertNotRegex(result, r'отправлений[^.]*Cursor|отправлений[^.]*Codex')
        self.assertEqual(self.normalize(result), result)

    def test_existing_variant_is_canonicalized_and_multiple_adjacent_cases_get_one_marker(self):
        text = ('Из смежного опыта — ' + CASE + '\n\n'
                'Из смежного опыта: разработал асинхронный сбор данных через asyncio, '
                'нормализацию данных и SQL-запросы.')
        result = self.normalize(text)
        self.assertEqual(result.count(ADJACENT_EXPERIENCE_PREFIX), 1)
        self.assertNotIn('Из смежного опыта —', result)

    def test_a_direct_case_is_not_relabelled_as_adjacent(self):
        self.plan = replace(self.plan, selected_projects=tuple(
            {**project, 'relation': 'direct'} for project in self.plan.selected_projects))
        self.assertNotIn(ADJACENT_EXPERIENCE_PREFIX, self.normalize(CASE))

    def test_an_empty_case_never_gets_an_experience_heading(self):
        self.assertNotIn(ADJACENT_EXPERIENCE_PREFIX, self.normalize(''))
        self.assertNotIn(ADJACENT_EXPERIENCE_PREFIX, self.normalize(WORKFLOW))

    def test_concrete_case_actions_are_labelled_and_validated_without_registry_names(self):
        for text in (
            'Настраивал провайдерные адаптеры доставки, валидацию заявок и обработку ошибок API.',
            'Работал над согласованием курьерских отправлений и обработкой ответов служб доставки.',
            'Писал код валидации заявок и обработки ошибок служб доставки.',
        ):
            with self.subTest(text=text):
                issues = validate_letter(text, self.analysis, self.profile, plan=self.plan)
                self.assertTrue(any(issue.startswith('style:adjacent_marker') for issue in issues), issues)
                result = self.normalize(text + '\n\n' + WORKFLOW)
                self.assertEqual(result.count(ADJACENT_EXPERIENCE_PREFIX), 1, result)
                self.assertIn('\n\n' + WORKFLOW, result)
                self.assertFalse(any(issue.startswith('style:adjacent_marker') for issue in
                                     validate_letter(result, self.analysis, self.profile, plan=self.plan)))
                self.assertEqual(self.normalize(result), result)

    def test_ai_workflow_does_not_receive_case_marker_even_with_shared_data_vocabulary(self):
        workflow = ('Работал с Cursor и Codex, писал промпты для валидации заявок и нормализации данных. '
                    'Проверял с ИИ-ассистентом код интеграций доставки.')
        self.assertNotIn(ADJACENT_EXPERIENCE_PREFIX, self.normalize(workflow))
        text = ADJACENT_EXPERIENCE_PREFIX + ' ' + workflow + '\n\n' + CASE
        result = self.normalize(text)
        self.assertEqual(result.count(ADJACENT_EXPERIENCE_PREFIX), 1, result)
        self.assertIn('\n\n' + ADJACENT_EXPERIENCE_PREFIX + ' разрабатывал', result)
        self.assertFalse(any(issue.startswith('style:adjacent_marker') for issue in
                             validate_letter(result, self.analysis, self.profile, plan=self.plan)))

    def test_duplicate_case_markers_are_reported_and_normalized_to_one(self):
        text = ADJACENT_EXPERIENCE_PREFIX + ' ' + CASE + '\n\n' + ADJACENT_EXPERIENCE_PREFIX + ' ' + CASE
        self.assertTrue(any(issue.startswith('style:adjacent_marker') for issue in
                            validate_letter(text, self.analysis, self.profile, plan=self.plan)))
        self.assertEqual(self.normalize(text).count(ADJACENT_EXPERIENCE_PREFIX), 1)

    def test_employer_keyword_and_english_keep_the_required_format(self):
        text = 'Начните сопроводительное со слова «Зэд.».\n' + DESCRIPTION
        self.analysis = analyze_vacancy('AI Product Engineer', 'Тестовая компания', text)
        result = self.normalize(CASE)
        self.assertEqual(result.splitlines()[0], 'Зэд.')
        self.assertIn(ADJACENT_EXPERIENCE_PREFIX, result)
        self.analysis = analyze_vacancy('AI Product Engineer', 'Company', 'Write the cover letter in English. ' + DESCRIPTION)
        result = self.normalize('I developed delivery integrations and request validation.')
        self.assertNotIn(ADJACENT_EXPERIENCE_PREFIX, result)

    def test_opening_questions_keep_priority_and_do_not_require_the_ordinary_marker(self):
        self.analysis = replace(self.analysis, opening_questions=('Опишите релевантную задачу.',))
        self.plan = replace(self.plan, analysis=self.analysis)
        text = '1. Настраивал валидацию заявок и обработку ошибок служб доставки.'
        self.assertEqual(self.normalize(text), text)
        self.assertFalse(any(issue.startswith('style:adjacent_marker') for issue in
                             validate_letter(text, self.analysis, self.profile, plan=self.plan)))

    def test_profile_aliases_recognize_only_the_confirmed_ai_workflow_skills(self):
        for name in ('Cursor', 'OpenAI Codex', 'prompt-engineering', 'промпт-инжиниринг', 'ИИ-инструменты'):
            self.assertEqual(self.profile.skill_level(name), 'hands_on', name)
        for name in ('LangChain', 'RAG', 'OpenAI API'):
            self.assertNotEqual(self.profile.skill_level(name), 'hands_on', name)
        self.assertTrue(all('Cursor' not in project.technologies for project in self.profile.projects))

    def test_ai_vacancy_context_keeps_confirmed_tools_beyond_the_general_skill_budget(self):
        extra = tuple(CandidateSkill(f'Tool{index}', 'hands_on', f'Tool{index}: Python backend API.',
                                     aliases=(f'Tool{index}',)) for index in range(20))
        profile = replace(self.profile, skills=extra + self.profile.skills)
        context = profile.render('AI Product Engineer: Codex, Python backend API and PostgreSQL.')
        for tool in ('Cursor', 'Codex', 'Prompt engineering', 'AI-assisted development'):
            self.assertIn(tool, context)

    def test_active_prompt_names_adjacent_experience_and_anchors_ai_to_vacancy_tasks(self):
        self.assertIn(ADJACENT_EXPERIENCE_PREFIX, LETTER_PROMPT)
        self.assertIn('1–2 конкретных задач', LETTER_PROMPT)
        self.assertIn('Cursor, Codex и prompt engineering', LETTER_PROMPT)
        self.assertIn('Работа с ИИ при разработке не означает', LETTER_PROMPT)

    def test_public_generation_and_provider_failure_both_label_the_adjacent_case(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                model = StubModel([CASE + '\n\n' + WORKFLOW], unavailable=unavailable)
                model.profile = self.profile
                result = model.write_letter(RESUME, '', 'Product Engineer / AI Product Engineer',
                                            'Тестовая компания', DESCRIPTION, 1600, vacancy_id='fixture-ai-product')
                self.assertEqual(result.count(ADJACENT_EXPERIENCE_PREFIX), 1, result)
                self.assertNotRegex(result, r'(?i)внедрил[^.]*LLM|обучал[^.]*модел|создал[^.]*RAG')

    def test_public_generation_keeps_concrete_action_and_labels_its_adjacent_case(self):
        for action in ('Настраивал валидацию заявок и обработку ошибок служб доставки.',
                       'Работал над согласованием курьерских отправлений и обработкой ошибок.',
                       'Писал код валидации заявок и обработки ошибок служб доставки.'):
            with self.subTest(action=action):
                model = StubModel([action + '\n\n' + WORKFLOW])
                model.profile = self.profile
                result = model.write_letter(RESUME, '', 'Product Engineer / AI Product Engineer',
                                            'Тестовая компания', DESCRIPTION, 1600)
                self.assertEqual(result.count(ADJACENT_EXPERIENCE_PREFIX), 1, result)
                self.assertIn(action[0].lower() + action[1:], result)
                self.assertNotIn('style:adjacent_marker', ' '.join(model.last_letter_record['validation_issues']))


if __name__ == '__main__':
    unittest.main()
