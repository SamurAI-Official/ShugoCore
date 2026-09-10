"""Protocol messages and topic routing for the KV mesh contract.

Defines the message types exchanged over the new kv contract topics under the
existing /shugocore/mobile/{device_id}/ namespace.  These are plain dicts --
serialised over the mesh transport (DDS/ROS2 topics in production, passed
directly in the offline simulator).
"""
import time
from typing import Any, Dict, Optional

# Topic tails (append to /shugocore/mobile/{device_id}/)
TOPIC_ADVERTISE = "kv/advertise"
TOPIC_ASSIGN = "kv/assign"
TOPIC_PUT = "kv/put"
TOPIC_GET = "kv/get"
TOPIC_EVICT = "kv/evict"
TOPIC_HEARTBEAT = "kv/heartbeat"


def kv_topic(device_id: str, tail: str) -> str:
    """Canonical kv contract topic for a device."""
    return f"/shugocore/mobile/{device_id}/{tail}"


def make_advertise(device_id: str, usable_ram_bytes: int,
                   total_ram_bytes: int) -> Dict[str, Any]:
    return {"type": "KVAdvertise", "device_id": device_id,
            "usable_ram_bytes": int(usable_ram_bytes),
            "total_ram_bytes": int(total_ram_bytes), "ts": time.time()}


def make_assign(shard: "KVShard") -> Dict[str, Any]:  # noqa: F821
    return {"type": "KVAssign", "shard_id": shard.shard_id,
            "model_id": shard.model_id, "layer_start": shard.layer_start,
            "layer_end": shard.layer_end, "head_start": shard.head_start,
            "head_end": shard.head_end, "seq_start": shard.seq_start,
            "seq_end": shard.seq_end, "bytes_size": shard.bytes_size,
            "checksum": shard.checksum}


def make_put(shard_id: str, data_b64: str, checksum: str) -> Dict[str, Any]:
    return {"type": "KVPut", "shard_id": shard_id, "data_b64": data_b64,
            "checksum": checksum}


def make_get(shard_id: str) -> Dict[str, Any]:
    return {"type": "KVGet", "shard_id": shard_id}


def make_evict(shard_id: str) -> Dict[str, Any]:
    return {"type": "KVEvict", "shard_id": shard_id}


def make_heartbeat(device_id: str) -> Dict[str, Any]:
    return {"type": "KVHeartbeat", "device_id": device_id,
            "ts": time.time()}


def make_response(shard_id: str, data_b64: str, checksum: str) -> Dict[str, Any]:
    return {"type": "KVResponse", "shard_id": shard_id, "data_b64": data_b64,
            "checksum": checksum}


def msg_type(msg: Dict[str, Any]) -> str:
    return str(msg.get("type", ""))
