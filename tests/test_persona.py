"""Phrasing as a service: the persona model shapes speech and nothing else.

The properties these tests exist for are the ones a second model could quietly break:
it must never approve anything (it runs before the gate), it must never cost the hive
its voice (every failure returns the draft), and it must never touch anything but speech.
"""
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import decision_engine as de  # noqa: E402
import persona  # noqa: E402


class _Recorder:
    """A stand-in for the HTTP POST that records what it was asked and answers."""

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.reply


def _reply(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class _OpenConnection:
    """What a successful ``connect()`` returns: a context manager that closes quietly."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class PersonaShaperTestCase(unittest.TestCase):
    @staticmethod
    def _shaper(reply=None, error=None, url="mac:11434"):
        """A shaper whose model is *named*, so no test here asks the endpoint for one.

        An empty model is the documented "ask the endpoint which model it has" path, and
        that is a network call: it made these tests depend on this fleet's LAN answering
        at :11434. Off-LAN they failed with "the endpoint lists no model to ask" instead
        of whatever the test injected -- and, worse, two of them passed without ever
        reaching the poster they were built around.
        """
        return persona.PersonaShaper(url, "persona-under-test",
                                     poster=_Recorder(reply=reply, error=error))

    def test_a_node_without_a_persona_speaks_its_own_draft(self):
        shaper = persona.PersonaShaper()
        self.assertFalse(shaper.enabled)
        self.assertEqual(shaper.label(), "off")
        result = shaper.shape("the capital of France is Paris")
        self.assertEqual(result["source"], "draft")
        self.assertEqual(result["text"], "the capital of France is Paris")

    def test_a_shaped_line_keeps_the_draft_it_came_from(self):
        poster = _Recorder(reply=_reply("Paris, of course."))
        shaper = persona.PersonaShaper("192.168.1.162:11434", "mac-persona",
                                       poster=poster, instructions="You are Shugo.")
        result = shaper.shape("the capital of France is Paris",
                              verdict={"tone": "warm", "max_sentences": 1})
        self.assertEqual(result["source"], "persona")
        self.assertEqual(result["text"], "Paris, of course.")
        self.assertEqual(result["draft"], "the capital of France is Paris")
        self.assertEqual(shaper.shaped, 1)
        self.assertEqual(shaper.label(), "mac-persona@192.168.1.162:11434")
        # The endpoint, the character and the governor's verdict all reached the model.
        sent = poster.calls[0]
        self.assertEqual(sent["url"],
                         "http://192.168.1.162:11434/v1/chat/completions")
        system = sent["payload"]["messages"][0]["content"]
        self.assertIn("You are Shugo.", system)
        self.assertIn("tone: warm", system)
        self.assertEqual(sent["payload"]["messages"][1]["content"],
                         "the capital of France is Paris")
        self.assertEqual(sent["payload"]["model"], "mac-persona")

    def test_an_unreachable_persona_speaks_the_draft_and_says_why(self):
        shaper = self._shaper(error=OSError("connection refused"))
        result = shaper.shape("the capital of France is Paris")
        self.assertEqual(result["source"], "unavailable")
        self.assertEqual(result["text"], "the capital of France is Paris")
        self.assertIn("connection refused", result["reason"])
        self.assertEqual(shaper.fallbacks, 1)

    def test_an_empty_completion_is_a_failure_not_an_empty_line(self):
        shaper = self._shaper(reply=_reply(""))
        result = shaper.shape("something to say")
        self.assertEqual(result["source"], "unavailable")
        self.assertEqual(result["text"], "something to say")

    def test_a_reply_with_no_choices_is_not_read_as_text(self):
        shaper = self._shaper(reply={})
        self.assertEqual(shaper.shape("something to say")["source"], "unavailable")

    def test_endpoint_forms_are_normalised(self):
        self.assertEqual(persona._chat_endpoint(""), "")
        self.assertEqual(persona._chat_endpoint("mac:11434"),
                         "http://mac:11434/v1/chat/completions")
        self.assertEqual(persona._chat_endpoint("http://mac:11434/"),
                         "http://mac:11434/v1/chat/completions")
        self.assertEqual(
            persona._chat_endpoint("http://mac:11434/v1/chat/completions"),
            "http://mac:11434/v1/chat/completions")

    def test_quoted_replies_are_unquoted_and_other_shapes_are_read(self):
        self.assertEqual(persona._first_message([{"message": {"content": '"hi"'}}]),
                         "hi")
        self.assertEqual(persona._first_message([{"text": " hi "}]), "hi")
        self.assertEqual(persona._first_message([]), "")
        self.assertEqual(persona._first_message(None), "")


class PersonaInDecisionTestCase(unittest.TestCase):
    """Where it sits in the path: after the governor, before the gate, speech only."""

    def _engine(self, shaper):
        engine = object.__new__(de.DecisionEngine)
        engine.persona_shaper = shaper
        engine.personality_governor = None      # a fleet without a governor still speaks
        return engine

    def _shaper(self, reply=None, error=None):
        # The model is *named* on purpose. Leaving it empty is the documented
        # "ask the endpoint which model it has" path, which is a network call --
        # it made these tests depend on this fleet's LAN, and on CI they failed
        # with "the endpoint lists no model to ask" instead of the injected error.
        # Discovery is the subject of PersonaModelChoiceTestCase below, on its own.
        return persona.PersonaShaper("mac:11434", "persona-under-test",
                                     poster=_Recorder(reply=reply, error=error))

    def test_a_speech_decision_is_phrased_and_both_drafts_are_recorded(self):
        engine = self._engine(self._shaper(reply=_reply("Paris, of course.")))
        decision = {"action_type": "speak", "params": {"text": "Paris is the capital"}}
        out = engine._apply_persona(decision, {})
        self.assertEqual(out["params"]["text"], "Paris, of course.")
        self.assertEqual(out["persona"]["source"], "persona")
        self.assertEqual(out["persona"]["draft"], "Paris is the capital")

    def test_a_tool_action_is_never_phrased(self):
        """The safety-relevant half: only speech goes near the persona model."""
        poster = _Recorder(reply=_reply("should not be used"))
        engine = self._engine(persona.PersonaShaper("mac:11434", poster=poster))
        decision = {"action_type": "api_call", "params": {"text": "not speech"}}
        out = engine._apply_persona(decision, {})
        self.assertNotIn("persona", out)
        self.assertEqual(out["params"]["text"], "not speech")
        self.assertEqual(poster.calls, [])

    def test_the_tool_path_stays_unshaped_even_for_speech(self):
        """advisory_only means "never modify" -- the persona respects that too."""
        poster = _Recorder(reply=_reply("should not be used"))
        engine = self._engine(persona.PersonaShaper("mac:11434", poster=poster))
        decision = {"action_type": "speak", "params": {"text": "leave me alone"}}
        out = engine._apply_persona(decision, {}, advisory_only=True)
        self.assertNotIn("persona", out)
        self.assertEqual(poster.calls, [])

    def test_an_unavailable_persona_leaves_the_line_alone(self):
        engine = self._engine(self._shaper(error=TimeoutError("timed out")))
        decision = {"action_type": "speak", "params": {"text": "still spoken"}}
        out = engine._apply_persona(decision, {})
        self.assertEqual(out["params"]["text"], "still spoken")
        self.assertEqual(out["persona"]["source"], "unavailable")
        self.assertIn("timed out", out["persona"]["reason"])

    def test_no_shaper_changes_nothing(self):
        engine = self._engine(None)
        decision = {"action_type": "speak", "params": {"text": "unchanged"}}
        self.assertEqual(engine._apply_persona(decision, {}), decision)

    def test_a_shaper_that_raises_is_style_only(self):
        class _Broken:
            enabled = True

            @staticmethod
            def label():
                return "broken@nowhere"

            @staticmethod
            def shape(*_args, **_kwargs):
                raise RuntimeError("bad shaper")

        engine = self._engine(_Broken())
        decision = {"action_type": "speak", "params": {"text": "still spoken"}}
        out = engine._apply_persona(decision, {})
        self.assertEqual(out["params"]["text"], "still spoken")
        self.assertEqual(out["persona"]["source"], "unavailable")


def _desktop_module():
    """The desktop script, loaded the way its own test file loads it."""
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "scripts"))       # its sibling imports live there
    spec = importlib.util.spec_from_file_location(
        "desktop_agent_under_test", root / "scripts" / "desktop_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OperatorLineTestCase(unittest.TestCase):
    """An operator's line goes through the same persona step a decided one does."""

    def _agent(self, engine):
        return types.SimpleNamespace(engine=engine)

    def test_the_operator_line_is_phrased_by_the_engine_step(self):
        class _Engine:
            @staticmethod
            def _apply_persona(decision, _task, _advisory_only=False):
                decision = dict(decision)
                decision["params"] = dict(decision["params"], text="shaped by persona")
                decision["persona"] = {"source": "persona", "label": "m@host"}
                return decision

        module = _desktop_module()
        self.assertEqual(module._phrase_line(self._agent(_Engine()), "as typed"),
                         "shaped by persona")

    def test_without_a_phrasing_step_the_line_is_spoken_as_typed(self):
        module = _desktop_module()
        self.assertEqual(module._phrase_line(self._agent(None), "as typed"),
                         "as typed")

    def test_a_broken_phrasing_step_still_speaks_the_line(self):
        class _Engine:
            @staticmethod
            def _apply_persona(*_args, **_kwargs):
                raise RuntimeError("bad shaper")

        module = _desktop_module()
        self.assertEqual(module._phrase_line(self._agent(_Engine()), "as typed"),
                         "as typed")


class PersonaResolutionTestCase(unittest.TestCase):
    """``--persona-url auto``: found when it appears, kept when it goes quiet."""

    def _agent(self, peers):
        return types.SimpleNamespace(
            mesh_election=types.SimpleNamespace(live_peers=lambda: peers))

    def _mac(self, locator="http://192.168.1.162:11434"):
        return {"node_id": "shugo-mac", "mem_available_bytes": 1_900_000_000,
                "caps": {"persona": locator}}

    @staticmethod
    def _probe(reachable=True):
        """A stand-in for the socket probe, so resolution never needs the LAN.

        The real probe is a connect to the advertised locator -- the thing that makes
        "advertised is not usable" true. Answering it here keeps each test about the
        *rule* (adopt a live locator, never adopt a dead one) rather than about
        whether the Mac happens to be awake, which is how these passed on the bench
        and failed on a runner.
        """
        def connect(_address, timeout=None, **_kwargs):
            if not reachable:
                raise OSError("connection refused")
            return _OpenConnection()
        return connect

    def test_it_points_the_shaper_at_the_peer_that_advertises_it(self):
        module = _desktop_module()
        shaper = persona.PersonaShaper("", "mac-persona")
        self.assertFalse(shaper.enabled)
        changed = module._resolve_persona(self._agent([self._mac()]), shaper,
                                          connect=self._probe())
        self.assertTrue(changed)
        self.assertEqual(shaper.url, "http://192.168.1.162:11434/v1/chat/completions")
        self.assertEqual(shaper.label(), "mac-persona@192.168.1.162:11434")

    def test_it_says_nothing_changed_when_the_answer_is_the_same(self):
        """The cadence must not rewrite the shaper every minute."""
        module = _desktop_module()
        shaper = persona.PersonaShaper("http://192.168.1.162:11434", "mac-persona")
        self.assertFalse(module._resolve_persona(self._agent([self._mac()]), shaper,
                                                 connect=self._probe()))

    def test_a_fleet_that_does_not_offer_it_leaves_the_shaper_alone(self):
        module = _desktop_module()
        shaper = persona.PersonaShaper("", "mac-persona")
        self.assertFalse(module._resolve_persona(self._agent([]), shaper))
        self.assertEqual(shaper.url, "")
        self.assertFalse(shaper.enabled)

    def test_a_service_found_once_is_not_forgotten_when_it_goes_quiet(self):
        module = _desktop_module()
        shaper = persona.PersonaShaper("http://mac:11434", "mac-persona")
        self.assertFalse(module._resolve_persona(self._agent([]), shaper))
        self.assertTrue(shaper.enabled)

    def test_a_shaper_set_to_auto_is_distinguishable_from_one_that_is_off(self):
        self.assertFalse(persona.PersonaShaper("").auto)     # deliberately off
        self.assertFalse(persona.PersonaShaper("mac:11434").auto)

    def test_a_shaper_enabled_late_still_gets_its_character(self):
        """Enabled after startup must not mean phrased without a personality."""
        from personality.loader import PersonalityProfile

        module = _desktop_module()
        shaper = persona.PersonaShaper("", "mac-persona")
        agent = self._agent([self._mac()])
        agent.personality = PersonalityProfile()
        module._resolve_persona(agent, shaper, connect=self._probe())
        self.assertTrue(shaper.enabled)
        self.assertTrue(shaper.instructions)

    def test_an_advertised_locator_that_does_not_answer_is_not_adopted(self):
        """Advertised is not usable -- the same rule as everywhere else.

        The locator is a real unroutable one (TEST-NET-1) so the case reads like the
        fleet's, but the answer comes from the probe rather than from the network:
        whether *this* machine can reach 192.0.2.1 is not the claim under test, and
        CapabilityMap's own suite covers the probe itself with a refusing connect.
        """
        module = _desktop_module()
        shaper = persona.PersonaShaper("", "mac-persona")
        self.assertFalse(module._resolve_persona(
            self._agent([self._mac("http://192.0.2.1:11434")]), shaper,
            connect=self._probe(False)))
        self.assertFalse(shaper.enabled)


class PersonaModelChoiceTestCase(unittest.TestCase):
    """A capability says where the service is, not which model to ask."""

    LISTING = {"data": [{"id": "qwen2.5:0.5b"}]}

    class _Response:
        def __init__(self, body):
            self._body = json.dumps(body).encode("utf-8")

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def test_an_unnamed_model_is_asked_for_at_the_endpoint(self):
        poster = _Recorder(reply=_reply("Phrased."))
        shaper = persona.PersonaShaper("mac:11434", poster=poster)
        with mock.patch.object(persona.urllib.request, "urlopen",
                               return_value=self._Response(self.LISTING)):
            result = shaper.shape("a line to phrase")
        self.assertEqual(result["source"], "persona")
        self.assertEqual(poster.calls[0]["payload"]["model"], "qwen2.5:0.5b")
        self.assertIn("qwen2.5:0.5b", shaper.label())

    def test_a_named_model_is_used_without_asking(self):
        poster = _Recorder(reply=_reply("Phrased."))
        shaper = persona.PersonaShaper("mac:11434", "qwen3.5:latest", poster=poster)
        with mock.patch.object(persona.urllib.request, "urlopen",
                               side_effect=AssertionError("should not ask")):
            self.assertEqual(shaper.shape("a line")["source"], "persona")
        self.assertEqual(poster.calls[0]["payload"]["model"], "qwen3.5:latest")

    def test_an_endpoint_that_lists_nothing_is_a_failure_not_a_guess(self):
        shaper = persona.PersonaShaper("mac:11434")
        with mock.patch.object(persona.urllib.request, "urlopen",
                               side_effect=OSError("no route to host")):
            result = shaper.shape("a line")
        self.assertEqual(result["source"], "unavailable")
        self.assertIn("lists no model", result["reason"])


if __name__ == "__main__":
    unittest.main()
