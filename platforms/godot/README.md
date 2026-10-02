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

### The headset on the desk (verified 2026-10-02)

Checked over adb, because "is there a headset?" is a question with an answer rather than an
assumption. Everything below was observed, not inferred:

- **It is attached.** `adb devices -l` lists
  `2G97C5ZH5P01GZ  device product:eureka model:Quest_3` — a Meta Quest 3 (`eureka`), maker
  `Oculus`, Android 14 / SDK 34, `arm64-v8a`.
- **The XR client is installed and not running.** `com.samurai.shugocore.xr` v1.0 (installed
  2026-09-22, updated 2026-09-23). It has **no** `user://shugocore_xr.json`, so it is still
  pointed at the shipped placeholder `http://192.0.2.10:11435` — TEST-NET-1, unreachable by
  design. Writing that file (or using the in-headset Settings panel) is what makes it reach
  anything at all.
- **It can reach the fleet's LAN.** The headset is `192.168.1.151`; this desktop is
  `192.168.1.152`; the Mac node is `192.168.1.162`. An HTTP request from the headset to this
  desktop answers `HTTP/1.0 200 OK`, on 11434 and on a fresh port 11435.
- **`ping` lies here, so reachability is proven with a request.** The same headset reports
  100% packet loss to this desktop while an HTTP request to it succeeds: Windows blocks ICMP,
  so a failed ping is not evidence of an unreachable host.
- **Nothing on the LAN serves the engine API the bridge needs yet.** The Mac answers on 11434
  (Ollama: `/api/generate` works, `/api/v1/*` is 404) and its 9000 is the *raw mesh transport*,
  not HTTP; this desktop is the same. A configured headset would therefore still find no
  agent until a desktop server is started:

  ```bash
  py -3.10 shugocore_server.py --host 0.0.0.0 --port 11435
  # loopback binds need no token; a LAN bind does, unless --allow-unauthenticated
  ```

  then point the headset at that host (the Settings panel does exactly this by hand):

  ```bash
  adb shell 'mkdir -p /sdcard/Android/data/com.samurai.shugocore.xr/files'
  # write {"agent_url": "http://192.168.1.152:11435", "bearer_token": ""} to
  #   /sdcard/Android/data/com.samurai.shugocore.xr/files/shugocore_xr.json
  adb shell monkey -p com.samurai.shugocore.xr -c android.intent.category.LAUNCHER 1
  ```

- **The desktop's own Oculus runtime is not usable from Godot headless.** `OVRService`,
  `OVRServer_x64` and `OculusDash` all run on the PC, yet Godot's OpenXR init fails with
  `XR_ERROR_FORM_FACTOR_UNAVAILABLE` (the runtime has no system for the form factor Godot
  asks for) and the engine honestly falls back to `desktop_preview`. That is why
  `runtime/tools/xr_session.py` records `desktop_preview`: it is the desktop path, and a
  real headset session needs the headset to run the client.

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
