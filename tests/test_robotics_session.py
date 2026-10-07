"""The robotics world session: the claim's evidence, judged by the real parser.

`world.robotics` was unproven because nothing produced a transcript for it. The
tool that does now runs entirely offline -- the deterministic ROS 2, MoveIt 2 and
Gazebo stubs -- so it can be run here, in the suite, and the thing it produces can
be judged by the consumer that will read it.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
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


SESSION = _module("robotics_session_under_test",
                  os.path.join("runtime", "tools", "robotics_session.py"))


class TheTranscriptTestCase(unittest.TestCase):
    """The lines the tool writes, judged by the consumer that will read them."""

    def test_a_gated_run_that_answered_is_proven(self):
        text = "\n".join(SESSION.transcript_lines(
            action="status=success", allowed=True, reason="",
            reply="the arm planned 6 joints through MoveIt and executed the trajectory"))
        ok, detail = claim_matrix.world_engagement(text)
        self.assertTrue(ok, detail)

    def test_a_refusal_is_not_dressed_up_as_an_answer(self):
        text = "\n".join(SESSION.transcript_lines(
            action="", allowed=False, reason="consent_required", reply=""))
        self.assertNotIn("[REPLY", text)
        self.assertNotIn("(gated)", text)
        ok, _detail = claim_matrix.world_engagement(text)
        self.assertIs(ok, False,
                      "a refused goal must not read as engagement")

    def test_a_robot_that_produced_nothing_writes_no_reply(self):
        """An action that ran and produced nothing is not an answer."""
        text = "\n".join(SESSION.transcript_lines(
            action="status=error", allowed=True, reason="", reply=""))
        self.assertIn("[ACTION ] robot_manipulate (gated)", text)
        self.assertNotIn("[REPLY", text)
        self.assertIn("produced no result", text)


class TheSessionRunsOfflineTestCase(unittest.TestCase):
    """End to end: no ROS 2, no MoveIt 2, no Gazebo -- and an arm still moves."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shugocore_robotics_session_")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.out = os.path.join(cls.tmp, "world.robotics.txt")
        cls.code = SESSION.main(["--out", cls.out,
                                 "--data-dir", os.path.join(cls.tmp, "node")])
        with open(cls.out, encoding="utf-8") as handle:
            cls.text = handle.read()

    def test_the_session_wrote_a_transcript_and_exited_zero(self):
        """Exit 0 because it produced its evidence; the evidence is what is judged."""
        self.assertEqual(self.code, 0)
        self.assertTrue(self.text.strip())

    def test_the_claim_is_proven_by_the_real_parser(self):
        ok, detail = claim_matrix.world_engagement(self.text)
        self.assertTrue(ok, detail)
        self.assertIn("(gated)", self.text)
        self.assertIn("[REPLY", self.text)

    def test_the_reply_names_what_the_arm_actually_planned(self):
        self.assertIn("through MoveIt", self.text)
        self.assertIn("6 joints", self.text)

    def test_the_action_went_through_the_engine_s_gate(self):
        """Not a direct handler call: the engine's gate is in the path."""
        self.assertIn("[ACTION ] robot_manipulate (gated)", self.text)
        self.assertIn("status=success", self.text)


if __name__ == "__main__":
    unittest.main()
