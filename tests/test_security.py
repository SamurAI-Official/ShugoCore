"""
Security & integration regression tests for ShugoCore.

Run:  python3 -m unittest discover -s tests -v
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit import AuditChain, verify_audit_file
from decision_engine import DecisionEngine
from execution_layer import ExecutionLayer
from model_backends import StubBackend, create_backend, validate_model_name
from policy import ApprovalBroker, CapabilityRegistry, ConsentRegistry
from security import (
    CircuitBreaker,
    RateLimiter,
    RedactionFilter,
    SecretResolver,
    canonical_hash,
    redact,
    sanitize_text,
    validate_url,
)

MODELS = [
    {"id": "m1", "type": "text", "weight": 0.5, "backend": {"type": "stub"}},
    {"id": "m2", "type": "text", "weight": 0.5, "backend": {"type": "stub"}},
]

# No per-model backend: the injected subconscious_backend is used instead.
MODELS_PLAIN = [
    {"id": "m1", "type": "text", "weight": 0.5},
    {"id": "m2", "type": "text", "weight": 0.5},
]


class _ProposingBackend(StubBackend):
    """Offline backend that proposes a concrete (allowlist-blocked) api_call."""

    def generate(self, model_id: str, prompt: str, timeout: float = None) -> str:
        return json.dumps({
            "action_type": "api_call",
            "params": {"url": "https://api.example.com/x"},
            "confidence": 0.9,
        })


class EngineGateTestCase(unittest.TestCase):
    """S1 regression: the autonomous path must not bypass the policy gate."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_engine_")
        self.engine = DecisionEngine(
            MODELS, {"type": "chroma"}, news_api_key=None,
            memory_db_path=os.path.join(self.tmp, "mem.db"),
            audit_path=os.path.join(self.tmp, "audit.jsonl"),
        )

    def tearDown(self):
        self.engine.task_manager.stop()
        self.engine.memory.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_autonomous_path_cannot_bypass_gate(self):
        result = self.engine.autonomy.execute_autonomous_task(
            {"type": "harmful", "content": "x"})
        self.assertEqual(result.get("status"), "refused")

    def test_side_effecting_task_requires_consent_grant(self):
        # A self-asserted consent flag is never trusted.
        result = self.engine.execute_task(
            {"type": "api_call", "content": "x", "consent": True})
        self.assertEqual(result.get("status"), "refused")
        self.assertIn("consent", str(result.get("reason", "")))

    def test_granted_consent_still_requires_approval(self):
        self.engine.consents.grant("api_call", granted_by="operator")
        # Use a backend that actually proposes an api_call so the decision
        # (not just the task) reaches the approval stage.
        engine2 = DecisionEngine(
            MODELS_PLAIN, {"type": "chroma"}, news_api_key=None,
            memory_db_path=os.path.join(self.tmp, "mem2.db"),
            audit_path=os.path.join(self.tmp, "audit2.jsonl"),
            subconscious_backend=_ProposingBackend(),
        )
        try:
            engine2.consents.grant("api_call", granted_by="operator")
            result = engine2.execute_task({"type": "api_call", "content": "x"})
            self.assertEqual(result.get("status"), "refused")
            self.assertIn("approval", str(result.get("reason", "")))
        finally:
            engine2.task_manager.stop()
            engine2.memory.shutdown()

    def test_full_gate_chain_reaches_executor_egress_controls(self):
        # Consent granted + operator approves -> decision reaches the executor,
        # where egress allowlisting still applies (defense in depth).
        engine2 = DecisionEngine(
            MODELS_PLAIN, {"type": "chroma"}, news_api_key=None,
            memory_db_path=os.path.join(self.tmp, "mem3.db"),
            audit_path=os.path.join(self.tmp, "audit3.jsonl"),
            subconscious_backend=_ProposingBackend(),
        )
        try:
            engine2.consents.grant("api_call", granted_by="operator")
            engine2.approvals.attach_operator(lambda request: True)
            result = engine2.execute_task({"type": "api_call", "content": "x"})
            # api.example.com is not on the default allowlist -> refused there.
            self.assertEqual(result.get("status"), "refused")
            self.assertIn("allowlist", str(result.get("reason", "")))
        finally:
            engine2.task_manager.stop()
            engine2.memory.shutdown()

    def test_internal_task_flows_but_never_fakes_success(self):
        result = self.engine.execute_task({"type": "text", "content": "hello"})
        # Stub models propose null actions -> honest error, never fake success.
        self.assertEqual(result.get("status"), "error")

    def test_policy_blocks_are_audited(self):
        self.engine.execute_task({"type": "harmful", "content": "x"})
        ok, errors, count = self.engine.audit.verify()
        self.assertTrue(ok, errors)
        self.assertGreaterEqual(count, 1)


