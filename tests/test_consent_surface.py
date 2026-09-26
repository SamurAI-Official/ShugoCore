"""Tests for the operator consent surface (GET/POST /api/v1/consent).

Why this matters: the decision engine gates side-effecting, robotics, mobile,
network and fleet actions behind ``ConsentRegistry``, and a grant may only come
from an operator channel -- the acting agent may never assert its own consent.
Until v1.30.5 no such channel existed on the wire, so a device could never be
granted network egress: its audit chain simply filled with "no external consent
grant for 'network_send'" while the transport was healthy.

These tests exercise the wire contract end to end against a real server (stub
backend, in-memory memory, throwaway audit chain) -- no mocked registry, because
the point is that the operator's grant really reaches the layer the engine
consults.
"""
import os
import socket
import tempfile
import threading
import time
import unittest

import requests

from shugocore_server import (
    ShugoCoreServer,
    _build_backend,
    build_engine,
    build_server,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ConsentSurfaceTestCase(unittest.TestCase):
    """Real server, real HTTP, real ConsentRegistry."""

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
            engine=cls.engine, backend=cls.backend, model="test-model",
            host="127.0.0.1", port=cls.port,
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def setUp(self):
        # Every test starts from "no grants in force".
        for action in ("network_send", "network_query", "fleet_deploy",
                       "api_call"):
            self.engine.consents.revoke(action)

    # -- routing and validation ------------------------------------------

    def test_route_names_resolve(self):
        self.assertEqual(ShugoCoreServer.route_name("GET", "/api/v1/consent"),
                         "consent")
        self.assertEqual(
            ShugoCoreServer.route_name("POST", "/api/v1/consent/network_send"),
            "consent_grant")
        self.assertEqual(
            ShugoCoreServer.route_name("POST",
                                       "/api/v1/consent/network_send/revoke"),
            "consent_revoke")
        # Slash-smuggling must never bucket as a consent route.
        self.assertIsNone(
            ShugoCoreServer.route_name("POST", "/evil/consent/network_send"))

    def test_listing_starts_empty_and_enabled(self):
        resp = requests.get(f"{self.base}/api/v1/consent", timeout=5)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["grants"], {})
        self.assertEqual(body["count"], 0)

    def test_unknown_action_type_is_refused(self):
        resp = requests.post(f"{self.base}/api/v1/consent/record_observation",
                             json={}, timeout=5)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not a consent-gated action", resp.json()["error"])
        self.assertFalse(self.engine.consents.has_grant("record_observation"))

    # -- grant / revoke round trip ---------------------------------------

    def test_grant_reaches_the_engine_and_revoke_blocks_it_again(self):
        action = "network_send"
        self.assertFalse(self.engine.consents.has_grant(action))

        granted = requests.post(
            f"{self.base}/api/v1/consent/{action}",
            json={"granted_by": "operator", "note": "fleet test"}, timeout=5)
        self.assertEqual(granted.status_code, 200)
        self.assertEqual(granted.json()["status"], "granted")
        self.assertEqual(granted.json()["active"], 1)

        # The layer the engine actually consults now allows the action --
        # including the attention-augmented check the Android path uses.
        self.assertTrue(self.engine.consents.has_grant(action))
        allowed, reason = self.engine.consents.has_grant_for_attended_action(
            action, "attending")
        self.assertTrue(allowed)
        self.assertIsNone(reason)

        listing = requests.get(f"{self.base}/api/v1/consent", timeout=5).json()
        self.assertIn(action, listing["actions"])
        entry = listing["grants"][action][0]
        self.assertEqual(entry["granted_by"], "operator")
        self.assertEqual(entry["note"], "fleet test")
        self.assertIsNone(entry["expires_at"])

        revoked = requests.post(
            f"{self.base}/api/v1/consent/{action}/revoke", json={}, timeout=5)
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json()["removed"], 1)
        self.assertFalse(self.engine.consents.has_grant(action))
        allowed, reason = self.engine.consents.has_grant_for_attended_action(
            action, "attending")
        self.assertFalse(allowed)
        self.assertIn("no external consent grant", reason)

    def test_grant_ttl_expires(self):
        resp = requests.post(f"{self.base}/api/v1/consent/api_call",
                             json={"ttl_seconds": 0.05}, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.engine.consents.has_grant("api_call"))
        time.sleep(0.15)
        self.assertFalse(self.engine.consents.has_grant("api_call"))

    def test_ttl_is_validated_and_capped(self):
        bad = requests.post(f"{self.base}/api/v1/consent/api_call",
                            json={"ttl_seconds": "soon"}, timeout=5)
        self.assertEqual(bad.status_code, 400)
        negative = requests.post(f"{self.base}/api/v1/consent/api_call",
                                 json={"ttl_seconds": -5}, timeout=5)
        self.assertEqual(negative.status_code, 400)
        capped = requests.post(f"{self.base}/api/v1/consent/api_call",
                               json={"ttl_seconds": 10 ** 9}, timeout=5)
        self.assertEqual(capped.status_code, 200)
        self.assertEqual(capped.json()["ttl_seconds"], 86400.0)

    def test_fleet_deploy_is_grantable(self):
        """The rollout capability's own consent gate must be satisfiable.

        Without this the agent could never execute a ``fleet_deploy`` decision:
        the engine requires a grant, and this surface is the only way an
        operator can issue one.
        """
        resp = requests.post(f"{self.base}/api/v1/consent/fleet_deploy",
                             json={"granted_by": "operator"}, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.engine.consents.has_grant("fleet_deploy"))

    def test_security_inventory_reports_the_engine_registry(self):
        """The SECURITY pane must see grants, not an always-empty registry.

        ``collect_agent_inventory`` used to read ``agent.consent_registry`` --
        an attribute the engine never sees -- so an operator's grants would have
        been invisible in the very pane meant to show them.
        """
        from security_inventory import collect_agent_inventory

        self.assertEqual(
            requests.post(f"{self.base}/api/v1/consent/network_send",
                          json={}, timeout=5).status_code, 200)
        inventory = collect_agent_inventory(self.engine)
        actions = (inventory.get("consent") or {}).get("actions", [])
        self.assertIn("network_send", actions)


