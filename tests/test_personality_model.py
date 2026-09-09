"""PersonalityModel tests: baby-state genesis, clamped growth,
comparison, persistence, and prompt rendering."""
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from personality import (PersonalityModel, PersonalityProfile,
                         personality_system_prompt, GROWTH_RATE)


def _baby() -> PersonalityModel:
    return PersonalityModel.genesis(PersonalityProfile())


class GenesisTest(unittest.TestCase):
    """The blank baby state: policy only, nothing learned."""

    def test_born_at_generation_zero(self):
        model = _baby()
        self.assertEqual(model.generation, 0)
        self.assertEqual(len(model.history), 1)
        self.assertEqual(model.history[0]["note"], "genesis (baby state)")

    def test_traits_at_neutral_baselines(self):
        model = _baby()
        self.assertAlmostEqual(model.traits["warmth"].value, 0.5)
        self.assertAlmostEqual(model.traits["humor"].value, 0.4)
        self.assertAlmostEqual(model.traits["curiosity"].value, 0.5)
        self.assertAlmostEqual(model.traits["verbosity"].value, 0.5)
        self.assertAlmostEqual(model.traits["formality"].value, 0.3)
        self.assertAlmostEqual(model.traits["proactivity"].value, 0.3)
        for state in model.traits.values():
            self.assertEqual(state.confidence, 0.0)
            self.assertEqual(state.evidence_count, 0)

    def test_policy_extracted_and_frozen(self):
        profile = PersonalityProfile()
        model = PersonalityModel.genesis(profile)
        self.assertEqual(model.name, profile.name)
        self.assertEqual(model.policy["never_say"],
                         profile.speech["never_say"])
        self.assertTrue(model.policy["refuse_harmful"])
        self.assertTrue(model.policy["first_person"])


class GrowthTest(unittest.TestCase):
    """Clamped, evidenced, recorded growth."""

    def test_growth_rate_clamps_big_deltas(self):
        model = _baby()
        applied = model.apply_delta({"warmth": 0.9}, note="big day")
        self.assertAlmostEqual(applied["warmth"], GROWTH_RATE)
        self.assertAlmostEqual(model.traits["warmth"].value,
                               0.5 + GROWTH_RATE)
        self.assertEqual(model.generation, 1)

    def test_negative_growth_clamps_at_zero(self):
        model = _baby()
        for _ in range(10):
            model.apply_delta({"humor": -GROWTH_RATE})
        self.assertGreaterEqual(model.traits["humor"].value, 0.0)

    def test_positive_growth_clamps_at_one(self):
        model = _baby()
        for _ in range(10):
            model.apply_delta({"curiosity": GROWTH_RATE})
        self.assertLessEqual(model.traits["curiosity"].value, 1.0)

    def test_evidence_and_confidence_rise(self):
        model = _baby()
        model.apply_delta({"warmth": 0.05})
        state = model.traits["warmth"]
        self.assertEqual(state.evidence_count, 1)
        self.assertAlmostEqual(state.confidence, 0.1)
        self.assertEqual(state.last_gen, 1)

    def test_unknown_trait_ignored(self):
        model = _baby()
        applied = model.apply_delta({"nonsense": 0.3})
        self.assertEqual(applied, {})
        self.assertEqual(model.generation, 0)

    def test_policy_never_moves(self):
        model = _baby()
        before = json.dumps(model.policy, sort_keys=True)
        for _ in range(5):
            model.apply_delta({"warmth": GROWTH_RATE,
                               "formality": -GROWTH_RATE})
        self.assertEqual(json.dumps(model.policy, sort_keys=True), before)

    def test_history_append_only(self):
        model = _baby()
        model.apply_delta({"warmth": 0.05}, note="first")
        model.apply_delta({"humor": 0.05}, note="second")
        self.assertEqual([h["gen"] for h in model.history], [0, 1, 2])
        self.assertEqual(model.history[1]["note"], "first")


class CompareModelsTest(unittest.TestCase):
    """PersonalityModel.compare_models: successor-vs-predecessor reports."""

    def test_successor_report(self):
        old, new = _baby(), _baby()
        new.apply_delta({"warmth": GROWTH_RATE})
        report = PersonalityModel.compare_models(old, new)
        self.assertAlmostEqual(report["trait_deltas"]["warmth"],
                               GROWTH_RATE)
        self.assertGreater(report["drift"], 0.0)
        self.assertFalse(report["policy_violation"])
        self.assertEqual((report["from_gen"], report["to_gen"]), (0, 1))

    def test_identical_models_zero_drift(self):
        report = PersonalityModel.compare_models(_baby(), _baby())
        self.assertEqual(report["drift"], 0.0)
        self.assertTrue(all(v == 0.0
                            for v in report["trait_deltas"].values()))

    def test_different_agents_rejected(self):
        other = _baby()
        other.name = "SomebodyElse"
        with self.assertRaises(ValueError):
            PersonalityModel.compare_models(_baby(), other)

    def test_matches_diff_direction(self):
        old, new = _baby(), _baby()
        new.apply_delta({"warmth": GROWTH_RATE})
        via_static = PersonalityModel.compare_models(old, new)
        via_method = old.diff(new)
        self.assertEqual(via_static["trait_deltas"],
                         via_method["trait_deltas"])
        self.assertAlmostEqual(via_static["drift"], via_method["drift"])



