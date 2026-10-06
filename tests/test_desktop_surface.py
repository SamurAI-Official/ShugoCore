#!/usr/bin/env python3
"""The desktop window is a surface the operator can talk to, not a dashboard.

The control plane could be watched but never used: nothing registered a speech
listener in GUI mode, so a typed turn was handled, decided, and then delivered to
nobody -- and the node never counted as one that can speak. The AGENT tab's
"Test speech" control, which `AndroidAgent.speak_test` and the README both name,
did not exist in the GUI at all. And the SECURITY pane read ``self.agent``, which
the UI does not have, so it reported "UI error" on every 1 Hz poll and Grant /
Revoke could not run.

These drive the *logic* without a Tk root (`DesktopUI.__new__`), matching
``test_desktop_ui_mesh.py``: the suite has to stay runnable on a headless CI host.
"""
import importlib.util
import os
import queue
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "clients", "desktop", "shugocore_desktop.py")


def _load_ui():
    spec = importlib.util.spec_from_file_location("desktop_surface_under_test",
                                                  _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ui = _load_ui()


class UiSpeakerTestCase(unittest.TestCase):
    """The listener itself: pure, thread-safe, and honest about empty text."""

    def test_a_reply_is_queued_for_the_operator(self):
        q = queue.Queue()
        speaker = _ui.UiSpeaker(q)
        self.assertTrue(speaker.speak("hello there"))
        self.assertEqual(q.get_nowait(), ("speak", "hello there"))
        self.assertEqual(speaker.delivered, 1)
        self.assertEqual(speaker.last, "hello there")

    def test_empty_text_is_not_a_delivery(self):
        q = queue.Queue()
        speaker = _ui.UiSpeaker(q)
        self.assertFalse(speaker.speak("   "))
        self.assertTrue(q.empty())
        self.assertEqual(speaker.delivered, 0)

    def test_it_never_touches_a_widget(self):
        """It is called from the tick thread; a widget here would be a race."""
        source = open(_PATH, encoding="utf-8").read()
        body = source.split("class UiSpeaker:")[1].split("\nclass ")[0]
        for forbidden in (".config(", ".insert(", "ttk.", "tk."):
            self.assertNotIn(forbidden, body,
                             f"UiSpeaker touches {forbidden} off the UI thread")


class TheNodeCanAnswerTestCase(unittest.TestCase):
    """A node built by the GUI must be able to speak, and say why if not."""

    def _controller(self):
        controller = _ui.AgentController.__new__(_ui.AgentController)
        controller.speak_listener = None
        controller.last_error = ""
        return controller

    def test_a_fresh_controller_has_no_listener(self):
        self.assertIsNone(self._controller().speak_listener)

    def test_the_surface_listener_is_attached_to_the_node(self):
        controller = self._controller()
        registered = []

        class _Agent:
            def register_speak_listener(self, listener):
                registered.append(listener)

        listener = object()
        controller.speak_listener = listener
        controller._attach_speaker(_Agent())
        self.assertEqual(registered, [listener])

    def test_no_listener_and_no_agent_are_both_survivable(self):
        controller = self._controller()
        controller._attach_speaker(None)          # nothing to attach to
        controller.speak_listener = object()
        controller._attach_speaker(None)          # no listener requested

    def test_a_refused_listener_is_reported_not_raised(self):
        controller = self._controller()
        controller.speak_listener = object()

        class _Agent:
            def register_speak_listener(self, _listener):
                raise RuntimeError("no speech provider")

        controller._attach_speaker(_Agent())      # must not raise
        self.assertIn("speak listener not attached", controller.last_error)


class TheSecurityPaneReadsTheControllerTestCase(unittest.TestCase):
    """`self.agent` does not exist on the UI -- the controller owns the node."""

    def _ui_with(self, agent):
        ui = _ui.DesktopUI.__new__(_ui.DesktopUI)
        ui.controller = types.SimpleNamespace(agent=agent)
        return ui

    def test_the_engine_registry_is_preferred(self):
        engine_registry = object()
        agent = types.SimpleNamespace(engine=types.SimpleNamespace(
            consents=engine_registry), consents=object())
        self.assertIs(self._ui_with(agent)._consent_registry(), engine_registry)

    def test_the_agent_registry_is_the_fallback(self):
        agent_registry = object()
        agent = types.SimpleNamespace(engine=None, consents=agent_registry)
        self.assertIs(self._ui_with(agent)._consent_registry(), agent_registry)

    def test_no_node_is_no_registry(self):
        self.assertIsNone(self._ui_with(None)._consent_registry())

    def test_it_no_longer_reaches_for_an_attribute_the_ui_lacks(self):
        source = open(_PATH, encoding="utf-8").read()
        body = source.split("def _consent_registry")[1].split("\n    def ")[0]
        self.assertNotIn("self.agent.", body)
        self.assertIn("self.controller", body)


class TheAgentTabTalksToTheNodeTestCase(unittest.TestCase):
    """The turn seam and the documented Test-speech control both exist."""

    def test_the_agent_tab_has_a_turn_box_and_a_speech_test(self):
        source = open(_PATH, encoding="utf-8").read()
        body = source.split("def _build_agent")[1].split("\n    def ")[0]
        self.assertIn("Talk to the node", body)
        self.assertIn("Test speech", body,
                      "the control speak_test and the README both name is "
                      "missing from the GUI")
        self.assertIn("_send_turn", body)

    def test_a_turn_goes_through_the_agents_own_seam(self):
        source = open(_PATH, encoding="utf-8").read()
        body = source.split("def _send_turn")[1].split("\n    def ")[0]
        self.assertIn("handle_typed_input(", body,
                      "the GUI drives something other than the agent's own "
                      "conversational path")
        self.assertNotIn("_handle_conversational_input", body)
        self.assertIn("Thread(", body,
                      "the turn would block the Tk main loop for the whole "
                      "model round trip")

    def test_the_speech_test_drives_the_gated_path(self):
        source = open(_PATH, encoding="utf-8").read()
        body = source.split("def _test_speech")[1].split("\n    def ")[0]
        self.assertIn("speak_test(", body,
                      "Test speech must use the node's gated speak action, not "
                      "talk to the device itself")

    def test_a_silent_turn_is_reported_rather_than_left_blank(self):
        """Handled-but-silent and broken must not look the same."""
        source = open(_PATH, encoding="utf-8").read()
        body = source.split("def _send_turn")[1].split("\n    def ")[0]
        self.assertIn("delivered", body)
        self.assertIn("no spoken reply", body)

    def test_the_ui_creates_and_registers_its_speaker(self):
        source = open(_PATH, encoding="utf-8").read()
        init = source.split("def __init__(self, controller: AgentController")[1]
        init = init.split("\n    def ")[0]
        self.assertIn("UiSpeaker(", init)
        self.assertIn("controller.speak_listener", init)


class StartingIsNotBrokenTestCase(unittest.TestCase):
    """`create_agent` takes seconds; that window must not read as a failure."""

    def _ui(self, agent, starting):
        ui = _ui.DesktopUI.__new__(_ui.DesktopUI)
        ui.controller = types.SimpleNamespace(agent=agent, starting=starting,
                                              running=lambda: agent is not None)
        return ui

    def test_a_starting_node_says_so(self):
        reason = self._ui(None, True)._no_node_reason()
        self.assertIn("still starting", reason)
        self.assertNotIn("start one on the SERVER tab", reason)

    def test_a_stopped_node_points_at_the_button(self):
        reason = self._ui(None, False)._no_node_reason()
        self.assertIn("SERVER tab", reason)

    def test_the_controller_marks_the_window(self):
        controller = _ui.AgentController.__new__(_ui.AgentController)
        controller.starting = False
        controller.last_error = ""
        seen = {}

        def _start(*_args, **_kwargs):
            seen["during"] = controller.starting
            return "boom"

        controller._start = _start
        result = controller.start({}, "", "", "", ".", "desktop", {})
        self.assertTrue(seen["during"],
                        "the start window was not marked, so a surface cannot "
                        "tell a node that is coming up from one that is absent")
        self.assertFalse(controller.starting,
                         "the flag survived the start, so every later failure "
                         "would be reported as 'still starting'")
        self.assertEqual(result, "boom")


if __name__ == "__main__":
    unittest.main()
