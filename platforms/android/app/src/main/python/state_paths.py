"""One place that decides where a node keeps its state.

A node writes four things side by side: its Tier 2 memory database, its audit
chain, its episodic journal and its log. Where those land is a property of the
*node*, not of the process that happened to start it -- but only
``shugocore_agent.AndroidAgent`` treated it that way, joining each name onto an
absolute data dir and handing that dir to the engine as ``log_dir``.
``shugocore_server.build_engine``, ``continuous_agent.ContinuousAgent`` and
``android_node.NodeConfig`` passed relative names and no data dir, so their
state landed in whatever directory the operator ran from. The repository root
grew ``semantic_memory.db``, ``audit_chain.jsonl``, ``node_audit.jsonl``,
``node_journal_*.jsonl`` and a 411 KB ``decision_engine.log`` exactly this way.

This module is the single answer, with two rules:

* **Absolute wins.** A caller that already resolved its own path -- the agent
  root, an operator's ``--memory-db-path``, a fleet DSN -- is never
  second-guessed.
* **Relative is anchored once.** With a data dir, a relative name lands under
  it. Without one, it is resolved against the working directory *at
  construction*. Resolving per-use instead is what let a ``chdir`` mid-run
  relocate a live node's state, which is the failure ``AndroidAgent``'s own
  comment describes (a relative ``data_dir`` resolved a second time against the
  new cwd, so ``runtime/node`` became ``runtime/node/runtime/node``).
"""
import os
from typing import Optional

# Not every value passed as a state path is a location on disk, and joining or
# ``abspath()``-ing the others is destructive:
#   * a Postgres DSN is a Tier 2 source string (``_resolve_memory_source``);
#   * ``:memory:`` is SQLite's in-memory sentinel;
#   * ``file:`` prefixes SQLite's URI filenames (``file:mem?mode=memory``).
# Rewriting ``:memory:`` produced ``sqlite3.OperationalError: unable to open
# database file``, because the colon is not legal in a Windows filename.
_PASSTHROUGH_PREFIXES = ("postgres://", "postgresql://", "file:")
_PASSTHROUGH_EXACT = (":memory:",)


def anchor_data_dir(data_dir: Optional[str]) -> Optional[str]:
    """Return ``data_dir`` as an absolute path, or None when it is not given."""
    if data_dir is None:
        return None
    text = str(data_dir).strip()
    if not text:
        return None
    return os.path.abspath(os.path.expanduser(text))


def state_dir(data_dir: Optional[str] = None) -> str:
    """The directory a node's state belongs in: its data dir, else the cwd.

    Never returns ``None``: a root that was given no data dir still gets a
    stable absolute answer, frozen at the moment it asks.
    """
    return anchor_data_dir(data_dir) or os.getcwd()


def anchor(path: Optional[str],
           data_dir: Optional[str] = None) -> Optional[str]:
    """Freeze a state path to an absolute location, once.

    A falsey path returns ``None``: an engine with no audit chain is a valid
    configuration, not an error.
    """
    if path is None:
        return None
    text = str(path).strip()
    if not text:
        return None
    if text.startswith(_PASSTHROUGH_PREFIXES) or text in _PASSTHROUGH_EXACT:
        return text
    expanded = os.path.expanduser(text)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    base = anchor_data_dir(data_dir)
    return os.path.abspath(os.path.join(base, expanded) if base else expanded)
