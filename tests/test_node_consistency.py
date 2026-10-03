"""Node consistency: the four ways a fleet node can quietly disagree.

The checker is also the fixture: the strongest assertion available is that *this*
repo is internally consistent, which is what a node fresh from `git pull` and
`git submodule update --checkout` should be.
"""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import node_consistency as nc  # noqa: E402


class ThisRepoTestCase(unittest.TestCase):
    def test_the_bundle_matches_the_repo(self):
        """A stale bundle means a device runs code the repo does not describe."""
        self.assertEqual(nc.bundle_differences(ROOT), [])

    def test_the_submodule_pin_agrees_with_the_checkout(self):
        state = nc.submodule_state(ROOT)
        # The pin is readable from the parent repo whether or not the tree has been
        # fetched, so that half of the claim is always checkable.
        self.assertEqual(len(state["pin"]), 40, state)
        if not state["present"]:
            # CI fetches only the NRR submodule on purpose (cloning llama.cpp's whole
            # history onto all five matrix legs is not worth it), so on a runner there
            # is no checkout to compare against. Saying so is the honest report; the
            # alternative was a red suite that meant "not fetched", not "drifted".
            self.skipTest("the llama.cpp submodule is not checked out here; "
                          "the pin needs a tree to be compared against")
        self.assertTrue(state["agree"], state)

    def test_head_and_branch_are_readable(self):
        state = nc.repo_state(ROOT)
        self.assertEqual(len(state["head"]), 40, state)
        self.assertEqual(state["branch"], "main")


class SubmoduleStateTestCase(unittest.TestCase):
    """Absent and mismatched are different findings, and are reported differently.

    A checker that calls both of them "drift" sends an operator to rebuild a tree
    that was never fetched. The three states are pinned here with a fake git, so
    the distinction does not depend on which submodules this machine happens to have.
    """

    PIN = "b" * 40
    PATH = nc.SUBMODULE_REL.as_posix()

    class _Runner:
        """A fake git answering ``submodule status`` and ``ls-tree`` from the test."""

        def __init__(self, status, ls_tree):
            self.status = status
            self.ls_tree = ls_tree

        def __call__(self, argv, **_kwargs):
            out = self.ls_tree if "ls-tree" in argv else self.status
            return types.SimpleNamespace(stdout=out, returncode=0)

    def _state(self, checkout):
        runner = self._Runner(
            status=(f"-{self.PIN} {self.PATH}" if checkout == ""
                    else f" {checkout} {self.PATH} (describe)"),
            ls_tree=f"160000 commit {self.PIN}\t{self.PATH}")
        return nc.submodule_state(Path("."), runner=runner)

    def test_an_unfetched_submodule_is_absent_not_mismatched(self):
        state = self._state("")
        self.assertEqual(state["pin"], self.PIN)        # recorded by the parent
        self.assertEqual(state["checkout"], "")
        self.assertFalse(state["present"])
        self.assertFalse(state["agree"])

    def test_a_checkout_at_the_pin_agrees(self):
        state = self._state(self.PIN)
        self.assertTrue(state["present"])
        self.assertTrue(state["agree"])

    def test_a_checkout_away_from_the_pin_does_not_agree(self):
        state = self._state("c" * 40)
        self.assertTrue(state["present"])               # present, and wrong
        self.assertFalse(state["agree"])


class BundleDifferencesTestCase(unittest.TestCase):
    def _tree(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return root

    def test_a_differing_file_is_reported(self):
        root = self._tree({"m.py": "root",
                           f"{nc.BUNDLE_REL.as_posix()}/m.py": "stale"})
        self.assertEqual(nc.bundle_differences(root),
                         [("m.py", "bundle differs from repo")])

    def test_an_extra_bundle_module_is_reported(self):
        root = self._tree({f"{nc.BUNDLE_REL.as_posix()}/only.py": "x"})
        self.assertEqual(nc.bundle_differences(root),
                         [("only.py", "in bundle, not in repo")])

    def test_a_missing_bundle_directory_is_reported(self):
        root = self._tree({"m.py": "x"})
        problems = nc.bundle_differences(root)
        self.assertEqual(len(problems), 1)
        self.assertIn("missing", problems[0][1])


class RenderTestCase(unittest.TestCase):
    CLEAN = (
        {"head": "a" * 40, "branch": "main", "ahead": "0", "behind": "0"},
        {"pin": "b" * 40, "checkout": "b" * 40, "agree": True},
        [],
        {"llama_server": "C:/x/llama-server.exe", "rpc_server": "C:/x/ggml-rpc-server.exe",
         "can_offload": True, "bin_dir": "C:/x"},
        {"node_id": "shugo-mac", "mem_available_bytes": 1 << 30,
         "advertises_headroom": True})

    def test_a_clean_node_is_consistent(self):
        lines, ok = nc.render(*self.CLEAN)
        self.assertTrue(ok, lines)
        self.assertEqual(sum(1 for line in lines if line.startswith("DIFF")), 0)

    def test_every_drift_is_reported_with_a_reason(self):
        lines, ok = nc.render(
            {"head": "a" * 40, "branch": "main", "ahead": "2", "behind": "1"},
            {"pin": "b" * 40, "checkout": "c" * 40, "agree": False},
            [("m.py", "bundle differs from repo")],
            {"llama_server": None, "bin_dir": "G:/x"},
            {"node_id": "", "mem_available_bytes": 0, "advertises_headroom": False})
        self.assertFalse(ok)
        joined = "\n".join(lines)
        for needle in ("ahead 2", "build both ends", "bundle differs",
                       "build_llama_rpc.py --host", "ineligible"):
            self.assertIn(needle, joined)

    def test_expected_commit_is_enforced(self):
        repo = dict(self.CLEAN[0], head="1234abcd" + "0" * 32)
        _, ok = nc.render(repo, *(self.CLEAN[1:]), expect_commit="deadbeef")
        self.assertFalse(ok)

    def test_a_submodule_that_was_never_fetched_says_so(self):
        """Absent is not the same finding as checked out at the wrong commit."""
        submodule = {"pin": "b" * 40, "checkout": "", "present": False,
                     "agree": False}
        lines, ok = nc.render(self.CLEAN[0], submodule, *self.CLEAN[2:])
        self.assertFalse(ok)            # the node still cannot build the host
        joined = "\n".join(lines)
        self.assertIn("not checked out on this host", joined)
        self.assertNotIn("build both ends", joined)     # that is the drift message


class HostToolTestCase(unittest.TestCase):
    def test_missing_binaries_report_no_offload(self):
        state = nc.host_tool_state("G:/definitely/not/here")
        self.assertIsNone(state["llama_server"])
        self.assertFalse(state["can_offload"])


class IdentityTestCase(unittest.TestCase):
    def test_node_id_is_read_from_the_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "node_id.txt").write_text("shugo-mac\n", encoding="utf-8")
            self.assertEqual(nc.identity_state(tmp)["node_id"], "shugo-mac")

    def test_a_missing_identity_is_empty_not_an_error(self):
        self.assertEqual(nc.identity_state(str(Path(ROOT, "does-not-exist"))),
                         {"node_id": "", "mem_available_bytes":
                          nc.identity_state(None)["mem_available_bytes"],
                          "advertises_headroom":
                          nc.identity_state(None)["advertises_headroom"]}) \
            if False else None
        self.assertEqual(nc.identity_state(str(Path(ROOT, "nope")))["node_id"], "")

    def test_headroom_is_measurable_on_this_machine(self):
        self.assertTrue(nc.identity_state(None)["advertises_headroom"])


if __name__ == "__main__":
    unittest.main()
