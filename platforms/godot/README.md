# ShugoCore XR — Track 3 OpenXR scaffold (Godot 4.x)

A minimal, honest OpenXR presence client for the ShugoCore agent,
designed to be dropped into an existing Godot OpenXR environment.

## What this scaffold is

- **`addons/shugocore_xr/`** — the ShugoCore XR addon (plugin descriptor +
  editor entry). The runtime behavior lives in two autoloads.
- **`scripts/shugocore_agent_bridge.gd`** — autoload that talks to the
  running ShugoCore agent over its real wire contracts:
  - `GET /health` — liveness
  - `GET /api/v1/status` — loop state, `mesh_role`, `security_baseline`
    (Track 1/2 surfaces), activity
  - `GET /api/v1/sensors` — bounded sensor-stream window
  - `POST /api/generate` — Ollama wire contract (chat with the agent)
  - `POST /api/v1/task` — the policy-gated `execute_task` path
  Bearer-token support follows the server convention
  (`SHUGOCORE_SERVER_TOKEN` env, sent as `Authorization: Bearer`).
- **`scripts/xr_bootstrap.gd`** — autoload that initializes OpenXR and
  falls back to an honest desktop preview when no runtime/HMD exists.
- **`scripts/shugo_presence.gd`** + **`scenes/`** — the agent's presence:
  state label + status ring driven ONLY by real bridge data
  (idle / listening / thinking / speaking / offline — mirroring the
  on-device face's honest modes). Never a decorative animation.
- **`addons/nrr_godot/`** — corrected Godot plugin descriptor for the
  NRR (Neural Rendering Runtime) GDScript API, with `NRR.gd` providing
  the documented API surface (`initialize`, `load_model`, `render_frame`)
  as an honest stub: calls succeed with `available == false` until the
  native NRR library is linked, never fabricating rendered output.

## Using it with your existing OpenXR environment

The scaffold is addon-first: copy `addons/shugocore_xr/`, `addons/nrr_godot/`,
and `scripts/` into your project, then register the autoloads:

```
[autoload]
ShugoCoreBridge="*res://scripts/shugocore_agent_bridge.gd"
XRBootstrap="*res://scripts/xr_bootstrap.gd"
```

Or open this directory as a standalone Godot project and press play —
it runs in desktop-preview mode with no headset attached.

## Configuration

On desktop, environment variables are supported. **On a Quest headset they
are not** (`OS.get_environment()` returns empty), so the settings are
file-first: `ShugoCoreConfig` reads `user://shugocore_xr.json` first, then the
shipped `res://shugocore_xr.default.json`, then the environment.
The Settings panel writes the first file and it takes precedence on next
launch.

| Env var | Default | Meaning |
| --- | --- | --- |
| `SHUGOCORE_XR_AGENT_URL` | `http://127.0.0.1:11434` | Agent/desktop-server base URL |
| `SHUGOCORE_SERVER_TOKEN` | *(empty)* | Bearer token (same env the server reads) |
| `SHUGOCORE_XR_POLL_SECONDS` | `2.0` | Status poll interval (bounded) |

### Connecting the Quest to a Mac-hosted agent

1. On the Mac, start the server bound to the LAN interface, e.g.
   `shugocore-server --host 0.0.0.0 --port 11435` (with `SHUGOCORE_SERVER_TOKEN`
   if the server requires one).
2. On the Quest, open the XR client **Settings** panel and set
   **Desktop server URL** to the Mac's LAN address, e.g.
   `http://192.168.1.10:11435` — `127.0.0.1` is the headset itself, never the Mac.
3. Enter the same bearer token the server expects, save, and relaunch the app.
4. Confirm the headset can reach the Mac (same Wi-Fi, no client isolation):
   `curl http://<mac-lan-ip>:11435/health` from a laptop on that network.

## Desktop preview vs XR

- **No OpenXR runtime** → `XRBootstrap` reports `mode = "desktop_preview"`,
  the scene renders flat, presence UI still driven by the bridge.
- **OpenXR runtime + HMD** → `mode = "xr"`, viewport uses XR, controllers
  (if present) are exposed via `XRBootstrap.get_controllers()`.

The mode is a real observed state — the scaffold never claims a headset
it does not have.

## Relationship to the agent

The XR client is a *surface*, not a second brain: decisions, side effects,
mesh election, and security posture stay owned by the running agent
(primary vs follower per Track 1; baseline per Track 2). The bridge only
reads what the agent already publishes, plus chat/task calls that go
through the same policy-gated paths a local call uses.

## Tests

`tests/test_xr_scaffold.py` (Python, source-level) locks the scaffold:
OpenXR enabled, autoloads registered, INI plugin descriptors, honest
presence modes, and — cross-language — the bridge's API paths verified
against `shugocore_server.py`'s actual route table.

## Known limits

- NRR rendering requires the native NRR library linked into the Godot
  build (see `engine_plugins/`); without it the stub reports
  `available == false` and presence renders with standard materials.
- The scaffold targets Godot 4.3+. Android XR export (mobile renderer)
  is the intended device target; the Forward Plus feature tag here suits
  desktop preview.
