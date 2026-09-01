"""
Model-execution stress tests, exercising the real requests HTTP path
against a fake loopback llama.cpp/Ollama/LM Studio server (no native deps).
"""

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from fake_llama_server import FakeLlamaServer, start_fake_server
from model_backends import (
    BackendError,
    OllamaBackend,
    OpenAICompatibleBackend,
    StubBackend,
    create_backend,
)
from android_node import detect_local_launcher, AndroidShugoCoreNode, NodeConfig
from policy import CapabilityRegistry
from tests.test_android import _FakeJBridge


class TestLauncherDetection(unittest.TestCase):
    def test_first_responder_wins_via_http(self):
        s = start_fake_server(mode="ok")
        try:
            candidates = [{"launcher": "llama.cpp", "base_url": s.base_url(),
                           "api": "openai", "probe_path": "/health"}]
            found = detect_local_launcher(candidates=candidates)
            self.assertIsNotNone(found)
            self.assertEqual(found["launcher"], "llama.cpp")
        finally:
            s.stop()

    def test_all_down_returns_none(self):
        s = start_fake_server(mode="status", status=503)
        try:
            candidates = [{"launcher": "llama.cpp", "base_url": s.base_url(),
                           "api": "openai", "probe_path": "/health"}]
            self.assertIsNone(detect_local_launcher(candidates=candidates))
        finally:
            s.stop()

    def test_ollama_responds_first(self):
        ollama = start_fake_server(model_name="ollama-model")
        llamacpp = start_fake_server(model_name="llama-model")
        try:
            candidates = [
                {"launcher": "ollama", "base_url": ollama.base_url(),
                 "api": "ollama", "probe_path": "/api/tags"},
                {"launcher": "llama.cpp", "base_url": llamacpp.base_url(),
                 "api": "openai", "probe_path": "/health"},
            ]
            found = detect_local_launcher(candidates=candidates)
            self.assertEqual(found["launcher"], "ollama")
        finally:
            ollama.stop()
            llamacpp.stop()

    def test_probe_timeout_moves_on(self):
        hanging = start_fake_server(mode="hang", hang_seconds=10)
        responding = start_fake_server(mode="ok")
        try:
            candidates = [
                {"launcher": "llama.cpp", "base_url": hanging.base_url(),
                 "api": "openai", "probe_path": "/health"},
                {"launcher": "llama.cpp", "base_url": responding.base_url(),
                 "api": "openai", "probe_path": "/health"},
            ]
            found = detect_local_launcher(candidates=candidates)
            self.assertIsNotNone(found)
            self.assertEqual(found["base_url"], responding.base_url())
        finally:
            hanging.stop()
            responding.stop()


class TestOllamaBackend(unittest.TestCase):

    def setUp(self):
        self.server = start_fake_server(mode="ok")

    def tearDown(self):
        self.server.stop()

    def test_generate_returns_response(self):
        backend = create_backend({"type": "ollama", "base_url": self.server.base_url()})
        result = backend.generate("fake-model", "hello")
        self.assertEqual(result, "fake-model-response")
        self.assertEqual(self.server.request_count(), 1)

    def test_list_models(self):
        backend = create_backend({"type": "ollama", "base_url": self.server.base_url()})
        models = backend.list_models()
        self.assertIn("fake-model", models)

    def test_timeout_raises(self):
        self.server.mode = "hang"
        self.server.hang_seconds = 0.1
        backend = create_backend({"type": "ollama", "base_url": self.server.base_url(), "timeout": 0.05})
        with self.assertRaises((BackendError, requests.exceptions.Timeout)):
            backend.generate("fake-model", "hello", timeout=0.05)
    def test_http_500_raises(self):
        self.server.mode = "status"
        self.server.status = 500
        backend = create_backend({"type": "ollama", "base_url": self.server.base_url()})
        with self.assertRaises((BackendError, requests.exceptions.HTTPError)):
            backend.generate("fake-model", "hello")

    def test_malformed_json_raises(self):
        self.server.mode = "malformed"
        backend = create_backend({"type": "ollama", "base_url": self.server.base_url()})
        with self.assertRaises((BackendError, ValueError, requests.exceptions.JSONDecodeError)):
            backend.generate("fake-model", "hello")

    def test_empty_choices(self):
        self.server.mode = "empty_choices"
        backend = create_backend({"type": "ollama", "base_url": self.server.base_url()})
        self.assertEqual(backend.generate("fake-model", "hello"), "")