class ExecutionLayerTestCase(unittest.TestCase):
    """S2/S5 regressions: verdict binding, SSRF defense, no fake success."""

    def setUp(self):
        self.layer = ExecutionLayer()

    @staticmethod
    def token(decision):
        return {"verdict": "allow", "decision_hash": canonical_hash(decision)}

    def test_missing_verdict_refused(self):
        result = self.layer.execute(
            {"action_type": "search_api", "params": {"query": "x"}})
        self.assertEqual(result["status"], "refused")

    def test_tampered_decision_refused(self):
        decision = {"action_type": "search_api", "params": {"query": "x"}}
        payload = dict(decision)
        payload["_policy"] = self.token(decision)
        payload["params"]["query"] = "tampered"
        self.assertEqual(self.layer.execute(payload)["status"], "refused")

    def test_ssrf_schemes_and_hosts_blocked(self):
        for url in ("http://api.duckduckgo.com/",          # cleartext
                    "https://169.254.169.254/latest",      # cloud metadata
                    "https://internal.example.com/x"):     # off-allowlist
            decision = {"action_type": "api_call", "params": {"url": url}}
            payload = dict(decision)
            payload["_policy"] = self.token(decision)
            result = self.layer.execute(payload)
            self.assertEqual(result["status"], "refused", msg=url)

    def test_database_update_never_fakes_success(self):
        decision = {"action_type": "database_update",
                    "params": {"statement": "SELECT 1"}}
        payload = dict(decision)
        payload["_policy"] = self.token(decision)
        result = self.layer.execute(payload)
        self.assertEqual(result["status"], "not_implemented")

    def test_disallowed_sql_refused(self):
        decision = {"action_type": "database_update",
                    "params": {"statement": "DROP TABLE users"}}
        payload = dict(decision)
        payload["_policy"] = self.token(decision)
        self.assertEqual(self.layer.execute(payload)["status"], "refused")

    def test_multi_step_refused_at_executor(self):
        decision = {"action_type": "multi_step_process", "params": {"steps": []}}
        payload = dict(decision)
        payload["_policy"] = self.token(decision)
        self.assertEqual(self.layer.execute(payload)["status"], "refused")


