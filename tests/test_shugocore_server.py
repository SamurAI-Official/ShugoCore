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
from unittest import mock

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

    def test_status_includes_activity_summary(self):
        # Exercise a work endpoint, then confirm the additive summary.
        requests.get(f"{self.base}/api/tags", timeout=5)
        resp = requests.get(f"{self.base}/api/v1/status", timeout=5)
        self.assertEqual(resp.status_code, 200)
        activity = resp.json().get("activity")
        self.assertIsInstance(activity, dict)
        self.assertGreaterEqual(activity["requests_total"], 2)  # tags + status
        self.assertGreaterEqual(activity["ok_total"], 2)
        self.assertIn("requests_per_minute", activity)

    def test_uptime_endpoint(self):
        resp = requests.get(f"{self.base}/api/v1/uptime", timeout=5)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertGreaterEqual(body["uptime_seconds"], 0.0)
        self.assertTrue(body["started_at"].endswith("+00:00"))

    def test_activity_counts_outcomes_per_endpoint(self):
        ok = requests.get(f"{self.base}/api/tags", timeout=5)
        bad = requests.post(f"{self.base}/api/chat", json={},
                            timeout=5)  # 400: messages required
        resp = requests.get(f"{self.base}/api/v1/activity", timeout=5)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(bad.status_code, 400)
        requests_stats = body["requests"]
        self.assertGreaterEqual(requests_stats["tags"]["ok"], 1)
        self.assertGreaterEqual(requests_stats["chat"]["client_error"], 1)
        self.assertIn("latency_ms", requests_stats["tags"])
        self.assertIn("requests_per_minute", requests_stats["tags"])
        # A bare DecisionEngine has no loop surface: no agent block, never fake.
        self.assertNotIn("agent", body)
        # Uptime carried on the activity snapshot too.
        self.assertGreaterEqual(body["uptime_seconds"], 0.0)

    def test_activity_agent_passthrough_when_hosted(self):
        """A hosted agent's Phase-A status surfaces verbatim under `agent`."""

        class _FakeAgent:
            def get_status(self):
                return {"loop": {"cycles": 7, "success_rate": 1.0},
                        "loop_stages": {"OBSERVE": {"state": "ok"}},
                        "mesh_activity": {"peers": [], "total_shared_facts": 0},
                        "uptime_seconds": 42.5,
                        "tick_count": 7}  # extra keys are not copied

        port = _free_port()
        server = build_server(engine=_FakeAgent(), backend=_build_backend("stub"),
                              model="test-model", host="127.0.0.1", port=port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            resp = requests.get(f"http://127.0.0.1:{port}/api/v1/activity",
                                timeout=5)
            self.assertEqual(resp.status_code, 200)
            agent = resp.json()["agent"]
            self.assertEqual(agent["loop"]["cycles"], 7)
            self.assertEqual(agent["loop_stages"]["OBSERVE"]["state"], "ok")
            self.assertEqual(agent["uptime_seconds"], 42.5)
            self.assertNotIn("tick_count", agent)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_agent_get_status_failure_is_omitted_not_fatal(self):
        class _BrokenAgent:
            def get_status(self):
                raise RuntimeError("boom")

        port = _free_port()
        server = build_server(engine=_BrokenAgent(), backend=_build_backend("stub"),
                              model="test-model", host="127.0.0.1", port=port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            resp = requests.get(f"http://127.0.0.1:{port}/api/v1/activity",
                                timeout=5)
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("agent", resp.json())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_new_routes_require_token_when_configured(self):
        port = _free_port()
        server = build_server(engine=self.engine, backend=self.backend,
                              model="test-model", host="127.0.0.1", port=port,
                              auth_token="sekrit")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            for path in ("/api/v1/activity", "/api/v1/uptime"):
                resp = requests.get(f"{base}{path}", timeout=5)
                self.assertEqual(resp.status_code, 401, path)
                resp = requests.get(
                    f"{base}{path}", headers={"Authorization": "Bearer sekrit"},
                    timeout=5)
                self.assertEqual(resp.status_code, 200, path)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_unknown_route(self):
        resp = requests.get(f"{self.base}/nope", timeout=5)
        self.assertEqual(resp.status_code, 404)

    def test_cors_preflight(self):
        resp = requests.options(f"{self.base}/api/v1/task", timeout=5)
        self.assertEqual(resp.status_code, 204)


class _RunningServer:
    """Spin one loopback server for the duration of a hardening test."""

    def __init__(self, **kwargs):
        self.port = _free_port()
        engine = build_engine(
            models=[{"id": "test-model", "type": "text",
                     "backend": {"type": "stub"}}],
            memory_db_path=":memory:", audit_path=None,
        )
        self.server = build_server(
            engine=engine, backend=_build_backend("stub"),
            model="test-model", host="127.0.0.1", port=self.port, **kwargs)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.15)
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class ServerHardeningTestCase(unittest.TestCase):
    """Auth, rate limiting, body-size and CORS hardening of the HTTP server."""

    def tearDown(self):
        if getattr(self, "_srv", None) is not None:
            self._srv.close()

    def _start(self, **kwargs):
        self._srv = _RunningServer(**kwargs)
        return self._srv

    # -- authentication ------------------------------------------------------

    def test_health_stays_open_with_token_configured(self):
        srv = self._start(auth_token="s3cret")
        resp = requests.get(f"{srv.base}/health", timeout=5)
        self.assertEqual(resp.status_code, 200)

    def test_other_routes_require_token(self):
        srv = self._start(auth_token="s3cret")
        self.assertEqual(
            requests.get(f"{srv.base}/api/v1/status", timeout=5).status_code, 401)
        self.assertEqual(
            requests.get(f"{srv.base}/api/v1/status", timeout=5,
                         headers={"Authorization": "Bearer wrong"}).status_code, 401)
        ok = requests.get(f"{srv.base}/api/v1/status", timeout=5,
                          headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(ok.status_code, 200)
        alt = requests.get(f"{srv.base}/api/v1/status", timeout=5,
                           headers={"X-ShugoCore-Token": "s3cret"})
        self.assertEqual(alt.status_code, 200)

    def test_token_exempts_nothing_else(self):
        srv = self._start(auth_token="s3cret")
        resp = requests.post(f"{srv.base}/api/v1/task",
                             json={"type": "user", "content": "x"}, timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_suffix_paths_do_not_hit_real_handlers(self):
        srv = self._start(auth_token="s3cret")
        # Auth runs before the existence check, so anonymous probes of
        # unknown/suffix-trick paths get 401 (no 404-vs-401 route oracle).
        for method, path in (("GET", "/evil/health"),
                             ("GET", "/api/v1/task/health"),
                             ("GET", "/evil/api/tags")):
            self.assertEqual(
                requests.request(method, f"{srv.base}{path}",
                                 timeout=5).status_code, 401, path)
        self.assertEqual(
            requests.post(f"{srv.base}/evil/approve", json={},
                          timeout=5).status_code, 401)
        # ...but with a valid token the same tricks 404: they never reach
        # a real handler (notably, /evil/health never serves health).
        auth = {"Authorization": "Bearer s3cret"}
        for method, path in (("GET", "/evil/health"),
                             ("GET", "/api/v1/task/health"),
                             ("GET", "/evil/api/tags")):
            self.assertEqual(
                requests.request(method, f"{srv.base}{path}", timeout=5,
                                 headers=auth).status_code, 404, path)
        self.assertEqual(
            requests.post(f"{srv.base}/evil/approve", json={}, timeout=5,
                          headers=auth).status_code, 404)

    def test_query_string_routes_like_bare_path(self):
        srv = self._start(auth_token="s3cret")
        # Liveness probes with a query string still serve /health openly...
        self.assertEqual(
            requests.get(f"{srv.base}/health?x=1", timeout=5).status_code, 200)
        # ...and authed task URLs tolerate a query string.
        ok = requests.post(f"{srv.base}/api/v1/task?x=1",
                           json={"content": "hi"}, timeout=10,
                           headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(ok.status_code, 200)
        # ...while suffix tricks with a query string still 404 when authed
        # (and 401 anonymously -- auth first, no route oracle).
        self.assertEqual(
            requests.get(f"{srv.base}/evil/health?x=1", timeout=5,
                         headers={"Authorization": "Bearer s3cret"}
                         ).status_code, 404)
        self.assertEqual(
            requests.get(f"{srv.base}/evil/health?x=1",
                         timeout=5).status_code, 401)

    def test_loopback_host_rejects_lookalike_dns(self):
        from shugocore_server import _is_loopback_host
        for good in ("127.0.0.1", "127.0.0.2", "localhost", "::1"):
            self.assertTrue(_is_loopback_host(good), good)
        for bad in ("127.evil.com", "127.0.0.1.nip.io",
                    "localhost.evil.com", "0.0.0.0", "192.168.1.5", ""):
            self.assertFalse(_is_loopback_host(bad), bad)

    def test_cors_rejects_lookalike_loopback_dns(self):
        srv = self._start()
        resp = requests.options(f"{srv.base}/api/v1/task", timeout=5,
                                headers={"Origin": "http://127.evil.com/"})
        self.assertEqual(resp.status_code, 204)
        self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))

    # -- rate limiting -------------------------------------------------------

    def test_rate_limit_returns_429(self):
        srv = self._start(rate_limit_per_minute=1, rate_limit_burst=1)
        first = requests.get(f"{srv.base}/api/v1/status", timeout=5)
        self.assertEqual(first.status_code, 200)
        second = requests.get(f"{srv.base}/api/v1/status", timeout=5)
        self.assertEqual(second.status_code, 429)

    def test_health_not_rate_limited(self):
        srv = self._start(rate_limit_per_minute=1, rate_limit_burst=1)
        for _ in range(3):
            self.assertEqual(
                requests.get(f"{srv.base}/health", timeout=5).status_code, 200)

    # -- body guards ---------------------------------------------------------

    def test_negative_content_length_rejected_without_hanging(self):
        srv = self._start()
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as sock:
            sock.sendall(b"POST /api/v1/task HTTP/1.1\r\nHost: x\r\n"
                         b"Content-Length: -1\r\nConnection: close\r\n\r\n")
            sock.settimeout(5)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        self.assertIn(b"400", data.split(b"\r\n", 1)[0])
        srv.close()
        self._srv = None

    # -- CORS ----------------------------------------------------------------

    def test_cors_preflight_loopback_origin_echoed(self):
        srv = self._start()
        resp = requests.options(f"{srv.base}/api/v1/task", timeout=5,
                                headers={"Origin": "http://127.0.0.1:5555"})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"),
                         "http://127.0.0.1:5555")

    def test_cors_preflight_remote_origin_not_allowed(self):
        srv = self._start()
        resp = requests.options(f"{srv.base}/api/v1/task", timeout=5,
                                headers={"Origin": "http://evil.example.com"})
        self.assertEqual(resp.status_code, 204)
        self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))

    # -- fail-closed bind ----------------------------------------------------

    def test_main_refuses_nonloopback_without_token(self):
        with mock.patch.dict(os.environ, {"SHUGOCORE_SERVER_TOKEN": ""}):
            rc = shugocore_server.main(
                ["--host", "0.0.0.0", "--port", "0", "--backend", "stub"])
        self.assertEqual(rc, 2)


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


