"""One source of truth for where a node keeps its state.

The runtime has four composition roots, and they had drifted. The agent root
(`shugocore_agent.AndroidAgent`) anchors every state path onto its data dir and
hands that dir to the engine as `log_dir`:

    self.memory_db_path        = self._join_data("semantic_memory.db")
    self.audit_path            = self._join_data("audit_chain.jsonl")
    self.episodic_journal_path = self._join_data("episodic_journal.jsonl")
    kwargs["log_dir"]          = self.data_dir

The other three passed relative names and no data dir:

    shugocore_server.build_engine   "semantic_memory.db" / "audit_chain.jsonl"
    continuous_agent.ContinuousAgent  whatever the caller passed
    android_node.NodeConfig         "node_semantic_memory.db" / "node_audit.jsonl"

so their state landed wherever the process happened to be started. That is not
hypothetical: the repository root contains `semantic_memory.db`,
`audit_chain.jsonl`, `node_audit.jsonl`, `node_journal_pixel8.jsonl` and a
411 KB `decision_engine.log`, which is this bug's output. It is the same
divergence the 1.30.25 release fixed for one root and left in the others.

What the tests below pin:
  * a state path given to the engine is frozen to an absolute location once, at
    construction, so a later `chdir` cannot relocate a running node's state
    (the failure class `AndroidAgent`'s comment describes);
  * when a data dir is supplied, relative names are anchored under it -- the
    behaviour the agent root already had.
"""
import os
import shutil
import tempfile
import unittest

from decision_engine import DecisionEngine
from state_paths import anchor


def _models():
    return [{"id": "m1", "type": "text", "weight": 1.0,
             "backend": {"type": "stub"}}]


class RelativeStatePathTestCase(unittest.TestCase):
    """A relative state path must not stay relative."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_parity_")
        self.was = os.getcwd()
        os.chdir(self.tmp)          # the drift only shows from a foreign cwd

    def tearDown(self):
        os.chdir(self.was)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _engine(self, **kwargs):
        return DecisionEngine(_models(), {"type": "chroma"}, **kwargs)

    def test_a_relative_audit_path_is_frozen_to_absolute(self):
        engine = self._engine()
        self.assertTrue(
            os.path.isabs(engine.audit.path),
            f"the audit chain is still relative: {engine.audit.path!r} -- a "
            f"later chdir would write the chain somewhere else")

    def test_a_relative_memory_path_is_frozen_to_absolute(self):
        engine = self._engine()
        self.assertTrue(
            os.path.isabs(engine.memory_db_path),
            f"the memory db is still relative: {engine.memory_db_path!r}")

    def test_a_relative_journal_path_is_frozen_to_absolute(self):
        engine = self._engine(episodic_journal_path="episodic_journal.jsonl")
        self.assertTrue(
            os.path.isabs(engine.episodic_journal_path),
            f"the episodic journal is still relative: "
            f"{engine.episodic_journal_path!r}")

    def test_a_later_chdir_cannot_relocate_the_state(self):
        """The AndroidAgent failure class: resolve once, not per-use.

        Comparing the stored string would be a tautology -- it never changes.
        What matters is where the path *resolves*: a relative `_path` is
        reopened against the current cwd on every write, so the chain silently
        follows the process.
        """
        engine = self._engine()
        before = os.path.abspath(engine.audit.path)
        elsewhere = os.path.join(self.tmp, "moved")
        os.makedirs(elsewhere, exist_ok=True)
        os.chdir(elsewhere)
        self.assertEqual(
            os.path.abspath(engine.audit.path), before,
            "the audit chain resolves somewhere else after a chdir -- the path "
            "is being resolved per-use instead of once, at construction")


class DataDirAnchoringTestCase(unittest.TestCase):
    """With a data dir, relative names land under it (the agent root's rule)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_parity_")
        self.was = os.getcwd()
        self.elsewhere = tempfile.mkdtemp(prefix="shugocore_parity_cwd_")
        os.chdir(self.elsewhere)

    def tearDown(self):
        os.chdir(self.was)
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.elsewhere, ignore_errors=True)

    def test_state_lands_under_the_supplied_data_dir(self):
        engine = DecisionEngine(
            _models(), {"type": "chroma"},
            memory_db_path="semantic_memory.db",
            audit_path="audit_chain.jsonl",
            episodic_journal_path="episodic_journal.jsonl",
            log_dir=self.tmp)
        for label, path in (("audit", engine.audit.path),
                            ("memory", engine.memory_db_path),
                            ("journal", engine.episodic_journal_path)):
            self.assertTrue(os.path.isabs(path), f"{label} not absolute: {path}")
            self.assertEqual(
                os.path.dirname(path), os.path.abspath(self.tmp),
                f"{label} did not land under the data dir: {path}")

    def test_an_absolute_path_is_left_alone(self):
        """The agent root already joins onto its data dir; that must survive."""
        target = os.path.join(self.tmp, "explicit", "audit.jsonl")
        engine = DecisionEngine(_models(), {"type": "chroma"},
                                audit_path=target, log_dir=self.tmp)
        self.assertEqual(engine.audit.path, target)


