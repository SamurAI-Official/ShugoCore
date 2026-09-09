"""Personality growth tests: feedback lexicon, memory-driven synthesis,
and the full agent growth cycle."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from personality import PersonalityModel, PersonalityProfile, GROWTH_RATE
from personality.growth import (extract_feedback, grow_from_memory,
                                synthesize_deltas)


class FeedbackLexiconTest(unittest.TestCase):
    """extract_feedback: explicit user feedback -> trait signals."""

    def test_praise_warms(self):
        self.assertGreater(extract_feedback(
            "thank you so much, you're the best")["warmth"], 0)

    def test_talk_too_much_quiets(self):
        self.assertLess(extract_feedback(
            "you talk too much, be shorter")["verbosity"], 0)

    def test_wants_more_detail(self):
        self.assertGreater(extract_feedback(
            "tell me more about it")["verbosity"], 0)

    def test_funny_praised(self):
        self.assertGreater(extract_feedback("haha you're funny")["humor"], 0)

    def test_annoying_criticized(self):
        self.assertLess(extract_feedback("that's annoying")["humor"], 0)

    def test_no_feedback_is_empty(self):
        self.assertEqual(extract_feedback("what time is it"), {})

    def test_mixed_nets(self):
        net = extract_feedback("you're funny but stop rambling")
        self.assertGreater(net["humor"], 0)
        self.assertLess(net["verbosity"], 0)


class SynthesisTest(unittest.TestCase):
    """synthesize_deltas: interaction stats -> clamped proposals."""

    def test_empty_stats_no_growth(self):
        self.assertEqual(synthesize_deltas({}), ({}, ""))
        self.assertEqual(synthesize_deltas({"turns": 0}), ({}, ""))

    def test_question_heavy_grows_curiosity(self):
        deltas, reasons = synthesize_deltas(
            {"turns": 10, "questions": 5})
        self.assertGreater(deltas["curiosity"], 0)
        self.assertIn("questions", reasons)

    def test_fact_sharing_grows_warmth(self):
        deltas, _ = synthesize_deltas({"turns": 4, "new_facts": 3})
        self.assertGreater(deltas["warmth"], 0)

    def test_command_heavy_grows_proactivity(self):
        deltas, _ = synthesize_deltas({"turns": 10, "commands": 7})
        self.assertGreater(deltas["proactivity"], 0)

    def test_tool_failures_soften_proactivity(self):
        deltas, _ = synthesize_deltas(
            {"turns": 10, "tool_runs": 4, "tool_failures": 3})
        self.assertLess(deltas["proactivity"], 0)

    def test_feedback_dominates(self):
        deltas, reasons = synthesize_deltas(
            {"turns": 6, "feedback": {"verbosity": -2, "humor": 1}})
        self.assertLessEqual(deltas["verbosity"], -0.1)
        self.assertGreater(deltas["humor"], 0)
        self.assertIn("criticized", reasons)

    def test_applied_growth_respects_model_clamp(self):
        # Proposals may stack in synthesis; the MODEL is the clamp
        # authority — no trait may move more than GROWTH_RATE per gen.
        model = PersonalityModel.genesis(PersonalityProfile())
        before = {t: s.value for t, s in model.traits.items()}
        grow_from_memory(model,
                         {"turns": 40, "questions": 20, "commands": 10,
                          "new_facts": 9, "tool_runs": 5, "tool_failures": 4,
                          "feedback": {"verbosity": -3, "warmth": 2}})
        for trait, state in model.traits.items():
            self.assertLessEqual(abs(state.value - before[trait]),
                                 GROWTH_RATE + 1e-9, msg=trait)


class GrowFromMemoryTest(unittest.TestCase):
    """grow_from_memory: one generation, report, policy untouched."""

    def test_grows_and_reports(self):
        model = PersonalityModel.genesis(PersonalityProfile())
        report = grow_from_memory(model, {"turns": 10, "questions": 5})
        self.assertIsNotNone(report)
        self.assertEqual(report["generation"], 1)
        self.assertGreater(model.traits["curiosity"].value, 0.5)

    def test_nothing_to_learn_returns_none(self):
        model = PersonalityModel.genesis(PersonalityProfile())
        self.assertIsNone(grow_from_memory(model, {"turns": 0}))
        self.assertEqual(model.generation, 0)

    def test_policy_untouched_by_growth(self):
        model = PersonalityModel.genesis(PersonalityProfile())
        policy = json.dumps(model.policy, sort_keys=True)
        grow_from_memory(model, {"turns": 10, "questions": 5,
                                 "feedback": {"verbosity": -2}})
        self.assertEqual(json.dumps(model.policy, sort_keys=True), policy)


class AgentPersonalityLifecycleTest(unittest.TestCase):
    """Full agent: birth, growth cadence, persistence, live rendering."""

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

    def _pm_path(self):
        return os.path.join(self._agent_data, "personality_model.json")

    def test_baby_state_born_on_first_boot(self):
        self.assertIsNotNone(self.agent.personality_model)
        self.assertEqual(self.agent.personality_model.generation, 0)
        self.assertTrue(os.path.exists(self._pm_path()))
        data = json.load(open(self._pm_path()))
        self.assertEqual(data["generation"], 0)
        self.assertEqual(data["history"][0]["note"],
                         "genesis (baby state)")

    def test_rendered_profile_is_live(self):
        profile = self.agent.personality
        self.assertEqual(profile.name,
                         self.agent.personality_model.name)
        self.assertEqual(profile.speech["never_say"],
                         self.agent.personality_model.policy["never_say"])

    def test_forced_growth_bumps_generation_and_persists(self):
        for _ in range(6):
            self.agent._handle_conversational_input(
                {"transcript": "what is love"})  # question-heavy window
        report = self.agent._growth_maybe(force=True)
        self.assertIsNotNone(report)
        self.assertEqual(self.agent.personality_model.generation, 1)
        data = json.load(open(self._pm_path()))
        self.assertEqual(data["generation"], 1)

    def test_growth_report_carries_comparison(self):
        """The report compares the successor against the pre-existing
        model: drift scalar + per-trait deltas, nonzero after real growth.
        """
        for _ in range(6):
            self.agent._handle_conversational_input(
                {"transcript": "what is love"})
        report = self.agent._growth_maybe(force=True)
        self.assertIsNotNone(report)
        comparison = report.get("comparison")
        self.assertIsNotNone(comparison)
        self.assertGreater(comparison["drift"], 0.0)
        self.assertFalse(comparison["policy_violation"])
        self.assertGreater(comparison["trait_deltas"].get("curiosity", 0.0),
                           0.0)
        self.assertEqual((comparison["from_gen"], comparison["to_gen"]),
                         (0, 1))

    def test_cadence_grows_after_n_turns(self):
        self.agent.GROWTH_EVERY = 5
        for _ in range(5):
            self.agent._handle_conversational_input(
                {"transcript": "who are you"})
        self.assertEqual(self.agent.personality_model.generation, 1)
        # Window reset: five more turns -> second generation.
        for _ in range(5):
            self.agent._handle_conversational_input(
                {"transcript": "who are you"})
        self.assertEqual(self.agent.personality_model.generation, 2)

    def test_facts_count_toward_growth(self):
        self.agent.GROWTH_EVERY = 2
        self.agent._handle_conversational_input(
            {"transcript": "my name is Alex"})
        self.agent._handle_conversational_input(
            {"transcript": "my favorite color is blue"})
        self.agent._handle_conversational_input(
            {"transcript": "my favorite food is sushi"})  # cadence hit
        self.assertEqual(self.agent.personality_model.generation, 1)
        self.assertGreater(self.agent.personality_model.traits["warmth"].value,
                           0.3)

    def test_feedback_captured_and_grown(self):
        self.agent.GROWTH_EVERY = 1
        self.agent._handle_conversational_input(
            {"transcript": "you talk too much, be shorter"})
        self.assertEqual(self.agent.personality_model.generation, 1)
        self.assertLess(self.agent.personality_model.traits["verbosity"].value,
                        0.5)

    def test_model_survives_agent_restart(self):
        self.agent.GROWTH_EVERY = 2
        for _ in range(2):
            self.agent._handle_conversational_input(
                {"transcript": "who are you"})
        self.assertEqual(self.agent.personality_model.generation, 1)
        self.agent.cleanup()
        from shugocore_agent import create_agent
        agent2 = create_agent(
            device_caps="TestSoC", api_url="http://127.0.0.1:11434",
            data_dir=self._agent_data)
        try:
            self.assertEqual(agent2.personality_model.generation, 1)
            data = json.load(open(self._pm_path()))
            self.assertEqual(data["generation"], 1)
        finally:
            agent2.cleanup()

    def test_policy_survives_growth_across_restart(self):
        policy_before = json.dumps(
            self.agent.personality_model.policy, sort_keys=True)
        self.agent.GROWTH_EVERY = 2
        for _ in range(2):
            self.agent._handle_conversational_input(
                {"transcript": "who are you"})
        self.agent.cleanup()
        from shugocore_agent import create_agent
        agent2 = create_agent(
            device_caps="TestSoC", api_url="http://127.0.0.1:11434",
            data_dir=self._agent_data)
        try:
            self.assertEqual(
                json.dumps(agent2.personality_model.policy,
                           sort_keys=True), policy_before)
        finally:
            agent2.cleanup()


if __name__ == "__main__":
    unittest.main()

