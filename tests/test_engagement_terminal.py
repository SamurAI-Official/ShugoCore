"""The operator terminal: one turn pipeline, and words labelled honestly.

The terminal is a second *front end*, not a second agent. These lock the
properties that make it trustworthy rather than merely working: typed words go
through the agent's own conversational path, they are labelled ``terminal`` so
nothing downstream records them as heard, replies arrive through the listener that
also makes this node a response candidate -- and a keyboard operator can actually
be chosen, which the routing floor previously made impossible.
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "shugocore_agent.py")
DESKTOP = os.path.join(ROOT, "clients", "desktop", "shugocore_desktop.py")
ROUTING = os.path.join(ROOT, "response_routing.py")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _routing_module():
    spec = importlib.util.spec_from_file_location("response_routing_under_test",
                                                  ROUTING)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TypedInputIsNotHeardTestCase(unittest.TestCase):
    """One path in, and it says which mouth the words came from."""

    def test_the_agent_exposes_one_typed_seam(self):
        agent = _read(AGENT)
        self.assertIn("def handle_typed_input(", agent)
        seam = agent.split("def handle_typed_input(")[1] \
                    .split("def terminal_input_recent")[0]
        self.assertIn('"source": str(source or "terminal")', seam)
        self.assertIn("self._handle_conversational_input(observation)", seam,
                      "typed input does not reach the conversational pipeline")
        self.assertIn('"typed": True', seam,
                      "a typed turn is not marked as typed, so it can be recorded "
                      "as something the microphone heard")

    def test_the_terminal_does_not_reach_into_the_pipeline_itself(self):
        terminal = _read(DESKTOP).split("def run_terminal(")[1]
        self.assertIn("handle_typed_input(", terminal,
                      "the terminal never hands words to the agent")
        self.assertNotIn("_handle_conversational_input", terminal,
                         "the terminal calls the private turn handler directly: "
                         "that is a second path to the same turn")
        self.assertNotIn("execute_task(", terminal,
                         "the terminal executes gated work itself instead of "
                         "letting the agent's own gated path decide")


class TerminalIsAResponseCandidateTestCase(unittest.TestCase):
    """The reply must be able to come back to the screen being typed at."""

    def test_typing_registers_a_speaker(self):
        desktop = _read(DESKTOP)
        self.assertIn("class TerminalSpeaker", desktop)
        self.assertIn("register_speak_listener(", desktop.split("def run_terminal(")[1],
                      "the terminal never registers a listener, so the node still "
                      "reports can_speak=False and is never chosen")

    def test_a_keyboard_operator_can_cross_the_routing_floor(self):
        routing = _routing_module()
        presence_only, _ = routing.proximity_score({"presence_present": True})
        self.assertLess(presence_only, routing.DEFAULT_FLOOR,
                        "bare presence now clears the floor, which contradicts "
                        "the reason that weight is deliberately small")
        terminal_only, why = routing.proximity_score({"terminal_active": True})
        self.assertGreaterEqual(terminal_only, routing.DEFAULT_FLOOR,
                                "a terminal that just took input still cannot be "
                                "chosen, so the hive answers a phone in the room "
                                "instead of the screen being typed at")
        self.assertIn("terminal-input", why)

    def test_a_face_still_outranks_a_keyboard(self):
        routing = _routing_module()
        face, _ = routing.proximity_score({"face_present": True})
        terminal, _ = routing.proximity_score({"terminal_active": True})
        self.assertGreater(face, terminal,
                           "a visible person must outrank a keyboard operator")

    def test_the_agent_stamps_the_signal_only_while_recent(self):
        agent = _read(AGENT)
        self.assertIn("if self.terminal_input_recent():", agent)
        self.assertIn('self_facts["terminal_active"] = True', agent)
        recent = agent.split("def terminal_input_recent")[1].split("def ")[0]
        self.assertIn("window_s", recent,
                      "the signal has no window: typing once would claim the "
                      "operator is present for the rest of the session")

    def test_the_signal_survives_a_missing_attribute(self):
        """A node that never took typed input must answer, not raise."""
        agent = _read(AGENT)
        recent = agent.split("def terminal_input_recent")[1].split("def ")[0]
        self.assertIn("getattr(self, \"_terminal_input_ts\", 0.0)", recent)


class TerminalDisplaysOnlyAgentStateTestCase(unittest.TestCase):

    def test_one_submit_path_serves_scripting_and_a_session(self):
        """--say and the interactive reader must do the same thing."""
        terminal = _read(DESKTOP).split("def run_terminal(")[1]
        self.assertIn("def _submit(text", terminal)
        self.assertIn("agent.handle_typed_input(text)", terminal)
        self.assertIn('for line in (getattr(args, "say", None) or []):', terminal)
        reader = terminal.split("def _reader()")[1]
        self.assertIn("_submit(text)", reader,
                      "the interactive reader has its own turn handling again, so "
                      "a scripted run and a session can diverge")

    def test_the_display_reads_the_controller_snapshot(self):
        terminal = _read(DESKTOP).split("def run_terminal(")[1]
        self.assertIn("controller.snapshot()", terminal)
        self.assertIn('getattr(agent, "_last_route", None)', terminal,
                      "the routing decision shown must be the agent's own")

    def test_headless_mode_does_not_require_tk(self):
        desktop = _read(DESKTOP)
        self.assertIn("TK_AVAILABLE", desktop)
        self.assertIn('getattr(tk, "Tk", object)', desktop,
                      "the UI class is defined against tkinter at import time, so "
                      "a host without Tk cannot even load the terminal")


if __name__ == "__main__":
    unittest.main()
