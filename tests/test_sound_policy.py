"""Consent for the microphone: the one sensor whose misuse an operator cannot see.

A node that listens and says nothing looks exactly like a node that is switched off, so the
policy answer has to be explicit, has to default to the behaviour the fleet already has, and
must be impossible for the agent to grant itself.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from policy import (MIC_CONSENT_ACTION, MICROPHONE_STATES, SOUND_MODES,  # noqa: E402
                    CapabilityRegistry, ConsentRegistry, sound_listen_decision)


def _granted():
    consent = ConsentRegistry()
    consent.grant(MIC_CONSENT_ACTION, granted_by="operator", scope="node")
    return consent


class TestPolicyVocabulary(unittest.TestCase):
    def test_the_microphone_is_a_known_sensor_topic(self):
        self.assertIn("microphone", CapabilityRegistry().mobile_sensor_topics)

    def test_policy_mirrors_the_contract_vocabulary_exactly(self):
        from sound.schema import KNOWN_LISTEN_MODES, KNOWN_MIC_STATES
        self.assertEqual(tuple(KNOWN_MIC_STATES), MICROPHONE_STATES)
        self.assertEqual(tuple(KNOWN_LISTEN_MODES), SOUND_MODES)

    def test_sound_perception_is_off_by_default(self):
        caps = CapabilityRegistry()
        self.assertFalse(caps.sound_enabled)
        self.assertIn(caps.sound_listen_mode, SOUND_MODES)


class TestConsentGating(unittest.TestCase):
    def test_nothing_configured_defaults_to_speech(self):
        mode, reason = sound_listen_decision(CapabilityRegistry(), ConsentRegistry())
        self.assertEqual(mode, "speech")
        self.assertIn("configuration", reason)

    def test_configuration_alone_is_not_consent(self):
        mode, reason = sound_listen_decision(
            CapabilityRegistry({"sound_enabled": True}), ConsentRegistry())
        self.assertEqual(mode, "speech")
        self.assertIn(MIC_CONSENT_ACTION, reason)

    def test_consent_alone_is_not_configuration(self):
        mode, reason = sound_listen_decision(CapabilityRegistry(), _granted())
        self.assertEqual(mode, "speech")
        self.assertIn("configuration", reason)

    def test_both_together_allow_sound(self):
        caps = CapabilityRegistry({"sound_enabled": True, "sound_listen_mode": "sound"})
        mode, reason = sound_listen_decision(caps, _granted())
        self.assertEqual(mode, "sound")
        self.assertIn("consent", reason)

    def test_an_expired_grant_stops_counting_immediately(self):
        consent = ConsentRegistry()
        consent.grant(MIC_CONSENT_ACTION, granted_by="operator", ttl_seconds=-1)
        caps = CapabilityRegistry({"sound_enabled": True})
        self.assertEqual(sound_listen_decision(caps, consent)[0], "speech")

    def test_a_revoked_grant_stops_counting_immediately(self):
        consent = _granted()
        consent.revoke(MIC_CONSENT_ACTION)
        caps = CapabilityRegistry({"sound_enabled": True})
        self.assertEqual(sound_listen_decision(caps, consent)[0], "speech")

    def test_off_is_honoured_even_with_consent(self):
        caps = CapabilityRegistry({"sound_enabled": True, "sound_listen_mode": "off"})
        self.assertEqual(sound_listen_decision(caps, _granted())[0], "off")

    def test_a_failing_or_missing_registry_fails_closed(self):
        class _Broken:
            sound_enabled = True
            sound_listen_mode = "sound"

            def has_grant(self, *_args):
                raise RuntimeError("registry exploded")

        self.assertEqual(sound_listen_decision(_Broken(), _Broken())[0], "speech")
        self.assertEqual(sound_listen_decision(None, None)[0], "speech")
        self.assertEqual(sound_listen_decision(_Broken(), None)[0], "speech")

    def test_an_unknown_configured_mode_is_refused_not_passed_on(self):
        caps = CapabilityRegistry({"sound_enabled": True, "sound_listen_mode": "broadcast"})
        self.assertIn(caps.sound_listen_mode, SOUND_MODES)  # normalised on construction
        caps.sound_listen_mode = "broadcast"                # ...or corrupted later
        mode, reason = sound_listen_decision(caps, _granted())
        self.assertEqual(mode, "speech")
        self.assertIn("unknown", reason)


class _Analyzer:
    """The SoundAnalyzerBridge surface, as far as the agent uses it."""

    def __init__(self, mode="speech", raises=False):
        self.mode = mode
        self.raises = raises
        self.set_calls = []

    def listenMode(self):
        return self.mode

    def setListenMode(self, mode):
        if self.raises:
            raise RuntimeError("runtime too old to switch modes")
        self.set_calls.append(mode)
        self.mode = mode
        return mode


def _agent(caps, consent, analyzer):
    """An AndroidAgent with only the wiring this feature touches (no full construction)."""
    from shugocore_agent import AndroidAgent
    agent = object.__new__(AndroidAgent)
    agent._sound_analyzer = analyzer
    agent.capability_registry = caps
    agent.consent_registry = consent
    agent.logs = []
    agent.log = lambda category, message, level="INFO": agent.logs.append((category, message))
    return agent


class TestAgentAppliesPolicy(unittest.TestCase):
    def test_registration_applies_the_decision_to_the_device(self):
        caps = CapabilityRegistry({"sound_enabled": True})
        agent = _agent(caps, _granted(), _Analyzer(mode="speech"))
        self.assertEqual(agent.apply_sound_policy(), "sound")
        self.assertEqual(agent._sound_analyzer.set_calls, ["sound"])

    def test_the_fleet_default_causes_no_write_at_all(self):
        analyzer = _Analyzer(mode="speech")
        agent = _agent(CapabilityRegistry(), ConsentRegistry(), analyzer)
        self.assertEqual(agent.apply_sound_policy(), "speech")
        self.assertEqual(analyzer.set_calls, [])

    def test_a_runtime_that_cannot_switch_does_not_take_the_agent_down(self):
        caps = CapabilityRegistry({"sound_enabled": True})
        analyzer = _Analyzer(mode="speech", raises=True)
        agent = _agent(caps, _granted(), analyzer)
        self.assertEqual(agent.apply_sound_policy(), "speech")
        self.assertTrue(any("could not set" in message for _c, message in agent.logs))

    def test_a_consent_file_is_what_lets_an_operator_enable_sound(self):
        """The agent cannot grant itself consent; an operator can write it down."""
        import json
        import tempfile
        path = tempfile.mkdtemp(prefix="shugo_consent_")
        with open(os.path.join(path, "sound_consent.json"), "w", encoding="utf-8") as handle:
            json.dump({"sound_enabled": True, "listen_mode": "sound",
                       "granted_by": "operator"}, handle)
        agent = _agent(CapabilityRegistry(), ConsentRegistry(), _Analyzer(mode="speech"))
        agent.data_dir = path
        self.assertEqual(agent.apply_sound_policy(), "sound")
        self.assertEqual(agent._sound_analyzer.set_calls, ["sound"])

    def test_a_malformed_consent_file_changes_nothing(self):
        import tempfile
        path = tempfile.mkdtemp(prefix="shugo_consent_")
        with open(os.path.join(path, "sound_consent.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        agent = _agent(CapabilityRegistry(), ConsentRegistry(), _Analyzer(mode="speech"))
        agent.data_dir = path
        self.assertEqual(agent.apply_sound_policy(), "speech")
        self.assertEqual(agent._sound_analyzer.set_calls, [])
        self.assertTrue(any("unusable consent file" in message for _c, message in agent.logs))

    def test_a_consent_file_without_a_granter_gives_nothing(self):
        """Keys without an external actor behind them are not consent."""
        import json
        import tempfile
        path = tempfile.mkdtemp(prefix="shugo_consent_")
        with open(os.path.join(path, "sound_consent.json"), "w", encoding="utf-8") as handle:
            json.dump({"sound_enabled": True, "listen_mode": "sound"}, handle)
        agent = _agent(CapabilityRegistry(), ConsentRegistry(), _Analyzer(mode="speech"))
        agent.data_dir = path
        self.assertEqual(agent.apply_sound_policy(), "speech")
        self.assertEqual(agent._sound_analyzer.set_calls, [])

    def test_a_granted_operator_can_hand_over_the_mic_live(self):
        agent = _agent(CapabilityRegistry(), ConsentRegistry(), _Analyzer(mode="speech"))
        self.assertEqual(agent.set_sound_mode("sound", granted_by="operator"), "sound")
        self.assertEqual(agent._sound_analyzer.set_calls, ["sound"])
        self.assertTrue(agent.consent_registry.has_grant(MIC_CONSENT_ACTION))

    def test_the_live_switch_refuses_without_an_external_actor(self):
        """No granter means no grant, and no device write -- the doctrine, kept live."""
        agent = _agent(CapabilityRegistry(), ConsentRegistry(), _Analyzer(mode="speech"))
        self.assertEqual(agent.set_sound_mode("sound"), "speech")
        self.assertEqual(agent._sound_analyzer.set_calls, [])
        self.assertFalse(agent.consent_registry.has_grant(MIC_CONSENT_ACTION))
        self.assertTrue(any("granted_by" in message for _c, message in agent.logs))

    def test_an_unknown_live_mode_is_refused(self):
        agent = _agent(CapabilityRegistry(), ConsentRegistry(), _Analyzer(mode="speech"))
        self.assertEqual(agent.set_sound_mode("broadcast", granted_by="operator"), "speech")
        self.assertEqual(agent._sound_analyzer.set_calls, [])

    def test_status_says_why_the_layer_is_idle(self):
        import json
        agent = _agent(CapabilityRegistry(), ConsentRegistry(), _Analyzer(mode="speech"))
        agent.data_dir = None
        status = json.loads(agent.sound_listen_status_json())
        self.assertEqual(status["policy_mode"], "speech")
        self.assertIn("configuration", status["policy_reason"])
        self.assertFalse(status["consent_granted"])
        self.assertEqual(status["device"]["listen_mode"], "speech")

    def test_status_reflects_an_operator_handover(self):
        import json
        agent = _agent(CapabilityRegistry(), ConsentRegistry(), _Analyzer(mode="speech"))
        agent.data_dir = None
        agent.set_sound_mode("sound", granted_by="operator")
        status = json.loads(agent.sound_listen_status_json())
        self.assertEqual(status["policy_mode"], "sound")
        self.assertTrue(status["consent_granted"])
        self.assertEqual(status["device"]["listen_mode"], "sound")

    def test_registration_is_what_triggers_it(self):
        src = open(os.path.join(ROOT, "shugocore_agent.py"), encoding="utf-8").read()
        body = src[src.index("def register_sound_analyzer"):]
        body = body[:body.index("def apply_sound_policy")]
        self.assertIn("self._sound_analyzer = analyzer", body)
        self.assertIn("self.apply_sound_policy()", body)

    def test_the_agent_has_no_path_to_consent_itself(self):
        """The doctrine: grants come from outside.

        The decision path may never write one. The consent-file loader may -- on the
        operator's behalf -- but only when that file names who granted it, which is what the
        behavioural tests around it pin down.
        """
        src = open(os.path.join(ROOT, "shugocore_agent.py"), encoding="utf-8").read()
        decision = src[src.index("def apply_sound_policy"):]
        decision = decision[:decision.index("def set_sound_mode")]
        self.assertNotIn(".grant(", decision)

        # The live switch may record one, but only for a named external actor.
        switch = src[src.index("def set_sound_mode"):]
        switch = switch[:switch.index("def sound_listen_status_json")]
        self.assertIn("if not granter:", switch)
        self.assertLess(switch.index("if not granter:"), switch.index(".grant("))

        loader = src[src.index("def _load_sound_consent"):]
        loader = loader[:loader.index("def _sound_mode_decision")]
        self.assertIn(".grant(", loader)
        self.assertIn('if payload.get("granted_by"):', loader)
        self.assertLess(loader.index('if payload.get("granted_by"):'),
                        loader.index(".grant("),
                        "the granter must be required before any grant is recorded")


if __name__ == "__main__":
    unittest.main()
