import unittest

from hh_auto.ai.application import analyze_vacancy


class VacancySkillCatalog(unittest.TestCase):
    def test_data_tools_are_extracted_from_requirements(self):
        analysis = analyze_vacancy('Data analyst', 'Example',
            'Требования:\nR, RStudio, Power BI, Power Query M, HR Analytics, OpenAPI, Ruff, pytest-django.')
        for name in ('R', 'RStudio', 'Power BI', 'Power Query M', 'HR Analytics', 'OpenAPI', 'Ruff', 'pytest-django'):
            self.assertIn(name, analysis.required_skills)

    def test_application_and_network_tools_are_extracted(self):
        analysis = analyze_vacancy('Developer', 'Example',
            'Требования:\nGolang, C++, Delphi, Java, Express.js, Pyodide, WireGuard, nftables, IPAM, YooKassa, '
            'TOTP, CSRF, CSP, RBAC, ACL, Transactional Outbox.')
        for name in ('Go', 'C++', 'Delphi', 'Java', 'Express.js', 'Pyodide', 'WireGuard', 'nftables', 'IPAM',
                     'YooKassa', 'TOTP', 'CSRF', 'CSP', 'RBAC', 'ACL', 'Transactional Outbox'):
            self.assertIn(name, analysis.required_skills)

    def test_short_language_names_do_not_match_research_or_ordinary_english(self):
        analysis = analyze_vacancy('R&D engineer', 'Example', 'Go through logs and inspect requests; research and development.')
        self.assertNotIn('R', analysis.required_skills)
        analysis = analyze_vacancy('Engineer', 'Example', 'Please go through logs; R&D projects.')
        self.assertNotIn('Go', analysis.required_skills)
        self.assertNotIn('R', analysis.required_skills)

    def test_structured_go_practice_does_not_inherit_overall_years(self):
        from hh_auto.ai.application import validate_letter
        from hh_auto.ai.profile import parse_candidate_profile
        profile = parse_candidate_profile({'schema_version': '1.0', 'candidate': {'primary_title': 'Developer', 'experience_claim': '5 лет разработки'},
            'competencies': {'Languages': {'hands_on': ['Go: небольшой практический опыт']}}, 'projects': []})
        analysis = analyze_vacancy('Go developer', 'Example', 'Требования: Go.')
        issues = validate_letter('Работал с Go 5 лет.', analysis, profile)
        self.assertTrue(any('scoped_duration' in issue for issue in issues), issues)


if __name__ == '__main__':
    unittest.main()
