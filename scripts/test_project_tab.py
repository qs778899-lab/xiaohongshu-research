import contextlib
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xhs_project_tab as t
import xhs_manual_tab_recovery as m
import test_manual_tab_recovery as manual_fixtures

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

 def test_renewal_preserves_old_files_and_rejects_repeat_after_registry_loss(self):
  old=self.dest/'bridge/project-tab-preparation';old.mkdir(parents=True)
  (old/'parent.json').write_text('old immutable record')
  ref={'inspection':{'file':'synthetic','sha256':'a'*64},'grant':{'file':'synthetic','sha256':'b'*64}}
  with self.mocks(),patch.object(t,'renewal_archive',return_value=ref):
   r=t.prepare(self.dest,self.source,'test','Authorized renewal.',self.creator,Path('synthetic'))
   self.assertFalse(r['collection_permitted']);self.assertEqual(self.created,1)
   self.assertEqual((old/'parent.json').read_text(),'old immutable record')
   # Even loss of the temporary account directory cannot replay a creation.
   for f in (self.base/'guard/project-tab-preparations').glob('*.json'):f.unlink()
   with self.assertRaisesRegex(Exception,'ALREADY_ATTEMPTED'):
    t.prepare(self.dest,self.source,'test','Authorized renewal.',self.creator,Path('synthetic'))
   self.assertEqual(self.created,1)

 def test_renewal_cannot_bypass_invalid_current_parent(self):
  with self.mocks(),patch.object(t.bridge,'lease',side_effect=RuntimeError('STOP')),patch.object(t,'renewal_archive') as archive:
   with self.assertRaisesRegex(Exception,'STOP'):
    t.prepare(self.dest,self.source,'test','Authorized renewal.',self.creator,Path('synthetic'))
   archive.assert_not_called();self.assertEqual(self.created,0)

