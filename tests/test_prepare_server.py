"""Server preparation preserves the user's resume and job filters without changing the desktop config."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hh_auto.core.config import Config, ConfigError, load_config, save_config
from scripts.prepare_server import prepare, server_config


class PrepareServerTest(unittest.TestCase):
    def config(self):
        cfg = Config(search_url="https://hh.ru/search/vacancy?text=python", resume_url="https://hh.ru/resume/selected")
        cfg.filters.require_gph = True
        cfg.filters.gph_allow_unspecified = True
        cfg.filters.exclude_title_words = ["teacher"]
        cfg.ai.context = "Synthetic candidate context."
        cfg.ai.model = "gpt-6.1-terra"
        return cfg

    def test_server_copy_preserves_user_filters_resume_model_and_daily_limits(self):
        source = self.config()
        before = source.browser.headless
        cfg = server_config(source)
        self.assertFalse(before)
        self.assertFalse(source.browser.headless)
        self.assertFalse(source.schedule.enabled)
        self.assertTrue(cfg.browser.headless)
        self.assertEqual(cfg.browser.channel, "")
        self.assertEqual(cfg.browser.profile_dir, "browser-profile-server")
        self.assertEqual(cfg.geo.timezone, "Europe/Moscow")
        self.assertEqual(cfg.schedule.times, ["10:00"])
        self.assertEqual(cfg.schedule.days, list("1234567"))
        self.assertTrue(cfg.schedule.enabled)
        self.assertTrue(cfg.watch.robot_replies)
        self.assertEqual(cfg.watch.robot_every_minutes, 5)
        self.assertEqual(cfg.resume_id, source.resume_id)
        self.assertEqual(cfg.filters, source.filters)
        self.assertEqual(cfg.ai.context, source.ai.context)
        self.assertEqual(cfg.ai.model, source.ai.model)
        self.assertEqual(cfg.limits, source.limits)

    def test_missing_resume_cannot_create_unattended_server_config(self):
        cfg = self.config()
        cfg.resume_url = ""
        with self.assertRaisesRegex(ConfigError, "резюме"):
            server_config(cfg)

    def test_prepare_writes_separate_config_and_does_not_overwrite_existing_by_default(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "config.toml"
            output = Path(directory) / "config.server.toml"
            save_config(self.config(), source)
            original = source.read_bytes()
            prepare(source, output)
            self.assertEqual(source.read_bytes(), original)
            cfg = load_config(output, upgrade=False)
            self.assertTrue(cfg.browser.headless)
            self.assertEqual(cfg.filters.exclude_title_words, ["teacher"])
            with self.assertRaisesRegex(ConfigError, "существует"):
                prepare(source, output)
            with self.assertRaisesRegex(ConfigError, "отдельным"):
                prepare(source, source, overwrite=True)


if __name__ == "__main__":
    unittest.main()
