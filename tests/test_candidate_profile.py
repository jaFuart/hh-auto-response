"""Подтверждённый профиль отдельно от резюме: структура, факты, переносимые пути. Без API."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hh_auto.ai.profile import (CandidateProfile, ProfileError, load_candidate_profile, load_profile_file,
                                 parse_candidate_profile)
from hh_auto.core.config import Ai, config_values, load_config, save_config


class CandidateProfileStructure(unittest.TestCase):
    def test_empty_configured_path_is_backward_compatible_without_reading_a_file(self):
        with patch("hh_auto.ai.profile.load_profile_file") as read:
            profile = load_candidate_profile(Ai())
        self.assertIsInstance(profile, CandidateProfile)
        self.assertFalse(profile)
        self.assertEqual(profile.render(), "")
        read.assert_not_called()

    def test_explicit_facts_render_without_assigning_global_skills_to_projects(self):
        profile = parse_candidate_profile({
            "version": 1, "role": "Python-разработчик", "confirmed_skills": ["Python", "Django"],
            "clarifications": ["С Django знаком, промышленный проект на нём не вёл."],
            "projects": [{"name": "Каталог", "technologies": ["Python", "SQLite"],
                          "tasks": ["Реализовал поиск записей."]}],
        })
        self.assertEqual(profile.confirmed_skills, ("Python", "Django"))
        self.assertEqual(profile.projects[0].technologies, ("Python", "SQLite"))
        text = profile.render()
        self.assertIn("Есть практический опыт с Django.", text)
        self.assertIn("С Django знаком, промышленный проект на нём не вёл.", text)
        self.assertNotIn("Django", text.split("Проект: Каталог.", 1)[1])
        self.assertNotIn("FastAPI", text)
        self.assertNotIn("лет", text)

    def test_negative_clarifications_are_preserved_without_turning_into_positive_claims(self):
        profile = parse_candidate_profile({"version": 1, "clarifications": ["С Kubernetes не работал."]})
        self.assertEqual(profile.render(), "Уточнения соискателя:\nС Kubernetes не работал.")
        self.assertEqual(profile.confirmed_skills, ())

    def test_missing_or_unsupported_version_is_rejected(self):
        for version in (None, True, "1", 2):
            with self.subTest(version=version), self.assertRaisesRegex(ProfileError, "version = 1"):
                parse_candidate_profile({} if version is None else {"version": version})

    def test_unknown_style_or_pending_sections_are_not_silently_loaded_as_facts(self):
        for key in ("prompt", "answer_style", "pending_skills"):
            with self.subTest(key=key), self.assertRaisesRegex(ProfileError, "неизвестные поля"):
                parse_candidate_profile({"version": 1, key: "unconfirmed"})

    def test_wrong_types_empty_skills_and_unstructured_projects_are_rejected(self):
        invalid = [
            {"role": 1}, {"confirmed_skills": "Python"}, {"confirmed_skills": [""]},
            {"clarifications": [False]}, {"projects": {"name": "Каталог"}},
            {"projects": [{}]}, {"projects": [{"name": "Каталог", "technologies": [1]}]},
            {"projects": [{"name": "Каталог", "years": 5}]},
        ]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ProfileError):
                parse_candidate_profile({"version": 1, **data})


class CandidateProfileFiles(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "data" / "candidate_profile.toml"
        self.path.parent.mkdir()
        self.path.write_text('version = 1\nconfirmed_skills = ["Python", "Django"]\n', encoding="utf-8")

    def test_relative_profile_path_resolves_beside_selected_config_not_current_directory(self):
        config = self.root / "config.toml"
        config.write_text('search_url = ""\n[ai]\nprofile_path = "data/candidate_profile.toml"\n', encoding="utf-8")
        with patch("hh_auto.core.config.load_local_env"):
            cfg = load_config(config, check_search_url=False, upgrade=False)
        self.assertEqual(load_candidate_profile(cfg.ai).confirmed_skills, ("Python", "Django"))
        self.assertEqual(cfg.ai._profile_base_dir, self.root.resolve())
        self.assertEqual(config_values(cfg)["ai"]["profile_path"], "data/candidate_profile.toml")
        self.assertNotIn("_profile_base_dir", config_values(cfg)["ai"])
        saved = self.root / "copied-config.toml"
        save_config(cfg, saved)
        text = saved.read_text(encoding="utf-8")
        self.assertIn('profile_path = "data/candidate_profile.toml"', text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("_profile_base_dir", text)

    def test_absolute_explicit_profile_path_also_loads(self):
        self.assertEqual(load_candidate_profile(Ai(profile_path=str(self.path))).confirmed_skills, ("Python", "Django"))

    def test_configured_missing_profile_is_an_error_not_an_empty_fallback(self):
        with self.assertRaisesRegex(ProfileError, "не найден профиль"):
            load_candidate_profile(Ai(profile_path=str(self.root / "missing.toml")))

    def test_invalid_toml_profile_is_an_error(self):
        self.path.write_text("version = [", encoding="utf-8")
        with self.assertRaisesRegex(ProfileError, "ошибка TOML"):
            load_profile_file(self.path)

    def test_utf8_bom_from_windows_editor_is_supported(self):
        self.path.write_text('version = 1\nrole = "Разработчик"\n', encoding="utf-8-sig")
        self.assertEqual(load_profile_file(self.path).role, "Разработчик")


if __name__ == "__main__":
    unittest.main()