class NewSurfacesTestCase(unittest.TestCase):
    """C1/C2/C3 surfaces: approval queue, fleet snapshot, sensor stream."""

    def test_bare_engine_surfaces_report_disabled(self):
        engine = build_engine(
            models=[{"id": "m", "backend": {"type": "stub"}}],
            memory_db_path=":memory:",
            audit_path=None,
        )
        server = ShugoCoreServer(engine, _build_backend("stub"), model="m")
        status, body = server.handle_approvals()
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])  # engine owns an ApprovalBroker
        self.assertEqual(body["count"], 0)
        status, body = server.handle_fleet()  # no mobile registry
        self.assertEqual(status, 200)
        self.assertFalse(body["enabled"])
        status, body = server.handle_sensors()  # no hosted agent
        self.assertEqual(status, 200)
        self.assertFalse(body["enabled"])

    def test_approvals_enabled_with_pending_and_resolution(self):
        engine = build_engine(
            models=[{"id": "m", "backend": {"type": "stub"}}],
            memory_db_path=":memory:",
            audit_path=None,
        )
        broker = engine.approvals
        # A blocking operator channel: the worker asks the human, and the
        # request stays PENDING until we resolve it programmatically.
        ask_event = threading.Event()
        gate = threading.Event()

        def blocking_operator(req):
            ask_event.set()
            gate.wait(10.0)
            return False

        broker.attach_operator(blocking_operator)
        results: dict = {}
        worker = threading.Thread(
            target=lambda: results.update(
                broker.request_approval(
                    {"action_type": "database_update",
                     "params": {"table": "x"}},
                    ttl_seconds=8.0)),
            daemon=True)
        worker.start()
        self.assertTrue(ask_event.wait(2.0), "operator asked")
        pending = broker.list_pending()
        self.assertEqual(len(pending), 1)
        rid = pending[0]["request_id"]

        server = ShugoCoreServer(engine, _build_backend("stub"), model="m")
        status, body = server.handle_approvals()
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["approvals"][0]["request_id"], rid)
        self.assertIn("action_type", body["approvals"][0]["description"])
        status, body = server.resolve_approval(rid, approved=True)
        self.assertEqual(status, 200)
        self.assertTrue(body["resolved"])
        status, body = server.resolve_approval(rid, approved=True)
        self.assertEqual(status, 200)
        self.assertFalse(body["resolved"])  # already resolved -> no-op
        gate.set()
        worker.join(timeout=5)
        # The operator's LATE verdict (False) must NOT overwrite the
        # programmatic approval — first resolution wins (CSFA race fix).
        self.assertTrue(results.get("approved"))

    def test_approvals_resolution_without_broker_is_503(self):
        class _NoBrokerEngine:
            approvals = None
            get_status = None
        server = ShugoCoreServer(_NoBrokerEngine(), _build_backend("stub"),
                                 model="m")
        status, body = server.handle_approvals()
        self.assertEqual(status, 200)
        self.assertFalse(body["enabled"])
        status, body = server.resolve_approval("x", approved=True)
        self.assertEqual(status, 503)

    def test_approvals_skips_malformed_entries(self):
        class _BadBroker:
            ttl_seconds = 60.0

            def list_pending(self):
                return [
                    {"request_id": "good",
                     "description": {"action_type": "database_update"},
                     "requested_at": 1234.5},
                    {"request_id": "bad",
                     "description": {"action_type": "x"},
                     "requested_at": "not-a-float"},  # must not 500
                    "not-a-dict",  # skipped silently
                ]

        class _Engine:
            approvals = _BadBroker()

        server = ShugoCoreServer(_Engine(), _build_backend("stub"), model="m")
        status, body = server.handle_approvals()
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["approvals"][0]["request_id"], "good")

    def test_safe_payload_caps_nested_and_redacts(self):
        from shugocore_server import _safe_payload
        big = {"blob": "x" * 5_000_000, "api_key": "sk-live-123",
               "deep": {"a": {"b": {"c": {"d": {"e": {"f": "too-deep"}}}}}}}
        safe = _safe_payload({"status": "ok", "result": big,
                              "reason": "r" * 9000})
        import json
        self.assertLess(len(json.dumps(safe["result"])), 10000)
        self.assertEqual(safe["result"].get("api_key"), "***REDACTED***")
        self.assertLessEqual(len(safe["reason"]), 2000)

    def test_handle_task_deep_caps_params(self):
        seen = {}

        class _Engine:
            def execute_task(self, task):
                seen.update(task)
                return {"status": "ok", "result": "done"}

        server = ShugoCoreServer(_Engine(), _build_backend("stub"), model="m")
        status, body = server.handle_task(
            {"content": "hi",
             "params": {"nested": {"blob": "y" * 5_000_000}}})
        self.assertEqual(status, 200)
        import json
        self.assertLess(len(json.dumps(seen["params"])), 10000)

    def test_fleet_pair_route_operator_pairing(self):
        from mobile_nodes import MobileComputeBroker, MobileExecutionHandler, \
            MobileNodeManager, MobileNodeRegistry
        from policy import CapabilityRegistry
        from ros2_interface import StubROS2Interface
        engine = build_engine(
            models=[{"id": "m", "backend": {"type": "stub"}}],
            memory_db_path=":memory:",
            audit_path=None,
        )
        registry = MobileNodeRegistry()
        caps = CapabilityRegistry()
        ros2 = StubROS2Interface(rate_limit_hz=500.0)
        manager = MobileNodeManager(ros2, registry, caps)
        broker = MobileComputeBroker(ros2, registry, caps)
        engine.mobile_handler = MobileExecutionHandler(manager, broker)
        server = ShugoCoreServer(engine, _build_backend("stub"), model="m")
        # Missing/invalid device_id -> 400, nothing paired.
        status, body = server.handle_fleet_pair({})
        self.assertEqual(status, 400)
        status, body = server.handle_fleet_pair(
            {"device_id": "android-A51", "manifest": {"model": "SM-S515DL"}})
        self.assertEqual(status, 200)
        self.assertTrue(body["paired"])
        # Pairing keeps the topic-ACL allowlist in sync (consent == allowlist).
        self.assertIn("android-A51", caps.mobile_devices_allowlist)
        status, fleet = server.handle_fleet()
        self.assertEqual(status, 200)
        self.assertTrue(fleet["enabled"])
        self.assertEqual([n["device_id"] for n in fleet["nodes"]],
                         ["android-A51"])
        # Unknown action -> 400.
        status, body = server.handle_fleet_pair(
            {"device_id": "android-A51", "action": "teleport"})
        self.assertEqual(status, 400)
        # Unpair removes the node.
        status, body = server.handle_fleet_pair(
            {"device_id": "android-A51", "action": "unpair"})
        self.assertEqual(status, 200)
        self.assertTrue(body["removed"])
        # Unpair revokes consent: the allowlist entry goes too.
        self.assertNotIn("android-A51", caps.mobile_devices_allowlist)
        status, fleet = server.handle_fleet()
        self.assertEqual(fleet["nodes"], [])

    def test_fleet_pair_disabled_fleet_is_503(self):
        engine = build_engine(
            models=[{"id": "m", "backend": {"type": "stub"}}],
            memory_db_path=":memory:",
            audit_path=None,
        )
        server = ShugoCoreServer(engine, _build_backend("stub"), model="m")
        status, body = server.handle_fleet_pair({"device_id": "android-A51"})
        self.assertEqual(status, 503)
        self.assertIn("not enabled", body.get("error", ""))

    def test_fleet_aggregates_paired_nodes(self):
        from audit import AuditChain
        from mobile_nodes import MobileComputeBroker, MobileExecutionHandler, \
            MobileNodeManager, MobileNodeRegistry
        from policy import CapabilityRegistry
        from ros2_interface import StubROS2Interface
        engine = build_engine(
            models=[{"id": "m", "backend": {"type": "stub"}}],
            memory_db_path=":memory:",
            audit_path=None,
        )
        tmpd = tempfile.mkdtemp(prefix="shugocore_fleet_test_")
        try:
            audit = AuditChain(os.path.join(tmpd, "fleet_audit.jsonl"))
            registry = MobileNodeRegistry(audit=audit, heartbeat_timeout=10.0)
            registry.pair("pixel8", {"sensors": ["camera", "mic"]})
            registry.pair("tab", {"sensors": ["gps"]})
            caps = CapabilityRegistry({"mobile_devices_allowlist":
                                       ["pixel8", "tab"]})
            ros2 = StubROS2Interface(rate_limit_hz=500.0)
            manager = MobileNodeManager(ros2, registry, caps,
                                        fallbacks=None, audit=audit)
            broker = MobileComputeBroker(ros2, registry, caps, audit=audit)
            engine.mobile_handler = MobileExecutionHandler(manager, broker)
            server = ShugoCoreServer(engine, _build_backend("stub"), model="m")
            status, body = server.handle_fleet()
            self.assertEqual(status, 200)
            self.assertTrue(body["enabled"])
            ids = [n["device_id"] for n in body["nodes"]]
            self.assertEqual(sorted(ids), ["pixel8", "tab"])
            node = next(n for n in body["nodes"]
                        if n["device_id"] == "pixel8")
            self.assertIn("camera", node["manifest"]["sensors"])
        finally:
            import shutil as _shutil
            _shutil.rmtree(tmpd, ignore_errors=True)

    def test_sensors_stream_is_bounded_and_sanitized(self):
        class _HostedAgent:
            telemetry = {"thermal_c": 37.5, "power_w": 1.2,
                         "battery_pct": 91}

            def get_status(self):
                return {"tick_count": 3, "memory_usage_mb": 128.0,
                        "telemetry_received": True,
                        "mesh_peer_count": 1,
                        "mesh_peers": [{"device_id": "peer-a"}],
                        "capabilities": {"camera": "ok"}}

        server = ShugoCoreServer(_HostedAgent(), _build_backend("stub"),
                                 model="m")
        first = server.handle_sensors()
        second = server.handle_sensors()
        self.assertEqual(first[0], 200)
        body = first[1]
        self.assertTrue(body["enabled"])
        self.assertEqual(body["capacity"], 100)
        self.assertEqual(body["stream"][0]["telemetry"]["thermal_c"], "37.5")
        self.assertEqual(body["stream"][0]["mesh_peer_count"], 1)
        self.assertEqual(second[1]["count"], 2)  # two polls -> two samples

