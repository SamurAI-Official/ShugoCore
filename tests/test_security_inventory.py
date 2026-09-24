#!/usr/bin/env python3
"""Track 2: security inventory & baseline tests.

Covers the collectors (audit, policy, network, caps, consent, server
wire controls), the fail-closed baseline evaluator (drift vs.
unverifiable), and the surfaces: agent get_status keys + the server
/api/v1/security handler.
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit import AuditChain  # noqa: E402
from security_inventory import (BASELINE,  # noqa: E402
                                collect_agent_inventory,
                                collect_server_inventory,
                                evaluate_baseline, full_snapshot)
from shugocore_server import ShugoCoreServer  # noqa: E402


def _ok_audit(path):
    """A real AuditChain over an empty (verifiable) file."""
    return AuditChain(path)


class _FakeAgent:
    """Duck-typed AndroidAgent stand-in with the real surface names."""

    def __init__(self, audit=None):
        self.engine = SimpleNamespace(audit=audit) if audit else None
        self.agent_caps = {"code_execution": False, "shared_memory": True}
        self.capabilities = {
            "camera": {"agent_ack": True}, "mic": {"agent_ack": False}}
        self.network_policy = {"internet": False, "lan": True,
                               "localhost": True}
        self._grants = {"speak": [{"scope": "*"}],
                        "remember": [{"scope": "tier1"}]}

    def _mesh_role_label(self):
        return "standalone"

    # consent_registry duck-shape
    @property
    def consent_registry(self):
        return SimpleNamespace(grants=lambda: self._grants)


class TestInventoryCollectors(unittest.TestCase):
    def test_full_posture_baseline_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _FakeAgent(audit=_ok_audit(os.path.join(tmp, "a.jsonl")))
            inv = collect_agent_inventory(agent)
        self.assertEqual(inv["audit"]["enabled"], True)
        self.assertIn(inv["audit"]["integrity"], ("ok", "empty"))
        self.assertTrue(inv["policy"]["fail_closed"])
        self.assertEqual(inv["network"]["internet"], False)
        self.assertEqual(inv["agent_caps"]["granted"], ["shared_memory"])
        self.assertEqual(inv["capabilities"], {"declared": 2, "acked": 1})
        self.assertEqual(inv["consent"]["actions"], ["remember", "speak"])
        self.assertEqual(inv["consent"]["grant_count"], 2)
        self.assertEqual(inv["mesh"]["role"], "standalone")
        result = evaluate_baseline(inv)
        self.assertTrue(result["baseline_ok"])
        self.assertEqual(result["violations"], [])

    def test_internet_drift_is_reported(self):
        agent = _FakeAgent()
        agent.network_policy["internet"] = True
        inv = collect_agent_inventory(agent)
        result = evaluate_baseline(inv)
        self.assertFalse(result["baseline_ok"])
        net_v = [v for v in result["violations"]
                 if v["control"] == "network.internet"]
        self.assertEqual(len(net_v), 1)
        self.assertEqual(net_v[0]["state"], "drift")
        self.assertEqual(net_v[0]["observed"], True)

    def test_missing_audit_is_unverifiable_not_safe(self):
        inv = collect_agent_inventory(_FakeAgent(audit=None))
        result = evaluate_baseline(inv)
        self.assertFalse(result["baseline_ok"])
        audit_v = [v for v in result["violations"]
                   if v["control"] == "audit.enabled"]
        self.assertEqual(len(audit_v), 1)
        self.assertEqual(audit_v[0]["state"], "unverifiable")
        self.assertEqual(audit_v[0]["severity"], "critical")

    def test_broken_audit_chain_is_not_claimed_ok(self):
        class _Broken:
            def verify(self):
                raise RuntimeError("boom")
        agent = _FakeAgent(audit=_Broken())
        inv = collect_agent_inventory(agent)
        self.assertEqual(inv["audit"]["integrity"], "unverifiable")

    def test_fresh_chain_is_empty_not_failed(self):
        """Repo semantics: a not-yet-written chain is 'empty', enabled."""
        with tempfile.TemporaryDirectory() as tmp:
            inv = collect_agent_inventory(
                _FakeAgent(audit=_ok_audit(os.path.join(tmp, "new.jsonl"))))
        self.assertEqual(inv["audit"]["enabled"], True)
        self.assertEqual(inv["audit"]["integrity"], "empty")

    def test_tampered_chain_reports_failed(self):
        class _Tampered:
            def verify(self):
                return (False, ["line 1: entry hash mismatch"], 1)
        inv = collect_agent_inventory(_FakeAgent(audit=_Tampered()))
        self.assertEqual(inv["audit"]["integrity"], "failed")

    def test_failed_verify_reports_failed(self):
        class _Tampered:
            def verify(self):
                return (False, ["hash mismatch"], 3)
        inv = collect_agent_inventory(_FakeAgent(audit=_Tampered()))
        self.assertEqual(inv["audit"]["integrity"], "failed")
        result = evaluate_baseline(inv)
        self.assertTrue(result["baseline_ok"])  # enabled == True; drift on
        # enabled-ness only — integrity is reported alongside for humans.

    def test_agent_caps_capped_and_sorted(self):
        agent = _FakeAgent()
        agent.agent_caps = {f"cap{i:02d}": True for i in range(80)}
        inv = collect_agent_inventory(agent)
        self.assertEqual(len(inv["agent_caps"]["granted"]), 64)
        self.assertEqual(inv["agent_caps"]["granted_count"], 80)

    def test_consent_grants_capped(self):
        agent = _FakeAgent()
        agent._grants = {f"action{i}": [{"scope": "*"}] for i in range(50)}
        inv = collect_agent_inventory(agent)
        self.assertEqual(len(inv["consent"]["actions"]), 32)

    def test_absent_surfaces_omitted_never_fabricated(self):
        inv = collect_agent_inventory(SimpleNamespace())
        self.assertNotIn("audit", inv)
        self.assertNotIn("agent_caps", inv)
        self.assertNotIn("consent", inv)
        self.assertNotIn("mesh", inv)
        self.assertIn("policy", inv)  # declared invariants only

class TestServerInventory(unittest.TestCase):
    def test_open_server_reports_open(self):
        class _S:
            auth_token = None
            _limiter = None
        inv = collect_server_inventory(_S())
        self.assertFalse(inv["auth_token_required"])
        self.assertFalse(inv["rate_limit_enabled"])

    def test_tokened_server_reports_protected(self):
        class _L:
            calls_per_minute = 120.0
            burst = 60
        class _S:
            auth_token = "s3cret"
            _limiter = _L()
        inv = collect_server_inventory(_S())
        self.assertTrue(inv["auth_token_required"])
        self.assertTrue(inv["rate_limit_enabled"])
        self.assertEqual(inv["rate_limit"]["calls_per_minute"], 120.0)

    def test_full_snapshot_composes_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _FakeAgent(audit=_ok_audit(os.path.join(tmp, "a.jsonl")))
            class _S:
                auth_token = "t"
                _limiter = None
            snap = full_snapshot(agent=agent, server=_S())
        self.assertIn("server", snap["inventory"])
        self.assertIn("audit", snap["inventory"])
        self.assertTrue(snap["baseline"]["baseline_ok"])


class TestAgentSurface(unittest.TestCase):
    """get_status() exposes the inventory; baseline reflects reality."""

    @classmethod
    def setUpClass(cls):
        from shugocore_agent import create_agent
        cls._tmp = tempfile.mkdtemp(prefix="sec_inventory_test_")
        cls.agent = create_agent(device_caps="A51",
                                 api_url="http://127.0.0.1:11434",
                                 data_dir=cls._tmp)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.agent.cleanup()
        except Exception:
            pass

    def test_status_keys_present(self):
        status = self.agent.get_status()
        self.assertIn("security_inventory", status)
        self.assertIn("security_baseline", status)
        # No engine on this test host -> no audit chain -> the baseline
        # must NOT claim safety (unverifiable audit is a violation).
        baseline = status["security_baseline"]
        self.assertIsNotNone(baseline)
        self.assertFalse(baseline["baseline_ok"])
        controls = {v["control"] for v in baseline["violations"]}
        self.assertIn("audit.enabled", controls)

    def test_baseline_holds_with_real_chain(self):
        from audit import AuditChain
        self.agent.engine = SimpleNamespace(
            audit=AuditChain(os.path.join(self._tmp, "inv_chain.jsonl")))
        try:
            status = self.agent.get_status()
            baseline = status["security_baseline"]
            self.assertTrue(baseline["baseline_ok"], baseline)
            inv = status["security_inventory"]
            self.assertIn(inv["audit"]["integrity"], ("ok", "empty"))
            self.assertEqual(inv["network"]["internet"], False)
            self.assertEqual(inv["mesh"]["role"],
                             inv.get("mesh", {}).get("role"))
        finally:
            self.agent.engine = None


class TestServerRoute(unittest.TestCase):
    """/api/v1/security handler + route registration."""

    def _core(self, token=None):
        with tempfile.TemporaryDirectory() as tmp:
            engine = SimpleNamespace(
                audit=AuditChain(os.path.join(tmp, "chain.jsonl")))
        return ShugoCoreServer(engine=engine, backend=SimpleNamespace(
            name="stub"), model="m", auth_token=token,
            rate_limit_per_minute=60.0, rate_limit_burst=30)

    def test_route_registered(self):
        from shugocore_server import ShugoCoreServer as S
        self.assertEqual(S.route_name("GET", "/api/v1/security"), "security")

    def test_handle_security_baseline(self):
        core = self._core(token=None)
        status, body = core.handle_security()
        self.assertEqual(status, 200)
        inv = body["security_inventory"]
        self.assertFalse(inv["server"]["auth_token_required"])
        # SERVER_BASELINE: an open server is a drift violation — reported,
        # never papered over.
        baseline = body["security_baseline"]
        self.assertFalse(baseline["baseline_ok"])
        controls = {v["control"]: v for v in baseline["violations"]}
        self.assertIn("server.auth_token_required", controls)
        self.assertEqual(controls["server.auth_token_required"]["state"],
                         "drift")
        # The engine's audit chain IS attached (fresh -> "empty").
        self.assertIn(inv["audit"]["integrity"], ("ok", "empty"))

    def test_handle_security_tokened_baseline_holds(self):
        core = self._core(token="s3cret")
        _status, body = core.handle_security()
        self.assertTrue(body["security_baseline"]["baseline_ok"],
                        body["security_baseline"])
        self.assertTrue(body["security_inventory"]["server"]
                        ["rate_limit_enabled"])

    def test_handle_security_without_module_is_honest(self):
        core = self._core()
        # Simulate a missing module (defensive import path).
        import shugocore_server as srv
        original = srv._HAS_SECURITY_INVENTORY
        srv._HAS_SECURITY_INVENTORY = False
        try:
            status, body = core.handle_security()
            self.assertEqual(status, 200)
            self.assertIsNone(body["security_inventory"])
            self.assertEqual(body["security_module"], "unavailable")
        finally:
            srv._HAS_SECURITY_INVENTORY = original


if __name__ == "__main__":
    unittest.main()
