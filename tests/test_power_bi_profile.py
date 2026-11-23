"""Подтверждение одного BI-инструмента не добавляет вымышленные смежные навыки."""

import unittest

from hh_auto.ai.profile import parse_candidate_profile


class PowerBiProfile(unittest.TestCase):
    def profile(self, entry):
        return parse_candidate_profile({
            "schema_version": "1.0",
            "candidate": {"primary_title": "Developer"},
            "competencies": {"BI": {"hands_on": [entry]}},
            "projects": [],
        })

    def test_confirmed_power_bi_recognizes_both_spellings_without_asserting_another_tool(self):
        for source in ("Power BI", "PowerBI"):
            with self.subTest(source=source):
                profile = self.profile(source + " (практика подтверждена; задачи и стаж не уточнены)")
                self.assertEqual(profile.skill_level("Power BI"), "hands_on")
                self.assertEqual(profile.skill_level("PowerBI"), "hands_on")
                self.assertIn("Power BI", profile.confirmed_skills)
                self.assertIn(source, profile.render_facts())
                self.assertEqual(profile.skill_level("Tableau"), "")
                self.assertEqual(profile.projects, ())
                self.assertNotIn("Tableau", profile.render_facts())

    def test_familiar_power_bi_is_not_promoted_to_personal_practice(self):
        profile = parse_candidate_profile({
            "schema_version": "1.0", "candidate": {"primary_title": "Developer"},
            "competencies": {"BI": {"familiar": ["PowerBI"]}},
            "projects": [],
        })
        self.assertEqual(profile.skill_level("Power BI"), "familiar")
        self.assertNotIn("Power BI", profile.confirmed_skills)
        self.assertNotIn("PowerBI", profile.render_facts())


if __name__ == "__main__":
    unittest.main()
