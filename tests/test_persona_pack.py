"""The persona pack contract: what may be adopted, and what may not.

ShugoCore does not train personality models; it accepts them. These tests pin the
boundary: a versioned, hash-pinned artifact can bring a character, and it can never
bring a *weakening* of the node it lands on.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from personality.loader import PersonalityProfile  # noqa: E402
from personality.model import PersonalityModel  # noqa: E402
from personality.persona_pack import (PERSONA_SCHEMA, PersonaError,  # noqa: E402
                                     birth, compute_digest, from_dict, load)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFERENCE_PACK = os.path.join(REPO_ROOT, "config", "personas",
                              "shugo-foundation.json")


def _pack(**overrides):
    """A minimal valid pack, with any field overridden for a case."""
    data = {
        "schema": PERSONA_SCHEMA,
        "persona_id": "test-persona",
        "version": "1.2.3",
        "name": "Shugo",
        "traits": {"warmth": 0.7, "curiosity": 0.6},
        "licence": {"spdx": "MIT", "redistributable": True},
    }
    data.update(overrides)
    return data


class ValidationTestCase(unittest.TestCase):
    """Refusals, each naming the reason it refused."""

    def _refusal(self, data):
        with self.assertRaises(PersonaError) as caught:
            from_dict(data)
        return caught.exception.reason

    def test_a_minimal_pack_is_accepted(self):
        pack = from_dict(_pack())
        self.assertEqual(pack.identity, "test-persona@1.2.3")
        self.assertEqual(pack.traits["warmth"].value, 0.7)
        self.assertTrue(pack.may_redistribute)

    def test_an_unknown_schema_is_refused_by_name(self):
        reason = self._refusal(_pack(schema="shugocore.persona/99"))
        self.assertIn("shugocore.persona/99", reason)
        self.assertIn(PERSONA_SCHEMA, reason)

    def test_a_missing_schema_is_refused(self):
        data = _pack()
        del data["schema"]
        self.assertIn("schema", self._refusal(data))

    def test_an_unusable_persona_id_is_refused(self):
        self.assertIn("persona_id", self._refusal(_pack(persona_id="Not A Slug")))

    def test_a_missing_version_is_refused(self):
        data = _pack()
        del data["version"]
        self.assertIn("version", self._refusal(data))

    def test_a_trait_outside_the_unit_interval_is_refused(self):
        self.assertIn("[0, 1]", self._refusal(_pack(traits={"warmth": 1.4})))
        self.assertIn("[0, 1]", self._refusal(_pack(traits={"warmth": -0.2})))

    def test_a_non_numeric_trait_is_refused(self):
        self.assertIn("warmth",
                      self._refusal(_pack(traits={"warmth": "very warm"})))

    def test_an_unusable_trait_name_is_refused(self):
        self.assertIn("trait name", self._refusal(_pack(traits={"Warmth!": 0.5})))

    def test_a_trait_block_that_is_not_an_object_is_refused(self):
        self.assertIn("traits", self._refusal(_pack(traits=[0.5])))

    def test_content_that_does_not_match_its_hash_is_refused(self):
        data = _pack()
        data["hash"] = "0" * 64
        self.assertIn("does not match its own hash", self._refusal(data))

    def test_a_correct_hash_is_accepted(self):
        data = _pack()
        data["hash"] = compute_digest(data)
        self.assertEqual(from_dict(data).digest, data["hash"])

    def test_a_weight_reference_without_a_uri_is_refused(self):
        self.assertIn("uri", self._refusal(
            _pack(voice_model={"kind": "lora-adapter", "sha256": "ab"})))

    def test_a_weight_reference_is_accepted_and_recorded(self):
        data = _pack(voice_model={"kind": "lora-adapter",
                                  "uri": "hf://acme/shugo-voice-v1",
                                  "sha256": "ab" * 32, "base": "qwen2.5-0.5b",
                                  "licence": "apache-2.0"})
        meta = from_dict(data).meta()
        self.assertEqual(meta["voice_model"]["kind"], "lora-adapter")
        self.assertEqual(meta["voice_model"]["uri"], "hf://acme/shugo-voice-v1")

    def test_a_non_object_licence_is_refused(self):
        self.assertIn("licence", self._refusal(_pack(licence="MIT")))

    def test_a_pack_that_is_not_an_object_is_refused(self):
        self.assertIn("must be an object", self._refusal(["not", "a", "pack"]))

    def test_a_non_redistributable_licence_is_flagged_not_refused(self):
        """Running one locally is the operator's business; shipping one is not."""
        pack = from_dict(_pack(licence={"spdx": "CC-BY-NC-4.0",
                                        "redistributable": False}))
        self.assertFalse(pack.may_redistribute)

    def test_load_reports_a_missing_file_without_a_traceback(self):
        with self.assertRaises(PersonaError) as caught:
            load(os.path.join(tempfile.gettempdir(), "no-such-persona.json"))
        self.assertIn("cannot read", caught.exception.reason)

    def test_load_reports_invalid_json_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaises(PersonaError) as caught:
                load(path)
        self.assertIn("not valid JSON", caught.exception.reason)