class HistoricalPreparationTests(unittest.TestCase):
 def setUp(self):
  # Use the actual manual probe/review validators with synthetic archived data.
  self.fixture=manual_fixtures.ManualTabTests(methodName='runTest');self.fixture.setUp()
  self.addCleanup(self.fixture.doCleanups)
  f=self.fixture
  f.url=t.HOME;f.meta=f.put('meta.json',{'tabId':11,'windowId':22,'url':t.HOME,'active':True})
  result=f.run_inspect();initial=Path(result['inspection'])
  inspected=m.check_pointer(m.pointer(initial));self.root=initial.parent
  search=dict(inspected,initial_inspection=m.pointer(initial))
  search['capture']=m.evidence(self.root/'actual-search.json',f.capture)
  search_ref=m.evidence(self.root/'search-inspection.json',search)
  note={'uid':f.uid,'url':'https://www.xiaohongshu.com/explore/'+'3'*24,
        'title':'Synthetic diary','body':'Synthetic treatment narrative.',
        'authors':[{'name':'synthetic-author','url':'https://www.xiaohongshu.com/user/profile/'+'5'*24}]}
  capture=m.evidence(self.root/'normal-note.json',note)
  b=copy.deepcopy(inspected['binding']);b['tabs']['detail']['last_url']=note['url'];b['tabs']['search']=b['tabs']['detail']
  probe={'kind':m.KIND,'binding':b,'inspection':search_ref,'capture':capture,
         'excluded_note_id':'2'*24,'collection_permitted':False}
  probe_file=self.root/'note-probe.json';m.evidence(probe_file,probe)
  review={'reader':'codex','reviewed':True,'body_read':True,'capture_sha256':capture['sha256'],
          'summary':'Read the synthetic title, body and author only.',
          'positions':['/title','/body','/authors'],'comments_read':False,'media_read':False}
  review_file=f.put('synthetic-review.json',review)
  with f.mocks():self.result=m.attest(probe_file,review_file,'historical')
  self.binding=t.bridge.load_binding(f.p,'historical')
  self.record=f.put('preparation.json',{'project':str(f.p)})
  m.evidence(self.record.with_suffix('.done.json'),{'inspection':m.pointer(initial),'collection_permitted':False})
  self.grant=m.check_pointer(self.result['grant'])

 def check(self,stops=None):
  with patch.object(m.nr,'current_stops',return_value=self.fixture.stops if stops is None else stops):
   return t.completed_preparation(self.record,['k'])

 def write_binding(self,b):
  t.bridge.save_binding(self.fixture.p,'historical',b)

 def replace_grant(self,grant):
  # Deliberately construct internally rehashed corrupt fixtures; validators
  # must reject invalid semantics, not merely fail a stale outer checksum.
  p=Path(self.result['grant']['file']);p.write_text(json.dumps(grant))
  b=copy.deepcopy(self.binding);b['manual_tab_revalidation']=m.pointer(p)
  self.write_binding(b)

 def test_old_code_can_prove_completion_but_cannot_grant_current_access(self):
  changed={**m.code_fingerprint(),'xhs_bridge_driver.mjs':'0'*64}
  with patch.object(m,'code_fingerprint',return_value=changed):
   self.check()
   with patch.object(m.nr,'current_stops',return_value=self.fixture.stops),self.assertRaisesRegex(Exception,'MANUAL_VALIDATOR_CHANGED'):
    m.web_exceptions(self.fixture.p,self.binding,['k'])

 def test_stale_current_parent_still_prevents_creating_a_tab(self):
  changed={**m.code_fingerprint(),'xhs_bridge_driver.mjs':'0'*64}
  @contextlib.contextmanager
  def lease(project,b):
   m.web_exceptions(project,b,['k'])
   yield ['k']
  creator_calls=[]
  with patch.object(m,'code_fingerprint',return_value=changed),patch.object(m.nr,'current_stops',return_value=self.fixture.stops),patch.object(t.bridge,'lease',lease),self.assertRaisesRegex(Exception,'MANUAL_VALIDATOR_CHANGED'):
   t.prepare(self.fixture.p/'new',self.fixture.p,'historical','Synthetic authorized task.',lambda *a:creator_calls.append(a))
  self.assertEqual(creator_calls,[])

 def test_changed_current_stops_still_reject_historical_completion(self):
  changed={'k':dict(self.fixture.stops['k'],sha256='f'*64)}
  with self.assertRaisesRegex(Exception,'MANUAL_STOP_CHANGED'):self.check(changed)

 def test_renewal_archive_only_proves_old_completion_when_stops_have_changed(self):
  inspection=self.root/'inspection.json'
  changed={'k':dict(self.fixture.stops['k'],sha256='f'*64)}
  with patch.object(m.nr,'current_stops',return_value=changed):
   r=t.renewal_archive(self.fixture.p,inspection,self.binding)
   self.assertEqual(r['inspection'],m.pointer(inspection))
   with self.assertRaisesRegex(Exception,'MANUAL_STOP_CHANGED'):self.check(changed)

 def test_renewal_rejects_account_and_context_changes(self):
  for field in ['account_uid','context_id']:
   parent=dict(self.binding);parent[field]='other'
   with self.assertRaisesRegex(Exception,'RENEWAL_ACCOUNT_CHANGED'):
    t.renewal_archive(self.fixture.p,self.root/'inspection.json',parent)

 def test_renewal_requires_completed_review_and_exact_project(self):
  with self.assertRaisesRegex(Exception,'RENEWAL_LOCATION_CHANGED'):
   t.renewal_archive(self.fixture.p/'another',self.root/'inspection.json',self.binding)
  Path(self.grant['review']['file']).write_text('{}')
  with self.assertRaises(Exception):t.renewal_archive(self.fixture.p,self.root/'inspection.json',self.binding)

 def remove_temporary_reservation(self):
  probe=m.check_pointer(self.grant['probe']);search=m.check_pointer(probe['inspection'])
  attempt=m.check_pointer(search['attempt']);Path(attempt['account_attempt']['file']).unlink()

 def test_missing_temporary_reservation_is_history_only(self):
  self.remove_temporary_reservation()
  result=t.renewal_archive(self.fixture.p,self.root/'inspection.json',self.binding)
  self.assertEqual(result['grant'],m.pointer(self.root/'grant.json'))
  with self.assertRaisesRegex(Exception,'ORIGINAL_FILE_REQUIRED'):m.validated_probe(self.grant['probe'])

 def test_missing_reservation_does_not_hide_changed_source_or_review(self):
  self.remove_temporary_reservation()
  probe=m.check_pointer(self.grant['probe']);Path(probe['capture']['file']).write_text('{}')
  with self.assertRaises(Exception):t.renewal_archive(self.fixture.p,self.root/'inspection.json',self.binding)

 def test_present_but_changed_reservation_is_rejected(self):
  probe=m.check_pointer(self.grant['probe']);search=m.check_pointer(probe['inspection'])
  attempt=m.check_pointer(search['attempt']);Path(attempt['account_attempt']['file']).write_text('{}')
  with self.assertRaises(Exception):t.renewal_archive(self.fixture.p,self.root/'inspection.json',self.binding)

 def test_changed_binding_account_is_rejected(self):
  b=copy.deepcopy(self.binding);b['account_uid']='8'*24;self.write_binding(b)
  with self.assertRaisesRegex(Exception,'MANUAL_AUTHORITY_CHANGED'):self.check()

 def test_changed_binding_project_is_rejected(self):
  b=copy.deepcopy(self.binding);b['project']=str(self.fixture.p/'wrong');self.write_binding(b)
  with self.assertRaisesRegex(Exception,'BINDING_NOT_FOUND'):self.check()

 def test_changed_binding_page_is_rejected(self):
  b=copy.deepcopy(self.binding);b['tabs']['detail']['page']='B'*32;self.write_binding(b)
  with self.assertRaisesRegex(Exception,'PROJECT_TAB_ATTESTATION_TARGET_CHANGED'):self.check()

 def test_changed_initial_inspection_hash_is_rejected(self):
  done=json.loads(self.record.with_suffix('.done.json').read_text())
  Path(done['inspection']['file']).write_text('{}')
  with self.assertRaises(Exception):self.check()

 def test_changed_search_capture_hash_is_rejected(self):
  probe=m.check_pointer(self.grant['probe']);search=m.check_pointer(probe['inspection'])
  Path(search['capture']['file']).write_text('{}')
  with self.assertRaises(Exception):self.check()

 def test_changed_note_capture_hash_is_rejected(self):
  probe=m.check_pointer(self.grant['probe']);Path(probe['capture']['file']).write_text('{}')
  with self.assertRaises(Exception):self.check()

 def test_changed_review_hash_is_rejected(self):
  Path(self.grant['review']['file']).write_text('{}')
  with self.assertRaises(Exception):self.check()

 def test_changed_frozen_stop_hash_is_rejected(self):
  probe=m.check_pointer(self.grant['probe']);search=m.check_pointer(probe['inspection']);attempt=m.check_pointer(search['attempt'])
  Path(next(iter(attempt['frozen_stops'].values()))['file']).write_text('{}')
  with self.assertRaises(Exception):self.check()

 def test_incomplete_review_rejected_even_with_updated_hashes(self):
  review=m.check_pointer(self.grant['review']);review['body_read']=False
  p=self.root/'incomplete-review.json';m.evidence(p,review)
  g=copy.deepcopy(self.grant);g['review']=m.pointer(p);self.replace_grant(g)
  with self.assertRaisesRegex(Exception,'MANUAL_ACTUAL_CODEX_REVIEW_REQUIRED'):self.check()

 def test_changed_scope_rejected_even_with_updated_hashes(self):
  g=copy.deepcopy(self.grant);g['scope']='all_channels';self.replace_grant(g)
  with self.assertRaisesRegex(Exception,'MANUAL_GRANT_SCOPE_CHANGED'):self.check()

 def test_missing_fingerprint_is_not_a_historical_attestation(self):
  g=copy.deepcopy(self.grant);del g['code_fingerprint'];self.replace_grant(g)
  with self.assertRaisesRegex(Exception,'PROJECT_TAB_HISTORICAL_FINGERPRINT_INVALID'):self.check()

 def test_malformed_fingerprint_is_not_a_historical_attestation(self):
  g=copy.deepcopy(self.grant);g['code_fingerprint']['xhs_bridge_driver.mjs']='not-a-hash';self.replace_grant(g)
  with self.assertRaisesRegex(Exception,'PROJECT_TAB_HISTORICAL_FINGERPRINT_INVALID'):self.check()

 def test_unobserved_note_rejected_even_with_updated_hashes(self):
  probe=m.check_pointer(self.grant['probe']);note=m.check_pointer(probe['capture'])
  note['url']='https://www.xiaohongshu.com/explore/'+'6'*24
  p=self.root/'unobserved-note.json';m.evidence(p,note);probe['capture']=m.pointer(p)
  probe_path=self.root/'unobserved-probe.json';m.evidence(probe_path,probe)
  g=copy.deepcopy(self.grant);g['probe']=m.pointer(probe_path);self.replace_grant(g)
  with self.assertRaisesRegex(Exception,'MANUAL_NOTE_NOT_IN_SEARCH'):self.check()

 def test_modified_search_account_rejected_even_with_updated_hashes(self):
  probe=m.check_pointer(self.grant['probe']);search=m.check_pointer(probe['inspection']);capture=m.check_pointer(search['capture'])
  capture['uid']='7'*24
  capture_path=self.root/'wrong-account-search.json';m.evidence(capture_path,capture);search['capture']=m.pointer(capture_path)
  search_path=self.root/'wrong-account-inspection.json';m.evidence(search_path,search);probe['inspection']=m.pointer(search_path)
  probe_path=self.root/'wrong-account-probe.json';m.evidence(probe_path,probe)
  g=copy.deepcopy(self.grant);g['probe']=m.pointer(probe_path);self.replace_grant(g)
  with self.assertRaises(Exception):self.check()

 def test_wrong_initial_inspection_chain_rejected_even_with_updated_hashes(self):
  probe=m.check_pointer(self.grant['probe']);search=m.check_pointer(probe['inspection'])
  original=m.check_pointer(search['initial_inspection']);duplicate=self.root/'different-initial.json';m.evidence(duplicate,original)
  search['initial_inspection']=m.pointer(duplicate)
  search_file=self.root/'different-search.json';m.evidence(search_file,search)
  probe['inspection']=m.pointer(search_file)
  probe_file=self.root/'different-probe.json';m.evidence(probe_file,probe)
  g=copy.deepcopy(self.grant);g['probe']=m.pointer(probe_file);self.replace_grant(g)
  with self.assertRaisesRegex(Exception,'PROJECT_TAB_INSPECTION_CHANGED'):self.check()

if __name__=='__main__':unittest.main()
