"""
Tests for the v1.21 network delegation manager.
"""
import threading
import time
import unittest

from delegation import (
    BackendEntry,
    BackendScope,
    DelegationManager,
    TaskTier,
    _tier_for_action_type,
)


class TestBackendScope(unittest.TestCase):
    def test_enum_values(self):
        self.assertEqual(BackendScope.LOCALHOST.value, "localhost")
        self.assertEqual(BackendScope.LAN.value, "lan")
        self.assertEqual(BackendScope.INTERNET.value, "internet")


class TestTaskTier(unittest.TestCase):
    def test_tier_mapping(self):
        self.assertEqual(_tier_for_action_type("record_observation"), TaskTier.LIGHT)
        self.assertEqual(_tier_for_action_type("probe_model"), TaskTier.LIGHT)
        self.assertEqual(_tier_for_action_type("speak"), TaskTier.STANDARD)
        self.assertEqual(_tier_for_action_type("ask_user"), TaskTier.STANDARD)
        self.assertEqual(_tier_for_action_type("multi_step_process"), TaskTier.HEAVY)
        self.assertEqual(_tier_for_action_type("search_api"), TaskTier.HEAVY)
        self.assertEqual(_tier_for_action_type("unknown_action"), TaskTier.SHARED)
        self.assertEqual(_tier_for_action_type(None), TaskTier.SHARED)


