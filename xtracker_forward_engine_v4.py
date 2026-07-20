#!/usr/bin/env python3
"""Locked v4 adapter: register/evaluate entries, then monitor exits/settlements."""
from __future__ import annotations
import importlib.util
from pathlib import Path

ROOT=Path('/data/workspace/polymarket-research')

def load(path:Path,name:str):
    spec=importlib.util.spec_from_file_location(name,path)
    if not spec or not spec.loader: raise RuntimeError(path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

def configure(core):
    core.PROTOCOL=ROOT/'config/xtracker_forward_validation_v4.json'
    core.LOCK=ROOT/'config/xtracker_forward_validation_v4.lock.json'
    core.OUT=ROOT/'reports/xtracker_forward_validation/v4'
    core.STATE=core.OUT/'state.json'; core.STATUS=core.OUT/'status.json'
    core.REGISTRY=core.OUT/'opportunity_registry.jsonl'; core.EVENTS=core.OUT/'evidence_events.jsonl'
    core.LEDGER=core.OUT/'independent_event_ledger.csv'; core.RAW=core.OUT/'raw'
    return core

def enrich_open_positions(core):
    state=core.load_json(core.STATE,{})
    if not state or not core.EVENTS.exists(): return
    entry_records={}
    for line in core.EVENTS.read_text().splitlines():
        row=__import__('json').loads(line)
        if row.get('record_type') in {'ARM_ENTRY_EVALUATION','REBALANCE_ENTRY_EVALUATION'} and row.get('execution_evidence_eligible'):
            entry_records[(row.get('arm'),row.get('lifecycle_id'))]=row
    for arm,positions in state.get('open_positions',{}).items():
        for life,pos in positions.items():
            row=entry_records.get((arm,life),{})
            metadata=row.get('fee_metadata') or {}
            pos.setdefault('tick_size',metadata.get('tick_size'))
            pos.setdefault('lifecycle_realized_net_pnl_usd',0.0)
            pos.setdefault('lifecycle_realized_stressed_net_pnl_usd',0.0)
    core.atomic_json(core.STATE,state)

def main():
    core=configure(load(ROOT/'xtracker_forward_capture.py','xtracker_forward_core_v4'))
    if core.LOCK.exists():
        lock=core.load_json(core.LOCK,{})
        expected=core.sha_bytes(core.canonical({k:v for k,v in lock.items() if k!='lock_sha256'}))
        if expected!=lock.get('lock_sha256'): raise SystemExit('invalid v4 lock self-hash')
        for raw_path,digest in (lock.get('locked_source_sha256') or {}).items():
            path=Path(raw_path)
            if not path.is_file() or core.sha_file(path)!=digest: raise SystemExit(f'locked source hash mismatch: {path}')
        if core.sha_file(Path(lock['frozen_strategy_manifest_path']))!=lock['frozen_strategy_manifest_sha256']:
            raise SystemExit('frozen strategy manifest hash mismatch')
    rc=core.main()
    if rc: return rc
    enrich_open_positions(core)
    monitor=load(ROOT/'xtracker_forward_monitor_v4.py','xtracker_forward_monitor_v4')
    return monitor.main(core)

if __name__=='__main__': raise SystemExit(main())
