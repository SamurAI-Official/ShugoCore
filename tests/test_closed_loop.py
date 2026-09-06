"""v1.17 closed-loop validation: the loop is PROVEN, not assumed.

Human speaks -> bus pairs the answer with the pending question -> round trip
is recorded with latency -> agent shell journals it as metadata only (words
never enter memory) -> pipeline_health reports every stage honestly
(ok / stale / down / unknown). The provider rule continues to hold: nothing
here lets the decision core import the interaction module (guarded in
tests.test_human_interaction).
"""
import json
import os
import unittest

from human_interaction import AgentResponse, InteractionBus
from shugocore_agent import create_agent
from tests.test_human_interaction import _CLEAN_FILES, _FakeClock, _obs


def _speech(text, clock, confidence=0.9, source="stt"):
    return _obs(type_="speech", source=source,
                payload={"transcript": text}, confidence=confidence,
                timestamp=clock())


class _FakeSpeaker:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)
        return True


class TestRoundTripRecord(unittest.TestCase):
    """The Record stage: completed question/answer exchanges, with latency."""

    def setUp(self):
        self.clock = _FakeClock()
        self.bus = InteractionBus(clock=self.clock)

    def test_round_trip_recorded_and_drained(self):
        self.assertTrue(self.bus.expect_answer("Should I continue?"))
        self.clock.advance(2.5)
        self.bus.publish(_speech("yes", self.clock))
        event = self.bus.stats()["last_conversation_event"]
        self.assertIsNotNone(event)
        self.assertEqual(event["question"], "Should I continue?")
        self.assertEqual(event["answer"], "yes")
        self.assertEqual(event["round_trip_s"], 2.5)
        drained = self.bus.drain_conversation_events()
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0]["question"], "Should I continue?")
        # Drain consumes: a second drain is empty (journal, not archive).
        self.assertEqual(self.bus.drain_conversation_events(), [])
        self.assertIsNone(self.bus.stats()["last_conversation_event"])

    def test_round_trip_via_question_response(self):
        # The ask_user path: a speech AgentResponse that expects an answer
        # arms the pairing exactly like expect_answer does.
        self.bus.record_agent_response(AgentResponse(
            type="speech", content="Where should I go?",
            expects_answer=True))
        self.clock.advance(1.0)
        self.bus.publish(_speech("the kitchen", self.clock))
        event = self.bus.drain_conversation_events()
        self.assertEqual(len(event), 1)
        self.assertEqual(event[0]["question"], "Where should I go?")
        self.assertEqual(event[0]["answer"], "the kitchen")

    def test_expired_question_never_pairs(self):
        self.bus.expect_answer("Come again?")
        self.clock.advance(121.0)  # past _ANSWER_TTL_S
        self.bus.publish(_speech("hello", self.clock))
        self.assertEqual(self.bus.drain_conversation_events(), [])
        self.assertIsNone(self.bus.stats()["last_answer"])

    def test_events_bounded(self):
        for n in range(10):
            self.bus.expect_answer(f"q{n}?")
            self.clock.advance(0.1)
            self.bus.publish(_speech(f"a{n}", self.clock))
            self.clock.advance(0.1)
        drained = self.bus.drain_conversation_events()
        self.assertEqual(len(drained), 8)  # _MAX_CONVERSATION_EVENTS
        self.assertEqual(drained[0]["question"], "q2?")
        self.assertEqual(drained[-1]["answer"], "a9")

    def test_words_survive_in_conversation_memory(self):
        # The bus keeps the words (bounded conversation); draining for the
        # journal is the shell's choice. stats counts what completed.
        self.bus.expect_answer("Status?")
        self.bus.publish(_speech("all good", self.clock))
        self.assertEqual(self.bus.stats()["conversation_events"], 1)
        turns = self.bus.human_context()["conversation"]
        self.assertEqual(turns[-1]["text"], "all good")


