"""CSFA endurance verification — sustained model-driven functional agency.

The achievement being tested as a *property*, not an anecdote: over N
consecutive cycles the agent loop must

  1. complete every stage its outcome declares (the stage trail is fully
     stamped and non-decreasing in time),
  2. produce model-sourced decisions (zero rule fallbacks) from grammar-valid
     proposals,
  3. grow Tier 2 memory and keep the audit chain intact across the whole run,
  4. account for it all honestly in get_status() (cycles, by_outcome,
     by_source, by_action, success_rate, uptime, loop_stages, activity ring),
  5. and report the null dialect honestly when the model says "nothing to do"
     (documented 3-strike rule fallback, semantics unchanged).

Deterministic: a scripted backend emits grammar-valid proposals; no network,
no model, no Android runtime.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit import verify_audit_file  # noqa: E402
from model_backends import BaseBackend, register_backend  # noqa: E402
from shugocore_agent import PIPELINE_STAGES, create_agent  # noqa: E402


class ScriptedBackend(BaseBackend):
    """Deterministic grammar-valid proposals (the shape the decision grammar
    constrains the real model to). Constructible from a config dict so the
    engine's per-model backend entry ({"type": "scripted"}) works unchanged.
    """

    name = "scripted"

    def __init__(self, model_id="shugocore-local", **kwargs):
        # **kwargs: _backend_for() adds the URL kwarg (base_url/api_url) to
        # per-model backend configs; scripted proposals ignore networking.
        self.model_id = model_id
        self.calls = 0

    def list_models(self):
        return [self.model_id]

    def generate(self, model_id, prompt, timeout=None, grammar=None):
        n = self.calls
        self.calls += 1
        return json.dumps({
            "action_type": "record_observation",
            "params": {"text": f"endurance cycle {n} observed."},
            "confidence": 0.9,
            "text": f"cycle {n} recorded",
        })


register_backend("scripted", ScriptedBackend)


class NullScriptedBackend(BaseBackend):
    """Always answers "nothing to do" (the documented null dialect)."""

    name = "null_scripted"

    def __init__(self, model_id="shugocore-local", **kwargs):
        self.model_id = model_id

    def list_models(self):
        return [self.model_id]

    def generate(self, model_id, prompt, timeout=None, grammar=None):
        return json.dumps({"action_type": None, "params": {},
                           "confidence": 0.9, "text": "nothing to record"})


register_backend("null_scripted", NullScriptedBackend)


class _FakeSpeaker:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)
        return True


class _EnduranceBase(unittest.TestCase):
    N = 30
    MODEL_ID = "shugocore-local"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_csfa_")
        self.orig_cwd = os.getcwd()
        self.agent = create_agent(device_caps="Exynos-1380",
                                  api_url="http://127.0.0.1:11434",
                                  data_dir=self.tmp)
        self.agent._bootstrap()
        # Point the engine's per-model backend at the scripted one (no
        # network) and drop the backend cache so it is rebuilt from config.
        for model in self.agent.engine.models:
            if isinstance(model.get("backend"), dict):
                model["backend"] = {"type": "scripted",
                                    "model_id": self.MODEL_ID}
        cache = getattr(self.agent.engine, "_backend_cache", None)
        if isinstance(cache, dict):
            cache.clear()
        self.tier2_before = self.agent.get_status()["tier2_facts"]

    def tearDown(self):
        try:
            self.agent.cleanup()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.chdir(self.orig_cwd)
        for name in ("semantic_memory.db", "semantic_memory.db-shm",
                     "semantic_memory.db-wal", "audit_chain.jsonl",
                     "episodic_journal.jsonl"):
            try:
                os.remove(name)
            except FileNotFoundError:
                pass

    def _drive(self, cycles):
        for _ in range(cycles):
            self.agent.tick()


class TestSustainedModelDrivenLoop(_EnduranceBase):
    """N consecutive model-driven cycles, verified as a property."""

    def test_endurance(self):
        audit_path = os.path.join(self.tmp, "audit_chain.jsonl")
        self._drive(self.N)

        status = self.agent.get_status()
        loop = status["loop"]

        # (1) Every cycle accounted; every one model-sourced, zero fallbacks.
        self.assertEqual(loop["cycles"], self.N)
        self.assertEqual(loop["by_outcome"].get("SUCCESS"), self.N)
        self.assertEqual(loop["by_source"].get(self.MODEL_ID), self.N)
        self.assertEqual(loop["by_source"].get("rule_fallback", 0), 0)
        self.assertEqual(loop["success_rate"], 1.0)

        # (2) The stages the SUCCESS trail declares are stamped: OBSERVE, GATE,
        # DECIDE, EXECUTE, EVALUATE, RECORD — plus VERIFY_ATTENTION, whose
        # verdict is real loop work on this path (v1.20 attention layer).
        # CONSOLIDATE runs on the decoupled worker, which may or may not have
        # fired within N fast ticks — any honest state is acceptable there.
        stages = status["loop_stages"]
        for stage in ("OBSERVE", "VERIFY_ATTENTION", "GATE", "DECIDE",
                      "EXECUTE", "EVALUATE", "RECORD"):
            self.assertIn(stages[stage]["state"], ("ok", "stale"), stage)
            self.assertIsNotNone(stages[stage]["last_ts"], stage)
        self.assertIn(stages["CONSOLIDATE"]["state"],
                      ("ok", "stale", "unknown"))

        # (3) Tier 2 memory grew across the run.
        self.assertGreater(status["tier2_facts"], self.tier2_before)

        # (4) The audit chain survived the whole run intact.
        self.assertTrue(verify_audit_file(audit_path))

        # (5) The activity ring is bounded and ordered oldest -> newest.
        recent = loop["recent"]
        self.assertGreaterEqual(len(recent), 1)
        self.assertLessEqual(len(recent), 100)
        self.assertEqual([e["ts"] for e in recent],
                         sorted(e["ts"] for e in recent))

        # (6) Uptime is honest.
        self.assertGreater(status["uptime_seconds"], 0.0)

        # (7) The actions the model proposed are the ones accounted.
        self.assertGreaterEqual(
            loop["by_action"].get("record_observation", 0), self.N)


class TestNullDialect(_EnduranceBase):
    """The model says "nothing to do": reported honestly, semantics unchanged."""

    def test_null_proposals_reported_and_loop_keeps_running(self):
        for model in self.agent.engine.models:
            if isinstance(model.get("backend"), dict):
                model["backend"] = {"type": "null_scripted",
                                    "model_id": self.MODEL_ID}
        cache = getattr(self.agent.engine, "_backend_cache", None)
        if isinstance(cache, dict):
            cache.clear()
        self._drive(6)

        status = self.agent.get_status()
        loop = status["loop"]
        # A null-only model trips the engine's documented 3-strike rule, and
        # the safe fallback observation executes successfully every time: the
        # loop stayed productive and accounted throughout. (No Tier 2 growth
        # is the honest result: nothing was learned.)
        self.assertEqual(loop["cycles"], 6)
        self.assertGreater(loop["by_source"].get("rule_fallback", 0), 0)
        self.assertGreater(loop["by_outcome"].get("SUCCESS", 0), 0)


if __name__ == "__main__":
    unittest.main()