class ShippedPacksTestCase(unittest.TestCase):
    """Every pack in the tree must validate — and must be shippable."""

    def test_the_reference_pack_validates(self):
        pack = load(REFERENCE_PACK)
        self.assertEqual(pack.persona_id, "shugo-foundation")
        self.assertEqual(pack.declared_hash, pack.digest,
                         "the shipped pack's pinned hash must match its content")

    def test_every_shipped_pack_is_redistributable(self):
        """MODELS.md's rule, enforced: nothing NC-licensed gets committed here."""
        directory = os.path.join(REPO_ROOT, "config", "personas")
        offenders = []
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json"):
                continue
            pack = load(os.path.join(directory, name))
            if not pack.may_redistribute:
                offenders.append(f"{name} ({pack.licence.get('spdx')})")
        self.assertEqual(offenders, [],
                         "non-redistributable packs committed to an MIT repository: "
                         + ", ".join(offenders))

    def test_the_reference_pack_declares_provenance_and_licence(self):
        """A released model says where it came from; ours must model that."""
        pack = load(REFERENCE_PACK)
        self.assertTrue(pack.provenance.get("produced_by"))
        self.assertTrue(pack.licence.get("spdx"))


class AdoptionTestCase(unittest.TestCase):
    """What a pack may change on a node, and what it may not."""

    def _birth(self, local=None, **overrides):
        pack = from_dict(_pack(**overrides))
        return birth(pack, local or PersonalityProfile())

    def test_adoption_sets_the_trait_vector(self):
        model, report = self._birth(traits={"warmth": 0.9, "verbosity": 0.2})
        self.assertEqual(model.traits["warmth"].value, 0.9)
        self.assertEqual(model.traits["verbosity"].value, 0.2)
        self.assertEqual(sorted(report["traits"]), ["verbosity", "warmth"])
        self.assertEqual(report["added_traits"], [])

    def test_an_unknown_trait_is_kept_and_flagged(self):
        """The open vocabulary: a trainer may learn a trait this build never had."""
        model, report = self._birth(traits={"playfulness": 0.35})
        self.assertIn("playfulness", model.traits)
        self.assertEqual(model.traits["playfulness"].value, 0.35)
        self.assertEqual(report["added_traits"], ["playfulness"])
        self.assertTrue(any("not one this build reasons about" in w
                            for w in report["warnings"]), report["warnings"])

    def test_generation_starts_at_zero_for_an_adopted_character(self):
        model, _ = self._birth()
        self.assertEqual(model.generation, 0)

    def test_the_history_says_adopted_not_born(self):
        """A trained character must not have a blank birth recorded."""
        model, _ = self._birth()
        self.assertIn("adopted persona test-persona@1.2.3",
                      model.history[0]["note"])

    def test_the_model_records_which_pack_it_came_from(self):
        model, _ = self._birth()
        self.assertEqual(model.persona["id"], "test-persona")
        self.assertEqual(model.persona["version"], "1.2.3")
        self.assertTrue(model.persona["digest"])

    def test_the_persona_record_survives_a_save_and_load(self):
        model, _ = self._birth()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "personality_model.json")
            model.save(path)
            reloaded = PersonalityModel.load(path)
        self.assertEqual(reloaded.persona["id"], "test-persona")

    def test_never_say_is_unioned_never_replaced(self):
        local = PersonalityProfile()
        local.speech["never_say"] = ["Local rule"]
        model, report = self._birth(local=local,
                                    speech={"never_say": ["Pack rule"]})
        phrases = model.policy["never_say"]
        self.assertIn("Local rule", phrases)
        self.assertIn("Pack rule", phrases)
        self.assertIn("never_say += ['Pack rule']", report["reinforced"])

    def test_a_pack_cannot_relax_a_boundary(self):
        """The load-bearing refusal: an artifact may not widen its own perimeters."""
        model, report = self._birth(
            policy={"boundaries": {"refuse_harmful": False}})
        self.assertTrue(model.policy["boundaries"]["refuse_harmful"],
                        "refuse_harmful must survive a pack asking for false")
        self.assertTrue(any("cannot relax a boundary" in item
                            for item in report["refused"]), report["refused"])

    def test_a_pack_can_reinforce_a_boundary(self):
        local = PersonalityProfile()
        local.boundaries["honest_about_limitations"] = False
        model, report = self._birth(
            local=local,
            policy={"boundaries": {"honest_about_limitations": True}})
        self.assertTrue(model.policy["boundaries"]["honest_about_limitations"])
        self.assertIn("boundaries.honest_about_limitations = true",
                      report["reinforced"])

    def test_a_pack_cannot_clear_refuse_harmful_at_top_level(self):
        model, _ = self._birth(policy={"refuse_harmful": False})
        self.assertTrue(model.policy["refuse_harmful"])

    def test_a_pack_cannot_change_proactivity_permissions(self):
        """Permissions are the operator's grant, not an artifact's."""
        local = PersonalityProfile()          # greet on, offer off
        model, report = self._birth(
            local=local,
            proactivity={"greet_on_arrival": False, "offer_help": True})
        rendered = model.as_profile()
        self.assertTrue(rendered.proactivity["greet_on_arrival"])
        self.assertFalse(rendered.proactivity["offer_help"])
        self.assertTrue(any("proactivity" in item for item in report["refused"]),
                        report["refused"])

    def test_a_pack_sets_the_wording_and_the_voice(self):
        model, report = self._birth(
            voice={"max_sentences": 2, "style": "clipped and precise"},
            speech={"greeting": "Evening, {name}.", "offer": "Want a hand?"})
        rendered = model.as_profile()
        self.assertEqual(rendered.speech["greeting"], "Evening, {name}.")
        self.assertEqual(rendered.speech["offer"], "Want a hand?")
        self.assertEqual(rendered.speech["max_sentences"], 2)
        self.assertEqual(rendered.speech["style"], "clipped and precise")
        self.assertEqual(sorted(report["wording"]), ["greeting", "offer"])

    def test_an_adopted_character_reaches_the_prompt(self):
        model, _ = self._birth(traits={"warmth": 0.95})
        profile = model.as_profile()
        self.assertIn("genuinely warm and caring",
                      " ".join(profile.traits.values()))

    def test_a_non_redistributable_pack_warns_on_adoption(self):
        _, report = self._birth(
            licence={"spdx": "CC-BY-NC-4.0", "redistributable": False})
        self.assertTrue(any("must not be committed" in w
                            for w in report["warnings"]), report["warnings"])

    def test_adoption_leaves_the_report_answerable(self):
        """The report is what a node's log answers 'what did this change?' with."""
        _, report = self._birth(traits={"warmth": 0.88}, voice={"max_sentences": 4})
        self.assertEqual(report["pack"], "test-persona@1.2.3")
        self.assertTrue(report["digest"])
        self.assertIn("warmth", report["traits"])
        self.assertIn("max_sentences", report["voice"])


class TheReferencePackAdoptionTestCase(unittest.TestCase):
    """The shipped pack, adopted for real."""

    def test_the_reference_persona_is_adoptable(self):
        pack = load(REFERENCE_PACK)
        model, report = birth(pack, PersonalityProfile())
        self.assertEqual(model.traits["warmth"].value, 0.62)
        self.assertEqual(model.traits["playfulness"].value, 0.35)
        self.assertEqual(report["added_traits"], ["playfulness"])
        self.assertEqual(model.persona["id"], "shugo-foundation")
        # The pack's own never_say arrives, and the node keeps its local ones.
        self.assertIn("As a language model", model.policy["never_say"])
        self.assertIn("I'm just an AI", model.policy["never_say"])


if __name__ == "__main__":
    unittest.main()
