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

    def test_registration_is_what_triggers_it(self):
        src = open(os.path.join(ROOT, "shugocore_agent.py"), encoding="utf-8").read()
        body = src[src.index("def register_sound_analyzer"):]
        body = body[:body.index("def apply_sound_policy")]
        self.assertIn("self._sound_analyzer = analyzer", body)
        self.assertIn("self.apply_sound_policy()", body)

    def test_the_agent_has_no_path_to_consent_itself(self):
        """The doctrine: grants come from outside. Nothing in the mic path writes one."""
        src = open(os.path.join(ROOT, "shugocore_agent.py"), encoding="utf-8").read()
        body = src[src.index("def apply_sound_policy"):]
        body = body[:body.index("def _log_speak_failure")]
        self.assertNotIn(".grant(", body)
        tail = src[src.index("def register_sound_analyzer"):]
        self.assertNotIn("consent_registry.grant", tail)


if __name__ == "__main__":
    unittest.main()