class GovernanceTestCase(unittest.TestCase):
    """S3/S7 regressions: consent registry, approval broker, Tier 3 ledger."""

    def test_consent_grant_and_expiry(self):
        registry = ConsentRegistry()
        self.assertFalse(registry.has_grant("api_call"))
        registry.grant("api_call", granted_by="operator", ttl_seconds=0.05)
        self.assertTrue(registry.has_grant("api_call"))
        time.sleep(0.08)
        self.assertFalse(registry.has_grant("api_call"))

    def test_consent_revoke(self):
        registry = ConsentRegistry()
        registry.grant("database_update", granted_by="operator")
        self.assertTrue(registry.has_grant("database_update"))
        self.assertEqual(registry.revoke("database_update"), 1)
        self.assertFalse(registry.has_grant("database_update"))

    def test_broker_fails_closed_without_operator(self):
        broker = ApprovalBroker()
        verdict = broker.request_approval({"action_type": "api_call"})
        self.assertFalse(verdict["approved"])

    def test_broker_operator_approval(self):
        broker = ApprovalBroker(ttl_seconds=2.0)
        broker.attach_operator(lambda request: True)
        verdict = broker.request_approval({"action_type": "api_call"})
        self.assertTrue(verdict["approved"])

    def test_broker_ttl_timeout_denies(self):
        broker = ApprovalBroker(ttl_seconds=0.1)

        def slow_operator(request):
            time.sleep(0.5)
            return True

        broker.attach_operator(slow_operator)
        verdict = broker.request_approval({"action_type": "api_call"})
        self.assertFalse(verdict["approved"])
        self.assertIn("timed out", verdict["reason"])

    def test_tier3_consent_flag_not_trusted(self):
        from memory_system import CoreIdentity
        core = CoreIdentity()  # no consent checker wired
        allowed, _ = core.check({"type": "api_call", "consent": True})
        self.assertFalse(allowed)
        core.set_consent_checker(lambda action_type: True)
        allowed, _ = core.check({"type": "api_call"})
        self.assertTrue(allowed)

    def test_tier3_promotion_requires_attribution_and_audits(self):
        tmp = tempfile.mkdtemp(prefix="shugocore_tier3_")
        try:
            from memory_system import CoreIdentity
            ledger_path = os.path.join(tmp, "tier3_ledger.jsonl")
            core = CoreIdentity(ledger_path=ledger_path)
            with self.assertRaises(ValueError):
                core.promote_invariant("rule", "no attribution")
            core.promote_invariant("rule", "operator-approved rule",
                                   authorized_by="operator")
            self.assertEqual(core.invariants()["rule"], "operator-approved rule")
            ok, errors, count = verify_audit_file(ledger_path)
            self.assertTrue(ok, errors)
            self.assertGreaterEqual(count, 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SecurityPrimitivesTestCase(unittest.TestCase):
    def test_redaction_masks_secret_keys_and_urls(self):
        data = redact({"apiKey": "sk-secret",
                       "nested": {"authorization": "Bearer x"},
                       "url": "https://host/v2?q=a&apiKey=sk-123"})
        self.assertEqual(data["apiKey"], "***REDACTED***")
        self.assertEqual(data["nested"]["authorization"], "***REDACTED***")
        self.assertNotIn("sk-123", data["url"])

    def test_redaction_filter_scrubs_records(self):
        import io
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactionFilter())
        log = logging.getLogger("redaction-test")
        log.handlers = [handler]
        log.propagate = False
        log.warning("calling https://newsapi.org/v2/everything?q=x&apiKey=abc123")
        self.assertNotIn("abc123", stream.getvalue())

    def test_sanitize_text_strips_control_characters(self):
        self.assertEqual(sanitize_text("bad\x00\x1b[31mtext"), "bad [31mtext")
        self.assertEqual(len(sanitize_text("a" * 5000, 100)), 100)

    def test_validate_url_rules(self):
        hosts = ["api.example.com", "*.good.org"]
        self.assertTrue(validate_url("https://api.example.com/x", hosts)[0])
        self.assertTrue(validate_url("https://sub.good.org/x", hosts)[0])
        self.assertFalse(validate_url("http://api.example.com/x", hosts)[0])
        self.assertFalse(validate_url("https://evil.com/x", hosts)[0])
        self.assertFalse(validate_url("https://user:pass@api.example.com/x", hosts)[0])

    def test_rate_limiter_blocks_burst_overflow(self):
        limiter = RateLimiter(calls_per_minute=0.6, burst=2)  # ~1 token / 100s
        self.assertTrue(limiter.acquire("k", timeout=0.0))
        self.assertTrue(limiter.acquire("k", timeout=0.0))
        self.assertFalse(limiter.acquire("k", timeout=0.05))

    def test_circuit_breaker_opens_after_failures(self):
        breaker = CircuitBreaker(failure_threshold=2, reset_timeout=60.0)
        self.assertTrue(breaker.allow())
        breaker.record_failure()
        self.assertTrue(breaker.allow())
        breaker.record_failure()
        self.assertFalse(breaker.allow())

    def test_secret_resolver_env_fallback(self):
        os.environ["SHUGOCORE_TEST_SECRET"] = "from-env"
        try:
            resolver = SecretResolver()
            self.assertEqual(resolver.get("test_secret"), "from-env")
            resolver.set("test_secret", "from-override")
            self.assertEqual(resolver.get("test_secret"), "from-override")
        finally:
            del os.environ["SHUGOCORE_TEST_SECRET"]

    def test_model_name_validation(self):
        self.assertTrue(validate_model_name("llama3:8b"))
        self.assertTrue(validate_model_name("gpt-4"))
        self.assertFalse(validate_model_name("-evil-flag"))
        self.assertFalse(validate_model_name("model; rm -rf /"))

    def test_stub_backend_emits_parseable_proposal(self):
        from decision_engine import DecisionEngine
        text = StubBackend().generate("stub", "prompt")
        proposal = DecisionEngine._parse_proposal(text)
        self.assertIsNotNone(proposal)
        self.assertIsNone(proposal["action_type"])

    def test_parse_proposal_rejects_unknown_action_types(self):
        from decision_engine import DecisionEngine
        self.assertIsNone(DecisionEngine._parse_proposal(
            '{"action_type": "launch_missiles", "params": {}, "confidence": 1.0}'))
        self.assertIsNone(DecisionEngine._parse_proposal("not json at all"))


