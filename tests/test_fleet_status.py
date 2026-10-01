"""The fleet capture: the transcript the mesh claims are judged from.

`runtime/tools/fleet_status.py` is a producer, and a producer is only as honest as its output.
These check its lines against the *real* parsers -- including the cases that must fail: a sync
that moved nothing, a node that never took the lease, a harness error. A claim cannot go green
on a transcript that says nothing, which is the whole reason the producer exists instead of a
hand-written status line.
"""
import contextlib
import importlib.util
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import claim_matrix  # noqa: E402

MODULE = os.path.join(ROOT, "runtime", "tools", "fleet_status.py")


def _module():
    spec = importlib.util.spec_from_file_location("fleet_status_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FLEET = _module()


def _console():
    """The operator terminal, loaded as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location(
        "console_under_test",
        os.path.join(ROOT, "clients", "desktop", "shugocore_desktop.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

PEERS = [{"device_id": "shugo-mac", "priority": 10, "thermal_status": 0,
          "mem_available_bytes": 1680637952, "paired": True, "can_speak": True,
          "source": "mesh"},
         {"device_id": "shugo-a51", "priority": 500, "thermal_status": 0,
          "mem_available_bytes": 132042752, "paired": True, "can_speak": False,
          "source": "mesh"}]


def _transcript(role="primary", imported=7, peers=None, lease="shugo-desktop",
                shared=None):
    return "\n".join(FLEET.status_lines(
        node_id="shugo-desktop", port=9000, priority=1, role=role, lease=lease,
        peers=PEERS if peers is None else peers,
        mesh={"stats": {"imported": imported}},
        sync={"imported": imported, "rounds": 2, "failed": 0},
        shared=imported if shared is None else shared, seconds=45.0))


class TheTranscriptTestCase(unittest.TestCase):
    """The producer and the parsers have to agree, or the claim means nothing."""

    def test_a_healthy_fleet_satisfies_the_real_parsers(self):
        text = _transcript()
        self.assertEqual(claim_matrix.hub_role(text), (True, "role=primary"))
        ok, detail = claim_matrix.hub_imported(text)
        self.assertTrue(ok, detail)

    def test_a_session_that_imported_nothing_still_proves_a_converged_fleet(self):
        # The trap: a healthy hive imports nothing on the second sync, so a claim that reads
        # only a session delta fails exactly when the mesh is working.
        text = _transcript(imported=0, shared=22)
        ok, detail = claim_matrix.hub_imported(text)
        self.assertTrue(ok, detail)
        self.assertIn("imported=22", detail)

    def test_a_sync_that_moved_nothing_fails_the_memory_claim(self):
        ok, detail = claim_matrix.hub_imported(_transcript(imported=0, shared=0))
        self.assertFalse(ok, "a fleet that imported nothing passed the memory claim")
        self.assertIn("imported=0", detail)

    def test_a_node_that_never_took_the_lease_fails_the_election_claim(self):
        ok, detail = claim_matrix.hub_role(_transcript(role="follower", lease="shugo-mac"))
        self.assertFalse(ok, "a follower passed the election claim")
        self.assertIn("follower", detail)

    def test_the_first_role_and_imported_are_the_meaningful_ones(self):
        text = _transcript()
        self.assertLess(text.index("role="), text.index("peer shugo-mac"),
                        "a peer line precedes the hub's own role")
        self.assertLess(text.index("imported="), text.index("verdict:"))

    def test_a_harness_error_is_recorded_and_fails_judgement(self):
        text = "\n".join(FLEET.status_lines(
            node_id="shugo-desktop", port=9000, priority=1, role="", lease="",
            peers=[], mesh={}, sync={}, error="RuntimeError: boom"))
        self.assertIn("error=RuntimeError: boom", text)
        self.assertFalse(claim_matrix.hub_role(text)[0])


class TheRosterLinesTestCase(unittest.TestCase):

    def test_bytes_are_readable(self):
        self.assertEqual(FLEET.human_bytes(1680637952), "1.57 GiB")
        self.assertEqual(FLEET.human_bytes(132042752), "125.93 MiB")

    def test_every_peer_appears_with_what_it_advertised(self):
        text = _transcript()
        self.assertIn("peer shugo-mac prio=10", text)
        self.assertIn("can_speak=True", text)
        self.assertIn("peer shugo-a51 prio=500", text)
        self.assertIn("can_speak=False", text)

    def test_a_quiet_mesh_says_so_rather_than_inventing_peers(self):
        text = _transcript(peers=[])
        self.assertIn("peers=0", text)
        self.assertNotIn("peer shugo-mac", text)


class TheTerminalRosterTestCase(unittest.TestCase):
    """`/nodes` reads the node's own records; the layout is pure and testable."""

    def test_the_roster_marks_the_lease_holder_and_sorts_by_priority(self):
        lines = _console().format_fleet(
            {"node_id": "shugo-desktop", "role": "primary", "peers": PEERS,
             "lease": "shugo-desktop"})
        self.assertIn("shugo-desktop (primary) (holds the lease)", lines[0])
        mac = next(index for index, line in enumerate(lines) if "shugo-mac" in line)
        a51 = next(index for index, line in enumerate(lines) if "shugo-a51" in line)
        self.assertLess(mac, a51, "a lower priority number has to come first")
        joined = "\n".join(lines)
        self.assertIn("1.57 GiB", joined)
        self.assertIn("lease held by      shugo-desktop", joined)

    def test_a_node_that_heard_nobody_says_so(self):
        lines = _console().format_fleet(
            {"node_id": "shugo-desktop", "role": "standalone", "peers": [], "lease": ""})
        self.assertIn("standalone", lines[0])
        self.assertIn("none heard yet", lines[1])


class TheNodesCommandTestCase(unittest.TestCase):
    """The terminal command itself, on a stub node. The roster formatter alone is not the
    feature: `/nodes` has to read the node's own telemetry, and a node that has heard nobody
    has to answer that way rather than printing an empty table."""

    class _Election:
        def primary(self):
            return "shugo-desktop"

    class _Agent:
        def __init__(self, peers, role="primary"):
            self.telemetry = {"mesh_peers": peers}
            self._role = role
            self.mesh_election = TheNodesCommandTestCase._Election()

        def get_status(self):
            return {"node_id": "shugo-desktop", "mesh_role": self._role}

    def _run(self, agent):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            handled = _console().handle_terminal_command(agent, "/nodes")
        return handled, out.getvalue()

    def test_the_command_prints_what_the_node_itself_has_heard(self):
        handled, text = self._run(self._Agent(PEERS))
        self.assertTrue(handled, "the terminal did not recognise /nodes")
        self.assertIn("shugo-mac", text)
        self.assertIn("holds the lease", text)

    def test_the_command_reports_a_node_that_heard_nobody(self):
        handled, text = self._run(self._Agent([], role="standalone"))
        self.assertTrue(handled)
        self.assertIn("none heard yet", text)
        self.assertNotIn("shugo-mac", text)