class TestBackendEntry(unittest.TestCase):
    def test_initial_state(self):
        entry = BackendEntry(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        self.assertTrue(entry.healthy)
        self.assertEqual(entry.latency_ms, 0.0)
        self.assertEqual(entry.consecutive_errors, 0)

    def test_healthy_after_success(self):
        entry = BackendEntry(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        entry.record_success(45.0)
        self.assertTrue(entry.healthy)
        self.assertEqual(entry.latency_ms, 45.0)
        self.assertEqual(entry.consecutive_errors, 0)

    def test_unhealthy_after_errors(self):
        entry = BackendEntry(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        for _ in range(3):
            entry.record_error()
        self.assertFalse(entry.healthy)
        self.assertEqual(entry.consecutive_errors, 3)

    def test_healthy_recovers_after_cooldown(self):
        entry = BackendEntry(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        for _ in range(3):
            entry.record_error()
        self.assertFalse(entry.healthy)
        entry.last_error_ts = time.time() - 60.0
        self.assertTrue(entry.healthy)

    def test_error_resets_on_success(self):
        entry = BackendEntry(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        entry.record_error()
        entry.record_error()
        entry.record_success(20.0)
        self.assertEqual(entry.consecutive_errors, 0)
        self.assertTrue(entry.healthy)

    def test_serves_tier_empty(self):
        entry = BackendEntry(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        self.assertTrue(entry.serves_tier(TaskTier.LIGHT))
        self.assertTrue(entry.serves_tier(TaskTier.STANDARD))

    def test_serves_tier_restricted(self):
        entry = BackendEntry(
            BackendScope.LAN, "http://192.168.1.50:11434",
            task_tiers=frozenset({TaskTier.STANDARD, TaskTier.HEAVY}),
        )
        self.assertFalse(entry.serves_tier(TaskTier.LIGHT))
        self.assertTrue(entry.serves_tier(TaskTier.STANDARD))
        self.assertTrue(entry.serves_tier(TaskTier.HEAVY))

    def test_to_dict(self):
        entry = BackendEntry(
            BackendScope.LAN, "http://192.168.1.50:11434",
            backend_type="ollama",
            task_tiers=frozenset({TaskTier.STANDARD}),
        )
        d = entry.to_dict()
        self.assertEqual(d["scope"], "lan")
        self.assertEqual(d["base_url"], "http://192.168.1.50:11434")
        self.assertEqual(d["backend_type"], "ollama")
        self.assertEqual(d["tiers"], ["standard"])
class TestDelegationManager(unittest.TestCase):
    def setUp(self):
        self.dm = DelegationManager()

    def test_register_and_status(self):
        self.dm.register(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        status = self.dm.status()
        self.assertEqual(len(status["backends"]), 1)
        self.assertEqual(status["backends"][0]["scope"], "localhost")

    def test_register_preserves_health(self):
        self.dm.register(BackendScope.LAN, "http://192.168.1.50:11434")
        self.dm.record_error(BackendScope.LAN, "http://192.168.1.50:11434")
        self.dm.record_error(BackendScope.LAN, "http://192.168.1.50:11434")
        self.dm.register(BackendScope.LAN, "http://192.168.1.50:11434")
        entry = self.dm._backends[0]
        self.assertEqual(entry.consecutive_errors, 2)

    def test_unregister(self):
        self.dm.register(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        self.assertTrue(self.dm.unregister(BackendScope.LOCALHOST,
                                           "http://127.0.0.1:11434"))
        self.assertEqual(len(self.dm.status()["backends"]), 0)

    def test_unregister_unknown(self):
        self.assertFalse(self.dm.unregister(BackendScope.LOCALHOST,
                                            "http://unknown:11434"))

    def test_select_backend_localhost(self):
        self.dm.register(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        policy = {"localhost": True, "lan": False, "internet": False}
        url, scope = self.dm.select_backend("speak", policy)
        self.assertEqual(url, "http://127.0.0.1:11434")
        self.assertEqual(scope, "localhost")

    def test_select_backend_lan_standard(self):
        self.dm.register(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        self.dm.register(
            BackendScope.LAN, "http://192.168.1.50:11434",
            task_tiers=frozenset({TaskTier.STANDARD, TaskTier.HEAVY}),
        )
        # STANDARD prefers localhost first; LAN is the fallback.
        policy = {"localhost": True, "lan": True, "internet": False}
        url, scope = self.dm.select_backend("speak", policy)
        self.assertEqual(scope, "localhost")

    def test_select_backend_lan_fallback_to_local(self):
        self.dm.register(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        self.dm.register(
            BackendScope.LAN, "http://192.168.1.50:11434",
            task_tiers=frozenset({TaskTier.STANDARD, TaskTier.HEAVY}),
        )
        policy = {"localhost": True, "lan": False, "internet": False}
        url, scope = self.dm.select_backend("speak", policy)
        self.assertEqual(scope, "localhost")

    def test_select_backend_light_never_lan(self):
        self.dm.register(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        self.dm.register(
            BackendScope.LAN, "http://192.168.1.50:11434",
            task_tiers=frozenset({TaskTier.STANDARD, TaskTier.HEAVY}),
        )
        policy = {"localhost": True, "lan": True, "internet": False}
        url, scope = self.dm.select_backend("record_observation", policy)
        self.assertEqual(scope, "localhost")

    def test_select_backend_heavy_skips_local(self):
        self.dm.register(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        self.dm.register(
            BackendScope.LAN, "http://192.168.1.50:11434",
            task_tiers=frozenset({TaskTier.STANDARD, TaskTier.HEAVY}),
        )
        policy = {"localhost": True, "lan": True, "internet": False}
        url, scope = self.dm.select_backend("multi_step_process", policy)
        self.assertEqual(scope, "lan")
    def test_select_backend_healthy_filter(self):
        self.dm.register(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        self.dm.register(
            BackendScope.LAN, "http://192.168.1.50:11434",
            task_tiers=frozenset({TaskTier.STANDARD, TaskTier.HEAVY}),
        )
        for _ in range(3):
            self.dm.record_error(BackendScope.LAN, "http://192.168.1.50:11434")
        policy = {"localhost": True, "lan": True, "internet": False}
        url, scope = self.dm.select_backend("speak", policy)
        self.assertEqual(scope, "localhost")

    def test_consent_gates_internet(self):
        self.dm.register(BackendScope.INTERNET, "https://api.openai.com/v1",
                         backend_type="openai")
        policy = {"localhost": True, "lan": True, "internet": True}
        url, scope = self.dm.select_backend("search_api", policy,
                                            consent_has_delegate_internet=False)
        self.assertIsNone(url)

    def test_consent_allows_internet(self):
        self.dm.register(BackendScope.INTERNET, "https://api.openai.com/v1",
                         backend_type="openai")
        policy = {"localhost": True, "lan": True, "internet": True}
        url, scope = self.dm.select_backend("search_api", policy,
                                            consent_has_delegate_internet=True)
        self.assertEqual(scope, "internet")

    def test_no_backends_returns_none(self):
        policy = {"localhost": True, "lan": True, "internet": True}
        url, scope = self.dm.select_backend("speak", policy)
        self.assertIsNone(url)
        self.assertIsNone(scope)

    def test_health_feedback_success(self):
        self.dm.register(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        for _ in range(3):
            self.dm.record_error(BackendScope.LOCALHOST,
                                 "http://127.0.0.1:11434")
        self.dm.record_success(BackendScope.LOCALHOST,
                               "http://127.0.0.1:11434", 30.0)
        entry = self.dm._backends[0]
        self.assertEqual(entry.consecutive_errors, 0)
        self.assertEqual(entry.latency_ms, 30.0)

    def test_thread_safety(self):
        self.dm.register(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        errors = []

        def worker():
            try:
                for _ in range(50):
                    self.dm.select_backend("speak", {"localhost": True,
                                                      "lan": False,
                                                      "internet": False})
                    self.dm.record_success(BackendScope.LOCALHOST,
                                           "http://127.0.0.1:11434", 10.0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_status_snapshot(self):
        self.dm.register(BackendScope.LOCALHOST, "http://127.0.0.1:11434")
        self.dm.register(BackendScope.LAN, "http://192.168.1.50:11434",
                         backend_type="ollama",
                         task_tiers=frozenset({TaskTier.STANDARD}))
        self.dm.select_backend("speak",
                               {"localhost": True, "lan": True, "internet": False})
        status = self.dm.status()
        self.assertEqual(len(status["backends"]), 2)
        self.assertIn(status["active_scope"], ("lan", "localhost"))
        self.assertEqual(status["backends"][0]["backend_type"], "ollama")