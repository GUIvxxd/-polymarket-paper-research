#!/usr/bin/env python3
"""Unactivated v5 adapter over unchanged hardened paper-execution economics."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

import verify_xtracker_forward_v5
import xtracker_forward_provenance as provenance
import xtracker_forward_v5


ROOT = Path("/data/workspace/polymarket-research")


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure(core: Any, root: Path = ROOT) -> Any:
    paths = xtracker_forward_v5.paths_for(root)
    core.ROOT = root
    core.PROTOCOL = paths.protocol
    core.LOCK = paths.lock
    core.OUT = paths.output
    core.STATE = paths.state
    core.STATUS = paths.status
    core.REGISTRY = paths.registry
    core.EVENTS = paths.events
    core.LEDGER = paths.ledger
    core.RAW = paths.raw
    core.DEPTH = root / "reports/xtracker_tweet_depth_latest.json"
    core.WATCHDOG_STATE = root / "data/xtracker_tweet_watchdog_state.json"
    core.LEDGER_FIELDS = provenance.LEDGER_FIELDS
    return core


def enrich_open_positions(core: Any) -> None:
    state = core.load_json(core.STATE, {})
    if not state or not core.EVENTS.exists():
        return
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for line in core.EVENTS.read_text(encoding="utf-8").splitlines():
        row = __import__("json").loads(line)
        if row.get("record_type") in {"ARM_ENTRY_EVALUATION", "REBALANCE_ENTRY_EVALUATION"} \
                and row.get("execution_evidence_eligible"):
            entries[(str(row.get("arm")), str(row.get("lifecycle_id")))] = row
    for arm, positions in state.get("open_positions", {}).items():
        for lifecycle_id, position in positions.items():
            row = entries.get((arm, lifecycle_id), {})
            metadata = row.get("fee_metadata") or {}
            position.setdefault("tick_size", metadata.get("tick_size"))
            position.setdefault("lifecycle_realized_net_pnl_usd", 0.0)
            position.setdefault("lifecycle_realized_stressed_net_pnl_usd", 0.0)
    core.atomic_json(core.STATE, state)


def run_locked_sequence(
    core: Any,
    monitor_loader: Callable[[], Any],
    *,
    enrich: Callable[[Any], None] = enrich_open_positions,
) -> int:
    with core.exclusive_run_lock("xtracker_forward_engine_v5"):
        result = core.main()
        if result:
            return result
        enrich(core)
        return monitor_loader().main(core)


def main(root: Path = ROOT) -> int:
    paths = xtracker_forward_v5.paths_for(root)
    if not paths.lock.is_file():
        raise SystemExit("v5 is not activated; explicit activation is required")
    lock = __import__("json").loads(paths.lock.read_text(encoding="utf-8"))
    lock_errors = provenance.verify_lock(root, lock)
    if lock_errors:
        raise SystemExit("v5 lock verification failed: " + ", ".join(lock_errors))
    preflight = verify_xtracker_forward_v5.audit(root, write_audit=False)
    if not preflight["ok"]:
        raise SystemExit("v5 evidence preflight failed: " + ", ".join(preflight["errors"]))
    core = configure(load(root / "xtracker_forward_capture.py", "xtracker_forward_core_v5"), root)
    xtracker_forward_v5.install_runtime_provenance(core, lock)
    return run_locked_sequence(
        core,
        lambda: load(root / "xtracker_forward_monitor_v4.py", "xtracker_forward_monitor_v5"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
