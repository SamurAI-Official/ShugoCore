"""KV-cache / context mesh split -- offline prototype tests.

Proves the shard model, allocator memory accounting, protocol round-trip, and
the simulated mesh lifecycle (join/leave/rebalance/put/get/evict).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kv_mesh.shard import (KVShard, ShardSpec, make_kv_shards,
                            make_context_shards)
from kv_mesh.allocator import KVAllocator
from kv_mesh.simulator import MeshSimulator
import kv_mesh.protocol as proto


# A small model shape for testing (not a real model).
SPEC = ShardSpec(model_id="test-model", num_layers=8, num_heads=4,
                 head_dim=32, max_seq_len=64, dtype_bytes=2)


class ShardModelTest(unittest.TestCase):
    def test_kv_bytes_per_token_per_layer(self):
        self.assertEqual(SPEC.kv_bytes_per_token_per_layer(), 512)

    def test_kv_total_bytes(self):
        self.assertEqual(SPEC.kv_total_bytes(), 8 * 64 * 512)

    def test_make_kv_shards_sequence(self):
        shards = make_kv_shards(SPEC, 4, split="sequence")
        self.assertEqual(len(shards), 4)
        for s in shards:
            self.assertEqual(s.layer_start, 0)
            self.assertEqual(s.layer_end, SPEC.num_layers)
        self.assertEqual(shards[0].seq_start, 0)
        self.assertEqual(shards[-1].seq_end, SPEC.max_seq_len)
        for a, b in zip(shards, shards[1:]):
            self.assertEqual(a.seq_end, b.seq_start)

    def test_make_kv_shards_layers(self):
        shards = make_kv_shards(SPEC, 2, split="layers")
        self.assertEqual(len(shards), 2)
        self.assertEqual(shards[0].layer_start, 0)
        self.assertEqual(shards[1].layer_end, SPEC.num_layers)

    def test_make_context_shards(self):
        shards = make_context_shards(SPEC, 4)
        self.assertEqual(len(shards), 4)
        self.assertEqual(shards[0].token_start, 0)
        self.assertEqual(shards[-1].token_end, SPEC.max_seq_len)

    def test_zero_shards_rejected(self):
        with self.assertRaises(ValueError):
            make_kv_shards(SPEC, 0)

    def test_shard_canonical_id(self):
        sid = KVShard.canonical_id("m", 0, 8, 0, 4, 0, 64)
        self.assertIn("L0-8", sid)
        self.assertIn("S0-64", sid)


class AllocatorTest(unittest.TestCase):
    def setUp(self):
        self.alloc = KVAllocator()

    def test_register_and_capacity(self):
        self.alloc.register_node("d1", 1000)
        self.assertEqual(self.alloc.remaining("d1"), 1000)

    def test_assign_fits(self):
        self.alloc.register_node("d1", 1000)
        s = KVShard("s1", "m", 0, 8, 0, 4, 0, 64, 500)
        self.assertEqual(self.alloc.assign(s), "d1")
        self.assertEqual(self.alloc.remaining("d1"), 500)

    def test_assign_respects_cap(self):
        self.alloc.register_node("d1", 100)
        s = KVShard("s1", "m", 0, 8, 0, 4, 0, 64, 500)
        self.assertIsNone(self.alloc.assign(s))
        self.assertIsNone(s.device_id)

    def test_assign_picks_biggest_room(self):
        self.alloc.register_node("d1", 300)
        self.alloc.register_node("d2", 1000)
        s = KVShard("s1", "m", 0, 8, 0, 4, 0, 64, 200)
        self.assertEqual(self.alloc.assign(s), "d2")

    def test_assign_prefer_hint(self):
        self.alloc.register_node("d1", 1000)
        self.alloc.register_node("d2", 1000)
        s = KVShard("s1", "m", 0, 8, 0, 4, 0, 64, 200)
        self.assertEqual(self.alloc.assign(s, prefer="d1"), "d1")

    def test_evict_frees_space(self):
        self.alloc.register_node("d1", 500)
        s = KVShard("s1", "m", 0, 8, 0, 4, 0, 64, 400)
        self.alloc.assign(s)
        self.assertEqual(self.alloc.remaining("d1"), 100)
        self.assertTrue(self.alloc.evict("s1"))
        self.assertEqual(self.alloc.remaining("d1"), 500)

    def test_unregister_orphans_shards(self):
        self.alloc.register_node("d1", 1000)
        s = KVShard("s1", "m", 0, 8, 0, 4, 0, 64, 400)
        self.alloc.assign(s)
        orphaned = self.alloc.unregister_node("d1")
        self.assertEqual(len(orphaned), 1)
        self.assertIsNone(orphaned[0].device_id)

    def test_rebalance_places_orphans(self):
        self.alloc.register_node("d1", 1000)
        s1 = KVShard("s1", "m", 0, 8, 0, 4, 0, 64, 400)
        self.alloc.assign(s1)
        self.alloc.unregister_node("d1")
        self.alloc.register_node("d2", 1000)
        report = self.alloc.rebalance()
        self.assertIn("s1", report["placed"])
        self.assertEqual(self.alloc.shard_location("s1"), "d2")

    def test_rebalance_reports_unplaced(self):
        s1 = KVShard("s1", "m", 0, 8, 0, 4, 0, 64, 400)
        self.alloc._shards["s1"] = s1
        report = self.alloc.rebalance()
        self.assertIn("s1", report["unplaced"])

    def test_total_never_exceeds_cap(self):
        self.alloc.register_node("d1", 500)
        shards = [KVShard(f"s{i}", "m", 0, 8, 0, 4, 0, 64, 200) for i in range(5)]
        for s in shards:
            self.alloc.assign(s)
        _, allocated = self.alloc.node_capacity("d1")
        self.assertLessEqual(allocated, 500)

class ProtocolTest(unittest.TestCase):
    def test_topic_routing(self):
        self.assertEqual(proto.kv_topic("d1", proto.TOPIC_PUT),
                         "/shugocore/mobile/d1/kv/put")

    def test_advertise_round_trip(self):
        msg = proto.make_advertise("d1", 1024, 2048)
        self.assertEqual(msg["type"], "KVAdvertise")
        self.assertEqual(msg["usable_ram_bytes"], 1024)

    def test_assign_carries_shard_fields(self):
        s = KVShard("s1", "m", 0, 8, 0, 4, 0, 64, 500, checksum="abc")
        msg = proto.make_assign(s)
        self.assertEqual(msg["shard_id"], "s1")
        self.assertEqual(msg["checksum"], "abc")

    def test_msg_type(self):
        self.assertEqual(proto.msg_type(proto.make_get("s1")), "KVGet")
        self.assertEqual(proto.msg_type({}), "")


class SimulatorTest(unittest.TestCase):
    def setUp(self):
        self.mesh = MeshSimulator()

    def test_join_and_place(self):
        # Shards are ~131 KB each (see SPEC); node needs headroom.
        self.mesh.join_node("d1", 1024 * 1024)
        shards = make_kv_shards(SPEC, 2, split="sequence")
        report = self.mesh.place_shards(shards)
        self.assertEqual(len(report["placed"]), 2)
        self.assertEqual(len(report["unplaced"]), 0)

    def test_place_respects_cap(self):
        self.mesh.join_node("d1", 1)  # 1 byte -- nothing fits
        shards = make_kv_shards(SPEC, 2, split="sequence")
        report = self.mesh.place_shards(shards)
        self.assertEqual(len(report["unplaced"]), 2)

    def test_put_get_round_trip(self):
        self.mesh.join_node("d1", 1024 * 1024)
        shards = make_kv_shards(SPEC, 4, split="sequence")
        self.mesh.place_shards(shards)
        sid = shards[0].shard_id
        device = shards[0].device_id
        data = b"x" * 100
        import hashlib
        checksum = hashlib.sha256(data).hexdigest()
        self.assertTrue(self.mesh.put_shard(device, sid, data, checksum))
        resp = self.mesh.get_shard(device, sid)
        self.assertIsNotNone(resp)
        self.assertEqual(resp["shard_id"], sid)
        self.assertEqual(resp["checksum"], checksum)

    def test_get_missing_returns_none(self):
        self.mesh.join_node("d1", 1024)
        self.assertIsNone(self.mesh.get_shard("d1", "nope"))

    def test_evict_frees_space(self):
        self.mesh.join_node("d1", 1024 * 1024)
        shards = make_kv_shards(SPEC, 2, split="sequence")
        self.mesh.place_shards(shards)
        sid = shards[0].shard_id
        device = shards[0].device_id
        # Actually store the shard data on the node.
        import hashlib
        self.mesh.put_shard(device, sid, b"x" * 64, hashlib.sha256(b"x" * 64).hexdigest())
        self.assertTrue(self.mesh.evict_shard(device, sid))
        self.assertIsNone(self.mesh.get_shard(device, sid))

    def test_leave_rebalance(self):
        self.mesh.join_node("d1", 1024 * 1024)
        self.mesh.join_node("d2", 1024 * 1024)
        # 2 shards, 1 per node (each ~131KB < 1MB).
        shards = make_kv_shards(SPEC, 2, split="sequence")
        self.mesh.place_shards(shards)
        victim = shards[0].device_id
        self.mesh.leave_node(victim)
        report = self.mesh.rebalance()
        # The victim's single shard is re-placed on the survivor.
        self.assertEqual(len(report["placed"]), 1)
        survivor = "d2" if victim == "d1" else "d1"
        self.assertEqual(self.mesh.allocator.shard_location(shards[0].shard_id),
                         survivor)

    def test_stats_reflects_state(self):
        self.mesh.join_node("d1", 1024 * 1024)
        shards = make_kv_shards(SPEC, 2, split="sequence")
        self.mesh.place_shards(shards)
        stats = self.mesh.stats()
        self.assertEqual(stats["total_shards"], 2)
        self.assertEqual(stats["assigned"], 2)


if __name__ == "__main__":
    unittest.main()

