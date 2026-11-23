"""Fresh setup and doctor use only explicit local evidence; no hh/model interaction."""

from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hh_auto.ai.profile import ProfileError
from hh_auto.core.config import ConfigError, load_config
from hh_auto.core.config_file import write_values
from scripts.configure import SetupError, _atomic_write, doctor, initialize, main


SAFE_TIMING = {
    "limits": {
        "submission_interval_seconds": 180, "submission_batch_size": 2,
        "submission_batch_pause_minutes": 25, "captcha_cooldown_minutes": 120,
        "read_interval_seconds": 10, "max_responses_per_day": 10,
    },
    "watch": {
        "robot_every_minutes": 15, "robot_max_chats_per_check": 10, "robot_reply_interval_seconds": 30,
    },
}


class ConfigureCommands(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.data = self.root / "new user data"
        self.source = self.root / "source files"
        self.source.mkdir()
        self.profile = self.source / "my actual profile.toml"
        self.profile.write_text('version = 1\nrole = "Python developer"\nconfirmed_skills = ["Python"]\n'
                                'clarifications = ["Private profile fact marker"]\n'
                                '[[projects]]\nname = "Catalog"\ntechnologies = ["Python"]\n'
                                'tasks = ["Built catalog search"]\n', encoding="utf-8")
        self.mapping = self.source / "my case map.markdown"
        self.mapping.write_text("# Cases\n\n### 1. Catalog\nPrivate case fact marker.\n", encoding="utf-8")

    def config(self):
        return load_config(self.data / "config.server.toml", check_search_url=False, upgrade=False, load_env=False)

    def tree(self, path):
        return {str(item.relative_to(path)): item.read_bytes() for item in path.rglob("*") if item.is_file()} if path.exists() else {}

    def test_fresh_init_has_no_synthetic_candidate_evidence_and_no_automatic_tasks(self):
        result = initialize(self.data, gender="female", model="configured-model")
        cfg = self.config()
        self.assertEqual(cfg.ai.candidate_gender, "female")
        self.assertEqual(cfg.ai.model, "configured-model")
        self.assertEqual(cfg.ai.profile_path, "")
        self.assertEqual(cfg.ai.competency_map_path, "")
        self.assertFalse(cfg.schedule.enabled)
        self.assertFalse(cfg.watch.robot_replies)
        self.assertFalse(cfg.raising.enabled)
        self.assertFalse(cfg.dream.enabled)
        self.assertTrue(cfg.browser.headless)
        self.assertEqual(cfg.browser.backend, "playwright")
        self.assertEqual(result["browser_backend"], "playwright")
        self.assertFalse(result["automatic_tasks_enabled"])
        self.assertIn("OPENAI_API_KEY=\n", (self.data / ".env").read_text())
        for section, values in SAFE_TIMING.items():
            for name, value in values.items():
                self.assertEqual(getattr(getattr(cfg, section), name), value)
        self.assertEqual(cfg.limits.captcha_rest, 0)

    def test_public_examples_share_safe_timing_with_automation_off(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("config.example.toml", "config.openai.example.toml", "config.server.example.toml"):
            with self.subTest(example=name):
                cfg = load_config(root / name, check_search_url=False, upgrade=False, load_env=False)
                for section, values in SAFE_TIMING.items():
                    for field, value in values.items():
                        self.assertEqual(getattr(getattr(cfg, section), field), value)
                self.assertEqual(cfg.limits.captcha_rest, 0)
                self.assertFalse(cfg.schedule.enabled)
                self.assertFalse(cfg.watch.robot_replies)

    def test_cli_uc_choice_keeps_explicit_profile_resume_and_automation_off(self):
        output = io.StringIO()
        with redirect_stdout(output), patch("urllib.request.urlopen", side_effect=AssertionError("No network")), \
                patch("hh_auto.ai.client.AiClient", side_effect=AssertionError("No model")):
            code = main(["init", "--data-dir", str(self.data), "--browser-backend", "seleniumbase_uc",
                         "--profile", str(self.profile), "--competency-map", str(self.mapping),
                         "--resume", "selected", "--json"])
        self.assertEqual(code, 0)
        cfg = self.config()
        self.assertEqual(cfg.browser.backend, "seleniumbase_uc")
        self.assertTrue(cfg.browser.headless)
        self.assertEqual(cfg.browser.channel, "")
        self.assertEqual(cfg.resume_id, "selected")
        self.assertEqual((self.data / cfg.ai.profile_path).read_bytes(), self.profile.read_bytes())
        self.assertFalse(cfg.schedule.enabled)
        self.assertFalse(cfg.watch.robot_replies)
        self.assertEqual(json.loads(output.getvalue())["browser_backend"], "seleniumbase_uc")

    def test_invalid_browser_backend_is_rejected_before_copying_any_files(self):
        with self.assertRaisesRegex(SetupError, "browser_backend"):
            initialize(self.data, profile_source=self.profile, browser_backend="unsupported")
        self.assertFalse(self.data.exists())

    def test_custom_file_paths_copy_explicit_facts_and_portable_links(self):
        initialize(self.data, profile_source=self.profile, map_source=self.mapping, resume="abc123",
                   provider="custom", model="custom-model", base_url="https://provider.example/v1", gender="female")
        cfg = self.config()
        self.assertEqual(cfg.ai.profile_path, "data/candidate_profile.toml")
        self.assertEqual(cfg.ai.competency_map_path, "data/competency_map.md")
        self.assertEqual((self.data / cfg.ai.profile_path).read_bytes(), self.profile.read_bytes())
        self.assertEqual((self.data / cfg.ai.competency_map_path).read_bytes(), self.mapping.read_bytes())
        self.assertEqual(cfg.resume_id, "abc123")
        self.assertIn("resume=abc123", cfg.search_url)
        self.assertEqual(cfg.ai.api_key, "")

    def test_json_profile_with_arbitrary_name_is_validated_and_copied(self):
        profile = self.source / "chosen facts.json"
        profile.write_text(json.dumps({"version": 1, "role": "Developer", "confirmed_skills": ["Python"]}))
        initialize(self.data, profile_source=profile)
        self.assertEqual(self.config().ai.profile_path, "data/candidate_profile.json")
        self.assertEqual((self.data / "data/candidate_profile.json").read_bytes(), profile.read_bytes())

    def test_invalid_profile_or_map_is_rejected_before_any_destination_exists(self):
        invalid = self.source / "invalid.json"
        invalid.write_text("{broken")
        with self.assertRaises(ProfileError):
            initialize(self.data, profile_source=invalid)
        self.assertFalse(self.data.exists())
        self.mapping.write_text("# A valid Markdown document without case headers")
        with self.assertRaisesRegex(SetupError, "заголовки"):
            initialize(self.data, profile_source=self.profile, map_source=self.mapping)
        self.assertFalse(self.data.exists())

    def test_map_must_have_a_matching_nonempty_project_case(self):
        self.mapping.write_text("### 1. Different project\nUnrelated evidence.\n")
        with self.assertRaisesRegex(SetupError, "совпадающим"):
            initialize(self.data, profile_source=self.profile, map_source=self.mapping)
        self.assertFalse(self.data.exists())
        with self.assertRaisesRegex(SetupError, "требует"):
            initialize(self.data, map_source=self.mapping)
        self.assertFalse(self.data.exists())

    def test_destination_conflict_preserves_all_existing_data_without_partial_config(self):
        target = self.data / "data/candidate_profile.toml"
        target.parent.mkdir(parents=True)
        target.write_text("Preserve existing facts")
        before = self.tree(self.data)
        with self.assertRaisesRegex(SetupError, "уже существует"):
            initialize(self.data, profile_source=self.profile)
        self.assertEqual(self.tree(self.data), before)

    def test_atomic_publication_refuses_an_existing_target_even_without_init_preflight(self):
        target = self.source / "concurrent-destination.txt"
        target.write_bytes(b"Existing data")
        with self.assertRaises(SetupError):
            _atomic_write(target, b"Must not replace existing data", overwrite=False)
        self.assertEqual(target.read_bytes(), b"Existing data")
        self.assertEqual(list(self.source.glob(".hh-init-*")), [])

    def test_overwrite_is_explicit_and_preserves_existing_secret_file(self):
        initialize(self.data, profile_source=self.profile)
        env_file = self.data / ".env"
        env_file.write_text("OPENAI_API_KEY=local-private-marker\n")
        before = self.tree(self.data)
        with self.assertRaisesRegex(SetupError, "уже существует"):
            initialize(self.data, gender="female")
        self.assertEqual(self.tree(self.data), before)
        initialize(self.data, gender="female", overwrite=True)
        self.assertEqual(self.config().ai.candidate_gender, "female")
        self.assertFalse(self.config().schedule.enabled)
        self.assertEqual(env_file.read_text(), "OPENAI_API_KEY=local-private-marker\n")

    def test_doctor_complete_state_is_readonly_redacted_and_has_no_network_or_model(self):
        initialize(self.data, profile_source=self.profile, map_source=self.mapping, resume="selected",
                   browser_backend="seleniumbase_uc")
        (self.data / ".env").write_text("OPENAI_API_KEY=private-key-marker\n")
        (self.data / "data/resume.txt").write_text("Private resume text marker")
        (self.data / "data/resume.txt.id").write_text("selected")
        (self.data / "data/hh-session.json").write_text(json.dumps({"cookies": [{"name": "hhrole", "value": "applicant",
            "domain": ".hh.ru", "path": "/", "expires": -1}, {"name": "session", "value": "private-cookie-marker",
            "domain": ".hh.ru", "path": "/", "expires": -1}], "origins": []}))
        write_values(self.data / "config.server.toml", {"": {"search_url": "https://hh.ru/search/vacancy?resume=selected&text=private-query-marker"}})
        before, environment = self.tree(self.data), dict(os.environ)
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network")), \
                patch("hh_auto.ai.client.AiClient", side_effect=AssertionError("No model")), \
                patch("hh_auto.core.config.load_local_env", side_effect=AssertionError("No implicit env files")):
            report = doctor(self.data / "config.server.toml", self.data, environ={})
        self.assertTrue(report["ok"])
        self.assertEqual(report["browser_backend"], "seleniumbase_uc")
        self.assertEqual(report["request_timing"], SAFE_TIMING)
        backend_check = next(check for check in report["checks"] if check["item"] == "browser_backend")
        self.assertEqual(backend_check["status"], "ok")
        self.assertIn("Xvfb", backend_check["message"])
        self.assertEqual(self.tree(self.data), before)
        self.assertEqual(dict(os.environ), environment)
        output = json.dumps(report)
        for marker in ("private-key-marker", "private-cookie-marker", "private-query-marker", "Private resume text marker",
                       "Private profile fact marker", "Private case fact marker"):
            self.assertNotIn(marker, output)

    def test_doctor_reports_custom_timing_without_reading_runtime_state_or_mutating_files(self):
        initialize(self.data)
        custom = {
            "limits": {
                "submission_interval_seconds": 240, "submission_batch_size": 3,
                "submission_batch_pause_minutes": 40, "captcha_cooldown_minutes": 180,
                "read_interval_seconds": 12.5, "max_responses_per_day": 7,
            },
            "watch": {
                "robot_every_minutes": 20, "robot_max_chats_per_check": 5, "robot_reply_interval_seconds": 45,
            },
        }
        write_values(self.data / "config.server.toml", custom)
        runtime = self.data / "data/server-state.json"
        runtime.parent.mkdir(exist_ok=True)
        runtime.write_text("Not a valid runtime state; doctor must not read it")
        before, environment = self.tree(self.data), dict(os.environ)
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network")), \
                patch("hh_auto.ai.client.AiClient", side_effect=AssertionError("No model")), \
                patch("hh_auto.core.config.load_local_env", side_effect=AssertionError("No implicit env files")):
            report = doctor(self.data / "config.server.toml", self.data, environ={})
        self.assertEqual(report["request_timing"], custom)
        checks = {check["item"]: check for check in report["checks"]}
        self.assertEqual(checks["request_timing"]["status"], "ok")
        self.assertIn("12.5 с", checks["request_timing"]["message"])
        self.assertIn("server --status", checks["request_timing"]["message"])
        self.assertIn("20 мин", checks["robot_timing"]["message"])
        self.assertEqual(self.tree(self.data), before)
        self.assertEqual(dict(os.environ), environment)

    def test_doctor_preserves_disabled_limits_in_existing_config(self):
        initialize(self.data)
        zero = {"limits": dict.fromkeys(SAFE_TIMING["limits"], 0),
                "watch": {"robot_every_minutes": 5, "robot_max_chats_per_check": 0,
                          "robot_reply_interval_seconds": 0}}
        write_values(self.data / "config.server.toml", zero)
        report = doctor(self.data / "config.server.toml", self.data, environ={})
        self.assertEqual(report["request_timing"], zero)
        message = next(check["message"] for check in report["checks"] if check["item"] == "request_timing")
        self.assertIn("0 отключает", message)

    def test_invalid_timing_template_fails_before_copying_candidate_files(self):
        template = self.source / "invalid-limits.toml"
        text = (Path(__file__).resolve().parents[1] / "config.server.example.toml").read_text(encoding="utf-8")
        text = text.replace("submission_interval_seconds = 180", "submission_interval_seconds = -1")
        template.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "submission_interval_seconds"):
            initialize(self.data, profile_source=self.profile, map_source=self.mapping, template=template)
        self.assertFalse(self.data.exists())

    def test_doctor_on_absent_config_creates_no_files(self):
        report = doctor(self.data / "config.server.toml", self.data, environ={})
        self.assertFalse(report["ok"])
        self.assertFalse(self.data.exists())

    def test_new_init_doctor_identifies_expected_key_and_login_steps(self):
        initialize(self.data)
        report = doctor(self.data / "config.server.toml", self.data, environ={})
        self.assertFalse(report["ok"])
        errors = {check["item"] for check in report["checks"] if check["status"] == "error"}
        self.assertTrue({"api_key", "resume", "search", "session"}.issubset(errors))
        self.assertFalse(report["automatic_tasks_enabled"])
        self.assertEqual(report["browser_backend"], "playwright")

    def test_external_env_file_is_read_only_when_explicitly_selected(self):
        initialize(self.data, provider="ollama", model="local-model")
        explicit = self.source / "chosen.env"
        explicit.write_text("HH_AI_API_KEY=chosen-key-marker\n")
        report = doctor(self.data / "config.server.toml", self.data, env_file=explicit, environ={})
        key_check = next(check for check in report["checks"] if check["item"] == "api_key")
        self.assertEqual(key_check["status"], "ok")
        self.assertNotIn("chosen-key-marker", json.dumps(report))

    def test_load_env_false_does_not_invoke_loader_and_default_remains_compatible(self):
        initialize(self.data)
        with patch("hh_auto.core.config.load_local_env") as loader:
            self.config()
            loader.assert_not_called()
            load_config(self.data / "config.server.toml", check_search_url=False, upgrade=False)
            loader.assert_called_once()

    def test_empty_process_key_is_not_falsely_replaced_by_file_key(self):
        initialize(self.data)
        (self.data / ".env").write_text("OPENAI_API_KEY=valid-local-key\n")
        report = doctor(self.data / "config.server.toml", self.data, environ={"OPENAI_API_KEY": ""})
        key_check = next(check for check in report["checks"] if check["item"] == "api_key")
        self.assertEqual(key_check["status"], "error")

    def test_model_api_and_resume_validation_happen_before_copying(self):
        for kwargs in ({"provider": "custom", "model": "x"}, {"resume": "https://other.example/resume/id"},
                       {"base_url": "https://provider.example/v1?key=not-in-arguments"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SetupError):
                initialize(self.data, profile_source=self.profile, **kwargs)
            self.assertFalse(self.data.exists())


if __name__ == "__main__":
    unittest.main()
