"""Tests for ShugoNet memory sharing — the two-agent combined-memory contract.

Two real ``ShugonetAgentRuntime`` peers are bound on loopback, each backed by
its own ``MemoryManager`` / SQLite Tier 2 store. Each agent learns a distinct
fact, then syncs the other. The assertion is the user-visible contract: after
sync each agent's *own* memory contains the other agent's fact (the combined
knowledge base) and can answer a model-style query from it — not a stub.

Also covers the Tier 2-only isolation invariant, provenance, idempotent
dedupe, and the ``memory_sync_conflict_storm`` fallback guard.
"""
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest

from agent_runtime import ShugonetAgentRuntime
from memory_system import MemoryManager, SemanticMemory


def _wait_bound(runtime: ShugonetAgentRuntime, timeout: float = 5.0) -> int:
    """Wait for the runtime's accept socket to bind; return the real port."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        server = runtime._server
        if server is not None and server._server_sock is not None:
            port = server._server_sock.getsockname()[1]
            if port:  # never 0: an unbound socket reports port 0
                return port
        time.sleep(0.01)
    raise AssertionError("shugonet server did not bind in time")


def _retry(fn, ok, attempts: int = 12, delay: float = 0.25):
    """Retry a network call until ``ok(result)`` or attempts run out.

    Loopback connects can transiently fail with EADDRNOTAVAIL under heavy
    port churn (the full suite), which is environmental, not a logic bug --
    so network assertions retry instead of flaking.
    """
    result = None
    for _ in range(attempts):
        result = fn()
        if ok(result):
            return result
        time.sleep(delay)
    return result


class _Agent:
    """One mesh peer: a MemoryManager plus a ShugonetAgentRuntime."""

    def __init__(self, agent_id: str, tmpdir: str,
                 fallback_controller=None, **runtime_kwargs):
        self.agent_id = agent_id
        self.memory = MemoryManager(
            agent_id=agent_id,
            semantic=SemanticMemory(
                db_path=os.path.join(tmpdir, f"{agent_id}.db")),
            auto_start=False)
        self.runtime = ShugonetAgentRuntime(
            agent_id=agent_id, host="127.0.0.1", port=_free_port(),
            memory=self.memory, fallback_controller=fallback_controller,
            **runtime_kwargs)
        self.port = 0

    def start(self) -> int:
        self.runtime.start()
        self.port = _wait_bound(self.runtime)
        return self.port

    def learn(self, content: str, **kw) -> int:
        return self.memory.tier2.store_fact(content, **kw)

    def knows(self, content: str) -> bool:
        return self.memory.tier2.content_exists(content)

    def recall(self, query: str, top_k: int = 5):
        return self.memory.tier2.search(query, top_k=top_k, reinforce=False)

    def stop(self) -> None:
        self.runtime.stop()
        self.memory.shutdown()


class TestSemanticMemorySharingPrimitives(unittest.TestCase):
    """facts_since / content_exists: the dedupe + watermark primitives."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mem = SemanticMemory(db_path=os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_content_exists_and_facts_since(self):
        self.assertFalse(self.mem.content_exists("alpha recall target"))
        self.mem.store_fact("alpha recall target")
        self.assertTrue(self.mem.content_exists("alpha recall target"))
        facts = self.mem.facts_since()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["content"], "alpha recall target")
        # A watermark at the newest fact excludes everything already seen.
        self.assertEqual(
            self.mem.facts_since(since_iso=facts[0]["created_at"]), [])

    def test_facts_since_is_bounded(self):
        for i in range(10):
            self.mem.store_fact(f"fact number {i}")
        self.assertEqual(len(self.mem.facts_since(limit=3)), 3)


