#!/usr/bin/env python3
"""--model re-points the engine at a model the host backend actually serves.

Regression cover for the silent-fallback class of bug: the engine bootstraps
with the on-device placeholder id ``shugocore-local``, which a host backend
(Ollama / an OpenAI-compatible server) does not know. It answers 404, and the
only symptom was a ``rule_fallback`` decision every cycle — the node looked
alive while the model was never consulted.
"""
import importlib.util
import os
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_launcher():
    path = os.path.join(_ROOT, "scripts", "desktop_agent.py")
    spec = importlib.util.spec_from_file_location("desktop_agent_under_test",
                                                  path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_da = _load_launcher()


class _Engine:
    def __init__(self):
        self.models = [{"id": "shugocore-local", "type": "text", "weight": 1.0,
                        "backend": {"type": "android",
                                    "model_name": "shugocore-local"}}]
        self._backend_cache = {"stale": object()}


class _Agent:
    def __init__(self):
        self.engine = _Engine()


class ApplyModelTestCase(unittest.TestCase):

    def test_sets_registry_id_and_backend_model_name(self):
        agent = _Agent()
        self.assertEqual(_da._apply_model(agent, "qwen2.5:0.5b"),
                         "qwen2.5:0.5b")
        self.assertEqual(agent.engine.models[0]["id"], "qwen2.5:0.5b")
        # The backend client carries its own name; a stale one would still put
        # the placeholder on the wire after the registry id changed.
        self.assertEqual(agent.engine.models[0]["backend"]["model_name"],
                         "qwen2.5:0.5b")

    def test_clears_the_backend_cache(self):
        agent = _Agent()
        _da._apply_model(agent, "ornith:9b")
        self.assertEqual(agent.engine._backend_cache, {})

    def test_without_the_flag_the_default_is_left_alone(self):
        agent = _Agent()
        self.assertEqual(_da._apply_model(agent, None), "shugocore-local")
        self.assertEqual(agent.engine.models[0]["id"], "shugocore-local")

    def test_blank_model_is_not_an_override(self):
        agent = _Agent()
        self.assertEqual(_da._apply_model(agent, "   "), "shugocore-local")
        self.assertEqual(agent.engine.models[0]["id"], "shugocore-local")

    def test_missing_engine_never_raises(self):
        agent = types.SimpleNamespace(engine=None)
        self.assertEqual(_da._apply_model(agent, "qwen2.5:0.5b"),
                         "qwen2.5:0.5b")

    def test_malformed_registry_never_raises(self):
        agent = types.SimpleNamespace(
            engine=types.SimpleNamespace(models="not-a-list",
                                         _backend_cache=None))
        self.assertEqual(_da._apply_model(agent, "qwen2.5:0.5b"),
                         "qwen2.5:0.5b")

    def test_flag_is_parsed(self):
        args = _da.parse_args(["--model", "ornith:9b"])
        self.assertEqual(args.model, "ornith:9b")

    def test_flag_defaults_to_none(self):
        self.assertIsNone(_da.parse_args([]).model)


class _FakeRuntime:
    """A Shugonet stand-in that records syncs and can be told to misbehave."""

    def __init__(self, results=None, raisers=None):
        self.results = results or {}
        self.raisers = raisers or {}
        self.calls = []

    def sync(self, peer_id):
        self.calls.append(peer_id)
        if peer_id in self.raisers:
            raise self.raisers[peer_id]
        return self.results.get(peer_id, {"status": "error",
                                          "message": "unreachable"})


class SyncOnceTestCase(unittest.TestCase):
    """A pull that finds nothing new is success; one bad peer is not fatal."""

    def test_counts_success_and_imported(self):
        rt = _FakeRuntime({
            "a": {"status": "success", "imported": 3, "duplicates": 10},
            "b": {"status": "success", "imported": 0, "duplicates": 20},
        })
        tally = _da._sync_once(rt, ["a", "b"], "periodic")
        self.assertEqual(tally, {"ok": 2, "imported": 3, "failed": 0})
        self.assertEqual(rt.calls, ["a", "b"])

    def test_zero_imports_is_success_not_failure(self):
        """A converged fleet pulls 200 duplicates and that is a healthy sync."""
        rt = _FakeRuntime({"a": {"status": "success", "imported": 0,
                                 "duplicates": 200}})
        tally = _da._sync_once(rt, ["a"], "periodic")
        self.assertEqual(tally["failed"], 0)
        self.assertEqual(tally["ok"], 1)

    def test_one_bad_peer_does_not_abort_the_rest(self):
        rt = _FakeRuntime(
            results={"b": {"status": "success", "imported": 1}},
            raisers={"a": TimeoutError("timed out")})
        tally = _da._sync_once(rt, ["a", "b"], "periodic")
        self.assertEqual(tally["failed"], 1)
        self.assertEqual(tally["ok"], 1)
        self.assertEqual(tally["imported"], 1)
        # The healthy peer must still have been attempted.
        self.assertEqual(rt.calls, ["a", "b"])

    def test_error_status_is_a_failure(self):
        rt = _FakeRuntime({"a": {"status": "error", "message": "unreachable"}})
        self.assertEqual(_da._sync_once(rt, ["a"], "periodic")["failed"], 1)

    def test_non_numeric_imported_does_not_raise(self):
        rt = _FakeRuntime({"a": {"status": "success", "imported": None}})
        self.assertEqual(_da._sync_once(rt, ["a"], "periodic")["imported"], 0)

    def test_no_targets_is_a_noop(self):
        rt = _FakeRuntime()
        self.assertEqual(_da._sync_once(rt, [], "periodic"),
                         {"ok": 0, "imported": 0, "failed": 0})
        self.assertEqual(rt.calls, [])


class SyncIntervalTestCase(unittest.TestCase):
    """--sync-interval must be opt-in and default to the old one-shot."""

    def test_defaults_to_off(self):
        self.assertEqual(_da.parse_args([]).sync_interval, 0.0)

    def test_parses_a_positive_interval(self):
        self.assertEqual(
            _da.parse_args(["--sync-interval", "300"]).sync_interval, 300.0)

    def test_interval_alone_selects_no_peers(self):
        """An interval with no --sync must not sync an arbitrary fleet."""
        args = _da.parse_args(["--sync-interval", "60"])
        self.assertEqual(args.sync, [])
        self.assertGreater(args.sync_interval, 0)


if __name__ == "__main__":
    unittest.main()
