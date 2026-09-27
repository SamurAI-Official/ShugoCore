"""Phrasing as a service: the persona model shapes speech and nothing else.

The properties these tests exist for are the ones a second model could quietly break:
it must never approve anything (it runs before the gate), it must never cost the hive
its voice (every failure returns the draft), and it must never touch anything but speech.
"""
import os
import sys
import types
import unittest
from pathlib import Path

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


class PersonaShaperTestCase(unittest.TestCase):
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
        poster = _Recorder(error=OSError("connection refused"))
        shaper = persona.PersonaShaper("mac:11434", poster=poster)
        result = shaper.shape("the capital of France is Paris")
        self.assertEqual(result["source"], "unavailable")
        self.assertEqual(result["text"], "the capital of France is Paris")
        self.assertIn("connection refused", result["reason"])
        self.assertEqual(shaper.fallbacks, 1)

    def test_an_empty_completion_is_a_failure_not_an_empty_line(self):
        shaper = persona.PersonaShaper("mac:11434", poster=_Recorder(reply=_reply("")))
        result = shaper.shape("something to say")
        self.assertEqual(result["source"], "unavailable")
        self.assertEqual(result["text"], "something to say")

    def test_a_reply_with_no_choices_is_not_read_as_text(self):
        shaper = persona.PersonaShaper("mac:11434", poster=_Recorder(reply={}))
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
        return persona.PersonaShaper("mac:11434",
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


if __name__ == "__main__":
    unittest.main()
