#!/usr/bin/env python3
"""The desktop control plane must show the mesh it is actually part of.

Regression cover for a silent-dishonesty bug: on a host node,
``get_status()["mesh_peers"]`` is filled from the Android shell's telemetry,
which no host ever sends. The UI read that field, so a MacBook with three
connected peers rendered as a lone node. The runtime
(``shugonet_runtime.status()``) is the truth, and the UI now reads it.
"""
import importlib.util
import os
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "clients", "desktop", "shugocore_desktop.py")

def _load_ui():
    spec = importlib.util.spec_from_file_location("desktop_ui_under_test",
                                                  _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ui = _load_ui()


def _runtime(status):
    return types.SimpleNamespace(status=lambda: status)


def _agent(runtime):
    return types.SimpleNamespace(shugonet_runtime=runtime)


def _rt(results):
    """A runtime stub exposing the _outbound map and a scripted sync()."""
    def _sync(peer_id):
        return results.get(peer_id, {"status": "error", "message": "unreachable"})
    return types.SimpleNamespace(_outbound={p: None for p in results},
                                 sync=_sync)


class MeshSnapshotTestCase(unittest.TestCase):

    def _snapshot(self, runtime):
        controller = _ui.AgentController.__new__(_ui.AgentController)
        controller.agent = _agent(runtime) if runtime is not None else None
        return controller._mesh_snapshot()

    def test_reports_connected_peers_from_the_runtime(self):
        snap = self._snapshot(_runtime({
            "agent_id": "shugo-MacBook", "running": True, "port": 9000,
            "peers": ["shugo-a51", "shugo-tab", "shugo-desktop"],
            "connected_peers": ["shugo-a51", "shugo-tab", "shugo-desktop"],
            "stats": {"sent": 5531, "received": 1223, "errors": 0,
                      "imported": 654, "heartbeats_sent": 4,
                      "heartbeats_received": 13},
            "heartbeat": {"interval_s": 10, "advertising": True,
                          "handler": True, "heard": 3},
        }))
        self.assertEqual(snap["node_id"], "shugo-MacBook")
        self.assertEqual(snap["connected_count"], 3)
        self.assertEqual(len(snap["configured_peers"]), 3)
        self.assertTrue(snap["heartbeat"]["advertising"])
        self.assertEqual(snap["heartbeat"]["rx"], 13)
        self.assertEqual(snap["stats"]["imported"], 654)
        self.assertEqual(snap["stats"]["errors"], 0)

    def test_distinguishes_configured_from_connected(self):
        """A configured-but-unconnected peer must not read as connected."""
        snap = self._snapshot(_runtime({
            "agent_id": "n", "peers": ["a", "b", "c"],
            "connected_peers": ["a"],
        }))
        self.assertEqual(snap["connected_count"], 1)
        self.assertEqual(len(snap["configured_peers"]), 3)

    def test_no_runtime_is_empty_not_fabricated(self):
        self.assertEqual(self._snapshot(None), {})

    def test_a_raising_runtime_is_reported_not_swallowed(self):
        class Boom:
            def status(self):
                raise RuntimeError("socket gone")

        snap = self._snapshot(Boom())
        self.assertIn("socket gone", snap.get("error", ""))

    def test_malformed_status_does_not_raise(self):
        self.assertEqual(self._snapshot(_runtime("not-a-dict")), {})


class HeaderMeshTextTestCase(unittest.TestCase):
    """The header line must name the role and the live peer count."""

    def _render(self, status, mesh):
        # Each header cell needs its own sink: one shared dict would keep only
        # the last .config() call and every assertion would read "state".
        captured = {}

        def _cell(name):
            return types.SimpleNamespace(
                config=lambda **kw: captured.update({name: kw.get("text", "")}))

        ui = _ui.DesktopUI.__new__(_ui.DesktopUI)
        ui.controller = types.SimpleNamespace(interval=2.0)
        ui._hdr = {k: _cell(k) for k in
                   ("node", "backend", "model", "engine", "memory",
                    "mesh", "uptime", "ticks", "state")}
        ui._update_header({"status": status, "uptime": 1.0, "applied": {},
                           "running": True, "error": "", "mesh": mesh})
        return captured["mesh"]

    def test_follower_shows_role_primary_and_peer_count(self):
        text = self._render(
            {"device_caps": "macbook", "mesh_role": "follower",
             "mesh_primary": "shugo-desktop"},
            {"connected_count": 3, "configured_peers": ["a", "b", "c"]})
        self.assertIn("follower", text)
        self.assertIn("shugo-desktop", text)
        self.assertIn("3/3", text)

    def test_primary_is_shouted(self):
        text = self._render(
            {"mesh_role": "primary", "mesh_primary": "shugo-MacBook"},
            {"connected_count": 3, "configured_peers": ["a", "b", "c"]})
        self.assertIn("PRIMARY", text)
        self.assertIn("3/3", text)

    def test_partial_connectivity_is_visible(self):
        text = self._render(
            {"mesh_role": "follower", "mesh_primary": "shugo-desktop"},
            {"connected_count": 1, "configured_peers": ["a", "b", "c"]})
        self.assertIn("1/3", text)

    def test_mesh_off_is_stated_not_implied_lone(self):
        text = self._render({"mesh_role": "standalone"}, {})
        self.assertIn("mesh off", text)

    def test_unavailable_mesh_reports_the_error(self):
        text = self._render({"mesh_role": "follower"},
                            {"error": "RuntimeError: socket gone"})
        self.assertIn("socket gone", text)


class SensorCopyTestCase(unittest.TestCase):
    """The SENSORS pane must not claim the host has no camera or mic."""

    def test_copy_does_not_assert_a_desktop_has_no_camera(self):
        with open(_PATH, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("has no camera", source)
        self.assertIn("camera and microphone", source)


class LiveRuntimeTestCase(unittest.TestCase):
    """The snapshot must read a real Shugonet runtime, not just a stub.

    Mock-shaped tests pass even when the real runtime's status() keys differ;
    this binds the UI to the actual transport so a rename upstream breaks here.
    """

    def test_reads_the_real_runtime_status_shape(self):
        from agent_runtime import ShugonetAgentRuntime
        # Constructed but never started, so no socket is bound: the point is
        # the status() key shape the UI renders against.
        runtime = ShugonetAgentRuntime(agent_id="shugo-ui-probe", port=0)
        controller = _ui.AgentController.__new__(_ui.AgentController)
        controller.agent = types.SimpleNamespace(shugonet_runtime=runtime)
        snap = controller._mesh_snapshot()
        self.assertEqual(snap["node_id"], "shugo-ui-probe")
        # Keys the UI renders must exist on a real, unstarted runtime.
        for key in ("connected_peers", "configured_peers", "heartbeat",
                    "stats", "connected_count", "port", "running"):
            self.assertIn(key, snap)
        self.assertEqual(snap["connected_count"], 0)
        self.assertEqual(snap["connected_peers"], [])


class ControllerSyncTestCase(unittest.TestCase):
    """The UI's node must keep pulling peer facts, not just at boot."""

    def _controller(self, sync_interval=120.0):
        c = _ui.AgentController.__new__(_ui.AgentController)
        c.sync_interval = sync_interval
        c.sync_state = {"rounds": 0, "imported": 0, "failed": 0, "last": ""}
        c._logs = []
        c.log_seq = 0
        return c

    def test_pulls_every_peer_and_tallies_imports(self):
        c = self._controller()
        rt = _rt({"a": {"status": "success", "imported": 4},
                   "b": {"status": "success", "imported": 0}})
        c.agent = types.SimpleNamespace(shugonet_runtime=rt)
        c._sync_peers()
        self.assertEqual(c.sync_state["rounds"], 1)
        self.assertEqual(c.sync_state["imported"], 4)
        self.assertEqual(c.sync_state["failed"], 0)

    def test_zero_imports_is_not_a_failure(self):
        c = self._controller()
        rt = _rt({"a": {"status": "success", "imported": 0, "duplicates": 200}})
        c.agent = types.SimpleNamespace(shugonet_runtime=rt)
        c._sync_peers()
        self.assertEqual(c.sync_state["failed"], 0)
        self.assertEqual(c.sync_state["rounds"], 1)

    def test_one_dead_peer_does_not_stop_the_others(self):
        c = self._controller()

        def _sync(peer):
            if peer == "dead":
                raise TimeoutError("timed out")
            return {"status": "success", "imported": 2}

        rt = types.SimpleNamespace(_outbound={"dead": None, "live": None},
                                   sync=_sync)
        c.agent = types.SimpleNamespace(shugonet_runtime=rt)
        c._sync_peers()
        self.assertEqual(c.sync_state["failed"], 1)
        self.assertEqual(c.sync_state["imported"], 2)
        self.assertIn("dead", c.sync_state["last"])

    def test_no_mesh_is_a_quiet_noop(self):
        c = self._controller()
        c.agent = types.SimpleNamespace(shugonet_runtime=None)
        c._sync_peers()
        self.assertEqual(c.sync_state["rounds"], 0)

    def test_error_status_counts_as_failed(self):
        c = self._controller()
        rt = _rt({"a": {"status": "error", "message": "unreachable"}})
        c.agent = types.SimpleNamespace(shugonet_runtime=rt)
        c._sync_peers()
        self.assertEqual(c.sync_state["failed"], 1)

    def test_flag_defaults_to_off(self):
        self.assertEqual(_ui.build_parser().parse_args([]).sync_interval, 0.0)

    def test_flag_parses(self):
        self.assertEqual(
            _ui.build_parser().parse_args(["--sync-interval", "300"]
                                          ).sync_interval, 300.0)


if __name__ == "__main__":
    unittest.main()
