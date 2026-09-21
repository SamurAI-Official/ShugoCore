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
    usable_headroom,
)

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
    """Records the command it was asked to run."""

    def __init__(self, alive=True, boom=False):
        self.commands = []
        self.proc = _FakeProc(alive=alive)
        self.boom = boom

    def __call__(self, cmd, **kwargs):
        self.commands.append(list(cmd))
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
