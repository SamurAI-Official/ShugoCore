"""MeshSimulator -- run assign/get/put/evict cycles over simulated nodes.

Models a set of peripheral nodes with RAM caps and a host-side KVAllocator.
Each simulated node stores assigned shards in a dict (keyed by shard_id) and
serves get/put/evict requests.  This exercises the protocol and memory
accounting without real distributed inference.
"""
import base64
import hashlib
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from kv_mesh.allocator import KVAllocator
from kv_mesh.shard import KVShard, ShardSpec, make_kv_shards
import kv_mesh.protocol as proto

logger = logging.getLogger(__name__)


class SimNode:
    """One simulated peripheral node with a RAM cap."""

    def __init__(self, device_id: str, usable_ram_bytes: int,
                 total_ram_bytes: int = 0):
        self.device_id = device_id
        self.usable_ram_bytes = usable_ram_bytes
        self.total_ram_bytes = total_ram_bytes
        self.store: Dict[str, Dict[str, Any]] = {}  # shard_id -> {data_b64, checksum}
        self.allocated_bytes = 0
        self.alive = True

    def advertise(self) -> Dict[str, Any]:
        return proto.make_advertise(self.device_id, self.usable_ram_bytes,
                                    self.total_ram_bytes)

    def capacity_remaining(self) -> int:
        return max(0, self.usable_ram_bytes - self.allocated_bytes)

    def put(self, msg: Dict[str, Any]) -> bool:
        sid = msg["shard_id"]
        size = len(msg.get("data_b64", ""))
        # Evict existing copy if present (idempotent re-put).
        if sid in self.store:
            self.allocated_bytes -= len(self.store[sid]["data_b64"])
        if self.allocated_bytes + size > self.usable_ram_bytes:
            return False
        self.store[sid] = {"data_b64": msg["data_b64"],
                           "checksum": msg.get("checksum", "")}
        self.allocated_bytes += size
        return True

    def get(self, shard_id: str) -> Optional[Dict[str, Any]]:
        entry = self.store.get(shard_id)
        if entry is None:
            return None
        return proto.make_response(shard_id, entry["data_b64"],
                                   entry["checksum"])

    def evict(self, shard_id: str) -> bool:
        if shard_id in self.store:
            self.allocated_bytes -= len(self.store[shard_id]["data_b64"])
            del self.store[shard_id]
            return True
        return False


class MeshSimulator:
    """Host-side simulator: an allocator + a set of simulated nodes."""

    def __init__(self):
        self.allocator = KVAllocator()
        self.nodes: Dict[str, SimNode] = {}

    def join_node(self, device_id: str, usable_ram_bytes: int,
                  total_ram_bytes: int = 0) -> SimNode:
        node = SimNode(device_id, usable_ram_bytes, total_ram_bytes)
        self.nodes[device_id] = node
        self.allocator.register_node(device_id, usable_ram_bytes,
                                     total_ram_bytes)
        return node

    def leave_node(self, device_id: str) -> None:
        """Remove a node; its shards become unassigned (rebalance to go)."""
        node = self.nodes.pop(device_id, None)
        if node is None:
            return
        node.alive = False
        self.allocator.unregister_node(device_id)

    def place_shards(self, shards: List[KVShard]) -> Dict[str, list]:
        """Assign a batch of shards; returns {placed, unplaced}."""
        placed, unplaced = [], []
        for shard in shards:
            if self.allocator.assign(shard) is not None:
                placed.append(shard.shard_id)
            else:
                unplaced.append(shard.shard_id)
        return {"placed": placed, "unplaced": unplaced}

    def put_shard(self, device_id: str, shard_id: str,
                  data: bytes, checksum: str) -> bool:
        """Store a shard's bytes on a node (simulating kv/put)."""
        node = self.nodes.get(device_id)
        if node is None or not node.alive:
            return False
        data_b64 = base64.b64encode(data).decode("ascii")
        return node.put(proto.make_put(shard_id, data_b64, checksum))

    def get_shard(self, device_id: str,
                  shard_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a shard from a node (simulating kv/get)."""
        node = self.nodes.get(device_id)
        if node is None or not node.alive:
            return None
        return node.get(shard_id)

    def evict_shard(self, device_id: str, shard_id: str) -> bool:
        node = self.nodes.get(device_id)
        if node is None:
            return False
        ok = node.evict(shard_id)
        if ok:
            self.allocator.evict(shard_id)
        return ok

    def rebalance(self) -> Dict[str, list]:
        return self.allocator.rebalance()

    def stats(self) -> Dict[str, Any]:
        return self.allocator.stats()
