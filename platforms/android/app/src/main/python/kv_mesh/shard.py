"""Shard data types for the KV-cache / context mesh split.

A *shard* is a contiguous slice of a transformer's KV cache or context window,
assigned to a single mesh node.  These types model the shard's identity, its
shape, and its assignment -- the inputs/outputs of the KVAllocator.
"""
import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Optional


def _digest(obj: dict) -> str:
    """Stable SHA-256 over a dict (mirrors security.canonical_hash)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ShardSpec:
    """Static shape of a model's KV cache -- the thing being split."""
    model_id: str
    num_layers: int
    num_heads: int
    head_dim: int
    max_seq_len: int
    dtype_bytes: int = 2  # fp16/bf16

    def kv_bytes_per_token_per_layer(self) -> int:
        """One token's K+V for a single layer: 2 * H * D * B."""
        return 2 * self.num_heads * self.head_dim * self.dtype_bytes

    def kv_total_bytes(self) -> int:
        """Full KV cache size: L * S * per_token_per_layer."""
        return (self.num_layers * self.max_seq_len
                * self.kv_bytes_per_token_per_layer())

    def bytes_for_layers(self, l_start: int, l_end: int,
                         seq_len: Optional[int] = None) -> int:
        """Byte count for a layer range over seq_len (default max)."""
        seq = seq_len if seq_len is not None else self.max_seq_len
        return (l_end - l_start) * seq * self.kv_bytes_per_token_per_layer()


@dataclass(slots=True)
class KVShard:
    """A contiguous slice of the KV cache assigned to one node."""
    shard_id: str
    model_id: str
    layer_start: int      # inclusive
    layer_end: int        # exclusive
    head_start: int       # inclusive
    head_end: int         # exclusive
    seq_start: int        # inclusive
    seq_end: int          # exclusive
    bytes_size: int
    device_id: Optional[str] = None
    checksum: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def canonical_id(model_id: str, layer_start: int, layer_end: int,
                     head_start: int, head_end: int,
                     seq_start: int, seq_end: int) -> str:
        return (f"{model_id}:L{layer_start}-{layer_end}"
                f":H{head_start}-{head_end}:S{seq_start}-{seq_end}")


@dataclass(slots=True)
class ContextShard:
    """A contiguous chunk of the input context assigned to one node."""
    shard_id: str
    model_id: str
    token_start: int      # inclusive
    token_end: int        # exclusive
    bytes_size: int
    device_id: Optional[str] = None
    checksum: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def make_kv_shards(spec: ShardSpec, num_shards: int,
                   split: str = "sequence") -> list:
    """Partition a model's KV cache into ``num_shards`` shards.

    split="sequence" -> contiguous token-position ranges (KV-offload / seq-parallel).
    split="layers"   -> contiguous layer ranges (layer-split variant).
    """
    if num_shards < 1:
        raise ValueError("num_shards must be >= 1")
    shards = []
    if split == "sequence":
        chunk = spec.max_seq_len // num_shards
        for s in range(num_shards):
            seq_start = s * chunk
            seq_end = spec.max_seq_len if s == num_shards - 1 else (s + 1) * chunk
            bspec = {"model_id": spec.model_id, "layer_start": 0,
                     "layer_end": spec.num_layers, "head_start": 0,
                     "head_end": spec.num_heads, "seq_start": seq_start,
                     "seq_end": seq_end}
            sid = KVShard.canonical_id(**bspec)
            size = spec.num_layers * (seq_end - seq_start) * spec.kv_bytes_per_token_per_layer()
            shards.append(KVShard(
                shard_id=sid, model_id=spec.model_id,
                layer_start=0, layer_end=spec.num_layers,
                head_start=0, head_end=spec.num_heads,
                seq_start=seq_start, seq_end=seq_end, bytes_size=size,
                checksum=_digest(bspec)))
    elif split == "layers":
        chunk = spec.num_layers // num_shards
        for s in range(num_shards):
            l_start = s * chunk
            l_end = spec.num_layers if s == num_shards - 1 else (s + 1) * chunk
            bspec = {"model_id": spec.model_id, "layer_start": l_start,
                     "layer_end": l_end, "head_start": 0,
                     "head_end": spec.num_heads, "seq_start": 0,
                     "seq_end": spec.max_seq_len}
            sid = KVShard.canonical_id(**bspec)
            size = spec.bytes_for_layers(l_start, l_end)
            shards.append(KVShard(
                shard_id=sid, model_id=spec.model_id,
                layer_start=l_start, layer_end=l_end,
                head_start=0, head_end=spec.num_heads,
                seq_start=0, seq_end=spec.max_seq_len, bytes_size=size,
                checksum=_digest(bspec)))
    else:
        raise ValueError(f"unknown split: {split!r}")
    return shards


def make_context_shards(spec: ShardSpec, num_shards: int) -> list:
    """Partition the input context into ``num_shards`` contiguous token chunks."""
    if num_shards < 1:
        raise ValueError("num_shards must be >= 1")
    shards = []
    chunk = spec.max_seq_len // num_shards
    for s in range(num_shards):
        t_start = s * chunk
        t_end = spec.max_seq_len if s == num_shards - 1 else (s + 1) * chunk
        bspec = {"model_id": spec.model_id, "ts": t_start, "te": t_end}
        sid = f"{spec.model_id}:ctx:{t_start}-{t_end}"
        # Rough byte size: tokens * hidden_dim proxy (use head_dim*num_heads).
        size = (t_end - t_start) * spec.num_heads * spec.head_dim * spec.dtype_bytes
        shards.append(ContextShard(
            shard_id=sid, model_id=spec.model_id,
            token_start=t_start, token_end=t_end, bytes_size=size,
            checksum=_digest(bspec)))
    return shards
