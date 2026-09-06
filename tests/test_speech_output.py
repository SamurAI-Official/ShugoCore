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
from human_interaction import (AgentResponse, InteractionBus,  # noqa: E402
                               truncate_sentence)
from policy import (  # noqa: E402
    KNOWN_ACTION_TYPES,
    SIDE_EFFECTING_ACTION_TYPES,
    SPEECH_OUTPUT_ACTION_TYPES,
)
from security import canonical_hash  # noqa: E402
from shugocore_agent import create_agent  # noqa: E402
from subconscious import _build_decision_prompt  # noqa: E402


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


class TestAskUser(unittest.TestCase):
    """v1.16: the agent asking the human instead of guessing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_ask_")
        self.agent = create_agent(device_caps="Exynos-1380",
                                  api_url="http://127.0.0.1:11434",
                                  data_dir=self.tmp)

    def tearDown(self):
        try:
            self.agent.cleanup()
        except Exception:
            pass

    def test_ask_user_in_model_schema(self):
        # Registered with the same speech edge as speak, so the prompt
        # offers it exactly when a real executor exists.
        self.assertIn("ask_user", self.agent.engine.available_action_types())

    def test_ask_user_unknown_without_registration(self):
        from decision_engine import DecisionEngine
        from execution_layer import ExecutionLayer as L
        layer = L()
        self.assertNotIn("ask_user", layer._handlers)
        result = layer.execute({
            "action_type": "ask_user", "params": {}, "confidence": 1.0,
            "_policy": _token({"action_type": "ask_user", "params": {},
                               "confidence": 1.0})})
        self.assertEqual(result["status"], "not_implemented")

    def test_ask_user_through_real_gate(self):
        speaker = _FakeSpeaker()
        self.agent.register_speak_listener(speaker)
        decision = {"action_type": "ask_user",
                    "params": {"question": "Where should I place it?"},
                    "confidence": 0.9,
                    "proposal_source": "shugocore-local"}
        result = self.agent._execute_ask_user(decision)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(speaker.spoken, ["Where should I place it?"])
        stats = self.agent.interaction.stats()
        self.assertEqual(stats["last_question"], "Where should I place it?")
        self.assertEqual(stats["conversation_turns"], 1)

    def test_ask_user_journaled(self):
        speaker = _FakeSpeaker()
        self.agent.register_speak_listener(speaker)
        self.agent._execute_ask_user(
            {"action_type": "ask_user",
             "params": {"question": "Should I continue?"},
             "confidence": 1.0, "proposal_source": "test"})
        events = [e.get("type") for e in self.agent.memory.tier1._events]
        self.assertIn("agent_question", events)

    def test_ask_user_empty_question_refused(self):
        result = self.agent._execute_ask_user(
            {"action_type": "ask_user", "params": {}, "confidence": 1.0,
             "proposal_source": "test"})
        self.assertEqual(result["status"], "refused")

    def test_ask_user_without_provider_is_honest(self):
        result = self.agent._execute_ask_user(
            {"action_type": "ask_user", "params": {"question": "hello?"},
             "confidence": 1.0, "proposal_source": "test"})
        self.assertEqual(result["status"], "no_output")

    def test_speak_accepts_utterance_param(self):
        # 1B dialect fix: parser routes top-level text into
        # params.utterance; the executor must accept it.
        speaker = _FakeSpeaker()
        self.agent.register_speak_listener(speaker)
        result = self.agent._execute_speak(
            {"action_type": "speak",
             "params": {"utterance": "words from the text field"},
             "confidence": 1.0, "proposal_source": "test"})
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(speaker.spoken, ["words from the text field"])


class TestSentenceBoundTruncation(unittest.TestCase):
    """v1.18: the agent never speaks a fragment it did not choose —
    speak/ask_user texts are bounded at a sentence or word boundary."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_bound_")
        self.agent = create_agent(device_caps="Exynos-1380",
                                  api_url="http://127.0.0.1:11434",
                                  data_dir=self.tmp)

    def tearDown(self):
        try:
            self.agent.cleanup()
        except Exception:
            pass

    def test_short_text_untouched(self):
        self.assertEqual(truncate_sentence("Hello there.", 400),
                         "Hello there.")

    def test_cut_at_sentence_boundary(self):
        text = ("First sentence is reasonably long here. "
                "Second sentence continues and gets chopped at the limit! "
                + "filler " * 80)
        out = truncate_sentence(text, 120)
        self.assertLessEqual(len(out), 120)
        # kept text ends on a complete sentence (the "!" sentence), the
        # filler after it is dropped whole
        self.assertTrue(out.endswith(("!", ".", "?")), out)
        self.assertIn("limit!", out)

    def test_cut_at_word_boundary_when_no_sentence(self):
        text = (" ".join(["word"] * 200))
        out = truncate_sentence(text, 137)
        self.assertLessEqual(len(out), 137)
        self.assertFalse(out.endswith("wordword"), out)
        self.assertTrue(len(out.split()) < 200)
        self.assertFalse(out.endswith("wo"), out)  # never a partial word

    def test_hard_limit_when_no_boundaries(self):
        out = truncate_sentence("a" * 900, 100)
        self.assertLessEqual(len(out), 100)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(truncate_sentence("", 400), "")
        self.assertEqual(truncate_sentence(None, 400), "")

    def test_speak_action_bounds_long_text(self):
        # The executor path itself uses the sentence-aware bound now.
        speaker = _FakeSpeaker()
        self.agent.register_speak_listener(speaker)
        # ~48 chars per sentence → the last complete sentence inside the
        # 400-char window ends in the second half, so the sentence rule wins.
        long_text = "".join(
            "This is sentence number %d and it is complete. " % i
            for i in range(12))
        result = self.agent._execute_speak(
            {"action_type": "speak", "params": {"text": long_text},
             "confidence": 1.0, "proposal_source": "test"})
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(len(speaker.spoken), 1)
        spoken = speaker.spoken[0]
        self.assertLessEqual(len(spoken), 400)
        self.assertTrue(spoken.endswith("."), spoken[-60:])
        self.assertIn("sentence number", spoken)
        # dropped everything after the kept sentence — never a fragment
        self.assertNotIn("sentence number 11", spoken)

    def test_speak_action_word_bounds_punctuation_free_text(self):
        speaker = _FakeSpeaker()
        self.agent.register_speak_listener(speaker)
        long_text = "filler word " * 80   # no sentence marks → word cut
        result = self.agent._execute_speak(
            {"action_type": "speak", "params": {"text": long_text},
             "confidence": 1.0, "proposal_source": "test"})
        self.assertEqual(result["status"], "success", result)
        spoken = speaker.spoken[0]
        self.assertLessEqual(len(spoken), 400)
        self.assertTrue(spoken.endswith("word"), spoken[-30:])