class MemoryAndQueueTestCase(unittest.TestCase):
    def test_semantic_memory_sanitizes_and_chmods(self):
        tmp = tempfile.mkdtemp(prefix="shugocore_mem_")
        try:
            from memory_system import SemanticMemory
            db_path = os.path.join(tmp, "mem.db")
            memory = SemanticMemory(db_path=db_path)
            fact_id = memory.store_fact("secret\x00fact\nwith\nnewlines" * 100)
            fact = memory.get_fact(fact_id)
            self.assertNotIn("\x00", fact["content"])
            self.assertNotIn("\n", fact["content"])
            self.assertLessEqual(len(fact["content"]), 2000)
            mode = os.stat(db_path).st_mode & 0o777
            self.assertEqual(mode, 0o600)
            memory.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_audit_chain_detects_tampering(self):
        tmp = tempfile.mkdtemp(prefix="shugocore_audit_")
        try:
            path = os.path.join(tmp, "audit.jsonl")
            chain = AuditChain(path)
            chain.append("event", {"n": 1})
            chain.append("event", {"n": 2})
            chain.append("event", {"n": 3})
            ok, errors, count = verify_audit_file(path)
            self.assertTrue(ok, errors)
            self.assertEqual(count, 3)
            # Tamper with the second entry.
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
            entry = json.loads(lines[1])
            entry["payload"]["n"] = 999
            lines[1] = json.dumps(entry, sort_keys=True) + "\n"
            with open(path, "w", encoding="utf-8") as handle:
                handle.writelines(lines)
            ok, errors, _ = verify_audit_file(path)
            self.assertFalse(ok)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_task_manager_refuses_without_executor_and_is_bounded(self):
        from task_manager import TaskManager
        manager = TaskManager(max_queue_size=1, poll_timeout=0.05)
        try:
            result = manager.execute_task({"type": "test"})
            self.assertEqual(result["status"], "refused")

            release = threading.Event()
            gate_first = threading.Event()
            executed = []

            def executor(task):
                if not executed:
                    gate_first.set()
                    release.wait(timeout=5.0)
                executed.append(task)
                return {"status": "success", "task": task}

            manager.set_executor(executor)
            self.assertTrue(manager.add_task({"n": 1}))   # worker blocks inside executor
            self.assertTrue(gate_first.wait(timeout=2.0))
            self.assertTrue(manager.add_task({"n": 2}))   # fills the queue
            self.assertFalse(manager.add_task({"n": 3}))  # bounded: rejected
            release.set()
            for _ in range(100):
                if len(executed) == 2:
                    break
                time.sleep(0.05)
        finally:
            manager.stop()
        self.assertEqual(len(executed), 2)


if __name__ == "__main__":
    unittest.main()



