# KV-Cache / Context Mesh Split

## Goal

Spread transformer inference across a mesh of peripheral (Android) devices so that
the **host contributes compute** and the **peripherals contribute RAM**.  The KV
cache and context windows are sharded across nodes; each node stores and serves
the KV pairs (or context chunks) assigned to it.  This lessens the host's memory
pressure and, for sequence-parallel context splitting, its compute load too.

This is an **Option 1** design (KV-cache + context split), NOT a layer/tensor
split (Option 2).  See [Splitting strategies](#splitting-strategies) below.

## Background: current mesh

The existing fleet layer (`mobile_nodes.py`, `android_node.py`) already gives us:

- **Pairing = consent.**  `MobileNodeRegistry.pair()` allowlists a `device_id`,
  audits the grant, and attaches a TTL.  Only paired nodes are reachable.
- **Topic ACL.**  A node may only surface data on
  `/shugocore/mobile/{device_id}/{contract_topic}`.  Inbound data on any other
  topic is refused and audited.
- **Compute offload.**  `android_node._run_compute_request()` runs a workload on
  the device's local inference runtime and publishes the result.  Offload is a
  side-effecting action gated through the same consent/approval path as other
  side effects.

The KV-cache split reuses all of this.  A KV shard is just another contract topic
under the same `/shugocore/mobile/{device_id}/...` namespace, and storing a shard
on a peripheral is a privacy-relevant action gated the same way.

## Shard model

```
KVShard
    shard_id: str            # canonical: "{model_id}:{layer_range}:{head_range}:{seq_range}"
    model_id: str            # e.g. "shugocore-local"
    layer_start: int         # inclusive
    layer_end: int           # exclusive
    head_start: int          # inclusive (attention-head index)
    head_end: int            # exclusive
    seq_start: int           # inclusive (token position)
    seq_end: int             # exclusive
    bytes_size: int          # advertised byte size of the stored tensor
    device_id: str | None    # assigned node; None while unassigned
    checksum: str            # canonical_hash of the tensor bytes (integrity)

ContextShard
    shard_id: str
    model_id: str
    token_start: int         # inclusive
    token_end: int           # exclusive
    bytes_size: int
    device_id: str | None
    checksum: str

ShardSpec
    model_id: str
    num_layers: int
    num_heads: int
    head_dim: int
    max_seq_len: int
    dtype_bytes: int         # 2 for fp16/bf16, 4 for fp32
```

### Memory accounting

For a transformer with `L` layers, `H` attention heads, head dimension `D`, max
sequence length `S`, and `B` bytes per element:

```
kv_per_token_per_layer = 2 * H * D * B        # K and V
kv_total                = L * S * kv_per_token_per_layer
```


## Splitting strategies

### A. Sequence-parallel context split (compute + memory savings)

The input context of `N` tokens is partitioned into `P` chunks (one per node).
Each node runs the **full forward pass** on its chunk, producing KV pairs cached
locally.  A second pass (or ring attention) aggregates cross-chunk attention.

- **Memory:** each node stores `KV / P` instead of the full cache.
- **Compute:** prefill is split across `P` nodes (ideal speedup ~ `P`, bounded by
  the cross-chunk aggregation step).
- **Bandwidth:** during decode, every token attends all cached KV, so the host
  must fetch the non-local shards each step.  See [Latency budget](#latency-budget).

### B. KV-offload (memory savings, host does compute)

The host runs the full model but stores KV shards on peripherals, fetching on
demand during decode.

- **Memory:** host RAM drops by up to `(P-1)/P` of the KV cache.
- **Compute:** unchanged (host still does all matmuls).
- **Bandwidth:** each decode step fetches `KV / P` from each remote node.

### C. Head-split KV cache (memory savings, minimal bandwidth)

KV pairs are partitioned by attention head across nodes.  During attention, the
host fetches only the heads it needs for the current layer.

- **Memory:** each node stores `KV * (heads_per_node / H)`.
- **Bandwidth:** proportional to the head split; fetching can be pipelined
  layer-by-layer.

**This design focuses on A and B** (sequence-parallel context split and KV-offload),
which map cleanly onto the existing compute-offload contract.  C is noted as a
future optimization.

## Protocol

New contract topics under the existing `/shugocore/mobile/{device_id}/` namespace:

```
/shugocore/mobile/{device_id}/kv/advertise    # node -> host: RAM capacity + health
/shugocore/mobile/{device_id}/kv/assign       # host -> node: shard allocation
/shugocore/mobile/{device_id}/kv/put          # host -> node: store a shard
/shugocore/mobile/{device_id}/kv/get          # host -> node: fetch a shard
/shugocore/mobile/{device_id}/kv/evict        # host -> node: drop a shard
/shugocore/mobile/{device_id}/kv/heartbeat    # node -> host: liveness
```

Message types (see `kv_mesh/protocol.py`):

```python
KVAdvertise    = {"device_id", "usable_ram_bytes", "total_ram_bytes", "ts"}

## Memory accounting

1. Each node advertises `usable_ram_bytes` at pairing and on heartbeat.
2. The `KVAllocator` maintains `allocated_bytes[device_id]` and refuses assignments
   that would exceed capacity.
3. On node join: unassigned shards (and overflow) are re-balanced onto the new node.
4. On node leave (TTL expiry / heartbeat loss): its shards are re-allocated to
   surviving nodes; shards that don't fit anywhere trigger a host-memory fallback
   event (audited).
5. Shard integrity is verified via `canonical_hash` on every put/get.

## Safety surface

- **KV shards may contain user text embeddings.**  They are personal data.  Storing
  a shard on a peripheral is gated by the same consent/approval path as other
  mobile compute (the existing `mobile_request_compute` consent gate).
- **Encryption in transit.**  Shard bytes travel base64-encoded over the mesh
  transport; the transport layer is responsible for link-level encryption (the
  existing DDS Security / application-layer trust model).
- **No persistence past TTL.**  A shard's lifetime is bounded by the node's pairing
  TTL; on expiry the host evicts and re-allocates.  Nodes never retain shards
  past their assignment.
- **Integrity.**  Every `get` response is checksummed; a mismatch is audited and
  the shard is re-fetched from the host's authoritative copy.
- **Fail-closed.**  If the mesh cannot hold a shard (all nodes full / left), the
  host falls back to local KV storage.  The agent keeps working; only the memory
  savings are lost.

## Latency budget

For a 0.5B model (L=22, H=8, D=64, B=2) split across P nodes:

| Quantity | Formula | P=1 (local) | P=2 | P=4 |
|----------|---------|-------------|-----|-----|
| KV total | L*S*2*H*D*B | 118 MB | 118 MB | 118 MB |
| KV / node | KV / P | 118 MB | 59 MB | 29 MB |
| Fetch per step (B) | KV / P * (P-1) | 0 | 59 MB | 88 MB |

At WiFi bandwidth (~300 MB/s realistic), fetching 59 MB adds ~200 ms per decode
step -- acceptable for a 0.5B model on Exynos 1380 (decode ~ 1.3 s/token) but
dominant for larger models.  This confirms the design is viable for the current
0.5B on-device target and a 2-3 node mesh; larger models need the head-split
strategy (C) or a higher-bandwidth transport.

## Prototype scope

The offline prototype (`kv_mesh/`) proves the **protocol and memory accounting**
without real distributed inference:

- `shard.py` -- `KVShard`, `ContextShard`, `ShardSpec` data types.
- `allocator.py` -- `KVAllocator` assigns shards by advertised RAM, handles
  join/leave/rebalance, enforces capacity caps.
- `protocol.py` -- message types + topic routing for the kv contract.
- `simulator.py` -- `MeshSimulator` runs assign/get/put/evict cycles over simulated
  nodes with RAM caps.

It does **not** run real model inference -- that requires multi-instance
llama.cpp + a high-bandwidth activation transport, a research effort of its own.
Real on-device multi-node KV splitting is explicitly a follow-on phase.

## Future integration (not built here)

- `MobileNodeRegistry` gains a `kv_allocator: KVAllocator` and advertise-RAM
  plumbing.
- `android_node.py` gains a `KVShardStore` holding assigned shards in app-private
  memory, serving `kv/get` and accepting `kv/put`/`kv/evict`.
- `AndroidBackend` / `android_inference.py` targets the mesh KV store instead of
  (or alongside) the local llama.cpp KV cache.

KVAssign       = {"shard_id", "layer_start", "layer_end", ...}
KVPut          = {"shard_id", "data_b64", "checksum}    # base64 tensor bytes
KVGet          = {"shard_id}
KVEvict        = {"shard_id}
KVHeartbeat    = {"device_id", "ts}
KVResponse     = {"shard_id", "data_b64", "checksum}    # reply to get
```

All messages are sanitized (`sanitize_text`, size-capped) before reaching the
allocator, consistent with the existing mobile-node contract.

Example: a 0.5B model (L=22, H=8, D=64, S=2048, B=2):
`22 * 2048 * 2 * 8 * 64 * 2 ~ 118 MB` -- already meaningful on a phone.

A peripheral advertises `usable_ram_bytes` (total RAM minus a safety margin for the
OS and the agent runtime).  The allocator assigns shards so that the sum of
`bytes_size` on any node never exceeds its advertised capacity.
