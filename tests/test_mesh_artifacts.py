"""Build artifacts (updates) travel between hive nodes over the mesh.

Two real runtimes on loopback: one shares a directory, the other stages what it
receives. The transport is the same one memory sync uses, so these checks are
about the properties that matter for shipping a *build*: the bytes are verified,
a partial transfer never becomes a real artifact, and nothing outside the share
directory is reachable.
"""
import hashlib
import os
import socket
import tempfile
import time
import unittest

from agent_runtime import ShugonetAgentRuntime

_BIG = 700 * 1024          # several chunks at the test chunk size
_CHUNK = 64 * 1024
_TOKEN = "fleet-secret"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(predicate, timeout: float = 8.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class MeshArtifactTestCase(unittest.TestCase):
    """A node that has a build, and a node that wants it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugo_artifact_")
        self.share = os.path.join(self.tmp, "share")
        self.stage_owner = os.path.join(self.tmp, "stage-owner")
        self.stage_peer = os.path.join(self.tmp, "stage-peer")
        os.makedirs(self.share)
        self.payload = bytes(range(256)) * (_BIG // 256) + b"tail"
        self.name = "app-debug.apk"
        with open(os.path.join(self.share, self.name), "wb") as handle:
            handle.write(self.payload)
        self.digest = hashlib.sha256(self.payload).hexdigest()
        # A file a peer must never be able to reach through a crafted name.
        with open(os.path.join(self.tmp, "secret.txt"), "wb") as handle:
            handle.write(b"not for you")

        self.owner = ShugonetAgentRuntime(
            agent_id="shugo-desktop", host="127.0.0.1", port=_free_port(),
            heartbeat_interval=0, auth_token=_TOKEN,
            artifact_root=self.share, artifact_dir=self.stage_owner,
            artifact_chunk_bytes=_CHUNK)
        self.peer = ShugonetAgentRuntime(
            agent_id="shugo-mac", host="127.0.0.1", port=_free_port(),
            heartbeat_interval=0, auth_token=_TOKEN,
            artifact_dir=self.stage_peer, artifact_chunk_bytes=_CHUNK)
        self.owner.start()
        self.peer.start()
        self.peer.add_peer("shugo-desktop", "127.0.0.1",
                           self.owner.status()["port"])
        self.owner.add_peer("shugo-mac", "127.0.0.1",
                            self.peer.status()["port"])
        self.assertTrue(_wait_for(lambda: (self.peer.reconnect_peers() >= 1
                                           and self.owner.reconnect_peers() >= 1)),
                        "loopback nodes did not connect")

    def tearDown(self):
        self.owner.stop()
        self.peer.stop()

    def staged(self, name=None):
        return os.path.join(self.stage_peer, name or self.name)

    def test_manifest_reports_size_and_digest(self):
        manifest = self.peer.artifact_manifest("shugo-desktop", self.name)
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["size"], len(self.payload))
        self.assertEqual(manifest["sha256"], self.digest)

    def test_shared_files_are_listed(self):
        listing = self.owner.list_artifacts()
        self.assertEqual([item["name"] for item in listing], [self.name])
        self.assertEqual(listing[0]["sha256"], self.digest)

    def test_fetch_transfers_verified_bytes(self):
        result = self.peer.fetch_artifact("shugo-desktop", self.name)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["sha256"], self.digest)
        self.assertEqual(result["bytes"], len(self.payload))
        self.assertGreater(result["chunks"], 1)          # really chunked
        with open(self.staged(), "rb") as handle:
            self.assertEqual(handle.read(), self.payload)
        self.assertFalse(os.path.exists(self.staged() + ".part"))
        self.assertEqual(self.peer.status()["artifacts"]["received"], 1)
        self.assertEqual(self.peer.artifact_receipts()[0]["name"], self.name)
        self.assertEqual(self.owner.status()["artifacts"]["sent"], 0)

    def test_fetch_refuses_a_digest_that_does_not_match(self):
        result = self.peer.fetch_artifact("shugo-desktop", self.name,
                                          expect_sha256="00" * 32)
        self.assertEqual(result["status"], "refused")
        self.assertFalse(os.path.exists(self.staged()))

    def test_fetch_refuses_an_unshared_name(self):
        result = self.peer.fetch_artifact("shugo-desktop", "nope.apk")
        self.assertEqual(result["status"], "refused")
        self.assertIn("not shared", result["reason"])

    def test_traversal_names_never_reach_outside_the_share(self):
        outside = os.path.join(self.tmp, "secret.txt")
        before = open(outside, "rb").read()
        for name in ("../secret.txt", "..\\secret.txt", ".hidden", "..",
                     "/etc/passwd"):
            result = self.peer.fetch_artifact("shugo-desktop", name)
            self.assertNotEqual(result["status"], "success", name)
        # Nothing was written anywhere: not into the stage dir (which the
        # transfer never even had to create), and the file outside the share
        # root is byte-identical.
        if os.path.isdir(self.stage_peer):
            self.assertEqual(os.listdir(self.stage_peer), [])
        self.assertEqual(open(outside, "rb").read(), before)

    def test_a_link_out_of_the_share_is_not_servable(self):
        # A symlink inside the share root that points outside it must not be
        # followed: containment is checked on the real path.
        outside = os.path.join(self.tmp, "secret.txt")
        link = os.path.join(self.share, "link.txt")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks unavailable on this platform")
        self.assertIsNone(self.owner.artifact_descriptor("link.txt"))
        result = self.peer.fetch_artifact("shugo-desktop", "link.txt")
        self.assertEqual(result["status"], "refused")

    def test_chunk_requests_are_clamped_to_the_configured_size(self):
        result = self.peer.fetch_artifact("shugo-desktop", self.name)
        self.assertEqual(result["status"], "success")
        # A whole-file request is answered one chunk at a time, never in full.
        self.assertEqual(result["chunks"], -(-len(self.payload) // _CHUNK))


class MeshArtifactOfferTestCase(unittest.TestCase):
    """The push path: the node holding the build offers it, the peer pulls."""

    setUp = MeshArtifactTestCase.setUp
    tearDown = MeshArtifactTestCase.tearDown
    staged = MeshArtifactTestCase.staged

    def test_offer_makes_the_peer_pull_the_build(self):
        result = self.owner.offer_artifact("shugo-mac", self.name)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["size"], len(self.payload))
        self.assertTrue(_wait_for(lambda: os.path.exists(self.staged())),
                        "the peer never pulled the offered build")
        with open(self.staged(), "rb") as handle:
            self.assertEqual(hashlib.sha256(handle.read()).hexdigest(),
                             self.digest)
        self.assertEqual(self.owner.status()["artifacts"]["sent"], 1)
        self.assertEqual(self.peer.status()["artifacts"]["received"], 1)

    def test_offer_refuses_a_file_it_does_not_share(self):
        result = self.owner.offer_artifact("shugo-mac", "nope.apk")
        self.assertEqual(result["status"], "refused")
        self.assertIn("share directory", result["reason"])

    def test_offer_refuses_when_the_receiver_cap_is_exceeded(self):
        small = ShugonetAgentRuntime(
            agent_id="shugo-a51", host="127.0.0.1", port=_free_port(),
            heartbeat_interval=0, auth_token=_TOKEN,
            artifact_dir=self.stage_owner, max_artifact_bytes=64 * 1024)
        small.start()
        try:
            small.add_peer("shugo-desktop", "127.0.0.1",
                           self.owner.status()["port"])
            self.assertTrue(_wait_for(lambda: small.reconnect_peers() >= 1))
            self.owner.add_peer("shugo-a51", "127.0.0.1", small.status()["port"])
            self.assertTrue(_wait_for(lambda: self.owner.reconnect_peers() >= 2))
            result = self.owner.offer_artifact("shugo-a51", self.name)
            self.assertEqual(result["status"], "refused")
            self.assertIn("cap", result["reason"])
        finally:
            small.stop()

    def test_a_build_over_the_cap_is_not_served_at_all(self):
        big_name = "huge.bin"
        with open(os.path.join(self.share, big_name), "wb") as handle:
            handle.write(b"x" * (200 * 1024))
        small = ShugonetAgentRuntime(
            agent_id="shugo-tiny", host="127.0.0.1", port=_free_port(),
            heartbeat_interval=0, auth_token=_TOKEN, artifact_root=self.share,
            max_artifact_bytes=64 * 1024)
        self.assertIsNone(small.artifact_descriptor(big_name))
        self.assertEqual(small.list_artifacts(), [])
        self.assertEqual(small._artifact_manifest_reply(big_name)["type"], "error")

    def test_a_peer_without_the_secret_is_refused(self):
        outsider = ShugonetAgentRuntime(
            agent_id="shugo-intruder", host="127.0.0.1", port=_free_port(),
            heartbeat_interval=0, artifact_dir=self.stage_owner)
        outsider.start()
        try:
            outsider.add_peer("shugo-desktop", "127.0.0.1",
                              self.owner.status()["port"])
            self.assertTrue(_wait_for(lambda: outsider.reconnect_peers() >= 1))
            result = outsider.fetch_artifact("shugo-desktop", self.name)
            self.assertNotEqual(result["status"], "success")
            self.assertFalse(os.path.exists(os.path.join(self.stage_owner,
                                                         self.name)))
            self.assertEqual(self.owner.status()["artifacts"]["sent"], 0)
        finally:
            outsider.stop()

    def test_receipts_carry_the_path_and_digest(self):
        self.peer.fetch_artifact("shugo-desktop", self.name)
        receipt = self.peer.artifact_receipts()[-1]
        self.assertEqual(receipt["peer"], "shugo-desktop")
        self.assertEqual(receipt["bytes"], len(self.payload))
        self.assertTrue(receipt["path"].endswith(self.name))
        self.assertEqual(receipt["sha256"], self.digest)


