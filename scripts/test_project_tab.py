import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xhs_project_tab as t

class ProjectTabTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name);self.dest=self.base/'dest';self.source=self.base/'source'
  self.parent={'runtime':{},'context_id':'synthetic','account_uid':'1'*24,'tabs':{'detail':{'page':'A'*32,'chrome_tab_id':11,'window_id':22}}}
  self.created=0;self.stops={'k':{'sha256':'unchanged'}};self.mismatch=False
  case=self
  class Driver:
   def __init__(self,r):pass
   def close(self):pass
   def call(self,action,*args):return {'page':'A'*32,'uid':'2'*24 if case.mismatch else '1'*24,'url':t.HOME}
  self.driver=Driver
 def creator(self,*args):self.created+=1;return {'url':t.HOME,'active':True,'tabId':12,'windowId':22}
 @contextlib.contextmanager
 def mocks(self):
  @contextlib.contextmanager
  def lease(*args):yield ['k']
  def inspect(*args):
   p=self.dest/'proof.json';p.write_text('{}');return {'inspection':str(p),'collection_permitted':False}
  with patch.object(t.bridge,'load_binding',return_value=self.parent),patch.object(t.bridge,'lease',lease),patch.object(t.bridge,'Driver',self.driver),patch.object(t.nr,'current_stops',return_value=self.stops),patch.object(t.xp.legacy,'account_root',return_value=self.base/'guard'),patch.object(t.manual,'inspect',side_effect=inspect):yield
 def run_prepare(self,creator=None):return t.prepare(self.dest,self.source,'test','User permits autonomous new research tab.',creator or self.creator)
 def test_fresh_home_is_not_a_collection_grant(self):
  with self.mocks():r=self.run_prepare()
  self.assertFalse(r['collection_permitted']);self.assertTrue(r['fresh_search_and_body_required']);self.assertEqual(self.created,1)
  self.assertFalse((self.source/'bridge').exists());self.assertEqual(self.stops,{'k':{'sha256':'unchanged'}})
 def test_identity_mismatch_prevents_creation(self):
  self.mismatch=True
  with self.mocks(),self.assertRaisesRegex(Exception,'PARENT_CHANGED'):self.run_prepare()
  self.assertEqual(self.created,0)
 def test_unknown_creation_cannot_retry_or_change_destination(self):
  def fail(*a):self.created+=1;raise RuntimeError('unknown')
  with self.mocks():
   with self.assertRaisesRegex(Exception,'unknown'):self.run_prepare(fail)
   with self.assertRaisesRegex(Exception,'ALREADY_ATTEMPTED'):self.run_prepare()
   self.dest=self.base/'another'
   with self.assertRaisesRegex(Exception,'PRIOR_OUTCOME'):self.run_prepare()
  self.assertEqual(self.created,1)
 def test_home_inspection_alone_does_not_allow_another_project(self):
  with self.mocks():
   self.run_prepare();self.dest=self.base/'another'
   with self.assertRaisesRegex(Exception,'FRESH_ATTESTATION'):self.run_prepare()
  self.assertEqual(self.created,1)
 def test_same_account_other_context_does_not_retry(self):
  def fail(*a):self.created+=1;raise RuntimeError('unknown')
  with self.mocks():
   with self.assertRaises(RuntimeError):self.run_prepare(fail)
   self.parent=dict(self.parent,context_id='another-context');self.dest=self.base/'another'
  with self.mocks(),self.assertRaisesRegex(Exception,'PRIOR_OUTCOME'):self.run_prepare()
  self.assertEqual(self.created,1)
 def test_parent_access_warning_writes_shared_stop(self):
  class FailedDriver(self.driver):
   def call(self,*args):raise t.bridge.BridgeError('ACCESS_STOP')
  self.driver=FailedDriver
  with self.mocks(),patch.object(t.bridge.identity,'stop_all') as stop,self.assertRaisesRegex(Exception,'ACCESS_STOP'):
   self.run_prepare()
  stop.assert_called_once();self.assertEqual(self.created,0)
 def test_account_stop_prevents_creation(self):
  with self.mocks(),patch.object(t.bridge,'lease',side_effect=RuntimeError('STOP')),self.assertRaisesRegex(Exception,'STOP'):self.run_prepare()
  self.assertEqual(self.created,0)
 def test_invalid_destination_precedes_browser(self):
  with patch.object(t.bridge,'load_binding') as load,self.assertRaises(Exception):t.prepare(Path(t.__file__).parent,'/tmp/source','test','yes')
  load.assert_not_called()
 def test_same_project_rejected(self):
  with self.assertRaisesRegex(Exception,'DISTINCT_PROJECT'):t.prepare(self.source,self.source,'test','yes')

if __name__=='__main__':unittest.main()
