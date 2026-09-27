"""Identity: one name per node, stable across restarts, safe to pass around."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import node_identity as ni  # noqa: E402


class SanitizeTestCase(unittest.TestCase):
    def test_device_capability_strings_become_usable_ids(self):
        # The old self-declared form: spaces, parentheses, mixed case.
        self.assertEqual(ni.sanitize("android-Unknown (s5e8835)"),
                         "android-unknown-s5e8835")
        self.assertEqual(ni.sanitize("shugo-Tab S9FE"), "shugo-tab-s9fe")

    def test_edges_and_empties(self):
        self.assertEqual(ni.sanitize("  Shugo TAB  "), "shugo-tab")
        self.assertEqual(ni.sanitize("---"), None)
        self.assertIsNone(ni.sanitize(""))
        self.assertEqual(ni.sanitize("", fallback="shugo-node"), "shugo-node")

    def test_bounded_length(self):
        self.assertLessEqual(len(ni.sanitize("x" * 200)), ni.MAX_LEN)

    def test_is_valid_matches_sanitize(self):
        self.assertTrue(ni.is_valid("shugo-tab"))
        self.assertFalse(ni.is_valid("shugo Tab"))
        self.assertFalse(ni.is_valid(None))


class LoadOrCreateTestCase(unittest.TestCase):
    def test_adopts_a_stored_id(self):
        """Migration property: naming a device in its data dir is enough."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ni.FILE_NAME), "w", encoding="utf-8") as fh:
                fh.write("shugo-tab\n")
            self.assertEqual(ni.load_or_create(tmp, caps="s5e8835"), "shugo-tab")
            self.assertEqual(ni.read(tmp), "shugo-tab")

    def test_honours_and_persists_an_explicit_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ni.load_or_create(tmp, suggested="shugo-mac"),
                             "shugo-mac")
            self.assertEqual(ni.read(tmp), "shugo-mac")

    def test_an_explicit_choice_overrides_a_generated_name(self):
        """A host told its identity adopts it; nothing silently renames a node."""
        with tempfile.TemporaryDirectory() as tmp:
            generated = ni.load_or_create(tmp, caps="desktop")
            self.assertNotEqual(generated, "shugo-desktop")
            self.assertEqual(
                ni.load_or_create(tmp, suggested="shugo-desktop",
                                  caps="desktop"), "shugo-desktop")
            self.assertEqual(ni.read(tmp), "shugo-desktop")

    def test_generated_ids_are_unique_stable_and_suffixed(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = ni.load_or_create(a, caps="s5e8835")
            self.assertTrue(first.startswith("shugo-s5e8835-"), first)
            self.assertEqual(ni.load_or_create(a, caps="s5e8835"), first)
            self.assertNotEqual(ni.load_or_create(b, caps="s5e8835"), first,
                                "two devices with the same SoC collided")

    def test_unwritable_data_dir_still_yields_an_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A *file* where the data dir should be: makedirs must fail.
            blocked = os.path.join(tmp, "not-a-dir")
            with open(blocked, "w", encoding="utf-8") as fh:
                fh.write("x")
            node_id = ni.load_or_create(blocked, caps="a51")
            self.assertTrue(ni.is_valid(node_id), node_id)

    def test_read_is_none_without_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ni.read(tmp))
            self.assertIsNone(ni.read(""))


class AgentIdentityWiringTestCase(unittest.TestCase):
    """The agent must *use* the persisted identity, not just load it."""

    def test_stored_identity_becomes_the_election_and_transport_id(self):
        import os as _os
        import tempfile as _tempfile
        from shugocore_agent import create_agent
        # A workspace dir, not a TemporaryDirectory: the engine keeps its Chroma
        # SQLite file open, so deleting the directory afterwards cannot work on
        # Windows (the pre-existing WinError 32 class). The dir is gitignored.
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            os.pardir, "runtime", "test_node_identity")
        os.makedirs(root, exist_ok=True)
        tmp = os.path.abspath(_tempfile.mkdtemp(dir=root))
        with open(_os.path.join(tmp, ni.FILE_NAME), "w", encoding="utf-8") as fh:
            fh.write("shugo-tab\n")
        # Keep the test's own mesh off the hive's port.
        saved = {key: _os.environ.get(key) for key in
                 ("SHUGOCORE_MESH_PORT", "SHUGOCORE_MESH_PEERS")}
        _os.environ["SHUGOCORE_MESH_PORT"] = "0"
        _os.environ["SHUGOCORE_MESH_PEERS"] = ""
        agent = create_agent(device_caps="s5e8835", data_dir=tmp)
        try:
            self.assertEqual(agent.node_id, "shugo-tab")
            self.assertEqual(agent.mesh_election.node_id, "shugo-tab",
                             "the election still uses a capability string")
            runtime = getattr(agent, "shugonet_runtime", None)
            if runtime is not None:
                self.assertEqual(runtime.agent_id, "shugo-tab",
                                 "the transport answers to a different name")
        finally:
            try:
                agent.cleanup()
            except Exception:
                pass
            for key, value in saved.items():
                if value is None:
                    _os.environ.pop(key, None)
                else:
                    _os.environ[key] = value


if __name__ == "__main__":
    unittest.main()

