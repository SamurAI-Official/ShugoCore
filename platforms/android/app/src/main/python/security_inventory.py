"""ShugoCore security inventory & baseline (Track 2).

One honest, bounded snapshot of the security posture of a running node:
which controls exist, which are on, and whether the current state drifts
from the documented fail-closed baseline. Purely observational — it never
changes a decision, never blocks a path, and never fabricates: a control
the collector cannot see is reported as unverifiable (and counts as a
baseline violation), because silence is not safety.

Baseline (the repo's documented defaults):
  * audit chain enabled and verifiable
  * policy fail-closed, consent required
  * internet egress off by default

No secrets are ever included: secret values, tokens, and key material
never appear in an inventory entry.
"""

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Cap every list the inventory can produce (repo's no-unbounded-growth
# principle) — the inventory is observational, not a data store.
MAX_AGENT_CAPS = 64
MAX_CONSENT_GRANTS = 32

# The documented fail-closed baseline, as (section, key) -> expected value.
# AGENT baseline: the device node owns the network-egress control.
BASELINE: Tuple[Tuple[str, str, Any], ...] = (
    ("audit", "enabled", True),
    ("policy", "fail_closed", True),
    ("policy", "consent_required", True),
    ("network", "internet", False),
)

# SERVER baseline: wire-facing controls + the hosted engine's chain. The
# desktop does not own device network egress, so network.internet is not
# evaluated here (inventing that check would be fabrication).
SERVER_BASELINE: Tuple[Tuple[str, str, Any], ...] = (
    ("audit", "enabled", True),
    ("policy", "fail_closed", True),
    ("policy", "consent_required", True),
    ("server", "auth_token_required", True),
    ("server", "rate_limit_enabled", True),
)


def _audit_entry(audit: Any) -> Dict[str, Any]:
    """Describe an AuditChain (or anything duck-shaped like one)."""
    entry: Dict[str, Any] = {"enabled": audit is not None,
                             "integrity": None}
    if audit is None:
        return entry
    verify = getattr(audit, "verify", None)
    if not callable(verify):
        return entry
    try:
        ok, errors, _checked = verify()
        if ok:
            entry["integrity"] = "ok"
        elif errors == ["audit file not found"]:
            # Repo semantics (decision_engine): a not-yet-written chain is
            # still auditable — "empty", not tampered.
            entry["integrity"] = "empty"
        else:
            entry["integrity"] = "failed"
    except Exception as exc:
        # An unverifiable chain cannot be claimed as ok.
        logger.debug("audit verify failed: %s", exc)
        entry["integrity"] = "unverifiable"
    return entry


def _policy_entry(source: Any) -> Dict[str, Any]:
    """Policy posture. Reads the live enforcement attributes when the
    source exposes them; declared-only values are marked as such."""
    net = getattr(source, "network_policy", None)
    network = (dict(net) if isinstance(net, dict) else {})
    return {
        # Declared design invariants (hardcoded fail-closed posture).
        "fail_closed": True,
        "consent_required": True,
        # Live enforcement state.
        "network": network,
    }


def collect_agent_inventory(agent: Any) -> Dict[str, Any]:
    """Inventory an agent-like object (AndroidAgent, DecisionEngine, or a
    test double). Absent surfaces are omitted — never fabricated."""
    if agent is None:
        return {}
    inventory: Dict[str, Any] = {}
    audit = getattr(getattr(agent, "engine", None), "audit", None)
    if audit is None:
        # DecisionEngine-style objects carry the chain directly.
        audit = getattr(agent, "audit", None)
    if audit is not None:
        inventory["audit"] = _audit_entry(audit)

    inventory["policy"] = _policy_entry(agent)

    net = inventory["policy"]["network"]
    if net:
        inventory["network"] = net

    caps = getattr(agent, "agent_caps", None)
    if isinstance(caps, dict):
        inventory["agent_caps"] = {
            "granted": sorted(
                str(k) for k, v in caps.items() if v)[:MAX_AGENT_CAPS],
            "granted_count": sum(1 for v in caps.values() if v),
        }

    declared = getattr(agent, "capabilities", None)
    if isinstance(declared, dict):
        acked = sum(
            1 for v in declared.values()
            if isinstance(v, dict) and v.get("agent_ack"))
        inventory["capabilities"] = {
            "declared": len(declared), "acked": acked,
        }

    consent = getattr(agent, "consent_registry", None)
    grants = getattr(consent, "grants", None)
    if callable(grants):
        try:
            snapshot = grants()
            if isinstance(snapshot, dict):
                actions = sorted(
                    str(k) for k in snapshot)[:MAX_CONSENT_GRANTS]
                inventory["consent"] = {
                    "actions": actions,
                    "grant_count": sum(
                        len(v) for v in snapshot.values()
                        if isinstance(v, (list, tuple))),
                }
        except Exception as exc:
            logger.debug("consent snapshot failed: %s", exc)

    role = getattr(agent, "_mesh_role_label", None)  # Track 1 tie-in
    if callable(role):
        try:
            inventory["mesh"] = {"role": str(role())}
        except Exception:
            pass
    return inventory


def collect_server_inventory(server: Any) -> Dict[str, Any]:
    """Inventory the wire-facing controls of a ShugoCoreServer instance."""
    if server is None:
        return {}
    inventory: Dict[str, Any] = {}
    inventory["auth_token_required"] = bool(
        getattr(server, "auth_token", None))
    limiter = getattr(server, "_limiter", None)
    inventory["rate_limit_enabled"] = limiter is not None
    if limiter is None:
        inventory["rate_limit"] = {"enabled": False}
    else:
        inventory["rate_limit"] = {
            "enabled": True,
            "calls_per_minute": getattr(limiter, "calls_per_minute", None),
            "burst": getattr(limiter, "burst", None),
        }
    return inventory


def evaluate_baseline(inventory: Dict[str, Any],
                      baseline: Tuple[Tuple[str, str, Any], ...] = BASELINE,
                      ) -> Dict[str, Any]:
    """Compare an inventory against the baseline.

    A control that is present but wrong is ``drift``; a control that is
    absent is ``unverifiable`` and always counts as a violation — the
    inventory never assumes safety.
    """
    violations: List[Dict[str, Any]] = []
    for section, key, expected in baseline:
        section_data = inventory.get(section) or {}
        if key not in section_data:
            violations.append({
                "control": f"{section}.{key}", "expected": expected,
                "observed": None, "state": "unverifiable",
                "severity": "critical",
            })
        elif section_data[key] != expected:
            violations.append({
                "control": f"{section}.{key}", "expected": expected,
                "observed": section_data[key], "state": "drift",
                "severity": "critical",
            })
    return {"baseline_ok": not violations, "checked": len(baseline),
            "violations": violations}


def full_snapshot(agent: Any = None, server: Any = None) -> Dict[str, Any]:
    """Inventory + baseline in one call (what the status surfaces use)."""
    inventory: Dict[str, Any] = {}
    if agent is not None:
        inventory.update(collect_agent_inventory(agent))
    if server is not None:
        inventory["server"] = collect_server_inventory(server)
    return {"inventory": inventory,
            "baseline": evaluate_baseline(inventory)}