class ConsentSurfaceAuthTestCase(unittest.TestCase):
    """A configured bearer token gates the consent surface like any other."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()  # noqa: SLF001
        cls.port = _free_port()
        cls.token = "test-operator-token"
        cls.engine = build_engine(
            models=[{"id": "test-model", "type": "text",
                     "backend": {"type": "stub"}}],
            memory_db_path=":memory:",
            audit_path=os.path.join(cls._tmp.name, "audit.jsonl"),
        )
        cls.server = build_server(
            engine=cls.engine, backend=_build_backend("stub"),
            model="test-model", host="127.0.0.1", port=cls.port,
            auth_token=cls.token,
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def setUp(self):
        # Test isolation: a grant issued by one test must not leak into the
        # next (the assertion below checks that a rejected request granted
        # nothing, which requires starting from no grants).
        for action in ("network_send", "api_call"):
            self.engine.consents.revoke(action)

    def test_tokenless_and_wrong_token_are_rejected(self):
        tokenless = requests.get(f"{self.base}/api/v1/consent", timeout=5)
        self.assertIn(tokenless.status_code, (401, 403))
        wrong = requests.post(
            f"{self.base}/api/v1/consent/network_send", json={},
            headers={"Authorization": "Bearer nope"}, timeout=5)
        self.assertIn(wrong.status_code, (401, 403))
        self.assertFalse(self.engine.consents.has_grant("network_send"))

    def test_token_opens_the_surface(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        self.assertEqual(
            requests.get(f"{self.base}/api/v1/consent",
                         headers=headers, timeout=5).status_code, 200)
        granted = requests.post(f"{self.base}/api/v1/consent/network_send",
                                json={}, headers=headers, timeout=5)
        self.assertEqual(granted.status_code, 200)
        self.assertTrue(self.engine.consents.has_grant("network_send"))


if __name__ == "__main__":
    unittest.main()
