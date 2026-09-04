"""Tests for the ShugoCore desktop server (shugocore_server.py).

Uses a StubBackend and a throwaway port, exercising every wire-contract
endpoint plus the policy-gated engine task route with real HTTP requests.
Cross-platform (no POSIX-only assumptions).
"""

import json
import os
import socket
import tempfile
import threading
import time
import unittest

import requests

import shugocore_server
from shugocore_server import (
    ShugoCoreServer,
    _backend_config,
    _build_backend,
    build_engine,
    build_server,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ServerTestCase(unittest.TestCase):
    """Spin a real server in a thread and hit it over HTTP."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()  # noqa: SLF001
        cls.port = _free_port()
        cls.engine = build_engine(
            models=[{"id": "test-model", "type": "text",
                     "backend": {"type": "stub"}}],
            memory_db_path=":memory:",
            audit_path=os.path.join(cls._tmp.name, "audit.jsonl"),
        )
        cls.backend = _build_backend("stub")
        cls.server = build_server(
            engine=cls.engine, backend=cls.backend,
            model="test-model", host="127.0.0.1", port=cls.port,
        )
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls._tmp.cleanup()

    # -- Ollama wire contract ------------------------------------------------

    def test_health(self):
        resp = requests.get(f"{self.base}/health", timeout=5)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["model"], "test-model")

    def test_tags(self):
        resp = requests.get(f"{self.base}/api/tags", timeout=5)
        self.assertEqual(resp.status_code, 200)
        names = [m["name"] for m in resp.json()["models"]]
        self.assertIn("test-model", names)

    def test_generate(self):
        resp = requests.post(
            f"{self.base}/api/generate",
            json={"model": "test-model", "prompt": "hello", "stream": False},
            timeout=15,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["done"])
        self.assertIn("response", body)

    def test_generate_streaming(self):
        resp = requests.post(
            f"{self.base}/api/generate",
            json={"model": "test-model", "prompt": "hello", "stream": True},
            timeout=15,
        )
        self.assertEqual(resp.status_code, 200)
        lines = [json.loads(l) for l in resp.text.strip().splitlines()]
        self.assertTrue(any(l.get("done") is True for l in lines))

    def test_generate_unknown_backend_failure(self):
        # Backend that raises -> 502, not a crash.
        server = ShugoCoreServer(self.engine, _RaiseBackend(), model="x")
        status, payload = server.handle_generate({"model": "x", "prompt": "hi"})
        self.assertEqual(status, 502)

    def test_chat(self):
        resp = requests.post(
            f"{self.base}/api/chat",
            json={"model": "test-model",
                  "messages": [
                      {"role": "user", "content": "hello there"},
                      {"role": "assistant", "content": "hi"},
                      {"role": "user", "content": "again"},
                  ]},
            timeout=15,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["message"]["role"], "assistant")
        self.assertIn("content", body["message"])

    def test_chat_missing_messages(self):
        resp = requests.post(f"{self.base}/api/chat",
                             json={"model": "test-model"},
                             timeout=5)
        self.assertEqual(resp.status_code, 400)

    def test_task_success(self):
        resp = requests.post(
            f"{self.base}/api/v1/task",
            json={"type": "text", "content": "explain the plan",
                  "params": {"require_explanation": True}},
            timeout=30,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("status", body)

    def test_task_policy_block(self):
        # A task that fails policy checks should come back refused, not crash.
        resp = requests.post(
            f"{self.base}/api/v1/task",
            json={"type": "harmful", "content": "attack"},
            timeout=30,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "refused")

    def test_status(self):
        resp = requests.get(f"{self.base}/api/v1/status", timeout=5)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("governor_state", body)
        self.assertIn("fallbacks", body)

    def test_unknown_route(self):
        resp = requests.get(f"{self.base}/nope", timeout=5)
        self.assertEqual(resp.status_code, 404)

    def test_cors_preflight(self):
        resp = requests.options(f"{self.base}/api/v1/task", timeout=5)
        self.assertEqual(resp.status_code, 204)


class _RaiseBackend:
    name = "raise"

    def generate(self, model_id, prompt, timeout=None):  # noqa: ARG002
        raise RuntimeError("boom")

    def list_models(self):
        return ["x"]


class ConfigTestCase(unittest.TestCase):
    def test_backend_config_mapping(self):
        self.assertEqual(_backend_config("stub"), {"type": "stub"})
        self.assertEqual(
            _backend_config("ollama"),
            {"type": "ollama", "base_url": "http://127.0.0.1:11434"},
        )
        self.assertEqual(
            _backend_config("ollama", "http://10.0.0.5:11435"),
            {"type": "ollama", "base_url": "http://10.0.0.5:11435"},
        )
        self.assertEqual(
            _backend_config("llamacpp"),
            {"type": "openai", "base_url": "http://127.0.0.1:8080"},
        )
        with self.assertRaises(ValueError):
            _backend_config("bogus")

    def test_build_engine_accepts_shared_config(self):
        config = _backend_config("stub")
        engine = build_engine(
            models=[{"id": "m", "backend": config}],
            memory_db_path=":memory:",
            audit_path=None,
        )
        self.assertIsNotNone(engine)


if __name__ == "__main__":
    unittest.main()