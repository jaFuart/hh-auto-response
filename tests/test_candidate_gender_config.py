"""Candidate grammar is explicit, backward compatible, and independent of provider."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from hh_auto.core.config import Ai, Config, ConfigError, load_config, save_config, validate_config


class CandidateGenderConfig(unittest.TestCase):
    def test_default_and_supported_values(self):
        self.assertEqual(Ai().candidate_gender, "male")
        for value in ("male", "female"):
            cfg = Config(search_url="")
            cfg.ai.candidate_gender = value
            validate_config(cfg)

    def test_invalid_value_or_type_is_rejected_clearly(self):
        for value in ("", "Male", "neutral", None, True, 1, [], {}):
            with self.subTest(value=value):
                cfg = Config(search_url="")
                cfg.ai.candidate_gender = value
                with self.assertRaisesRegex(ConfigError, "candidate_gender.*male.*female"):
                    validate_config(cfg)

    def test_legacy_config_defaults_male_and_female_roundtrips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text('search_url = ""\n[ai]\nprovider = "openai"\n', encoding="utf-8")
            cfg = load_config(path, check_search_url=False, upgrade=False)
            self.assertEqual(cfg.ai.candidate_gender, "male")
            cfg.ai.candidate_gender = "female"
            save_config(cfg, path)
            self.assertEqual(load_config(path, check_search_url=False, upgrade=False).ai.candidate_gender, "female")

    def test_public_examples_declare_gender_explicitly(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("config.example.toml", "config.openai.example.toml", "config.server.example.toml"):
            with self.subTest(example=name):
                data = tomllib.loads((root / name).read_text(encoding="utf-8"))
                self.assertEqual(data["ai"]["candidate_gender"], "male")


if __name__ == "__main__":
    unittest.main()
