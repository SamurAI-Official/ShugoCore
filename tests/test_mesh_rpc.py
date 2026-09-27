"""Mesh RPC layer split: launcher safety, launch shape, capacity planning.

The numbers in these tests come from the 2026-09-20 hardware spike recorded in
``docs/layer_split_rpc.md``: a Tab S9 FE held 402 MB of layers while the host's
RSS fell 677 MB -> 206 MB, and the device advertised 5425 MiB free while only
~504 MB was actually available.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mesh_rpc import (  # noqa: E402
    DEFAULT_RESERVE_BYTES,
    MeshRpcLauncher,
    find_rpc_server,
    is_private_bind,
    plan_layer_split,
    process_alive,
    process_rss_mb,
    usable_headroom,
)
from shugocore_agent import create_agent  # noqa: E402

MIB = 1024 * 1024


class _FakeProc:
    def __init__(self, alive=True):
        self.pid = 4242
        self._alive = alive
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True
        self._alive = False


class _FakePopen:
    """Records the command (and env) it was asked to run."""

    def __init__(self, alive=True, boom=False):
        self.commands = []
        self.envs = []
        self.proc = _FakeProc(alive=alive)
        self.boom = boom

    def __call__(self, cmd, **kwargs):
        self.commands.append(list(cmd))
        self.envs.append(kwargs.get("env"))
        if self.boom:
            raise OSError("exec denied")
        return self.proc


class _FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event_type, payload):
        self.events.append((event_type, dict(payload or {})))

    def types(self):
        return [e[0] for e in self.events]


class TestFindRpcServer(unittest.TestCase):
    def test_env_override_wins(self):
        os.environ["SHUGOCORE_RPC_SERVER"] = "/opt/rpc/ggml-rpc-server"
        try:
            self.assertEqual(find_rpc_server(prefix="/termux"),
                             "/opt/rpc/ggml-rpc-server")
        finally:
            os.environ.pop("SHUGOCORE_RPC_SERVER", None)

    def test_prefix_binary_is_used_when_executable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            binary = bindir / "ggml-rpc-server"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            self.assertEqual(find_rpc_server(prefix=tmp), str(binary))

    def test_which_fallback_and_missing(self):
        self.assertEqual(find_rpc_server(prefix="", which=lambda n: "/usr/bin/" + n),
                         "/usr/bin/ggml-rpc-server")
        self.assertIsNone(find_rpc_server(prefix="", which=lambda n: None))


class TestPrivateBind(unittest.TestCase):
    def test_private_addresses(self):
        for host in ("127.0.0.1", "localhost", "192.168.1.164", "10.0.0.5",
                     "172.16.4.4", "100.101.102.103", "::1"):
            with self.subTest(host=host):
                self.assertTrue(is_private_bind(host))

    def test_public_and_wildcard_addresses(self):
        # The wildcard is the case llama.cpp warns about: it includes whatever
        # public interface the device has.
        for host in ("0.0.0.0", "::", "8.8.8.8", "example.com", ""):
            with self.subTest(host=host):
                self.assertFalse(is_private_bind(host))



class TestMeshRpcLauncher(unittest.TestCase):
    def test_start_refuses_without_a_binary(self):
        popen = _FakePopen()
        launcher = MeshRpcLauncher(binary="", popen=popen)
        self.assertFalse(launcher.start())
        self.assertEqual(popen.commands, [])
        self.assertFalse(launcher.running())

    def test_start_refuses_a_wildcard_bind_unless_allowed(self):
        audit = _FakeAudit()
        popen = _FakePopen()
        launcher = MeshRpcLauncher(binary="/bin/ggml-rpc-server", host="0.0.0.0",
                                   popen=popen, audit=audit)
        self.assertFalse(launcher.start())
        self.assertEqual(popen.commands, [])
        self.assertIn("mesh_rpc_bind_refused", audit.types())

    def test_lan_bind_is_allowed_but_audited(self):
        audit = _FakeAudit()
        popen = _FakePopen()
        launcher = MeshRpcLauncher(binary="/bin/ggml-rpc-server", host="0.0.0.0",
                                   port=50061, threads=4, allow_lan=True,
                                   popen=popen, audit=audit)
        self.assertTrue(launcher.start())
        self.assertIn("mesh_rpc_bind_exposed", audit.types())
        self.assertIn("mesh_rpc_started", audit.types())
        self.assertEqual(popen.commands[0][:5],
                         ["/bin/ggml-rpc-server", "-H", "0.0.0.0", "-p", "50061"])

    def test_private_bind_needs_no_override(self):
        popen = _FakePopen()
        launcher = MeshRpcLauncher(binary="/bin/ggml-rpc-server",
                                   host="127.0.0.1", devices=["CPU"],
                                   extra_args=["-c"], popen=popen)
        self.assertTrue(launcher.start())
        self.assertEqual(popen.commands[0][-5:],
                         ["-t", str(launcher.threads), "-d", "CPU", "-c"])

    def test_spawn_failure_is_fail_closed(self):
        popen = _FakePopen(boom=True)
        launcher = MeshRpcLauncher(binary="/bin/ggml-rpc-server",
                                   host="127.0.0.1", popen=popen)
        self.assertFalse(launcher.start())
        self.assertFalse(launcher.running())

    def test_stop_is_idempotent(self):
        popen = _FakePopen()
        launcher = MeshRpcLauncher(binary="/bin/ggml-rpc-server",
                                   host="127.0.0.1", popen=popen)
        self.assertTrue(launcher.start())
        self.assertTrue(launcher.running())
        self.assertTrue(launcher.stop())
        self.assertFalse(launcher.running())
        self.assertTrue(launcher.stop())

    def test_child_env_points_the_loader_at_the_packaged_libraries(self):
        # Device-measured requirement: a directly-exec'd helper does not get
        # nativeLibraryDir on its search path, so the packaged server fails with
        # 'library "libggml.so" not found' unless LD_LIBRARY_PATH is set.
        popen = _FakePopen()
        binary = "/data/app/pkg/lib/arm64/libshugocore_rpc_server.so"
        launcher = MeshRpcLauncher(binary=binary, host="127.0.0.1", popen=popen)
        self.assertTrue(launcher.start())
        env = popen.envs[0]
        self.assertIsNotNone(env)
        self.assertEqual(env["LD_LIBRARY_PATH"].split(":")[0],
                         "/data/app/pkg/lib/arm64")

    def test_child_env_preserves_an_inherited_loader_path(self):
        popen = _FakePopen()
        os.environ["LD_LIBRARY_PATH"] = "/pre-existing"
        try:
            launcher = MeshRpcLauncher(binary="/opt/rpc/ggml-rpc-server",
                                       host="127.0.0.1", popen=popen)
            self.assertTrue(launcher.start())
        finally:
            os.environ.pop("LD_LIBRARY_PATH", None)
        self.assertEqual(popen.envs[0]["LD_LIBRARY_PATH"],
                         "/opt/rpc:/pre-existing")


class TestUsableHeadroom(unittest.TestCase):
    def test_unknown_memory_is_zero(self):
        self.assertEqual(usable_headroom(None), 0)
        self.assertEqual(usable_headroom(0), 0)

    def test_reserve_is_held_back(self):
        self.assertEqual(usable_headroom(512 * MIB, reserve_bytes=192 * MIB),
                         320 * MIB)

    def test_advertised_total_cannot_raise_the_budget(self):
        # The measured hazard: 5425 MiB advertised against ~504 MB available.
        room = usable_headroom(504 * MIB, advertised_bytes=5425 * MIB,
                               reserve_bytes=192 * MIB)
        self.assertEqual(room, 312 * MIB)

    def test_advertised_lower_than_measured_clamps_down(self):
        self.assertEqual(usable_headroom(600 * MIB, advertised_bytes=300 * MIB,
                                         reserve_bytes=0), 300 * MIB)

    def test_never_negative(self):
        self.assertEqual(usable_headroom(64 * MIB,
                                         reserve_bytes=DEFAULT_RESERVE_BYTES), 0)


class TestAgentMeshRpcEntryPoint(unittest.TestCase):
    """The debug entry point the service's broadcast receiver calls.

    The adb shell user cannot exec an app's nativeLibraryDir, so the app itself
    has to start the peripheral; this is the make-or-break call for the headless
    probes, and it must never raise (its caller is a broadcast receiver).
    """

    def setUp(self):
        import json
        import tempfile
        self.json = json
        self.tmp = tempfile.mkdtemp(prefix="mesh_entry_")
        self.agent = create_agent(device_caps="Exynos-1380",
                                  api_url="http://127.0.0.1:11434",
                                  data_dir=self.tmp)
        import mesh_rpc as mesh_module

        class _FakeLauncher:
            instances = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.started = False
                self.stopped = False
                _FakeLauncher.instances.append(self)

            def start(self):
                # Same fail-closed rule as the real launcher.
                self.started = bool(self.kwargs.get("binary"))
                return self.started

            def running(self):
                return self.started and not self.stopped

            def stop(self):
                self.stopped = True
                return True

            def endpoint(self):
                return f"{self.kwargs.get('host')}:{self.kwargs.get('port')}"

        self._real = mesh_module.MeshRpcLauncher
        self._fake = _FakeLauncher
        mesh_module.MeshRpcLauncher = _FakeLauncher

    def tearDown(self):
        import mesh_rpc as mesh_module
        mesh_module.MeshRpcLauncher = self._real
        try:
            self.agent.cleanup()
        except Exception:
            pass

    def _packaged_lib_dir(self):
        import tempfile
        lib_dir = tempfile.mkdtemp(prefix="mesh_lib_")
        binary = os.path.join(lib_dir, "libshugocore_rpc_server.so")
        with open(binary, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
        return lib_dir, binary

    def test_start_uses_the_packaged_binary_and_an_exposed_bind(self):
        lib_dir, binary = self._packaged_lib_dir()
        summary = self.json.loads(
            self.agent.debug_mesh_rpc("start", lib_dir, "50061", "1"))
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["binary"], binary)
        self.assertEqual(summary["endpoint"], "0.0.0.0:50061")
        self.assertTrue(summary["running"])
        self.assertTrue(self._fake.instances[-1].kwargs["allow_lan"])

    def test_default_bind_is_loopback_only(self):
        lib_dir, _ = self._packaged_lib_dir()
        summary = self.json.loads(self.agent.debug_mesh_rpc("start", lib_dir))
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["endpoint"], "127.0.0.1:50052")
        self.assertFalse(self._fake.instances[-1].kwargs["allow_lan"])

    def test_stop_reports_success_and_is_idempotent(self):
        lib_dir, _ = self._packaged_lib_dir()
        self.agent.debug_mesh_rpc("start", lib_dir)
        stopped = self.json.loads(self.agent.debug_mesh_rpc("stop"))
        self.assertTrue(stopped["ok"])
        self.assertFalse(stopped["running"])
        self.assertTrue(self._fake.instances[-1].stopped)
        self.assertTrue(self.json.loads(self.agent.debug_mesh_rpc("stop"))["ok"])

    def test_start_without_a_binary_is_fail_closed(self):
        import mesh_rpc as mesh_module
        real_find = mesh_module.find_rpc_server
        mesh_module.find_rpc_server = lambda *a, **k: None
        try:
            summary = self.json.loads(self.agent.debug_mesh_rpc("start", ""))
        finally:
            mesh_module.find_rpc_server = real_find
        self.assertFalse(summary["ok"])
        self.assertFalse(summary["running"])
        self.assertIsNone(self.agent._mesh_rpc)

    def test_a_second_start_replaces_the_first(self):
        lib_dir, _ = self._packaged_lib_dir()
        self.agent.debug_mesh_rpc("start", lib_dir, "50052", "0")
        self.agent.debug_mesh_rpc("start", lib_dir, "50053", "0")
        self.assertTrue(self._fake.instances[-2].stopped)
        self.assertFalse(self.json.loads(self.agent.debug_mesh_rpc("stop"))
                         ["running"])


class PeripheralProcessTestCase(unittest.TestCase):
    """Liveness/RSS must work on the platform we actually measure on.

    The benchmark answered "is the peripheral still there?" with `ps -p`, which
    does not exist on Windows: the check raised instead of reporting, and the
    first loopback run died on it.
    """

    def test_this_process_is_alive(self):
        self.assertTrue(process_alive(os.getpid()))

    def test_a_bogus_pid_is_not_alive(self):
        self.assertFalse(process_alive(999999))
        self.assertFalse(process_alive(0))
        self.assertFalse(process_alive("not-a-pid"))

    def test_rss_of_this_process_is_measurable(self):
        self.assertGreater(process_rss_mb(os.getpid()), 0.0)

    def test_rss_of_a_bogus_pid_is_zero_not_an_error(self):
        self.assertEqual(process_rss_mb(999999), 0.0)
        self.assertEqual(process_rss_mb(None), 0.0)


class DelegatedMeshRpcTestCase(unittest.TestCase):
    """The primary asks a device to start its own peripheral, over the mesh.

    Only the app can execute the peripheral binary inside a phone, so this action
    is how the layer split reaches a device without an adb debug broadcast.
    """

    def _probe(self, node_id="shugo-mac", primary="shugo-desktop"):
        from mesh_election import MeshElection
        from shugocore_agent import AndroidAgent

        agent = AndroidAgent.__new__(AndroidAgent)
        agent.device_caps = "desktop"
        agent.node_id = node_id
        election = MeshElection(node_id, priority=500)
        election.observe_heartbeat({"node_id": primary, "priority": 10,
                                    "mem_available_bytes": 1 << 30})
        agent.mesh_election = election
        agent._delegated_from = None
        agent._native_library_dir = "/data/app/lib/arm64"
        agent.log = lambda *a, **k: None
        return agent

    def test_a_delegated_start_uses_the_registered_library_dir(self):
        agent = self._probe()
        seen = {}

        def _fake(action="start", native_library_dir="", port=0, lan=0):
            seen.update(action=action, lib=native_library_dir, port=port,
                        lan=lan)
            return '{"ok": true, "endpoint": "0.0.0.0:50052", "running": true}'

        agent.debug_mesh_rpc = _fake
        agent._delegated_from = "shugo-desktop"
        result = agent._execute_mesh_rpc(
            {"params": {"action": "start", "port": 50052, "lan": 1}})
        self.assertEqual(result["status"], "success")
        self.assertEqual(seen["lib"], "/data/app/lib/arm64")
        self.assertEqual(seen["port"], 50052)
        self.assertEqual(seen["lan"], 1)
        self.assertTrue(result["rpc"]["running"])

    def test_a_failed_peripheral_reports_the_reason(self):
        agent = self._probe()
        agent.debug_mesh_rpc = lambda **_kw: '{"ok": false, "error": "no binary"}'
        agent._delegated_from = "shugo-desktop"
        result = agent._execute_mesh_rpc({"params": {"action": "start"}})
        self.assertEqual(result["status"], "error")
        self.assertIn("no binary", result["reason"])

    def test_only_the_primary_may_ask_a_device_to_start(self):
        agent = self._probe(primary="shugo-tab")
        agent._delegated_from = "shugo-desktop"
        result = agent._execute_mesh_rpc({"params": {"action": "start"}})
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "mesh_follower")


if __name__ == "__main__":
    unittest.main()


def _load_bench():
    """Load tests/mesh_rpc_bench.py without importing the tests package."""
    import importlib.util
    module_path = Path(__file__).resolve().parent / "mesh_rpc_bench.py"
    spec = importlib.util.spec_from_file_location("mesh_rpc_bench_under_test",
                                                  module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["mesh_rpc_bench_under_test"] = module
    spec.loader.exec_module(module)
    return module


class _BenchAdb:
    """Records adb invocations and answers canned ones."""

    def __init__(self, rc=0, stdout=""):
        self.calls = []
        self.rc = rc
        self.stdout = stdout

    def __call__(self, serial, *args, timeout=60.0):
        import subprocess as _sp
        self.calls.append((serial, list(args)))
        return _sp.CompletedProcess(args, self.rc, stdout=self.stdout,
                                    stderr="")


class TestBenchAdbContract(unittest.TestCase):
    """The benchmark drives devices over adb; pin the command shapes."""

    def setUp(self):
        self.bench = _load_bench()
        self.serial = "adb-TEST"

    def test_device_lan_ip_parses_wlan0(self):
        fake = _BenchAdb(stdout="2: wlan0: <BROADCAST,MULTICAST,UP> mtu 1500\n"
                                "    inet 192.168.1.164/24 brd 192.168.1.255 "
                                "scope global wlan0\n")
        self.bench.adb = fake
        self.assertEqual(self.bench.device_lan_ip(self.serial),
                         "192.168.1.164")
        self.assertEqual(fake.calls[0][1][1:],
                         ["ip", "-4", "addr", "show", "wlan0"])

    def test_device_lan_ip_empty_on_failure(self):
        fake = _BenchAdb(rc=1, stdout="")
        self.bench.adb = fake
        self.assertEqual(self.bench.device_lan_ip(self.serial), "")

    def test_usb_forward_is_up_then_torn_down(self):
        fake = _BenchAdb()
        self.bench.adb = fake
        self.assertTrue(self.bench.setup_usb_forward(self.serial, 50552, 50052))
        self.bench.remove_usb_forward(self.serial, 50552)
        commands = [args for _, args in fake.calls]
        self.assertIn(["forward", "--remove", "tcp:50552"], commands)
        self.assertIn(["forward", "tcp:50552", "tcp:50052"], commands)
        self.assertEqual(commands.index(["forward", "--remove", "tcp:50552"]),
                         0)

    def test_usb_forward_failure_is_reported_not_raised(self):
        fake = _BenchAdb(rc=1, stdout="error: device offline")
        self.bench.adb = fake
        self.assertFalse(self.bench.setup_usb_forward(self.serial, 50552,
                                                      50052))

