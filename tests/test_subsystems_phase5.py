"""Phase 5 subsystem tests: relevance-ranked memory surfacing and
deterministic memory questions."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subsystems import MemoryManager
from subsystems.memory import fact_to_second_person


class FactToSecondPersonTest(unittest.TestCase):
    """Third-person fact -> natural second-person speech."""

    def test_name(self):
        self.assertEqual(fact_to_second_person("User's name is Alex"),
                         "You're Alex")

    def test_attribute(self):
        self.assertEqual(fact_to_second_person("User's sister is Ana"),
                         "Your sister is Ana")

    def test_likes(self):
        self.assertEqual(fact_to_second_person("User likes tea"),
                         "You like tea")

    def test_dislikes(self):
        self.assertEqual(fact_to_second_person("User dislikes rain"),
                         "You don't like rain")

    def test_goal(self):
        self.assertEqual(fact_to_second_person("User wants to run"),
                         "You want to run")

    def test_self_description(self):
        self.assertEqual(fact_to_second_person(
            "User describes themselves as a runner"),
            "You describe yourself as a runner")

    def test_explicit_fact_passes_through(self):
        self.assertEqual(fact_to_second_person("my sister is Ana"),
                         "my sister is Ana")

    def test_empty(self):
        self.assertEqual(fact_to_second_person(""), "")


class RelevantFactsTest(unittest.TestCase):
    """Keyword-overlap ranking with score fallback."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.mem = MemoryManager(self._tmp)

    def _populate(self):
        # "sister" fact stated once; "name" fact repeated three times so
        # its score is higher.
        self.mem.on_user_input("my sister is Ana")
        for _ in range(3):
            self.mem.on_user_input("my name is Bob")

    def test_overlap_beats_score(self):
        self._populate()
        rows = self.mem.facts.relevant_facts("how is my sister doing",
                                             limit=2)
        self.assertTrue(rows)
        self.assertIn("sister", rows[0]["text"])

    def test_no_overlap_falls_back_to_score(self):
        self._populate()
        rows = self.mem.facts.relevant_facts(
            "the zebra crossed the road", limit=2)
        self.assertTrue(rows)
        self.assertIn("name", rows[0]["text"])

    def test_empty_transcript_recalls_top(self):
        self._populate()
        rows = self.mem.facts.relevant_facts("", limit=2)
        self.assertTrue(rows)
        self.assertIn("name", rows[0]["text"])

    def test_stopword_only_transcript_recalls_top(self):
        self._populate()
        rows = self.mem.facts.relevant_facts("what is it", limit=2)
        self.assertTrue(rows)
        self.assertIn("name", rows[0]["text"])

    def test_limit_respected(self):
        self._populate()
        self.mem.on_user_input("i like coffee")
        rows = self.mem.facts.relevant_facts("tell me everything", limit=1)
        self.assertEqual(len(rows), 1)

    def test_recall_fact_strings_about(self):
        self._populate()
        strings = self.mem.recall_fact_strings(about="how is my sister",
                                               limit=2)
        self.assertTrue(strings)
        self.assertIn("sister", strings[0])

    def test_recall_fact_strings_default(self):
        self._populate()
        strings = self.mem.recall_fact_strings(limit=2)
        self.assertTrue(strings)
        self.assertIn("name", strings[0])


class AgentMemoryQuestionsTest(unittest.TestCase):
    """Full agent: deterministic memory questions without a model call."""

    def setUp(self):
        from shugocore_agent import create_agent
        self._agent_data = tempfile.mkdtemp()
        self.agent = create_agent(
            device_caps="TestSoC", api_url="http://127.0.0.1:11434",
            data_dir=self._agent_data)

    def tearDown(self):
        try:
            self.agent.cleanup()
        except Exception:
            pass

    class _FakeSpeaker:
        """Minimal speak listener contract: .speak(text) -> bool."""

        def __init__(self):
            self.spoken = []

        def speak(self, text):
            self.spoken.append(text)
            return True

        def register_hook(self, hook):
            return None

    def _fresh_speaker(self):
        speaker = self._FakeSpeaker()
        self.agent.register_speak_listener(speaker)
        return speaker

    def test_what_is_my_name(self):
        self.agent._handle_conversational_input(
            {"transcript": "my name is Alex"})
        speaker = self._fresh_speaker()
        self.agent._handle_conversational_input(
            {"transcript": "what's my name"})
        self.assertTrue(any("Alex" in s for s in speaker.spoken),
                        speaker.spoken)

    def test_what_is_my_favorite_color(self):
        self.agent._handle_conversational_input(
            {"transcript": "my favorite color is blue"})
        speaker = self._fresh_speaker()
        self.agent._handle_conversational_input(
            {"transcript": "what is my favorite color"})
        self.assertTrue(any("blue" in s.lower() for s in speaker.spoken),
                        speaker.spoken)

    def test_who_am_i(self):
        self.agent._handle_conversational_input(
            {"transcript": "my name is Alex"})
        speaker = self._fresh_speaker()
        self.agent._handle_conversational_input({"transcript": "who am i"})
        self.assertTrue(any("Alex" in s for s in speaker.spoken),
                        speaker.spoken)

    def test_unknown_memory_question_falls_through(self):
        speaker = self._fresh_speaker()
        self.agent._handle_conversational_input(
            {"transcript": "what's my name"})
        # Falls through to the conversational path: graceful fallback, and
        # never a hallucinated second-person fact.
        self.assertTrue(speaker.spoken)
        self.assertTrue(all("You" not in s for s in speaker.spoken),
                        speaker.spoken)

    def test_explicit_fact_question(self):
        self.agent._handle_conversational_input(
            {"transcript": "remember that my dog is Rex"})
        speaker = self._fresh_speaker()
        self.agent._handle_conversational_input(
            {"transcript": "what is my dog"})
        self.assertTrue(any("Rex" in s for s in speaker.spoken),
                        speaker.spoken)

    def test_what_do_you_remember_still_works(self):
        self.agent._handle_conversational_input(
            {"transcript": "my name is Alex"})
        speaker = self._fresh_speaker()
        self.agent._handle_conversational_input(
            {"transcript": "what do you remember about me"})
        self.assertTrue(any("Alex" in s for s in speaker.spoken),
                        speaker.spoken)


if __name__ == "__main__":
    unittest.main()

