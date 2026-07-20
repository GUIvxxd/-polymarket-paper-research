from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE=Path('/data/workspace/polymarket-research/xtracker_forward_capture.py')
spec=importlib.util.spec_from_file_location('xf',MODULE)
assert spec and spec.loader
xf=importlib.util.module_from_spec(spec); spec.loader.exec_module(xf)

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
