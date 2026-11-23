"""Native OpenAI requests and literal local secret loading. No live API calls."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from hh_auto.ai import client as ai
from hh_auto.ai.client import AiClient, AiError
from hh_auto.core import env
from hh_auto.core.config import Ai, load_config


class OpenAiProviderTest(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_defaults_and_explicit_model(self):
        client = AiClient(Ai(provider="openai", api_key="local-test-key"))
        self.assertEqual(client.base_url, "https://api.openai.com/v1")
        self.assertEqual(client.model, "gpt-6.1-terra")
        self.assertEqual(AiClient(Ai(provider="openai", api_key="x", model="chosen-model")).model, "chosen-model")

    def test_key_precedence_and_provider_isolation(self):
        os.environ["OPENAI_API_KEY"] = "openai-env-key"
        self.assertEqual(AiClient(Ai(provider="openai")).api_key, "openai-env-key")
        with self.assertRaises(AiError):
            AiClient(Ai(provider="openrouter"))
        os.environ["HH_AI_API_KEY"] = "shared-env-key"
        self.assertEqual(AiClient(Ai(provider="openai")).api_key, "shared-env-key")
        self.assertEqual(AiClient(Ai(provider="openai", api_key=" config-key \n")).api_key, "config-key")

    def test_missing_key_explains_local_env(self):
        with self.assertRaisesRegex(AiError, "OPENAI_API_KEY"):
            AiClient(Ai(provider="openai"))

    def test_request_parameters_and_json_mode(self):
        response = json.dumps({"choices": [{"message": {"content": '{"interesting": true}'}}]}).encode()
        client = AiClient(Ai(provider="openai", api_key="local-test-key"))
        with mock.patch.object(ai, "_read_interruptible", return_value=response) as read:
            client.complete([{"role": "user", "content": "Return JSON"}], max_tokens=350, json_mode=True)
        request = read.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer local-test-key")
        body = json.loads(request.data)
        self.assertEqual(body["max_completion_tokens"], 350)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["store"])
        self.assertNotIn("temperature", body)
        self.assertNotIn("max_tokens", body)

    def test_letter_receives_resume_and_vacancy_as_text(self):
        response = json.dumps({"choices": [{"message": {
            "content": "Здравствуйте! Разрабатываю приложения на Python. Буду рад обсудить задачи."}}]}).encode()
        client = AiClient(Ai(provider="openai", api_key="local-test-key"))
        with mock.patch.object(ai, "_read_interruptible", return_value=response) as read:
            text = client.write_letter("Разрабатываю приложения на Python", "Ищу ГПХ", "Разработчик",
                                       "Компания", "Разработка сервиса, оформление по ГПХ")
        body = json.loads(read.call_args.args[0].data)
        self.assertIn("Разрабатываю приложения на Python", body["messages"][1]["content"])
        self.assertIn("Разработка сервиса, оформление по ГПХ", body["messages"][1]["content"])
        self.assertNotIn("response_format", body)
        self.assertIn("Python", text)

    def test_terra_and_luna_disable_reasoning_for_short_completions(self):
        response = json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()
        for model in ("gpt-6.1-terra", "gpt-6-luna", "gpt-5.4-mini"):
            with self.subTest(model=model):
                client = AiClient(Ai(provider="openai", api_key="local-test-key", model=model))
                with mock.patch.object(ai, "_read_interruptible", return_value=response) as read:
                    client.complete([{"role": "user", "content": "Return JSON"}], max_tokens=30, json_mode=True)
                body = json.loads(read.call_args.args[0].data)
                self.assertEqual(body["model"], model)
                self.assertEqual(body["max_completion_tokens"], 30)
                self.assertEqual(body["response_format"], {"type": "json_object"})
                if model in ("gpt-6.1-terra", "gpt-6-luna"):
                    self.assertEqual(body["reasoning_effort"], "none")
                else:
                    self.assertNotIn("reasoning_effort", body)

    def test_key_is_redacted_from_http_errors(self):
        client = AiClient(Ai(provider="openai", api_key="local-test-key"))
        body = json.dumps({"error": {"message": "Incorrect API key: local-test-key"}}).encode()
        error = urllib.error.HTTPError(client.base_url, 401, "Unauthorized", {}, io.BytesIO(body))
        message = str(client._http_error(error))
        self.assertNotIn("local-test-key", message)
        self.assertIn("platform.openai.com", message)

    def test_quota_error_explains_balance(self):
        message = ai.explain_http_error(429, '{"error":{"message":"You exceeded your current quota"}}',
                                        "https://api.openai.com/v1", "openai", "gpt-5.4-mini")
        self.assertIn("баланс", message)
        self.assertNotIn("просит помедленнее", message)


class LocalEnvTest(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_local_precedence_process_environment_and_literal_values(self):
        data = self.root / "data"
        data.mkdir()
        (data / ".env").write_text('OPENAI_API_KEY="local-${OTHER}-$(ignored)"\n', encoding="utf-8-sig")
        (self.root / ".env").write_text("OPENAI_API_KEY=root-key\nOTHER=root-value\n", encoding="utf-8")
        with mock.patch.object(env, "PROJECT_DIR", self.root), mock.patch.object(env, "is_frozen", return_value=False):
            env.load_local_env(data / "config.toml")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "local-${OTHER}-$(ignored)")
            os.environ["OPENAI_API_KEY"] = "process-key"
            env.load_local_env(data / "config.toml")
        self.assertEqual(os.environ["OPENAI_API_KEY"], "process-key")

    def test_config_loads_dotenv_without_copying_secret_into_config(self):
        path = self.root / "config.toml"
        path.write_text('[ai]\nprovider="openai"\n', encoding="utf-8")
        (self.root / ".env").write_text("OPENAI_API_KEY=file-test-key\n", encoding="utf-8")
        with mock.patch.object(env, "is_frozen", return_value=True):
            cfg = load_config(path, check_search_url=False)
        self.assertEqual(cfg.ai.api_key, "")
        self.assertEqual(AiClient(cfg.ai).api_key, "file-test-key")
        self.assertNotIn("file-test-key", path.read_text(encoding="utf-8"))

    def test_packaged_app_does_not_load_source_root(self):
        (self.root / ".env").write_text("OPENAI_API_KEY=root-test-key\n", encoding="utf-8")
        with mock.patch.object(env, "PROJECT_DIR", self.root), mock.patch.object(env, "is_frozen", return_value=True):
            env.load_local_env(self.root / "elsewhere" / "config.toml")
        self.assertNotIn("OPENAI_API_KEY", os.environ)


if __name__ == "__main__":
    unittest.main()
