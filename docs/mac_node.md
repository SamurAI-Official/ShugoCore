# The Mac node (what is Mac-specific)

The Mac is a first-class hive member (`shugo-mac`), not a special case bolted on: it
runs the same agent, heartbeats, elects, delegates and can host layers. This file
makes the Mac-specific parts explicit rather than folklore, so a future change knows
what it may not assume.

## Mac-owned code

| area | where | why it is Mac-specific |
|---|---|---|
| desktop control plane | `clients/desktop/shugocore_desktop.py` | the operator UI (backend picker, mesh panel, sensors) runs on a desktop |
| free-memory measurement | `mesh_election.macos_available_memory()` | macOS does not define `SC_AVPHYS_PAGES` for `os.sysconf`; this reads `vm_stat` (free + inactive + speculative) with the page size from `sysctl -n hw.pagesize` |
| host-launcher behaviour | `scripts/desktop_agent.py` (`--model`, sync on a timer, mesh panel) | the Mac serves a model backend the rest of the hive can name |

## Rules for shared code

- Nothing in the core may assume Mac paths, `vm_stat`, Homebrew prefixes or a
  case-insensitive filesystem. Where behaviour differs, it is branched on the
  platform -- as `available_memory_bytes()` is.
- A node that cannot measure free memory advertises none, which makes it
  **ineligible in the election and skipped by the layer planner**: it looks alive
  and contributes nothing. That is exactly what happened to the Mac (`mem=0`) until
  the `vm_stat` branch existed, and it is why this is a rule rather than a nicety.
- The layer-split host tooling is expected to work on macOS as written:
  `scripts/build_llama_rpc.py` is plain CMake + Ninja and skips the MSVC dance off
  Windows.

## Bringing a Mac node into line

```bash
git pull
git submodule update --init --checkout platforms/android/app/src/main/cpp/llama.cpp
python scripts/node_consistency.py --data-dir <its data dir>   # the four checks
python scripts/build_llama_rpc.py --host                       # only if needed
```

Then restart the node so it adopts its persisted identity, and watch the two rows
that have actually bitten us:

- **node identity** -- the id peers dial must equal the id it advertises
  (`shugo-mac`, not a hostname like `shugo-MacBook`);
- **advertised headroom** -- non-zero.

From the hub's side the same facts show up as
`mesh heartbeat: peer shugo-mac merged (prio=10 thermal=0 mem=<non-zero>)`, and the
Mac appears in the election's `candidates` list. With `mem=0` it never did.

### Priorities are the intent, and they must be unambiguous

Ranking is `(priority, node_id)`, and the leader is the best-ranked *eligible* live
node. Two nodes at the same priority used to be mutually non-deposable -- the rules
compared priority alone -- so whichever was heard first kept the lease for ever and
the fleet settled into camps that never merged. Measured here on 2026-09-27: the
desktop and the Mac both at priority 10, with the phones and the Mac holding to
`shugo-mac` while the desktop held to itself, so *every* delegated action the desktop
sent was refused with `sender is not the primary` by the peers it was asking -- and
each node's own status looked healthy while that happened.

Equal priorities now converge (a strictly better rank wins, `mesh_election._rank`),
but state the intent anyway so a joiner cannot cause a re-home:

| node | priority | why |
|---|---|---|
| PC (hub) | `--mesh-priority 1` | it runs the reasoning model and holds the gate |
| Mac | `--mesh-priority 10` | persona host plus the fleet's strongest peripheral |
| phones | `--mesh-priority 500` (default) | followers: sensors, capacity, journal |

A node that disagrees with a peer about the lease is now named -- in the log and in
the audit chain as `mesh_lease_disagreement` -- instead of only being visible to the
sender that got refused.

## Hosting layers on a Mac

With `--model-host`, the hub plans layers from each peer's *measured* headroom,
asks the Mac to start its `ggml-rpc-server` over the mesh (delegated `mesh_rpc`),
and offloads to it. A Mac is the strongest peripheral in this fleet -- measured
~1.5 GiB free against the phones' ~150-200 MB -- so it is normally the device that
can hold the most layers, and the one that makes a split worth starting.

