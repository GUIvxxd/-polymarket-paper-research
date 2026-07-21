from __future__ import annotations
import hashlib,importlib.util,json,unittest
from pathlib import Path

ROOT=Path('/data/workspace/polymarket-research')
def load(path,name):
 spec=importlib.util.spec_from_file_location(name,path); assert spec and spec.loader
 mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
def sha256_lf(path):
 return hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
core=load(ROOT/'xtracker_forward_capture.py','v4_core_test')
monitor=load(ROOT/'xtracker_forward_monitor_v4.py','v4_monitor_test')

class FrozenRuleTests(unittest.TestCase):
 def position(self):return {'entry_vwap':0.10,'event':'e','bucket':'a'}
 def test_absolute_profit_exact_frozen_threshold(self):
  self.assertNotIn('absolute_profit_exit',monitor.exit_reasons(core,'baseline',self.position(),{'complete':True,'vwap':0.1299},None,None))
  self.assertIn('absolute_profit_exit',monitor.exit_reasons(core,'baseline',self.position(),{'complete':True,'vwap':0.13},None,None))
 def test_relative_profit_exact_frozen_threshold(self):
  self.assertNotIn('relative_profit_exit',monitor.exit_reasons(core,'baseline',self.position(),{'complete':True,'vwap':0.1199},None,None))
  self.assertIn('relative_profit_exit',monitor.exit_reasons(core,'baseline',self.position(),{'complete':True,'vwap':0.1201},None,None))
 def test_fair_collapse_and_stale_edge_exact(self):
  reasons=monitor.exit_reasons(core,'baseline',self.position(),{'complete':True,'vwap':0.3001},{'fair':0.20,'edge':0.1},None)
  self.assertIn('stale_bucket_bid_above_model',reasons)
 def test_better_bucket_delta_exact(self):
  reasons=monitor.exit_reasons(core,'baseline',self.position(),{'complete':True,'vwap':0.10},{'fair':0.5,'edge':0.50},{'bucket':'b','edge':0.6001})
  self.assertIn('profitable_better_bucket_available',reasons)
 def test_registered_drawdown_only_in_candidate_arm(self):
  walk={'complete':True,'vwap':0.075}
  self.assertNotIn('registered_25pct_drawdown_exit',monitor.exit_reasons(core,'baseline',self.position(),walk,None,None))
  self.assertIn('registered_25pct_drawdown_exit',monitor.exit_reasons(core,'early_drawdown_exit_25pct',self.position(),walk,None,None))

class SettlementTests(unittest.TestCase):
 def test_bucket_boundaries(self):
  self.assertTrue(monitor.bucket_hit('20-39',20));self.assertTrue(monitor.bucket_hit('20-39',39));self.assertFalse(monitor.bucket_hit('20-39',40))
  self.assertTrue(monitor.bucket_hit('<20',19));self.assertFalse(monitor.bucket_hit('<20',20));self.assertTrue(monitor.bucket_hit('200+',200))

class ProtocolTests(unittest.TestCase):
 def test_protocol_matches_frozen_constants(self):
  protocol=json.loads((ROOT/'config/xtracker_forward_validation_v4.json').read_text())
  rules=protocol['baseline']['exit_rules_exactly_from_frozen_source']
  self.assertEqual(rules['minimum_absolute_profit_per_share'],0.03)
  self.assertEqual(rules['minimum_relative_profit'],0.20)
  self.assertEqual(rules['better_bucket_edge_delta'],0.10)
  self.assertEqual(rules['rebalance_minimum_edge'],0.50)
  self.assertEqual(rules['rebalance_minimum_fair'],0.70)
  self.assertEqual(rules['rebalance_maximum_ask'],0.25)
 def test_v3_is_explicitly_excluded(self):
  protocol=json.loads((ROOT/'config/xtracker_forward_validation_v4.json').read_text())
  self.assertIn('v3_pilot',protocol['excluded_samples'])
 def test_lock_self_hash_and_locked_repo_sources_match(self):
  lock=json.loads((ROOT/'config/xtracker_forward_validation_v4.lock.json').read_text())
  body={k:v for k,v in lock.items() if k!='lock_sha256'}
  expected=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
  self.assertEqual(lock['lock_sha256'],expected)
  prefix='/data/workspace/polymarket-research/'
  for raw_path,digest in lock['locked_source_sha256'].items():
   if not raw_path.startswith(prefix):continue
   rel=raw_path.removeprefix(prefix)
   self.assertEqual(sha256_lf(ROOT/rel),digest,rel)

class SafetyTests(unittest.TestCase):
 def test_locked_sources_have_no_order_or_auth_api(self):
  text='\n'.join((ROOT/name).read_text().lower() for name in ('xtracker_forward_capture.py','xtracker_forward_monitor_v4.py','xtracker_forward_engine_v4.py'))
  self.assertNotIn('/order',text);self.assertNotIn('private_key',text);self.assertNotIn('api_key',text);self.assertNotIn('authorization',text)
 def test_engine_wraps_capture_enrich_monitor_in_exclusive_lock(self):
  text=(ROOT/'xtracker_forward_engine_v4.py').read_text()
  self.assertIn("with core.exclusive_run_lock('xtracker_forward_engine_v4'):",text)

if __name__=='__main__':unittest.main()