class TestPipelineHealth(unittest.TestCase):
    """The SHUGOCORE LIVE monitor: every stage reports honestly."""

    def setUp(self):
        self.clock = _FakeClock()
        self.bus = InteractionBus(clock=self.clock)

    def test_unknown_when_no_evidence(self):
        health = self.bus.pipeline_health()
        self.assertEqual(health["overall"], "unknown")
        self.assertTrue(all(v == "unknown"
                            for v in health["stages"].values()))

    def test_ok_with_fresh_evidence(self):
        self.bus.publish(_obs(type_="visual",
                              payload={"person_present": True},
                              timestamp=self.clock()))
        self.bus.publish(_speech("hi", self.clock))
        health = self.bus.pipeline_health(
            model_ready=True, tts_attached=True, memory_ok=True)
        self.assertEqual(health["overall"], "ok")
        self.assertEqual(set(health["stages"].values()), {"ok"})

    def test_stale_after_time_passes(self):
        self.bus.publish(_obs(type_="visual",
                              payload={"person_present": True},
                              timestamp=self.clock()))
        self.bus.publish(_speech("hi", self.clock))
        self.clock.advance(130.0)
        stages = self.bus.pipeline_health(
            model_ready=True, tts_attached=True,
            memory_ok=True)["stages"]
        self.assertEqual(stages["sensors"], "stale")
        self.assertEqual(stages["vision"], "stale")
        self.assertEqual(stages["hearing"], "stale")
        self.assertEqual(stages["model"], "ok")  # agent truth does not age

    def test_down_when_provider_absent(self):
        self.bus.publish(_speech("hi", self.clock))
        health = self.bus.pipeline_health(tts_attached=False)
        self.assertEqual(health["stages"]["speech"], "down")
        self.assertEqual(health["overall"], "down")

    def test_model_truth_alone_is_not_loop_evidence(self):
        health = self.bus.pipeline_health(model_ready=True)
        self.assertEqual(health["stages"]["model"], "ok")
        self.assertEqual(health["overall"], "unknown")

    def test_round_trips_surfaced(self):
        self.bus.expect_answer("q?")
        self.bus.publish(_speech("a", self.clock))
        self.assertEqual(self.bus.pipeline_health()["round_trips"], 1)


class TestAgentClosedLoop(unittest.TestCase):
    """Agent-shell integration: journal metadata only + status pipeline."""

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

    def test_ask_answer_journal_metadata_only(self):
        agent = self._make()
        speaker = _FakeSpeaker()
        agent.register_speak_listener(speaker)
        result = agent._execute_ask_user(
            {"params": {"question": "What would you like next?"}})
        self.assertEqual(result["status"], "success", result)
        self.assertTrue(speaker.spoken)
        agent.publish_human_observation(
            {"type": "speech", "source": "stt",
             "payload": {"transcript": "check the battery"},
             "confidence": 0.9})
        agent.tick()
        events = [e for e in agent.memory.tier1.recent(20)
                  if e.get("type") == "conversation_event"]
        self.assertEqual(len(events), 1)
        payload = events[0].get("payload", {})
        self.assertIn("round_trip_s", payload)
        self.assertEqual(payload["question_chars"],
                         len("What would you like next?"))
        self.assertEqual(payload["answer_chars"],
                         len("check the battery"))
        # The privacy invariant: no raw words cross into memory.
        blob = json.dumps(events[0])
        for word in ("What would", "battery"):
            self.assertNotIn(word, blob)

    def test_tick_without_exchanges_journals_nothing(self):
        agent = self._make()
        agent.tick()
        events = [e for e in agent.memory.tier1.recent(20)
                  if e.get("type") == "conversation_event"]
        self.assertEqual(events, [])

    def test_status_pipeline_truthful(self):
        agent = self._make()
        pipeline = agent.get_status()["pipeline"]
        stages = pipeline["stages"]
        self.assertEqual(stages["model"], "ok")
        self.assertEqual(stages["memory"], "ok")
        self.assertEqual(stages["speech"], "down")  # no listener attached
        self.assertEqual(pipeline["overall"], "down")
        # With the TTS edge attached the loop reads ready.
        agent.register_speak_listener(_FakeSpeaker())
        stages = agent.get_status()["pipeline"]["stages"]
        self.assertEqual(stages["speech"], "ok")

    def test_status_json_boundary(self):
        agent = self._make()
        data = json.loads(agent.get_status_json())
        self.assertIn(data["pipeline"]["overall"],
                      {"ok", "stale", "down", "unknown"})


if __name__ == "__main__":
    unittest.main()

