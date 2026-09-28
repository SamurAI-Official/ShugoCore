"""Onboarding writes what the mesh actually reads (v1.30.23).

The point of `fleet_onboard.py` is that two files exist in the form the agent expects, so
these tests check the form rather than the intention: the peers file is read back by the
agent's own loader, and a secret that already exists is never quietly replaced.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet_onboard  # noqa: E402
from shugocore_agent import create_agent  # noqa: E402


class PeerSpecTestCase(unittest.TestCase):
    def test_a_typo_is_reported_not_dropped(self):
        peers, problems = fleet_onboard.parse_peers(
            "tab=192.168.1.164:9000,a16=192.168.1.176,=1.2.3.4:9000,bad")
        self.assertEqual([peer["id"] for peer in peers], ["tab"])
        self.assertEqual(peers[0], {"id": "tab", "host": "192.168.1.164", "port": 9000})
        self.assertEqual(len(problems), 3)

    def test_empty_spec_is_no_peers_and_no_complaints(self):
        self.assertEqual(fleet_onboard.parse_peers(""), ([], []))


class OnboardTestCase(unittest.TestCase):
    def setUp(self):
        self._origin = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="shugocore_onboard_")
        self.dir = self._tmp.name

    def tearDown(self):
        try:
            os.chdir(self._origin)
        except OSError:
            pass
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_a_new_node_gets_a_secret_a_peer_list_and_is_readable_by_the_agent(self):
        peers, _ = fleet_onboard.parse_peers("tab=192.168.1.164:9000")
        report = fleet_onboard.onboard(self.dir, "shugo-desktop", peers)
        self.assertEqual(report["problems"], [])
        token = fleet_onboard.read_token(os.path.join(self.dir, "mesh_token.txt"))
        self.assertEqual(len(token), fleet_onboard.TOKEN_CHARS)
        self.assertTrue(all(c in "0123456789abcdef" for c in token))
        # The real proof: the agent's own loader accepts what onboarding wrote.
        agent = create_agent(device_caps="desktop", data_dir=self.dir)
        try:
            self.assertEqual(agent._load_mesh_peers_file(),
                             [("tab", "192.168.1.164", 9000)])
        finally:
            try:
                agent.cleanup()
            except Exception:
                pass

    def test_onboarding_one_node_keeps_the_others(self):
        peers, _ = fleet_onboard.parse_peers("tab=192.168.1.164:9000")
        fleet_onboard.onboard(self.dir, "hub", peers)
        more, _ = fleet_onboard.parse_peers("a16=192.168.1.176:9000")
        report = fleet_onboard.onboard(self.dir, "hub", more)
        self.assertEqual(report["peers_added"], ["a16"])
        with open(os.path.join(self.dir, "mesh_peers.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual([peer["id"] for peer in saved], ["a16", "tab"])

    def test_a_different_secret_is_refused_unless_forced(self):
        fleet_onboard.onboard(self.dir, "hub", [], token="a" * 32)
        peers, _ = fleet_onboard.parse_peers("tab=192.168.1.164:9000")
        report = fleet_onboard.onboard(self.dir, "hub", peers, token="b" * 32)
        self.assertTrue(report["problems"])
        self.assertIn("forks it", report["problems"][0])
        self.assertEqual(fleet_onboard.read_token(
            os.path.join(self.dir, "mesh_token.txt")), "a" * 32)
        forced = fleet_onboard.onboard(self.dir, "hub", peers, token="b" * 32,
                                       force_token=True)
        self.assertEqual(forced["problems"], [])
        self.assertEqual(fleet_onboard.read_token(
            os.path.join(self.dir, "mesh_token.txt")), "b" * 32)

    def test_an_existing_dict_form_is_read_and_migrated(self):
        """What the desktop actually has on disk today."""
        with open(os.path.join(self.dir, "mesh_peers.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"shugo-mac": "192.168.1.162:9000"}, fh)
        peers, _ = fleet_onboard.parse_peers("tab=192.168.1.164:9000")
        report = fleet_onboard.onboard(self.dir, "hub", peers)
        self.assertEqual(report["peers_added"], ["tab"])
        with open(os.path.join(self.dir, "mesh_peers.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual([peer["id"] for peer in saved], ["shugo-mac", "tab"])

    def test_check_reports_a_bare_dir_and_writes_nothing(self):
        before = os.listdir(self.dir)
        report = fleet_onboard.check(self.dir)
        self.assertFalse(report["ok"])
        self.assertEqual(os.listdir(self.dir), before, "check must not write")
        self.assertFalse(report["token"]["present"])
        self.assertEqual(len(report["problems"]), 2)
        peers, _ = fleet_onboard.parse_peers("tab=192.168.1.164:9000")
        fleet_onboard.onboard(self.dir, "hub", peers)
        after = fleet_onboard.check(self.dir)
        self.assertTrue(after["ok"], after["problems"])
        self.assertTrue(after["token"]["hexish"])
        self.assertEqual(after["peers"][0]["id"], "tab")

    def test_a_missing_data_dir_is_a_problem_not_a_crash(self):
        report = fleet_onboard.onboard(os.path.join(self.dir, "nope"), "hub")
        self.assertTrue(report["problems"])


if __name__ == "__main__":
    unittest.main()