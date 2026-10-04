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
| `SHUGOCORE_XR_TRANSPARENT` | `true` | Transparent background (alpha blend). The room shows through the scene in the headset, and the **desktop mirror window renders black** — set `0` for an opaque background in both. The JSON key `transparent_background` wins over it, like every other setting here. |

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

- **With no headset connected, the desktop's Oculus runtime is not usable from Godot.** The
  services run (`OVRService`, `OVRServer_x64`, `OculusDash`) yet Godot's OpenXR init fails
  with `XR_ERROR_FORM_FACTOR_UNAVAILABLE` (the runtime has no system for the form factor
  Godot asks for), and the engine honestly falls back to `desktop_preview`. That is why
  `runtime/tools/xr_session.py` used to record `desktop_preview` on this machine.

### The same desktop, headset worn, over Quest Link (verified 2026-10-03)

The bullet above is what the desktop does *without* a headset. With the Quest 3 awake and
streamed over Quest Link the same Godot binary gets a real session, which corrects the
earlier conclusion rather than extending it:

- **A real OpenXR session comes up.** `OpenXR: Running on OpenXR runtime:  Oculus  1.208.0`,
  and the session reaches `XR_SESSION_STATE_READY` → `XR_SESSION_STATE_FOCUSED`.
  `XRServer.find_interface("OpenXR")` reports `initialized=true` and `XRBootstrap` adopts it:
  `presence = xr`. Measured 3 runs out of 3, exit 0, with the scene rendered to the headset.
- **The renderer matters, and gl_compatibility is the one that works.** The runtime reports
  `minApiVersionSupported 4.0.0` against Godot's Compatibility renderer at `3.3.0`, and
  accepts it "anyway"; the session then comes up. Forcing Forward+/Vulkan (Vulkan 1.4.341,
  the RTX 4070 Ti) removes that warning but makes the runtime **refuse** the session:
  `OpenXR: Failed to create session [ XR_ERROR_GRAPHICS_REQUIREMENTS_CALL_MISSING ]`.
  So `project.godot` keeps `gl_compatibility`, with a comment saying why.
- **Passthrough cannot be started on this runtime, and now says so.** The
  `OpenXRFbPassthroughExtension` singleton exists but does not expose
  `start_passthrough()`, so the call raised a `SCRIPT ERROR` on every session — reporting a
  crash where the truth was "this runtime cannot start passthrough". It is guarded and
  reported now. Relatedly, the runtime's own enumeration offers only
  `XR_ENVIRONMENT_BLEND_MODE_OPAQUE` while `XRInterface.environment_blend_mode` *reports*
  `alpha_blend` (the value Godot set from the project setting). Those two disagree, which
  is why the blend mode is read back and printed rather than inferred from either one. What
  a wearer sees behind the scene over Link is the Link environment, not an app passthrough
  layer.
- **The desktop mirror is black while the background is transparent — now a setting, not a
  mystery.** With `transparent_background` (default `true`) the framebuffer carries alpha,
  so the headset composites the scene over the Link environment; a mirror window has
  nothing behind it to blend that alpha against and renders black. Set
  `SHUGOCORE_XR_TRANSPARENT=0`, or `"transparent_background": false` in
  `user://shugocore_xr.json`, for an opaque background in both places — which is what you
  want when the desktop window is the thing you are looking at.
- **The mode settles late, so the bootstrap observes instead of concluding.** Autoloads run
  before the session reaches READY, so a one-shot check at `_ready()` reported
  `desktop_preview` while the operator was wearing the headset and looking at the scene.
  `XRBootstrap` now keeps watching (bounded by `SETTLE_FRAMES`) and adopts the session when
  it arrives, and it prints what it observed (`[xr] interface found; initialized=...`).
- **What is still broken is the engine's shutdown, not the session.** With a live session,
  the world-session path exits through a C++ crash (`signal 11`, `0xC0000005`) or a burst of
  `leaked GLES3 texture` / `leaked OpenXR object` errors, where the same scene driven by
  `--quit-after` exits 0. The transcript therefore reports
  `mode=not observed before the session ended` when the exchange finishes faster than the
  presence settles — which is a statement about what was seen, not a claim about a headset.

## Desktop preview vs XR

- **No OpenXR runtime** → `XRBootstrap` reports `mode = "desktop_preview"`,
  the scene renders flat, presence UI still driven by the bridge.
- **OpenXR runtime + HMD** → `mode = "xr"`, viewport uses XR, controllers
  (if present) are exposed via `XRBootstrap.get_controllers()`.

The mode is a real observed state — the scaffold never claims a headset
it does not have, and (since 2026-10-03) it no longer denies one it does:
the verdict is observed over a bounded window rather than taken once at
`_ready()`, because autoloads run before an OpenXR session reaches READY.

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