class TestOpenAICompatibleBackend(unittest.TestCase):

    def setUp(self):
        self.server = start_fake_server(mode="ok")
        os.environ["SHUGOCORE_LOCAL_KEY"] = "local-test-key"

    def tearDown(self):
        self.server.stop()
        os.environ.pop("SHUGOCORE_LOCAL_KEY", None)

    def test_generate_returns_content(self):
        backend = create_backend({"type": "openai", "base_url": self.server.base_url(), "api_key_env": "SHUGOCORE_LOCAL_KEY"})
        result = backend.generate("fake-model", "hello")
        self.assertEqual(result, "fake-model-response")

    def test_missing_api_key_raises(self):
        os.environ.pop("SHUGOCORE_LOCAL_KEY", None)
        backend = create_backend({"type": "openai", "base_url": self.server.base_url(), "api_key_env": "SHUGOCORE_LOCAL_KEY"})
        with self.assertRaises(BackendError):
            backend.generate("fake-model", "hello")

    def test_empty_choices_returns_empty(self):
        self.server.mode = "empty_choices"
        backend = create_backend({"type": "openai", "base_url": self.server.base_url(), "api_key_env": "SHUGOCORE_LOCAL_KEY"})
        self.assertEqual(backend.generate("fake-model", "hello"), "")

    def test_concurrent_generation_16_threads(self):
        backend = create_backend({"type": "openai", "base_url": self.server.base_url(), "api_key_env": "SHUGOCORE_LOCAL_KEY"})
        errors = []

        def gen(i):
            try:
                resp = backend.generate("fake-model", f"prompt-{i}")
                return (i, resp)
            except Exception as exc:
                return (i, exc)

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(gen, i) for i in range(16)]
            for f in as_completed(futures):
                idx, val = f.result()
                if isinstance(val, Exception):
                    errors.append((idx, repr(val)))

        self.assertEqual(len(errors), 0, f"errors: {errors}")
        self.assertEqual(self.server.request_count(), 16)


class TestModelEndpointValidation(unittest.TestCase):

    def test_loopback_allowed(self):
        caps = CapabilityRegistry()
        ok, _ = caps.validate_model_endpoint("http://127.0.0.1:11434")
        self.assertTrue(ok)

    def test_non_loopback_rejected(self):
        caps = CapabilityRegistry()
        ok, reason = caps.validate_model_endpoint("http://evil.example.com:8080")
        self.assertFalse(ok)
        self.assertIn("loopback", reason)

    def test_disallowlisted_port_rejected(self):
        caps = CapabilityRegistry()
        ok, reason = caps.validate_model_endpoint("http://127.0.0.1:31337")
        self.assertFalse(ok)
        self.assertIn("port", reason)

    def test_credentials_in_url_rejected(self):
        caps = CapabilityRegistry()
        ok, reason = caps.validate_model_endpoint("http://user:pass@127.0.0.1:11434")
        self.assertFalse(ok)
        self.assertIn("credentials", reason)


class TestStubBackendParity(unittest.TestCase):
    def test_stub_generates_valid_proposal(self):
        import json
        backend = StubBackend()
        proposal = json.loads(backend.generate("stub-model", "test prompt"))
        self.assertIn("action_type", proposal)
        self.assertIn("params", proposal)
        self.assertIn("confidence", proposal)


class TestFullAgentBackendIntegration(unittest.TestCase):
    def test_full_agent_uses_live_backend(self):
        server = start_fake_server(mode="ok")
        try:
            caps = CapabilityRegistry({"local_model_ports": [server.port]})
            config = NodeConfig(
                device_id="testphone", role="full_agent",
                bridge=_FakeJBridge(), capabilities=caps,
                audit_path="/tmp/test_fa_audit.jsonl",
                db_path="/tmp/test_fa.db")
            fake_candidates = [{"launcher": "ollama", "base_url": server.base_url(),
                                "api": "ollama", "probe_path": "/api/tags"}]
            with mock.patch("android_node.detect_local_launcher",
                            lambda: detect_local_launcher(candidates=fake_candidates)):
                node = AndroidShugoCoreNode(config)
                node.start()
                result = node.run_autonomous_task(
                    {"type": "research", "description": "self check"})
                node.stop()
                try:
                    node.engine.shutdown()
                except Exception:
                    pass
            self.assertIsNotNone(node.engine)
            self.assertIn("status", result)
            # full_agent should have called the live backend (not stub).
            self.assertGreater(server.request_count(), 0)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
