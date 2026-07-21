from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent
MODULE=ROOT/'xtracker_forward_capture.py'
spec=importlib.util.spec_from_file_location('xf',MODULE)
assert spec and spec.loader
xf=importlib.util.module_from_spec(spec); spec.loader.exec_module(xf)
HAS_FCNTL=importlib.util.find_spec('fcntl') is not None

CHILD_LOCK_SCRIPT=r"""
from __future__ import annotations
import importlib.util
import os
import sys
from pathlib import Path

module_path=Path(sys.argv[1])
out_path=Path(sys.argv[2])
mode=sys.argv[3]
spec=importlib.util.spec_from_file_location('xf_child',module_path)
assert spec and spec.loader
xf=importlib.util.module_from_spec(spec); spec.loader.exec_module(xf)
xf.OUT=out_path
try:
    if mode=='try':
        with xf.exclusive_run_lock('child'):
            print('entered',flush=True)
        raise SystemExit(0)
    if mode=='die':
        with xf.exclusive_run_lock('child'):
            print('held',flush=True)
            os._exit(17)
except SystemExit as exc:
    print(str(exc),flush=True)
    raise
raise SystemExit('unknown child mode')
"""

def run_lock_child(out: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable,'-c',CHILD_LOCK_SCRIPT,str(MODULE),str(out),mode],
        text=True,capture_output=True,check=False,
    )

class DepthWalkTests(unittest.TestCase):
    def test_full_depth_walk_uses_multiple_levels(self):
        out=xf.walk([(0.06,40),(0.07,60)],100)
        self.assertTrue(out['complete'])
        self.assertAlmostEqual(out['vwap'],0.066)
        self.assertEqual(out['marginal_price'],0.07)

    def test_incomplete_depth_fails_fok(self):
        out=xf.walk([(0.06,20)],100)
        self.assertFalse(out['complete'])
        self.assertEqual(out['filled_quantity'],20)

class FeeTests(unittest.TestCase):
    def test_fee_curve(self):
        execution=xf.walk([(0.25,100)],100)
        self.assertEqual(xf.fee_for_walk(execution,0.04),0.75)

    def test_risk_cap_includes_fee_and_adverse_tick(self):
        quantity,execution,fee=xf.risk_cap_quantity([(0.08,200)],0.05,0.01,5)
        self.assertIsNotNone(quantity)
        self.assertLessEqual(execution['gross_notional']+fee+0.01*quantity,10.0)
        self.assertGreaterEqual(quantity,5)

class ChainTests(unittest.TestCase):
    def test_chain_links_records(self):
        with tempfile.TemporaryDirectory() as td:
            state={}; path=Path(td)/'events.jsonl'
            first=xf.append_chain(path,{'x':1},state,'events')
            second=xf.append_chain(path,{'x':2},state,'events')
            self.assertEqual(second['previous_hash'],first['record_hash'])
            self.assertEqual(state['chains']['events']['sequence'],2)
            rows=[json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows),2)

    @unittest.skipIf(HAS_FCNTL,'unsupported-platform behavior is only active without fcntl')
    def test_exclusive_run_lock_fails_closed_without_fcntl(self):
        old_out=xf.OUT
        with tempfile.TemporaryDirectory() as td:
            xf.OUT=Path(td)
            try:
                with self.assertRaises(SystemExit) as ctx:
                    with xf.exclusive_run_lock('windows'):
                        pass
                self.assertIn('unsupported platform',str(ctx.exception))
            finally:
                xf.OUT=old_out

    @unittest.skipUnless(HAS_FCNTL,'requires POSIX fcntl.flock')
    def test_exclusive_run_lock_blocks_second_process_and_persists_path(self):
        old_out=xf.OUT
        with tempfile.TemporaryDirectory() as td:
            xf.OUT=Path(td)
            lock_path=xf.OUT/'run.lock'
            try:
                with xf.exclusive_run_lock('first'):
                    self.assertTrue(lock_path.exists())
                    blocked=run_lock_child(xf.OUT,'try')
                    self.assertNotEqual(blocked.returncode,0)
                    self.assertIn('exclusive run lock already held',blocked.stdout+blocked.stderr)
                self.assertTrue(lock_path.exists())
                entered=run_lock_child(xf.OUT,'try')
                self.assertEqual(entered.returncode,0,entered.stdout+entered.stderr)
                self.assertIn('entered',entered.stdout)
            finally:
                xf.OUT=old_out

    @unittest.skipUnless(HAS_FCNTL,'requires POSIX fcntl.flock')
    def test_exclusive_run_lock_releases_after_normal_return_and_exception(self):
        old_out=xf.OUT
        with tempfile.TemporaryDirectory() as td:
            xf.OUT=Path(td)
            try:
                with xf.exclusive_run_lock('normal'):
                    pass
                entered=run_lock_child(xf.OUT,'try')
                self.assertEqual(entered.returncode,0,entered.stdout+entered.stderr)
                with self.assertRaises(RuntimeError):
                    with xf.exclusive_run_lock('exception'):
                        raise RuntimeError('forced')
                entered=run_lock_child(xf.OUT,'try')
                self.assertEqual(entered.returncode,0,entered.stdout+entered.stderr)
            finally:
                xf.OUT=old_out

    @unittest.skipUnless(HAS_FCNTL,'requires POSIX fcntl.flock')
    def test_exclusive_run_lock_releases_after_abrupt_child_death(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)
            died=run_lock_child(out,'die')
            self.assertEqual(died.returncode,17,died.stdout+died.stderr)
            self.assertTrue((out/'run.lock').exists())
            entered=run_lock_child(out,'try')
            self.assertEqual(entered.returncode,0,entered.stdout+entered.stderr)
            self.assertIn('entered',entered.stdout)

class IdentityTests(unittest.TestCase):
    def test_decision_evidence_rejects_missing_request_start(self):
        with tempfile.TemporaryDirectory() as td:
            raw=json.dumps({'asset_id':'tok','market':'0xabc'}).encode()
            path=Path(td)/'book.json'; path.write_bytes(raw)
            row={'decision_book_raw_path':str(path),'decision_book_sha256':xf.sha_bytes(raw),'yes_token_id':'tok','condition_id':'0xabc','book_response_received_at':'2026-07-20T10:00:00Z','book_provider_timestamp':'2026-07-20T10:00:00Z','book_timing_quality':'exact_request_response','decision_book_http_status':200}
            out=xf.validate_decision_evidence(row,'2026-07-20T10:00:00Z')
            self.assertFalse(out['eligible'])
            self.assertIn('missing_book_request_started_at',out['problems'])

class SafetyTests(unittest.TestCase):
    def test_source_contains_no_order_or_auth_endpoint(self):
        text=MODULE.read_text().lower()
        self.assertNotIn('/order',text)
        self.assertNotIn('private_key',text)
        self.assertNotIn('api_key',text)
        self.assertNotIn('authorization',text)

if __name__=='__main__': unittest.main()
