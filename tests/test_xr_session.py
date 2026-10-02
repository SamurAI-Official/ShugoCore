"""The XR world session: the surface's own lines, judged by the real parser.

The producer only runs where a Godot binary exists, so what is pinned here is the part that
decides the claim: reading the surface's lines, and the two transcripts that must *not* pass --
one where the operator was never answered, and one where the action never passed the gate.
"""
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import claim_matrix  # noqa: E402


def _module(name, relative):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


XR = _module("xr_session_under_test", os.path.join("runtime", "tools", "xr_session.py"))


class TheSurfaceReaderTestCase(unittest.TestCase):
    def test_only_the_session_s_lines_are_kept_and_in_order(self):
        raw = ("Godot Engine v4.7.2\n"
               "OpenXR: Failed to get system [ XR_ERROR_FORM_FACTOR_UNAVAILABLE ]\n"
               "[WORLD  ] xr\n"
               "[PRESENCE] mode=desktop_preview\n"
               "[GOAL   ] operator (in the virtual space): look at this\n"
               "[ACTION ] execute_task conversation (gated) -> status=success stages=[]\n"
               "[REPLY  ] the charger is warm\n"
               "some other engine chatter\n")
        lines = XR.world_lines(raw)
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("[WORLD"))
        self.assertTrue(lines[-1].startswith("[REPLY"))

    def test_engine_warnings_are_not_mistaken_for_a_world(self):
        self.assertEqual(XR.world_lines("OpenXR was requested but failed to start."), [])

    def test_the_acted_and_replied_flags_come_from_the_lines(self):
        facts = {"world": XR.world_lines(
            "[WORLD  ] xr\n[ACTION ] execute_task (gated)\n[REPLY  ] an answer\n")}
        XR.scan_world(facts)
        self.assertTrue(facts["acted"])
        self.assertTrue(facts["replied"])

    def test_a_session_that_only_acted_did_not_reply(self):
        facts = {"world": XR.world_lines("[WORLD  ] xr\n[ACTION ] execute_task (gated)\n")}
        XR.scan_world(facts)
        self.assertTrue(facts["acted"])
        self.assertFalse(facts["replied"])

    def test_the_session_log_path_sits_beside_the_transcript(self):
        path = XR.session_log_path(os.path.join("runtime", "evidence", "world.xr.txt"))
        self.assertTrue(path.endswith("world.xr.session.log"))


class TheVerdictTestCase(unittest.TestCase):
    """The transcript a session writes must satisfy the real parser -- or say why not."""

    def _text(self, *, reply=True, gated=True):
        action = "execute_task conversation (gated)" if gated else "execute_task conversation"
        lines = ["[WORLD  ] xr", "[PRESENCE] mode=desktop_preview",
                 "[GOAL   ] operator (in the virtual space): note the charger",
                 f"[ACTION ] {action} -> status=success stages=[]"]
        if reply:
            lines.append("[REPLY  ] it is warm; nothing needs doing")
        return "\n".join(lines)

    def test_a_reached_and_answered_session_is_proven(self):
        ok, detail = claim_matrix.world_engagement(self._text())
        self.assertTrue(ok, detail)

    def test_a_session_that_never_answered_is_not_proven(self):
        ok, detail = claim_matrix.world_engagement(self._text(reply=False))
        self.assertFalse(ok)
        self.assertIn("never answered", detail)

    def test_an_action_that_did_not_pass_the_gate_is_not_engagement(self):
        ok, detail = claim_matrix.world_engagement(self._text(gated=False))
        self.assertFalse(ok)
        self.assertIn("gate", detail)

    def test_the_producer_transcript_reports_what_it_has(self):
        text = "\n".join(XR.transcript_lines({
            "port": 1234, "backend": "stub", "godot": "G:/godot/godot.exe",
            "world": XR.world_lines("[WORLD  ] xr\n[PRESENCE] mode=desktop_preview\n"
                                    "[GOAL   ] operator: x\n[ACTION ] t (gated)\n"),
            "rc": 0, "acted": True, "replied": False, "presence": "desktop_preview"}))
        self.assertIn("verdict: world=xr presence=desktop_preview acted=True replied=False",
                      text)


if __name__ == "__main__":
    unittest.main()