class TestListenBacklog(unittest.TestCase):
    """The accept queue must suit a fleet, not a demo (v1.30.5).

    macOS RESETS a SYN that arrives while the accept queue is full (Linux drops
    it and the client's retry succeeds), so socketserver's default of 5 loses
    concurrent clients before the handler — or even the rate limiter — runs.
    Phase 0 measured 8-9 of 16 simultaneous loopback connects being reset at the
    default and none at 128.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.port = _free_port()
        cls.engine = build_engine(
            models=[{"id": "test-model", "type": "text",
                     "backend": {"type": "stub"}}],
            memory_db_path=":memory:",
            audit_path=os.path.join(cls._tmp.name, "audit.jsonl"),
        )
        cls.server = build_server(
            engine=cls.engine, backend=_build_backend("stub"),
            model="test-model", host="127.0.0.1", port=cls.port,
        )
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls._tmp.cleanup()

    def test_backlog_is_raised(self):
        self.assertGreaterEqual(self.server.request_queue_size, 64)
        self.assertTrue(self.server.daemon_threads)

    def test_sixteen_simultaneous_clients_all_connect(self):
        barrier = threading.Barrier(16)
        statuses = []
        errors = []
        lock = threading.Lock()

        def one():
            barrier.wait()
            try:
                resp = requests.get(f"http://127.0.0.1:{self.port}/health",
                                    timeout=10)
                with lock:
                    statuses.append(resp.status_code)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                with lock:
                    errors.append(type(exc).__name__)

        workers = [threading.Thread(target=one) for _ in range(16)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        self.assertEqual(errors, [], f"connection errors: {errors}")
        self.assertEqual(len(statuses), 16)
        self.assertEqual(set(statuses), {200})




if __name__ == "__main__":
    unittest.main()