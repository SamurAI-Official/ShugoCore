# ShugoCore v1.29.1 — network-surface hardening

Patch release. **No public API change**; every new control is opt-in or a
backward-compatible default. Full suite: **965 tests passing** (Python 3.9–3.13).

## Desktop server (`shugocore-server`)

- **Fail-closed exposure.** A non-loopback bind is now refused unless
  `SHUGOCORE_SERVER_TOKEN` is set or `--allow-unauthenticated` is passed
  explicitly.
- **Bearer auth.** With a token configured, every route except `/health`
  requires `Authorization: Bearer <token>` (or `X-ShugoCore-Token`), compared
  in constant time.
- **Rate limiting.** Per-client token-bucket limiter
  (`--rate-limit-per-minute`, default 240; `--rate-limit-burst`, default 120);
  overflow returns 429; `/health` stays open.
- **Body guard fixed.** A negative `Content-Length` previously reached
  `rfile.read(-1)` (drain-to-EOF); negative and oversized lengths are now
  rejected before reading.
- **CORS narrowed** to loopback origins, allowing the auth headers.

## ShugoNet peer runtime (`agent_runtime.py`)

- **Frame-size bound** (1 MiB): an unterminated frame closes the connection
  instead of growing memory without bound.
- **Message validation**: inbound payloads must be dicts with a non-empty
  string `type` before dispatch.
- **Optional shared-secret mesh token** (`--auth-token-env`, default
  `SHUGOCORE_MESH_TOKEN`), off by default.

## Model backends (`model_backends.py`)

- Base URLs must be http(s) with a host and no embedded credentials.
- Redirects are never followed (3xx refuses); responses are streamed and
  capped at 256 KiB.

## Security primitives (`security.py`)

- **Fixed a token-redaction leak**: `Authorization: Bearer <token>` previously
  masked only the word "Bearer"; the whole credential is now redacted.
- `sanitize_text` strips invisible Unicode bidi/zero-width characters.
- `SecretResolver`'s bare-environment fallback is limited to env-var-shaped
  names.

## Audit integrity

- Audit-append failures are logged instead of being silently swallowed.

## Latent bugs found by the new lint gate (ruff `F821`)

- `audit.py` used an undefined `logger` — malformed/unreadable chains raised
  `NameError` instead of being reported.
- `mobile_nodes.KVTransportAdapter` never imported its `kv_mesh.protocol`
  helpers, so the whole KV-mesh DDS adapter was non-functional.
- `shugocore_agent.nrr_status_json` was missing its local `json` import;
  unreachable dead code in `_extract_perception` was removed.

## CI / supply chain

- Python **3.13** added to the test matrix.
- Blocking `lint` job (`ruff`, `E9`/`F821`) and `security-scan` job
  (`bandit` high blocking, medium advisory).
- `.github/dependabot.yml`, `SECURITY.md`, and a `dev` extra in
  `pyproject.toml`.

## Tests

+31 tests: server auth/rate-limit/body/CORS, mesh frame bound, backend egress,
redaction/sanitization, the KV transport adapter, and audit-failure visibility.

## Upgrade notes

- Existing loopback/LAN setups keep working, but a non-loopback bind now needs
  `--allow-unauthenticated` (the bundled Android app does not send a token yet).
- `pip install --upgrade shugocore==1.29.1`