class ComparisonTest(unittest.TestCase):
    """diff() against the previous generation."""

    def test_drift_measures_trait_distance(self):
        old, new = _baby(), _baby()
        new.apply_delta({"warmth": GROWTH_RATE})
        report = old.diff(new)
        self.assertAlmostEqual(report["trait_deltas"]["warmth"],
                               GROWTH_RATE)
        # RMS over the full 6-trait vector: one 0.1 move dilutes.
        expected = math.sqrt(GROWTH_RATE ** 2 / len(old.traits))
        self.assertAlmostEqual(report["drift"], expected, places=5)
        self.assertFalse(report["policy_violation"])

    def test_identical_models_zero_drift(self):
        report = _baby().diff(_baby())
        self.assertEqual(report["drift"], 0.0)

    def test_policy_violation_reported(self):
        old, tampered = _baby(), _baby()
        tampered.policy["never_say"].append("hacked")
        self.assertTrue(old.diff(tampered)["policy_violation"])

    def test_multi_trait_drift_is_rms(self):
        old, new = _baby(), _baby()
        new.apply_delta({"warmth": 0.1, "humor": -0.1})
        expected = math.sqrt((0.01 + 0.01) / len(old.traits))
        self.assertAlmostEqual(old.diff(new)["drift"], expected, places=5)

    def test_more_movement_more_drift(self):
        old, small, big = _baby(), _baby(), _baby()
        small.apply_delta({"warmth": GROWTH_RATE})
        for _ in range(3):
            big.apply_delta({"warmth": GROWTH_RATE})
        self.assertLess(old.diff(small)["drift"], old.diff(big)["drift"])


class PersistenceTest(unittest.TestCase):
    """personality_model.json round-trips like timers.json."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.path = os.path.join(self._tmp, "personality_model.json")

    def test_round_trip(self):
        model = _baby()
        model.apply_delta({"warmth": 0.1, "verbosity": -0.05}, note="grew")
        model.save(self.path)
        loaded = PersonalityModel.load(self.path)
        self.assertEqual(loaded.generation, 1)
        self.assertEqual(loaded.name, model.name)
        self.assertEqual(loaded.policy, model.policy)
        self.assertAlmostEqual(loaded.traits["warmth"].value, 0.6)
        self.assertEqual(len(loaded.history), 2)

    def test_load_missing_returns_none(self):
        self.assertIsNone(PersonalityModel.load(self.path))

    def test_load_corrupt_returns_none(self):
        with open(self.path, "w") as fh:
            fh.write("{oops")
        self.assertIsNone(PersonalityModel.load(self.path))

    def test_load_malformed_returns_none(self):
        with open(self.path, "w") as fh:
            json.dump({"name": "no traits here"}, fh)
        self.assertIsNone(PersonalityModel.load(self.path))

    def test_save_is_atomic(self):
        model = _baby()
        model.save(self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertTrue(os.path.exists(self.path))


class RenderingTest(unittest.TestCase):
    """as_profile() feeds the existing prompt translator."""

    def test_traits_render_as_phrases(self):
        model = _baby()
        profile = model.as_profile()
        self.assertEqual(profile.traits["warmth"], "kind but measured")

    def test_growth_changes_prompt(self):
        # Functional-agency assertion: a trait delta must measurably
        # change the rendered system prompt.
        model = _baby()
        before = personality_system_prompt(model.as_profile())
        model.apply_delta({"warmth": GROWTH_RATE})   # 0.5 -> 0.6
        model.apply_delta({"warmth": GROWTH_RATE})   # 0.6 -> 0.7 (bucket)
        model.apply_delta({"curiosity": GROWTH_RATE})
        model.apply_delta({"curiosity": GROWTH_RATE})  # 0.5 -> 0.7 (bucket)
        after = personality_system_prompt(model.as_profile())
        self.assertNotEqual(before, after)
        self.assertIn("genuinely warm and caring", after)
        self.assertIn("deeply curious", after)

    def test_policy_flows_to_never_say(self):
        profile = PersonalityProfile()
        model = PersonalityModel.genesis(profile)
        rendered = model.as_profile()
        self.assertEqual(rendered.speech["never_say"],
                         profile.speech["never_say"])
        prompt = personality_system_prompt(rendered)
        for phrase in profile.speech["never_say"][:2]:
            self.assertIn(phrase, prompt)

    def test_boundaries_flow_through(self):
        model = _baby()
        self.assertEqual(model.as_profile().boundaries,
                         model.policy["boundaries"])

    def test_formality_flips_bucket(self):
        model = _baby()
        self.assertEqual(model.as_profile().speech["formality"], "casual")
        for _ in range(3):
            model.apply_delta({"formality": GROWTH_RATE})  # 0.3 -> 0.6
        self.assertEqual(model.as_profile().speech["formality"], "formal")


if __name__ == "__main__":
    unittest.main()