class TestSpeechDialectPrompt(unittest.TestCase):
    """v1.18: when the executor set carries the human-facing speech actions,
    the decision prompt carries Shugo's persona and the one-complete-
    sentence rule (the small model needs it stated to finish sentences)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_dialect_")
        self.agent = create_agent(device_caps="Exynos-1380",
                                  api_url="http://127.0.0.1:11434",
                                  data_dir=self.tmp)

    def tearDown(self):
        try:
            self.agent.cleanup()
        except Exception:
            pass

    def test_persona_present_with_speech_actions(self):
        prompt = _build_decision_prompt(
            "test", ["record_observation", "speak", "ask_user"])
        self.assertIn("Shugo", prompt)
        self.assertIn("ONE warm, complete sentence", prompt)
        # the generated protocol must still list every real action type
        self.assertIn("speak", prompt)
        self.assertIn("record_observation", prompt)
        self.assertIn("or null", prompt)

    def test_no_speech_line_without_speech_actions(self):
        prompt = _build_decision_prompt("test", ["record_observation"])
        self.assertNotIn("Shugo", prompt)
        self.assertIn("record_observation", prompt)
        self.assertIn("or null", prompt)

    def test_prompt_is_generated_from_real_executor_set(self):
        # Alignment rule: the prompt lists exactly what the engine executes.
        self.assertIn("speak", self.agent.engine.available_action_types())
        prompt = _build_decision_prompt(
            "test", self.agent.engine.available_action_types())
        self.assertIn("Shugo", prompt)


if __name__ == "__main__":
    unittest.main()
