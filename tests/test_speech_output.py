"""v1.15 speech output: the internal `speak` action, end to end.

Covers: policy classification (speech output is INTERNAL — talking to the
local operator is not egress, so no consent/approval, but still sanitized
and journaled), execution dispatch (registered speech provider honored,
honest not_implemented without one, tampered verdicts refused), the
AgentResponse half of the interaction contract (validated, bounded, truth
in stats), and the agent's speak_test path through the real gate.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution_layer import ExecutionLayer  # noqa: E402
from human_interaction import AgentResponse, InteractionBus  # noqa: E402
from policy import (  # noqa: E402
    KNOWN_ACTION_TYPES,
    SIDE_EFFECTING_ACTION_TYPES,
    SPEECH_OUTPUT_ACTION_TYPES,
)
from security import canonical_hash  # noqa: E402
from shugocore_agent import create_agent  # noqa: E402


def _token(decision):
    return {"verdict": "allow", "decision_hash": canonical_hash(decision)}


def _gated(decision):
    payload = dict(decision)
    payload["_policy"] = _token(decision)
    return payload


class _FakeSpeaker:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)
        return True


class TestSpeechPolicy(unittest.TestCase):
    def test_speak_is_internal_not_side_effecting(self):
        self.assertEqual(SPEECH_OUTPUT_ACTION_TYPES, {"speak"})
        self.assertIn("speak", KNOWN_ACTION_TYPES)
        self.assertFalse(SPEECH_OUTPUT_ACTION_TYPES & SIDE_EFFECTING_ACTION_TYPES)


class TestSpeechDispatch(unittest.TestCase):
    def setUp(self):
        self.layer = ExecutionLayer()

    def test_registered_provider_is_called(self):
        seen = []

        def handler(decision):
            seen.append(decision["params"]["text"])
            return {"status": "success", "spoken": decision["params"]["text"]}

        self.layer.register_handler("speak", handler)
        result = self.layer.execute(_gated(
            {"action_type": "speak", "params": {"text": "hello there"}}))
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(seen, ["hello there"])

    def test_unregistered_is_not_implemented_never_simulated(self):
        result = self.layer.execute(_gated(
            {"action_type": "speak", "params": {"text": "hello"}}))
        self.assertEqual(result["status"], "not_implemented")
        self.assertIn("never", result["reason"])

    def test_tampered_verdict_refused(self):
        decision = {"action_type": "speak", "params": {"text": "hello"}}
        payload = dict(decision)
        payload["_policy"] = {"verdict": "allow", "decision_hash": "0" * 64}
        self.assertEqual(self.layer.execute(payload)["status"], "refused")



class TestAgentResponseContract(unittest.TestCase):
    def test_record_and_stats(self):
        bus = InteractionBus()
        self.assertIsNone(bus.stats()["last_spoken"])
        self.assertEqual(bus.stats()["agent_responses"], 0)
        ok, reason = bus.record_agent_response(
            AgentResponse(type="speech", content="I am here.", target="user"))
        self.assertTrue(ok, reason)
        self.assertEqual(bus.stats()["last_spoken"], "I am here.")
        self.assertEqual(bus.stats()["agent_responses"], 1)

    def test_non_speech_responses_do_not_set_last_spoken(self):
        bus = InteractionBus()
        bus.record_agent_response(
            AgentResponse(type="acknowledgement", content="noted"))
        self.assertIsNone(bus.stats()["last_spoken"])
        self.assertEqual(bus.stats()["agent_responses"], 1)

    def test_junk_response_rejected_at_boundary(self):
        bus = InteractionBus()
        ok, _ = bus.record_agent_response({"type": "speech"})  # not the class
        self.assertFalse(ok)
        ok, _ = bus.record_agent_response(AgentResponse(type="shout",
                                                        content="x"))
        self.assertFalse(ok)
        self.assertEqual(bus.stats()["rejected"], 2)
        self.assertEqual(bus.stats()["agent_responses"], 0)

    def test_content_bounded_by_schema(self):
        resp = AgentResponse(type="speech", content="x" * 5000)
        self.assertLessEqual(len(resp.content), 300)


class TestAgentSpeech(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_speak_")
        self.agent = create_agent(device_caps="Exynos-1380",
                                  api_url="http://127.0.0.1:11434",
                                  data_dir=self.tmp)

    def tearDown(self):
        try:
            self.agent.cleanup()
        except Exception:
            pass

    def test_speak_in_model_schema(self):
        # The prompt is generated from available_action_types, so the model
        # can only propose speak when a real executor is registered.
        self.assertIn("speak", self.agent.engine.available_action_types())

    def test_speak_without_provider_is_honest(self):
        result = self.agent.speak_test("any words")
        self.assertEqual(result["status"], "no_output", result)
        self.assertIn("never simulated", result["reason"])
        self.assertIsNone(self.agent.interaction.stats()["last_spoken"])

    def test_speak_through_real_gate(self):
        speaker = _FakeSpeaker()
        self.agent.register_speak_listener(speaker)
        result = self.agent.speak_test("I am here. Shugo can speak.")
        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["delivered"])
        self.assertEqual(speaker.spoken, ["I am here. Shugo can speak."])
        stats = self.agent.interaction.stats()
        self.assertEqual(stats["last_spoken"], "I am here. Shugo can speak.")
        self.assertEqual(stats["agent_responses"], 1)

    def test_speak_text_sanitized(self):
        speaker = _FakeSpeaker()
        self.agent.register_speak_listener(speaker)
        result = self.agent.speak_test("normal words \x00 and more")
        self.assertEqual(result["status"], "success", result)
        # sanitize_text strips control junk; the speaker never sees it.
        for text in speaker.spoken:
            self.assertNotIn("\x00", text)


if __name__ == "__main__":
    unittest.main()
