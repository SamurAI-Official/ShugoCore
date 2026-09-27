#!/usr/bin/env python3
"""One stable identity per node, used everywhere.

A hive node used to carry two names: the mesh id it *self-declared* -- derived
from device capabilities, e.g. ``android-Unknown (s5e8835)``, a string with
spaces, parentheses and a SoC token two devices can share -- and the name its
peers *dialled* it by (``shugo-tab``, from their peer maps). Nothing could
address a node by the name it answered to, which broke delegated work ("unknown
peer"), and two devices with the same SoC would have collided and shadowed each
other in the election.

This module is the single source of truth: a short, filesystem/CLI-safe id,
persisted per node, adopted on every start, used as the election node id, the
transport agent id and the dial name.
"""
import os
import re
import secrets
from typing import Optional

FILE_NAME = "node_id.txt"
MAX_LEN = 48
_NOT_ALLOWED = re.compile(r"[^a-z0-9-]+")
_EDGES = re.compile(r"^-+|-+$")


def sanitize(raw: Optional[str], *, fallback: str = None) -> Optional[str]:
    """Lower-case, hyphenate and bound a candidate id (None when unusable).

    Ids travel through mesh topics, CLI flags, adb arguments and file names, so
    spaces, parentheses and uppercase are stripped rather than escaped.
    """
    text = str(raw or "").strip().lower()
    if not text:
        return fallback
    text = _EDGES.sub("", _NOT_ALLOWED.sub("-", text))
    if not text:
        return fallback
    return text[:MAX_LEN].strip("-") or fallback


def is_valid(node_id: Optional[str]) -> bool:
    """True when ``node_id`` is already a sanitized id."""
    return bool(node_id) and sanitize(node_id) == node_id


def read(data_dir: str) -> Optional[str]:
    """The persisted identity for a node, or None when it has none yet."""
    if not data_dir:
        return None
    try:
        with open(os.path.join(str(data_dir), FILE_NAME), encoding="utf-8") as handle:
            return sanitize(handle.read())
    except OSError:
        return None


def load_or_create(data_dir: str, *, suggested: Optional[str] = None,
                   caps: Optional[str] = None, prefix: str = "shugo",
                   suffix_chars: int = 4) -> str:
    """This node's identity: persisted if present, else the best name available.

    Precedence: the stored id (so a node keeps its name across restarts and
    upgrades), then ``suggested`` (an operator's explicit choice, used verbatim),
    then ``<prefix>-<caps>-<random>``. The random suffix is what makes an
    automatically named node unique -- capability strings are not.
    """
    stored = read(data_dir)
    if stored:
        # An explicit choice is the operator speaking: it wins over a stored (or
        # previously generated) name, because a host that is told its identity
        # must adopt it -- otherwise renaming a host silently renames the node.
        choice = sanitize(suggested) if suggested else None
        if choice and choice != stored:
            _persist(data_dir, choice)
            return choice
        return stored
    if suggested:
        node_id = sanitize(suggested)
        if node_id:
            _persist(data_dir, node_id)
            return node_id
    suffix = secrets.token_hex(max(2, int(suffix_chars) // 2))
    base = sanitize(f"{prefix}-{caps or 'node'}", fallback=f"{prefix}-node")
    node_id = f"{base}-{suffix}"[:MAX_LEN].strip("-")
    _persist(data_dir, node_id)
    return node_id


def _persist(data_dir: str, node_id: str) -> None:
    """Best effort: a read-only data dir still yields a usable identity."""
    if not data_dir:
        return
    path = os.path.join(str(data_dir), FILE_NAME)
    try:
        os.makedirs(str(data_dir), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(node_id + "\n")
    except OSError:
        pass
