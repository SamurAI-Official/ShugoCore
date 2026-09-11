"""KVAllocator -- assign KV shards to mesh nodes by advertised RAM.

The allocator is the memory-accounting core of the mesh split.  It tracks each
node's advertised capacity and current allocation, assigns shards without
exceeding capacity, and rebalances on node join/leave.

Safety: the allocator refuses assignments that would exceed a node's advertised
capacity (fail-closed -- unassigned shards fall back to host memory).
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from kv_mesh.shard import KVShard

logger = logging.getLogger(__name__)


class AllocationError(RuntimeError):
    """Raised when a shard cannot be placed on any node."""


class KVAllocator:
    """Assign KV shards to nodes by advertised RAM, with rebalancing."""

    def __init__(self):
        # device_id -> {"usable_ram_bytes": int, "allocated_bytes": int}
        self._nodes: Dict[str, Dict[str, Any]] = {}
        # shard_id -> KVShard (assignment tracked on the shard's device_id)
        self._shards: Dict[str, KVShard] = {}

    # -- node membership -----------------------------------------------------

    def register_node(self, device_id: str, usable_ram_bytes: int,
                      total_ram_bytes: int = 0) -> None:
        """Advertise a node's RAM capacity (from kv/advertise)."""
        self._nodes[device_id] = {
            "usable_ram_bytes": max(0, int(usable_ram_bytes)),
            "total_ram_bytes": max(0, int(total_ram_bytes)),
            "allocated_bytes": 0,
        }
        logger.info("node registered: %s (%d MB usable)", device_id,
                    usable_ram_bytes // (1024 * 1024))

    def unregister_node(self, device_id: str) -> List[KVShard]:
        """Remove a node; return the shards that were on it (need re-allocation)."""
        self._nodes.pop(device_id, None)
        orphaned = [s for s in self._shards.values()
                    if s.device_id == device_id]
        for s in orphaned:
            s.device_id = None
        return orphaned

    def list_nodes(self) -> List[str]:
        return sorted(self._nodes)

    def node_capacity(self, device_id: str) -> Tuple[int, int]:
        """(usable_bytes, allocated_bytes) for a node; (0,0) if unknown."""
        n = self._nodes.get(device_id)
        if not n:
            return 0, 0
        return n["usable_ram_bytes"], n["allocated_bytes"]

    def remaining(self, device_id: str) -> int:
        usable, allocated = self.node_capacity(device_id)
        return max(0, usable - allocated)

    # -- assignment ----------------------------------------------------------

    def assign(self, shard: KVShard,
               prefer: Optional[str] = None) -> Optional[str]:
        """Assign a shard to the node with the most remaining room that fits.

        Returns the device_id it was assigned to, or None if no node fits
        (caller falls back to host memory).  ``prefer`` hints a node (e.g. the
        node that just joined) but is still subject to the capacity cap.
        """
        candidates = sorted(
            self._nodes.keys(),
            key=lambda d: self.remaining(d), reverse=True)
        if prefer in candidates:
            candidates.remove(prefer)
            candidates.insert(0, prefer)

        for device_id in candidates:
            if self.remaining(device_id) >= shard.bytes_size:
                shard.device_id = device_id
                self._nodes[device_id]["allocated_bytes"] += shard.bytes_size
                self._shards[shard.shard_id] = shard
                return device_id
        # Didn't fit anywhere -- still register it as unassigned so rebalance
        # and stats know about it (caller falls back to host memory for it).
        shard.device_id = None
        self._shards[shard.shard_id] = shard
        return None

    def evict(self, shard_id: str) -> bool:
        """Drop a shard from its node.  Returns True if it was assigned."""
        shard = self._shards.get(shard_id)
        if shard is None or shard.device_id is None:
            return False
        node = self._nodes.get(shard.device_id)
        if node:
            node["allocated_bytes"] = max(
                0, node["allocated_bytes"] - shard.bytes_size)
        shard.device_id = None
        return True

    def rebalance(self) -> Dict[str, list]:
        """Re-assign all currently-unassigned shards; report what got placed.

        Returns {"placed": [shard_ids], "unplaced": [shard_ids]}.
        """
        unassigned = [s for s in self._shards.values()
                      if s.device_id is None]
        placed, unplaced = [], []
        for shard in unassigned:
            if self.assign(shard) is not None:
                placed.append(shard.shard_id)
            else:
                unplaced.append(shard.shard_id)
        return {"placed": placed, "unplaced": unplaced}

    # -- introspection -------------------------------------------------------

    def shard_location(self, shard_id: str) -> Optional[str]:
        s = self._shards.get(shard_id)
        return s.device_id if s else None

    def all_shards(self) -> List[KVShard]:
        return list(self._shards.values())

    def stats(self) -> Dict[str, Any]:
        return {
            "nodes": {d: {"usable": n["usable_ram_bytes"],
                          "allocated": n["allocated_bytes"],
                          "remaining": self.remaining(d)}
                      for d, n in self._nodes.items()},
            "total_shards": len(self._shards),
            "assigned": sum(1 for s in self._shards.values()
                            if s.device_id is not None),
        }
