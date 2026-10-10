"""Proactivity: the agent speaking unprompted, and what gates it.

`personality.json` carries three switches -- `greet_on_arrival`, `ask_follow_up`,
`offer_help` -- that grant the agent permission to *originate* speech rather than
only answer. They were parsed, merged, and read by nothing, so an operator could
set them and observe no change at all.

Two things are deliberately different about the two that produce an utterance:

  * permission comes from config, and an unknown kind is never a permission;
  * `offer_help` additionally needs the *learned* proactivity trait to clear the
    personality governor's threshold, because volunteering unrequested help is
    exactly what that grown trait governs. A greeting is a social convention the
    operator asked for by name, so permission alone is enough for it.

The gate is passed the switch that granted the line. Getting that wrong is subtle:
`_arrival_line()` used to return only text, and its caller always quoted
`greet_on_arrival` -- so with greetings off and offers on, the offer was built and
then silently rejected by the gate for a switch that was off. `offer_help` looked
wired and did nothing, which is the same class of bug this feature exists to fix.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shugocore_agent as sa  # noqa: E402

from attention_layer import AttentionState  # noqa: E402
from personality.loader import PersonalityProfile  # noqa: E402
from personality.model import PersonalityModel  # noqa: E402

GREETING = "Hello — I'm Shugo. Good to see you."
OFFER = "Shugo here — anything you'd like a hand with?"


class _FakeAttention:
    """Stands in for AttentionLayer: reports one fixed state."""

    def __init__(self, state):
        self._state = state

    def evaluate(self):
        return self._state, 1.0


def _governor(proactivity: float):
    """A real governor whose learned proactivity trait sits at `proactivity`."""
    model = PersonalityModel.genesis(PersonalityProfile())
    steps = round((proactivity - 0.3) / 0.1)
    for _ in range(max(0, steps)):
        model.apply_delta({"proactivity": 0.1})
    from personality.governor import PersonalityGovernor
    return PersonalityGovernor(model)


def _profile(**switches):
    merged = {"greet_on_arrival": True, "ask_follow_up": True, "offer_help": False}
    merged.update(switches)
    profile = PersonalityProfile()
    profile.proactivity = merged
    return profile


def _agent(profile=None, state=AttentionState.UNKNOWN, governor=None,
           attention=True):
    """A bare agent: no boot, no engine, no mesh -- just the gate and the glue."""
    agent = sa.AndroidAgent.__new__(sa.AndroidAgent)
    agent.personality = profile if profile is not None else PersonalityProfile()
    agent.attention = (_FakeAttention(state) if attention else None)
    agent.personality_governor = governor
    agent._last_attention_state = ""
    agent._last_proactive_speak_at = 0.0
    agent.spoken = []
    agent.log = lambda *args, **kwargs: None
    agent._speak_direct = lambda text: (agent.spoken.append(text), True)[1]
    return agent


class ArrivalGreetingTestCase(unittest.TestCase):
    """The `greet_on_arrival` switch, at the one trigger that exists."""

    def test_a_person_arriving_is_greeted_once(self):
        agent = _agent(state=AttentionState.ATTENDING)
        agent._check_arrival_greeting()
        agent._check_arrival_greeting()
        agent._check_arrival_greeting()
        self.assertEqual(agent.spoken, [GREETING])

    def test_nobody_present_says_nothing(self):
        agent = _agent(state=AttentionState.UNKNOWN)
        agent._check_arrival_greeting()
        self.assertEqual(agent.spoken, [])

    def test_a_person_who_is_looking_away_is_not_greeted(self):
        """`diverted` is a person present but not attending -- a back, not a face."""
        agent = _agent(state=AttentionState.DIVERTED)
        agent._check_arrival_greeting()
        self.assertEqual(agent.spoken, [])

    def test_no_attention_layer_means_no_greeting(self):
        agent = _agent(attention=False)
        agent._check_arrival_greeting()
        self.assertEqual(agent.spoken, [])

    def test_the_switch_turns_the_greeting_off(self):
        agent = _agent(profile=_profile(greet_on_arrival=False),
                       state=AttentionState.ATTENDING)
        agent._check_arrival_greeting()
        self.assertEqual(agent.spoken, [])

    def test_a_second_arrival_within_the_cooldown_is_held(self):
        agent = _agent(state=AttentionState.ATTENDING)
        agent._check_arrival_greeting()
        agent.attention = _FakeAttention(AttentionState.DIVERTED)
        agent._check_arrival_greeting()
        agent.attention = _FakeAttention(AttentionState.ATTENDING)
        agent._check_arrival_greeting()
        self.assertEqual(agent.spoken, [GREETING])

    def test_a_broken_attention_layer_is_not_fatal(self):
        class _Broken:
            def evaluate(self):
                raise RuntimeError("camera gone")

        agent = _agent()
        agent.attention = _Broken()
        agent._check_arrival_greeting()          # must not raise
        self.assertEqual(agent.spoken, [])


class OfferHelpTestCase(unittest.TestCase):
    """`offer_help` needs permission *and* the learned trait, then speaks."""

    def test_without_permission_it_stays_quiet(self):
        agent = _agent(profile=_profile(greet_on_arrival=False),
                       state=AttentionState.ATTENDING,
                       governor=_governor(0.5))
        agent._check_arrival_greeting()
        self.assertEqual(agent.spoken, [])

    def test_permission_without_readiness_stays_quiet(self):
        """The learned trait is the other half: 0.30 is under the 0.40 floor."""
        governor = _governor(0.3)
        self.assertFalse(governor.policy["proactivity"]
                         >= governor.policy["proactivity_threshold"])
        agent = _agent(profile=_profile(greet_on_arrival=False, offer_help=True),
                       state=AttentionState.ATTENDING, governor=governor)
        agent._check_arrival_greeting()
        self.assertEqual(agent.spoken, [])

    def test_no_governor_means_not_ready(self):
        agent = _agent(profile=_profile(greet_on_arrival=False, offer_help=True),
                       state=AttentionState.ATTENDING, governor=None)
        self.assertFalse(agent.proactivity_ready())
        agent._check_arrival_greeting()
        self.assertEqual(agent.spoken, [])

    def test_permitted_and_ready_offers_help(self):
        agent = _agent(profile=_profile(greet_on_arrival=False, offer_help=True),
                       state=AttentionState.ATTENDING, governor=_governor(0.5))
        agent._check_arrival_greeting()
        self.assertEqual(agent.spoken, [OFFER])

    def test_the_gate_is_passed_the_switch_that_granted_the_line(self):
        """The bug: an offer quoted `greet_on_arrival` and was rejected by it."""
        agent = _agent(profile=_profile(greet_on_arrival=False, offer_help=True),
                       state=AttentionState.ATTENDING, governor=_governor(0.5))
        calls = []
        agent._proactive_speak = lambda kind, text: (
            calls.append((kind, text)), True)[1]
        agent._check_arrival_greeting()
        self.assertEqual(calls, [("offer_help", OFFER)])

    def test_a_greeting_is_passed_its_own_switch(self):
        agent = _agent(state=AttentionState.ATTENDING)
        calls = []
        agent._proactive_speak = lambda kind, text: (
            calls.append((kind, text)), True)[1]
        agent._check_arrival_greeting()
        self.assertEqual(calls, [("greet_on_arrival", GREETING)])

    def test_a_greeting_needs_no_readiness(self):
        """Permission only: the operator asked for greetings by name."""
        agent = _agent(state=AttentionState.ATTENDING, governor=_governor(0.3))
        agent._check_arrival_greeting()
        self.assertEqual(agent.spoken, [GREETING])


class PermissionTestCase(unittest.TestCase):
    """Permission is read from config and is fail-closed."""

    def test_an_unknown_switch_is_never_a_permission(self):
        agent = _agent()
        for kind in ("offerHelp", "greet", "", "GREET_ON_ARRIVAL", None):
            with self.subTest(kind=kind):
                self.assertFalse(agent.proactive_permitted(kind))

    def test_the_three_known_switches_are_read(self):
        agent = _agent(profile=_profile(greet_on_arrival=True,
                                        ask_follow_up=False, offer_help=True))
        self.assertTrue(agent.proactive_permitted("greet_on_arrival"))
        self.assertFalse(agent.proactive_permitted("ask_follow_up"))
        self.assertTrue(agent.proactive_permitted("offer_help"))

    def test_a_profile_missing_the_block_is_read_as_the_defaults(self):
        """A profile from before the block existed still answers, not crashes."""
        profile = PersonalityProfile()
        del profile.proactivity
        agent = _agent(profile=profile)
        self.assertTrue(agent.proactive_permitted("greet_on_arrival"))
        self.assertFalse(agent.proactive_permitted("offer_help"))

    def test_readiness_follows_the_governors_own_threshold(self):
        ready = _agent(governor=_governor(0.5))
        not_ready = _agent(governor=_governor(0.3))
        self.assertTrue(ready.proactivity_ready())
        self.assertFalse(not_ready.proactivity_ready())

    def test_readiness_is_false_when_the_governor_raises(self):
        class _Broken:
            @property
            def policy(self):
                raise RuntimeError("no model")

        agent = _agent()
        agent.personality_governor = _Broken()
        self.assertFalse(agent.proactivity_ready())


class TheTickCallsItTestCase(unittest.TestCase):
    """The behaviour has to be reached: a gate nobody calls is dead again."""

    def test_tick_runs_the_arrival_check(self):
        import inspect
        source = inspect.getsource(sa.AndroidAgent.tick)
        self.assertIn("_check_arrival_greeting()", source)


if __name__ == "__main__":
    unittest.main()