class TestMemoryManagerSharing(unittest.TestCase):
    """Export/import preserves provenance and never duplicates content."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = MemoryManager(
            agent_id="agent-a",
            semantic=SemanticMemory(db_path=os.path.join(self.tmp, "a.db")),
            auto_start=False)
        self.b = MemoryManager(
            agent_id="agent-b",
            semantic=SemanticMemory(db_path=os.path.join(self.tmp, "b.db")),
            auto_start=False)

    def tearDown(self):
        for mgr in (self.a, self.b):
            mgr.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_is_tier2_only_wire_form(self):
        self.a.tier2.store_fact("the observatory dome opens at dusk")
        self.a.record_event("private_episode", payload={"secret": "tier1"})
        exported = self.a.export_shared_facts()
        self.assertEqual(len(exported), 1)
        # Wire form carries knowledge, never ids/embeddings or Tier 1 events.
        self.assertEqual(
            set(exported[0]),
            {"content", "kind", "salience", "created_at", "metadata"})
        self.assertNotIn("secret", str(exported))

    def test_import_is_idempotent_and_records_provenance(self):
        self.a.tier2.store_fact("the north gate needs a keycard after 22:00")
        exported = self.a.export_shared_facts()

        first = self.b.import_shared_facts(exported, source="agent-a")
        self.assertEqual(first["imported"], 1)
        self.assertEqual(first["duplicates"], 0)
        self.assertTrue(self.b.tier2.content_exists(
            "the north gate needs a keycard after 22:00"))

        hits = self.b.tier2.search("north gate keycard", top_k=1,
                                   reinforce=False)
        self.assertEqual(hits[0]["metadata"]["shared_from"], "agent-a")
        self.assertIn("shared_at", hits[0]["metadata"])

        second = self.b.import_shared_facts(exported, source="agent-a")
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(self.b.tier2.facts_since()), 1)

    def test_malformed_facts_are_skipped(self):
        result = self.b.import_shared_facts(
            [{"content": ""}, "not-a-dict", {"content": "valid fact"}],
            source="agent-a")
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 2)

    def test_count_shared_facts_is_durable_and_filterable(self):
        self.a.tier2.store_fact("the pier closes at sunset")
        self.assertEqual(self.b.count_shared_facts(), 0)
        self.b.import_shared_facts(self.a.export_shared_facts(),
                                   source="agent-a")
        self.assertEqual(self.b.count_shared_facts(), 1)
        self.assertEqual(self.b.count_shared_facts("agent-a"), 1)
        self.assertEqual(self.b.count_shared_facts("someone-else"), 0)

    def test_shared_sources_list_provenance_peers(self):
        """The mesh UI needs per-peer durable counts without live peers."""
        self.assertEqual(self.b.shared_fact_sources(), [])
        self.a.tier2.store_fact("the pier closes at sunset")
        self.a.tier2.store_fact("the lighthouse flashes every 7s")
        self.b.import_shared_facts(self.a.export_shared_facts(),
                                   source="agent-a")
        sources = self.b.shared_fact_sources()
        self.assertEqual(sources, [{"peer": "agent-a", "shared_facts": 2}])
        # Durable: still listed after a fresh manager over the same db.
        reopened = MemoryManager(
            agent_id="agent-b-reopened",
            semantic=SemanticMemory(
                db_path=os.path.join(self.tmp, "b.db")),
            auto_start=False)
        try:
            self.assertEqual(reopened.shared_fact_sources(), sources)
        finally:
            reopened.shutdown()



class TestTwoAgentCombinedMemory(unittest.TestCase):
    """The headline contract: two agents, one shared knowledge base."""

    FACT_A = "agent A knows the rooftop garden is on level nine"
    FACT_B = "agent B knows the server room needs badge access"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = _Agent("agent-a", self.tmp)
        self.b = _Agent("agent-b", self.tmp)
        self.a.start()
        self.b.start()
        self.a.runtime.add_peer("agent-b", "127.0.0.1", self.b.port)
        self.b.runtime.add_peer("agent-a", "127.0.0.1", self.a.port)
        time.sleep(0.1)

    def tearDown(self):
        self.a.stop()
        self.b.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_query_returns_real_peer_memory_not_a_stub(self):
        self.a.learn(self.FACT_A)
        responses = _retry(
            lambda: self.b.runtime.query("rooftop garden level nine",
                                         peers=["agent-a"]),
            lambda r: len(r) == 1)
        self.assertEqual(len(responses), 1)
        results = responses[0]["results"]
        self.assertTrue(results)
        joined = " ".join(r["fact"] for r in results)
        self.assertIn("rooftop garden", joined)
        self.assertNotIn("stub", joined)
        self.assertEqual(responses[0]["from"], "agent-a")

    def test_sync_combines_memory_and_is_usable_by_each_agent(self):
        self.a.learn(self.FACT_A)
        self.b.learn(self.FACT_B)
        self.assertFalse(self.b.knows(self.FACT_A))
        self.assertFalse(self.a.knows(self.FACT_B))

        b_pull = _retry(lambda: self.b.runtime.sync("agent-a"),
                        lambda r: r.get("imported") == 1)
        self.assertEqual(b_pull["status"], "success")
        self.assertEqual(b_pull["imported"], 1)

        a_pull = _retry(lambda: self.a.runtime.sync("agent-b"),
                        lambda r: r.get("imported") == 1)
        self.assertEqual(a_pull["status"], "success")
        self.assertEqual(a_pull["imported"], 1)

        # Combined memory: each agent now holds BOTH agents' knowledge.
        self.assertTrue(self.b.knows(self.FACT_A))
        self.assertTrue(self.a.knows(self.FACT_B))
        self.assertEqual(len(self.b.memory.tier2.facts_since(limit=100)), 2)
        self.assertEqual(len(self.a.memory.tier2.facts_since(limit=100)), 2)

        # ...and it is usable through the agent's own recall path.
        hits = self.b.recall("rooftop garden level nine")
        self.assertTrue(any("rooftop garden" in h["content"] for h in hits))

        # The watermark makes the repeat sync incremental: the peer re-sends
        # nothing it already shared, so combined memory stays at 2 facts.
        again = _retry(lambda: self.b.runtime.sync("agent-a"),
                       lambda r: r.get("status") == "success")
        self.assertEqual(again["imported"], 0)
        self.assertEqual(again["received"], 0)
        self.assertEqual(len(self.b.memory.tier2.facts_since(limit=100)), 2)

        # Forcing a full re-pull is deduped, not duplicated.
        full = _retry(lambda: self.b.runtime.sync("agent-a", since=0),
                      lambda r: r.get("duplicates") == 2)
        self.assertEqual(full["imported"], 0)
        self.assertEqual(full["duplicates"], 2)
        self.assertEqual(len(self.b.memory.tier2.facts_since(limit=100)), 2)

        # Status advertises the live shared-memory state.
        status = self.b.runtime.status()
        self.assertTrue(status["memory_enabled"])
        self.assertEqual(status["stats"]["imported"], 1)

class TestNoMemoryBackend(unittest.TestCase):
    """A runtime without memory is still valid transport — never a fake fact."""

    def setUp(self):
        self.bare = ShugonetAgentRuntime(agent_id="bare", host="127.0.0.1",
                                         port=_free_port())
        self.client = ShugonetAgentRuntime(agent_id="client",
                                           host="127.0.0.1",
                                           port=_free_port())

    def tearDown(self):
        self.client.stop()
        self.bare.stop()

    def test_query_and_sync_answer_empty_without_memory(self):
        self.bare.start()
        port = _wait_bound(self.bare)
        self.client.start()
        self.client.add_peer("bare", "127.0.0.1", port)
        time.sleep(0.1)

        responses = _retry(
            lambda: self.client.query("anything", peers=["bare"]),
            lambda r: len(r) == 1)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["results"], [])

        result = _retry(lambda: self.client.sync("bare"),
                        lambda r: r.get("status") == "success")
        self.assertEqual(result["received"], 0)
        self.assertEqual(result["imported"], 0)
        self.assertFalse(self.client.status()["memory_enabled"])


class _SpyFallback:
    def __init__(self):
        self.violations = []

    def report_violation(self, kind: str, detail: str = "") -> None:
        self.violations.append((kind, detail))


class TestConflictStormGuard(unittest.TestCase):
    """A peer replaying facts we already hold trips the deterministic guard."""

    def test_duplicate_storm_reports_memory_sync_conflict_storm(self):
        tmp = tempfile.mkdtemp()
        spy = _SpyFallback()
        a = _Agent("agent-a", tmp)
        b = _Agent("agent-b", tmp, fallback_controller=spy,
                   conflict_threshold=1)
        try:
            a.start()
            b.start()
            b.runtime.add_peer("agent-a", "127.0.0.1", a.port)
            time.sleep(0.1)

            # Same knowledge on both sides -> the sync is all duplicates.
            a.learn("the meeting moved to Thursday")
            b.learn("the meeting moved to Thursday")
            result = _retry(lambda: b.runtime.sync("agent-a"),
                            lambda r: r.get("duplicates") == 1)
            self.assertEqual(result["duplicates"], 1)
            self.assertTrue(any(kind == "memory_sync_conflict_storm"
                                for kind, _ in spy.violations))
        finally:
            a.stop()
            b.stop()
            shutil.rmtree(tmp, ignore_errors=True)


class TestMeshPeerConfig(unittest.TestCase):
    """SHUGOCORE_MESH_PEERS parsing: valid entries in, typos skipped."""

    def test_parse_valid_and_malformed(self):
        from shugocore_agent import AndroidAgent

        peers = AndroidAgent._parse_mesh_peers(
            "shugo-b=192.168.1.161:9000, shugo-c=10.0.0.5:9001 ")
        self.assertEqual(peers, [("shugo-b", "192.168.1.161", 9000),
                                 ("shugo-c", "10.0.0.5", 9001)])

    def test_parse_skips_bad_entries(self):
        from shugocore_agent import AndroidAgent

        self.assertEqual(AndroidAgent._parse_mesh_peers(""), [])
        self.assertEqual(AndroidAgent._parse_mesh_peers("garbage"), [])
        self.assertEqual(AndroidAgent._parse_mesh_peers("id=host:notaport"), [])
        self.assertEqual(AndroidAgent._parse_mesh_peers("id=host:99999"), [])
        self.assertEqual(AndroidAgent._parse_mesh_peers("=host:9000"), [])

    def test_load_mesh_token_env_file_missing(self):
        import os
        from shugocore_agent import AndroidAgent

        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "mesh_token.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("  file-token-abc\nsecond-line\n")
            os.environ["SHUGOCORE_MESH_TOKEN"] = "env-wins"
            try:
                self.assertEqual(AndroidAgent._load_mesh_token(tmp),
                                 "env-wins")
            finally:
                del os.environ["SHUGOCORE_MESH_TOKEN"]
            self.assertEqual(AndroidAgent._load_mesh_token(tmp),
                             "file-token-abc")
            with open(os.path.join(tmp, "mesh_token.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("   \n")
            self.assertIsNone(AndroidAgent._load_mesh_token(tmp))
            self.assertIsNone(AndroidAgent._load_mesh_token(
                os.path.join(tmp, "nonexistent-dir")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            os.environ.pop("SHUGOCORE_MESH_TOKEN", None)
def _bare_agent(data_dir: str):
    """An AndroidAgent with no bootstrap side effects.

    Constructing AndroidAgent boots the whole agent (chdir, ShugoNet bind,
    memory workers), so tests that only exercise the mesh helpers use an
    uninitialised instance with just the attributes those helpers read.
    """
    from shugocore_agent import AndroidAgent

    agent = object.__new__(AndroidAgent)
    agent.data_dir = data_dir
    agent.shugonet_runtime = None
    return agent


class TestMeshCommandWiring(unittest.TestCase):
    """Intent + command routing + peer-file config for the memory mesh."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mesh_transcripts_classify_as_commands(self):
        from subsystems.intent import IntentParser, IntentType
        parser = IntentParser()
        for text in ("sync your memory with your peer",
                     "mesh status",
                     "share what you know with your peer"):
            self.assertEqual(parser.classify(text).intent_type,
                             IntentType.COMMAND, text)

    def test_mesh_transcripts_route_to_mesh_category(self):
        from subsystems.command_router import CommandExecutor
        executor = CommandExecutor()
        self.assertEqual(
            executor._categorize("sync", "sync your memory with your peer"),
            "mesh")
        self.assertEqual(executor._categorize("mesh", "mesh status"), "mesh")
        # Existing categories are untouched by the mesh rule.
        self.assertEqual(
            executor._categorize("remember", "remember my favorite color"),
            "memory")
        self.assertEqual(
            executor._categorize("set", "set a timer for 5 minutes"), "timer")

    def test_handle_mesh_sync_calls_tool(self):
        from subsystems.command_router import default_handlers
        from subsystems.tools import ToolResult

        class _Intent:
            transcript = "sync your memory with your peer"
            entities = {}

        class _Tools:
            def __init__(self):
                self.calls = []

            def has(self, name):
                return name in ("mesh_sync", "mesh_status")

            def call(self, name, **kwargs):
                self.calls.append((name, kwargs))
                return ToolResult.ok_result("synced ok", data={"imported": 3})

        tools = _Tools()
        result = default_handlers(tools)["mesh"](_Intent())
        self.assertTrue(result.success)
        self.assertEqual(result.action_taken, "mesh_sync")
        self.assertEqual(tools.calls[0][0], "mesh_sync")

    def test_load_mesh_peers_file_dict_and_list(self):
        agent = _bare_agent(self.tmp)
        path = os.path.join(self.tmp, "mesh_peers.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"shugo-tab": "192.168.1.164:9000"}, fh)
        self.assertEqual(agent._load_mesh_peers_file(),
                         [("shugo-tab", "192.168.1.164", 9000)])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([{"id": "shugo-a51", "host": "192.168.1.160",
                        "port": 9000},
                       {"id": "bad", "host": "x", "port": "nope"}], fh)
        self.assertEqual(agent._load_mesh_peers_file(),
                         [("shugo-a51", "192.168.1.160", 9000)])

    def test_load_mesh_peers_file_missing_or_malformed(self):
        agent = _bare_agent(self.tmp)
        self.assertEqual(agent._load_mesh_peers_file(), [])
        with open(os.path.join(self.tmp, "mesh_peers.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(agent._load_mesh_peers_file(), [])

    def test_mesh_tools_report_missing_runtime(self):
        agent = _bare_agent(self.tmp)
        self.assertFalse(agent._tool_mesh_status().ok)
        self.assertFalse(agent._tool_mesh_sync("shugo-tab").ok)

_USED_TEST_PORTS: set = set()


def _free_port() -> int:
    """A distinct free port OUTSIDE the ephemeral range.

    Binding a listener to port 0 picks from the ephemeral range (49152+ on
    macOS); connecting to such a port can fail with EADDRNOTAVAIL under
    pressure. Test listeners therefore come from a low, fixed range, and
    handed-out ports are remembered so repeated calls stay distinct.
    """
    for port in range(21000, 22000):
        if port in _USED_TEST_PORTS:
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            _USED_TEST_PORTS.add(port)
            return port
        except OSError:
            continue
        finally:
            sock.close()
    raise AssertionError("no free test port in 21000-21999")


class TestPeerReconnect(unittest.TestCase):
    """A first-dial failure at startup must not strand the peer connection."""

    def test_reconnect_peers_dials_a_later_listener(self):
        port = _free_port()
        a = ShugonetAgentRuntime(agent_id="a", host="127.0.0.1",
                                 port=_free_port(), reconnect_interval=0)
        b = None
        try:
            a.start()
            # Nothing is listening yet -> the initial dial fails.
            a.add_peer("b", "127.0.0.1", port)
            self.assertEqual(a.status()["connected_peers"], [])

            b = ShugonetAgentRuntime(agent_id="b", host="127.0.0.1", port=port)
            b.start()
            _wait_bound(b)

            self.assertEqual(_retry(a.reconnect_peers, lambda n: n == 1), 1)
            self.assertEqual(a.status()["connected_peers"], ["b"])
        finally:
            a.stop()
            if b is not None:
                b.stop()

    def test_background_reconnect_loop_recovers(self):
        port = _free_port()
        a = ShugonetAgentRuntime(agent_id="a", host="127.0.0.1",
                                 port=_free_port(), reconnect_interval=0.2)
        b = None
        try:
            a.start()
            a.add_peer("b", "127.0.0.1", port)
            b = ShugonetAgentRuntime(agent_id="b", host="127.0.0.1", port=port)
            b.start()
            _wait_bound(b)

            deadline = time.time() + 5.0
            while time.time() < deadline:
                if a.status()["connected_peers"] == ["b"]:
                    break
                time.sleep(0.1)
            self.assertEqual(a.status()["connected_peers"], ["b"])
        finally:
            a.stop()
            if b is not None:
                b.stop()

    def test_reconnect_disabled_leaves_no_thread(self):
        a = ShugonetAgentRuntime(agent_id="a", host="127.0.0.1",
                                 port=_free_port(), reconnect_interval=0)
        try:
            a.start()
            self.assertIsNone(a._reconnect_thread)
            self.assertEqual(a.reconnect_peers(), 0)
        finally:
            a.stop()

class TestPeerErrorsAreNotFalseSuccess(unittest.TestCase):
    """A peer refusal must surface as a failure, never an empty success.

    A peer that refuses a request (protocol/version mismatch, rejected token,
    unknown verb) answers with an ``error`` frame. That reply is a *truthy
    dict*, so a naive ``if not resp`` check reads it as a successful transfer
    of zero facts and reports ``status: success`` -- the node then looks
    connected while its mesh is silently broken.
    """

    def _client_with_peer(self, reply):
        client = ShugonetAgentRuntime(agent_id="client", host="127.0.0.1",
                                      port=_free_port())
        client.add_peer("peer", "127.0.0.1", _free_port())
        client._one_shot_request = lambda conn, msg: reply
        return client

    def test_sync_surfaces_peer_error_frame_as_failure(self):
        client = self._client_with_peer(
            {"type": "error", "status": "error", "message": "invalid_request"})
        result = client.sync("peer")
        self.assertEqual(result["status"], "error")
        self.assertIn("invalid_request", result["message"])
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["peer"], "peer")

    def test_sync_surfaces_status_error_frame_as_failure(self):
        client = self._client_with_peer(
            {"status": "error", "reason": "rejected token"})
        result = client.sync("peer")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["message"], "rejected token")

    def test_query_skips_peer_error_frames(self):
        client = self._client_with_peer(
            {"type": "error", "message": "unsupported_verb"})
        self.assertEqual(client.query("anything", peers=["peer"]), [])

    def test_sync_result_is_not_mistaken_for_an_error(self):
        # Guard against over-eager detection: a real (empty) result frame is
        # still a success.
        client = self._client_with_peer(
            {"type": "sync_result", "facts": [], "count": 0})
        result = client.sync("peer")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["received"], 0)

    def test_peer_error_helper_shapes(self):
        peer_error = ShugonetAgentRuntime._peer_error
        self.assertEqual(peer_error({"type": "error", "message": "x"}), "x")
        self.assertEqual(peer_error({"status": "error", "reason": "y"}), "y")
        self.assertEqual(peer_error({"type": "error"}), "peer reported error")
        self.assertIsNone(peer_error({"type": "sync_result", "facts": []}))
        self.assertEqual(peer_error("not-a-dict"), "malformed peer reply")