class CompositionRootParityTestCase(unittest.TestCase):
    """Every composition root reaches the engine, so every root must agree.

    Four roots build a `DecisionEngine` without going through the agent's
    `create_agent`. Anchoring in the engine (state_paths) is what makes them
    agree, so these pin the promise from the *outside*: whatever a root hands
    over, the state lands in an absolute place, and the engine it gets is still
    policy-gated.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_root_parity_")
        self.was = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.was)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_anchored(self, engine, root):
        for label, path in (("audit", engine.audit.path),
                            ("memory", engine.memory_db_path),
                            ("journal", engine.episodic_journal_path)):
            self.assertTrue(
                path is None or os.path.isabs(path),
                f"{root}: {label} path is not absolute: {path!r}")

    def test_the_server_root_anchors_its_state(self):
        from shugocore_server import build_engine
        self._assert_anchored(build_engine(), "shugocore_server.build_engine")

    def test_the_continuous_agent_root_anchors_its_state(self):
        from continuous_agent import ContinuousAgent
        agent = ContinuousAgent(models=_models())
        self._assert_anchored(agent.engine, "ContinuousAgent")

    def test_the_android_node_config_anchors_its_state(self):
        from android_node import NodeConfig
        config = NodeConfig(device_id="pixel8")
        for label, path in (("db", config.db_path), ("audit", config.audit_path),
                            ("journal", config.journal_path)):
            self.assertTrue(os.path.isabs(path),
                            f"NodeConfig {label} is not absolute: {path!r}")

    def test_the_android_node_honours_a_data_dir(self):
        from android_node import NodeConfig
        config = NodeConfig(device_id="pixel8", data_dir=self.tmp)
        self.assertEqual(os.path.dirname(config.audit_path), os.path.abspath(self.tmp))
        self.assertEqual(os.path.dirname(config.journal_path), os.path.abspath(self.tmp))
        self.assertIn("pixel8", config.journal_path)

    def _governance(self, engine):
        return (engine.capabilities, engine.approvals, engine.consents,
                engine.audit)

    def test_every_root_builds_a_policy_gated_engine(self):
        """The gates must exist on every root, not only the agent's."""
        from shugocore_server import build_engine
        from continuous_agent import ContinuousAgent
        from android_node import NodeConfig, AndroidShugoCoreNode

        engines = {
            "shugocore_server.build_engine": build_engine(),
            "ContinuousAgent": ContinuousAgent(models=_models()).engine,
            "AndroidShugoCoreNode": AndroidShugoCoreNode(
                NodeConfig(device_id="pixel8", data_dir=self.tmp)).engine,
        }
        # The android node only assembles its engine for the full_agent role;
        # with no engine assembled it still holds its own gate objects.
        for root, engine in engines.items():
            if engine is None:
                continue
            caps, approvals, consents, audit = self._governance(engine)
            for label, obj in (("capabilities", caps), ("approvals", approvals),
                               ("consents", consents)):
                self.assertIsNotNone(obj, f"{root}: {label} gate is missing")
            self.assertIsNotNone(audit, f"{root}: audit chain is missing")


class SqliteSentinelTestCase(unittest.TestCase):
    """Not every state path is a place on disk.

    `:memory:` is an address SQLite interprets. Anchoring it turned the
    in-memory database into a file path and broke every server test with
    `sqlite3.OperationalError: unable to open database file` -- discovered by
    the full-suite gate, not by the parity tests.
    """

    def test_the_in_memory_sentinel_is_left_alone(self):
        self.assertEqual(anchor(":memory:", r"C:\somewhere"), ":memory:")

    def test_a_sqlite_uri_filename_is_left_alone(self):
        uri = "file:shared?mode=memory&cache=shared"
        self.assertEqual(anchor(uri, r"C:\somewhere"), uri)

    def test_a_postgres_dsn_is_left_alone(self):
        dsn = "postgres://user:pw@db:5432/fleet"
        self.assertEqual(anchor(dsn, r"C:\somewhere"), dsn)

    def test_a_plain_name_is_still_anchored(self):
        self.assertEqual(anchor("mem.db", r"C:\somewhere"),
                         os.path.join(r"C:\somewhere", "mem.db"))


if __name__ == "__main__":
    unittest.main()
