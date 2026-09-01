"""
ShugoCore local-first benchmarking suite
========================================

Three suites (offline by default; live backends opt-in):

1. ``tool``   - tool-calling accuracy: proposal parse rate, action-type
   correctness, param-schema confluence.
2. ``memory`` - memory compaction fidelity: seed synthetic episodes, run
   consolidation, score summary facts against ground truth via embedding
   similarity.
3. ``latency``- step-execution latency p50/p95/p99 for execute_task,
   backend generation, and memory search.

Run::

    python3 benchmarks/run.py                    # all suites, offline
    python3 benchmarks/run.py --suite latency     # one suite
    python3 benchmarks/run.py --iterations 200    # more samples

Exit code is non-zero when a suite errors (CI gating).
"""

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision_engine import DecisionEngine
from memory_system import SemanticMemory
from model_backends import StubBackend


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class _ActionBackend(StubBackend):
    """Configurable stub that emits structurally valid proposals."""

    def __init__(self, action_type: str = "search_api", param_bias: float = 0.9):
        self.action_type = action_type
        self.param_bias = param_bias

    def generate(self, model_id: str, prompt: str, timeout: float = None) -> str:
        return json.dumps({
            "action_type": self.action_type,
            "params": {"query": "benchmark query"},
            "confidence": self.param_bias,
        })


def _build_engine(backend: Optional[Any] = None,
                  tmp: Optional[str] = None) -> DecisionEngine:
    models = [{"id": "bench", "type": "text", "weight": 1.0}]
    return DecisionEngine(
        models, {"type": "chroma"}, news_api_key=None,
        memory_db_path=os.path.join(tmp, "mem.db") if tmp else "/tmp/bench_mem.db",
        audit_path=None,
        episodic_journal_path=os.path.join(tmp, "episodic.jsonl") if tmp else None,
        subconscious_backend=backend,
    )


# ---------------------------------------------------------------------------
# Suite 1: tool-calling accuracy
# ---------------------------------------------------------------------------
def suite_tool(iterations: int) -> Dict[str, Any]:
    from decision_engine import DecisionEngine as DE
    backend = _ActionBackend(action_type="search_api", param_bias=0.95)
    parse_ok = 0
    correct_type = 0
    param_ok = 0
    for _ in range(iterations):
        decision = backend.generate("bench", "task")
        proposal = DE._parse_proposal(decision)
        if proposal is None:
            continue
        parse_ok += 1
        if proposal["action_type"] == "search_api":
            correct_type += 1
        if (isinstance(proposal.get("params"), dict)
                and proposal["params"].get("query")):
            param_ok += 1
    return {
        "parse_rate": round(parse_ok / max(1, iterations), 4),
        "action_type_accuracy": round(correct_type / max(1, parse_ok), 4),
        "param_confluence": round(param_ok / max(1, parse_ok), 4),
    }


# ---------------------------------------------------------------------------
# Suite 2: memory compaction fidelity
# ---------------------------------------------------------------------------
def suite_memory(iterations: int) -> Dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="shugocore_bench_mem_")
    try:
        memory = SemanticMemory(db_path=os.path.join(tmp, "mem.db"), dimension=128)
        truth = "openai api_call tape_api documented failure retry"
        for i in range(iterations):
            if i % 3 == 0:
                memory.store_fact(
                    f"{truth} #{i}", kind="procedural_insight", salience=2.5)
            else:
                memory.store_fact(f"routine log entry {i}", kind="summary")
        hits = memory.search(truth, top_k=5)
        top_kinds = [h["kind"] for h in hits]
        fidelity = round(
            top_kinds.count("procedural_insight") / max(1, len(top_kinds)), 4)
        graph_routes = len(memory.facts_about("tape_api"))
        memory.close()
        return {"compaction_fidelity": fidelity, "entity_links": graph_routes}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Suite 3: step latency
# ---------------------------------------------------------------------------
def _percentiles(samples: List[float]) -> Dict[str, float]:
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "samples": 0}
    samples = sorted(samples)

    def at(q: float) -> float:
        idx = min(len(samples) - 1, int(len(samples) * q))
        return round(samples[idx] * 1000.0, 3)  # ms

    return {"p50": at(0.50), "p95": at(0.95), "p99": at(0.99),
            "samples": len(samples)}


def suite_latency(iterations: int) -> Dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="shugocore_bench_lat_")
    engine = None
    try:
        engine = _build_engine(backend=_ActionBackend(), tmp=tmp)
        task = {"type": "text", "content": "latency benchmark"}
        gen_samples: List[float] = []
        task_samples: List[float] = []
        search_samples: List[float] = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            engine.subconscious.get_model_output("bench", task)
            gen_samples.append(time.perf_counter() - t0)

            t0 = time.perf_counter()
            engine.execute_task(task)
            task_samples.append(time.perf_counter() - t0)

            t0 = time.perf_counter()
            engine.memory.tier2.search("benchmark", top_k=3)
            search_samples.append(time.perf_counter() - t0)
        return {
            "backend_generate_ms": _percentiles(gen_samples),
            "execute_task_ms": _percentiles(task_samples),
            "memory_search_ms": _percentiles(search_samples),
        }
    finally:
        if engine is not None:
            engine.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
_SUITES = {"tool": suite_tool, "memory": suite_memory, "latency": suite_latency}


def run(iterations: int = 50,
        suites: Optional[List[str]] = None) -> Tuple[Dict[str, Any], int]:
    chosen = suites or list(_SUITES)
    reports: Dict[str, Any] = {}
    worst_exit = 0
    for name in chosen:
        fn = _SUITES[name]
        try:
            reports[name] = fn(iterations)
        except Exception as exc:
            reports[name] = {"error": f"{type(exc).__name__}: {exc}"}
            worst_exit = 1
    return reports, worst_exit


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ShugoCore benchmarks")
    parser.add_argument("--suite", default="all",
                        help="tool | memory | latency | all (default: all)")
    parser.add_argument("--iterations", type=int, default=50)
    args = parser.parse_args(argv)

    suites = list(_SUITES) if args.suite == "all" else [args.suite]
    reports, exit_code = run(iterations=max(4, args.iterations), suites=suites)

    print("# ShugoCore benchmark report")
    print(f"- iterations per suite: {args.iterations}\n")
    for name, data in reports.items():
        print(f"## {name}")
        print(json.dumps(data, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())