class TestPeerDialDoesNotBlockInit(unittest.TestCase):
    """Dialing an unreachable peer must not stall the caller (ANR on Android).

    ``add_peer`` runs on the agent/service init path. Several unreachable
    peers, each costing a full connect timeout, block that path for seconds;
    on Android that is an ANR, which the user sees as the app dying.
    """

    def test_add_peer_after_start_returns_without_blocking(self):
        runtime = ShugonetAgentRuntime(agent_id="a", host="127.0.0.1",
                                       port=_free_port(), reconnect_interval=0)
        try:
            runtime.start()
            dial_started = threading.Event()

            def slow_dial(conn):
                dial_started.set()
                time.sleep(1.0)

            runtime._dial_peer = slow_dial  # shadows the static dialer
            started = time.time()
            runtime.add_peer("dead-peer", "127.0.0.1", _free_port())
            elapsed = time.time() - started

            self.assertLess(elapsed, 0.25,
                            "add_peer blocked on the peer handshake")
            self.assertTrue(dial_started.wait(2.0),
                            "the dial was dropped instead of delegated")
        finally:
            runtime.stop()

    def test_start_dials_peers_without_blocking(self):
        runtime = ShugonetAgentRuntime(agent_id="a", host="127.0.0.1",
                                       port=_free_port(), reconnect_interval=0,
                                       peer_map={"dead": ("127.0.0.1",
                                                          _free_port())})
        dial_started = threading.Event()
        original = ShugonetAgentRuntime._dial_peer
        ShugonetAgentRuntime._dial_peer = staticmethod(
            lambda conn: (dial_started.set(), time.sleep(1.0)))
        try:
            started = time.time()
            runtime.start()
            elapsed = time.time() - started
            self.assertLess(elapsed, 0.25,
                            "start() blocked on the peer handshake")
            self.assertTrue(dial_started.wait(2.0))
        finally:
            ShugonetAgentRuntime._dial_peer = original
            runtime.stop()


if __name__ == "__main__":
    unittest.main()

