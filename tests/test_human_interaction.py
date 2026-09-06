"""Human Interaction contract (v1.12 Interaction Foundation).

Covers the event schema, sanitization, bus boundedness + presence state
machine, the agent ingestion path, the status/observation surfaces, and —
critically — the architectural rule that the decision core NEVER imports
the interaction module (providers feed the core, not the other way round).
"""
import ast
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from human_interaction import (  # noqa: E402
    AgentResponse, InteractionBus, HumanObservation, OBSERVATION_TYPES,
    PRIVACY_SCOPES, RESPONSE_TYPES, USER_ABSENT, USER_LEFT_EVENT,
    USER_PRESENT, USER_PRESENT_EVENT, USER_RETURNED_EVENT)
from shugocore_agent import create_agent  # noqa: E402


_CLEAN_FILES = (
    "semantic_memory.db", "semantic_memory.db-shm", "semantic_memory.db-wal",
    "audit_chain.jsonl", "episodic_journal.jsonl", "agent_ledger.jsonl",
)


class _FakeClock:
    """Deterministic clock for debounce testing."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _obs(type_="interaction", source="ui_touch", payload=None,
         confidence=1.0, timestamp=None, privacy_scope="local"):
    return HumanObservation(type_, source, payload=payload,
                            confidence=confidence, timestamp=timestamp,
                            privacy_scope=privacy_scope)


class TestObservationSchema(unittest.TestCase):
    def test_valid_round_trip(self):
        ok, reason = _obs().validate()
        self.assertTrue(ok, reason)
        d = _obs(payload={"tab": "sensors", "count": 3}).to_dict()
        obs2, reason2 = HumanObservation.from_dict(d)
        self.assertIsNotNone(obs2, reason2)
        self.assertEqual(obs2.type, "interaction")
        self.assertEqual(obs2.payload["count"], 3)

    def test_unknown_type_rejected(self):
        ok, reason = _obs(type_="telepathy").validate()
        self.assertFalse(ok)
        self.assertIn("unknown observation type", reason)

    def test_confidence_bounds(self):
        self.assertFalse(_obs(confidence=1.5).validate()[0])
        self.assertFalse(_obs(confidence=-0.1).validate()[0])
        self.assertFalse(_obs(confidence="nan").validate()[0])
        self.assertTrue(_obs(confidence=0.0).validate()[0])

    def test_empty_source_rejected(self):
        self.assertFalse(_obs(source="   ").validate()[0])

    def test_privacy_scope(self):
        for scope in PRIVACY_SCOPES:
            self.assertTrue(_obs(privacy_scope=scope).validate()[0])
        self.assertFalse(_obs(privacy_scope="cloud").validate()[0])

    def test_payload_sanitization(self):
        obs = _obs(payload={"injected": "a\x00\x1fb", "big": "x" * 500})
        self.assertEqual(obs.payload["injected"], "a b")
        self.assertLessEqual(len(obs.payload["big"]), 160)

    def test_junk_payloads_wrapped_not_raising(self):
        self.assertEqual(_obs(payload=None).payload, {})
        self.assertEqual(_obs(payload=42).payload, {"value": 42})
        self.assertIn("text", _obs(payload="hello").payload)

    def test_payload_key_cap(self):
        payload = {f"key{i}": i for i in range(50)}
        self.assertLessEqual(len(_obs(payload=payload).payload), 12)

    def test_from_dict_never_raises(self):
        obs, reason = HumanObservation.from_dict("junk")
        self.assertIsNone(obs)
        self.assertEqual(reason, "not an object")
        obs, reason = HumanObservation.from_dict({"type": "speech"})
        self.assertIsNone(obs)
        self.assertEqual(reason, "empty source")

    def test_response_schema(self):
        ok, _ = AgentResponse("speech", "I'm here.").validate()
        self.assertTrue(ok)
        self.assertFalse(AgentResponse("telepathy", "x").validate()[0])
        self.assertFalse(AgentResponse("speech", "").validate()[0])


class TestInteractionBus(unittest.TestCase):
    def test_publish_and_snapshot(self):
        bus = InteractionBus()
        accepted, detail = bus.publish(_obs(timestamp=1000.0))
        self.assertTrue(accepted, detail)
        snap = bus.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["source"], "ui_touch")
        self.assertEqual(snap[0]["seq"], 1)

    def test_rejects_non_observations(self):
        bus = InteractionBus()
        self.assertFalse(bus.publish("junk")[0])
        self.assertFalse(bus.publish(None)[0])
        self.assertEqual(bus.stats()["rejected"], 2)

    def test_boundedness(self):
        bus = InteractionBus(capacity=64)
        for i in range(300):
            bus.publish(_obs(timestamp=1000.0 + i * 10))
        self.assertEqual(len(bus.snapshot()), 64)
        self.assertEqual(bus.stats()["observations"], 300)

    def test_presence_state_machine(self):
        clock = _FakeClock()
        bus = InteractionBus(clock=clock)
        self.assertEqual(bus.presence(), USER_ABSENT)
        _, first = bus.publish(_obs(type_="interaction", timestamp=clock()))
        self.assertEqual(first, USER_PRESENT_EVENT)
        self.assertEqual(bus.presence(), USER_PRESENT)
        clock.advance(5.0)
        _, left = bus.publish(_obs(type_="presence",
                                   payload={"present": False},
                                   timestamp=clock()))
        self.assertEqual(left, USER_LEFT_EVENT)
        self.assertEqual(bus.presence(), USER_ABSENT)
        clock.advance(5.0)
        _, back = bus.publish(_obs(timestamp=clock()))
        self.assertEqual(back, USER_RETURNED_EVENT)
        events = bus.stats()["presence_events"]
        self.assertEqual(events[USER_PRESENT_EVENT], 1)
        self.assertEqual(events[USER_LEFT_EVENT], 1)
        self.assertEqual(events[USER_RETURNED_EVENT], 1)

    def test_presence_debounce_fights_flapping(self):
        clock = _FakeClock()
        bus = InteractionBus(clock=clock)
        bus.publish(_obs(timestamp=clock()))            # USER_PRESENT
        clock.advance(0.5)                              # inside debounce
        _, detail = bus.publish(_obs(type_="presence",
                                     payload={"present": False},
                                     timestamp=clock()))
        self.assertEqual(detail, "ok")                  # no transition
        self.assertEqual(bus.presence(), USER_PRESENT)
        clock.advance(5.0)
        _, detail = bus.publish(_obs(type_="presence",
                                     payload={"present": False},
                                     timestamp=clock()))
        self.assertEqual(detail, USER_LEFT_EVENT)

    def test_human_context(self):
        clock = _FakeClock()
        bus = InteractionBus(clock=clock)
        ctx = bus.human_context()
        self.assertEqual(ctx["presence"], USER_ABSENT)
        self.assertIsNone(ctx["seconds_since_last_observation"])
        bus.publish(_obs(type_="speech", timestamp=clock()))
        ctx = bus.human_context()
        self.assertEqual(ctx["presence"], USER_PRESENT)
        self.assertTrue(ctx["speech_recent"])
        self.assertEqual(ctx["observations_recorded"], 1)

    def test_visual_person_present_drives_presence(self):
        clock = _FakeClock()
        bus = InteractionBus(clock=clock)
        _, detail = bus.publish(_obs(type_="visual", source="front_camera",
                                     payload={"person_present": True,
                                              "face_count": 1},
                                     timestamp=clock()))
        self.assertEqual(detail, USER_PRESENT_EVENT)
        self.assertTrue(bus.human_context()["person_present"])

    def test_visual_person_absent_drives_absence(self):
        clock = _FakeClock()
        bus = InteractionBus(clock=clock)
        bus.publish(_obs(type_="visual", payload={"person_present": True},
                         timestamp=clock()))
        self.assertEqual(bus.presence(), USER_PRESENT)
        clock.advance(5.0)
        _, detail = bus.publish(_obs(type_="visual", source="front_camera",
                                     payload={"person_present": False,
                                              "face_count": 0},
                                     timestamp=clock()))
        self.assertEqual(detail, USER_LEFT_EVENT)
        self.assertEqual(bus.presence(), USER_ABSENT)
        self.assertEqual(bus.human_context()["person_present"], False)

    def test_human_context_vision_fields(self):
        clock = _FakeClock()
        bus = InteractionBus(clock=clock)
        ctx = bus.human_context()
        self.assertIsNone(ctx["person_present"])
        self.assertFalse(ctx["vision_recent"])
        bus.publish(_obs(type_="visual", payload={"person_present": True},
                         timestamp=clock()))
        ctx = bus.human_context()
        self.assertTrue(ctx["vision_recent"])
        self.assertEqual(ctx["person_present"], True)
        self.assertEqual(bus.stats()["person_present"], True)

    def test_last_transcript_in_stats(self):
        clock = _FakeClock()
        bus = InteractionBus(clock=clock)
        self.assertIsNone(bus.stats()["last_transcript"])
        bus.publish(_obs(type_="speech", source="vad",
                         payload={"speech_detected": True},
                         timestamp=clock()))
        self.assertIsNone(bus.stats()["last_transcript"])  # no words yet
        bus.publish(_obs(type_="speech", source="on_device_stt",
                         payload={"transcript": "hello shugo",
                                  "stt": "on_device"},
                         timestamp=clock()))
        self.assertEqual(bus.stats()["last_transcript"], "hello shugo")

    def test_listener_notified_and_isolated(self):
        bus = InteractionBus()
        seen, broken = [], []

        def broken_listener(entry):
            broken.append(entry)
            raise ZeroDivisionError("broken on purpose")

        bus.add_listener(seen.append)
        bus.add_listener(broken_listener)
        accepted, _ = bus.publish(_obs(timestamp=1000.0))
        self.assertTrue(accepted)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(broken), 1)
        bus.remove_listener(lambda e: None)  # removing unknown is a no-op
        self.assertEqual(len(bus.snapshot()), 1)

    def test_thread_safety(self):
        bus = InteractionBus()
        errors = []

        def worker(offset):
            try:
                for i in range(50):
                    bus.publish(_obs(timestamp=1000.0 + offset * 1000 + i))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(bus.stats()["observations"], 400)


class TestAgentIngestion(unittest.TestCase):
    def setUp(self):
        self.agents = []

    def tearDown(self):
        for agent in self.agents:
            try:
                agent.cleanup()
            except Exception:
                pass
        for name in _CLEAN_FILES:
            try:
                os.remove(name)
            except FileNotFoundError:
                pass

    def _make(self):
        agent = create_agent(device_caps="Exynos-1380",
                             api_url="http://127.0.0.1:11434")
        self.agents.append(agent)
        return agent

    def test_publish_accepted_and_recorded(self):
        agent = self._make()
        result = agent.publish_human_observation(
            {"type": "interaction", "source": "ui_touch",
             "payload": {"tab": "agent"}, "confidence": 1.0})
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["presence_event"], USER_PRESENT_EVENT)
        events = [e["type"] for e in agent.memory.tier1.recent(10)]
        self.assertIn("human_observation", events)
        self.assertEqual(agent.interaction.stats()["observations"], 1)

    def test_publish_json_boundary(self):
        agent = self._make()
        raw = json.dumps({"type": "speech", "source": "mic",
                          "payload": {"transcript": "hello"}})
        result = json.loads(agent.publish_human_observation_json(raw))
        self.assertTrue(result["accepted"], result)
        self.assertTrue(agent.interaction.human_context()["speech_recent"])

    def test_publish_invalid_rejected_with_reason(self):
        agent = self._make()
        result = agent.publish_human_observation({"type": "telepathy"})
        self.assertFalse(result["accepted"])
        self.assertIn("unknown observation type", result["reason"])
        self.assertEqual(agent.interaction.stats()["observations"], 0)

    def test_publish_json_garbage_never_raises(self):
        agent = self._make()
        for junk in (None, "", "{not json", "[1, 2, 3]", 42):
            result = json.loads(agent.publish_human_observation_json(junk))
            self.assertFalse(result["accepted"])
        self.assertEqual(agent.interaction.stats()["rejected"], 5)

    def test_observation_and_status_surfaces(self):
        agent = self._make()
        agent.publish_human_observation({"type": "interaction",
                                         "source": "ui_touch"})
        observation = agent._get_observation()
        self.assertIn("human", observation)
        self.assertEqual(observation["human"]["presence"], USER_PRESENT)
        status = agent.get_status()
        self.assertIsNotNone(status["interaction"])
        self.assertEqual(status["interaction"]["observations"], 1)


class TestProviderRule(unittest.TestCase):
    """The non-negotiable rule: the decision core never imports the
    human-interaction module. Providers feed the core as data."""

    CORE_MODULES = ("decision_engine.py", "subconscious.py",
                    "execution_layer.py", "policy.py")

    def test_core_never_imports_human_interaction(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for name in self.CORE_MODULES:
            with open(os.path.join(root, name)) as fh:
                tree = ast.parse(fh.read(), filename=name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name == "human_interaction"
                           for alias in node.names):
                        offenders.append(f"{name}: import human_interaction")
                elif isinstance(node, ast.ImportFrom):
                    if node.module == "human_interaction":
                        offenders.append(f"{name}: from human_interaction "
                                         f"import ...")
        self.assertEqual(offenders, [], "the decision core must receive "
                         "human context as data, not via imports")

    def test_contract_types_are_complete(self):
        self.assertEqual(
            OBSERVATION_TYPES,
            ("visual", "speech", "presence", "gesture", "interaction"))
        self.assertEqual(
            RESPONSE_TYPES,
            ("speech", "visual", "action", "acknowledgement"))


if __name__ == "__main__":
    unittest.main()

