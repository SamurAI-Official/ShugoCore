# Security Policy

## Supported versions

ShugoCore is in its frozen `1.x` series; security fixes land on `main` and the
latest released `1.x` minor. Older minors are not maintained.

| Version | Supported |
|---|---|
| latest `1.x` | ✅ |
| older `1.x` minors | ❌ |

## Reporting a vulnerability

Please report vulnerabilities through GitHub's **private** channel:
**Security → Report a vulnerability** on
<https://github.com/SamurAI-Official/ShugoCore/security/advisories/new>.

Do **not** open a public issue for an unfixed vulnerability, and do not include
live secrets in a report. Include:

- affected version / commit,
- the component (`decision_engine`, `execution_layer`, `shugocore_server`,
  `agent_runtime`, `policy`, Android app, …),
- a minimal reproduction,
- impact (confidentiality / integrity / availability / safety-gate bypass).

We aim to acknowledge within 7 days and to ship a fix or mitigation for
high-severity issues within 30 days. Credit is given in the release notes
unless you prefer to remain anonymous.

## Security model (what to look for)

The full model is documented in the README ("Safety model"). The invariants a
report should try to break:

- **Single gated path** — interactive tasks, autonomous cycles and the task
  queue all go through `DecisionEngine.execute_task`. The autonomous loop must
  not bypass the Tier 3 gate, `ConsentRegistry`, `ApprovalBroker`, the policy
  verdict token, or `CapabilityRegistry` allowlists.
- **Fail-closed** — a missing verdict, consent, approval channel, unknown host
  or unknown command must refuse, never fall back to allow.
- **Tier 3 is read-only at runtime** — only `promote_to_core()` (operator
  attribution + ledger) may mutate it.
- **Auditability** — every block/approval/execution is appended to the
  hash-chained (optionally HMAC-signed) audit log; audit-write failures must be
  visible, never silently swallowed.
- **Secret hygiene** — secrets are resolved at execution time and must not
  appear in decision dicts, logs, or HTTP responses.
- **Network surfaces** — `shugocore-server` binds loopback by default and
  refuses non-loopback binds without `SHUGOCORE_SERVER_TOKEN` or an explicit
  `--allow-unauthenticated`; the ShugoNet peer runtime bounds inbound frames
  and validates message shape.

## Verification

```bash
python -m compileall -q .
ruff check .                                   # syntax errors + undefined names
python -m unittest discover -s tests -v        # full suite
bandit -q -r . -x ./.venv,./.llama_build,./platforms,./dist,./build,./tests -lll
python audit.py verify audit_chain.jsonl       # audit-chain integrity
```

`tests/test_security.py`, `tests/test_shugocore_server.py`,
`tests/test_agent_runtime.py`, `tests/test_model_execution_stress.py` and the
hardware-stress suites are the regression surface for security-relevant code.
Hardware-facing changes must keep the lifecycle, thermal, transport and
model-execution stress suites green.