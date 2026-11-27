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


class PresearchReplacementTests(unittest.TestCase):
 def setUp(self):
  self.fixture=manual_fixtures.ManualTabTests(methodName='runTest');self.fixture.setUp()
  self.addCleanup(self.fixture.doCleanups);f=self.fixture
  self.project=f.p/'dest';self.source=f.p;f.url=t.HOME
  f.parent['tabs']['detail'].update(page='P'*32,chrome_tab_id=10,window_id=22,last_url=t.HOME,session='parent-session')
  f.parent['tabs']['detail'].pop('navigation_intent')
  self.created=0;self.mode='absent';self.warn=False;self.create_failure=False
  case=self
  class Driver:
   def __init__(self,*a):pass
   def close(self):pass
   def call(self,action,b=None,role=None,**kw):
    f.calls.append(action)
    if action=='lease_status':return {'matching':0,'leaseTabCount':0,'tabs':[]}
    if action=='probe_registered_target':return {'state':'registered_target_absent' if case.mode=='absent' else 'registered_target_exists_but_lease_missing','page':b['tabs']['detail']['page']}
    if action=='metadata':return {'page':'P'*32,'url':t.HOME}
    if action=='identity':
     if case.warn:raise t.bridge.BridgeError('ACCESS_STOP')
     return {'page':b['tabs']['detail']['page'],'url':t.HOME,'uid':f.uid}
    if action=='adopt_user_tab':return {'page':('A' if kw['chromeTabId']==11 else 'B')*32,'url':t.HOME,'owned':False}
    raise AssertionError(action)
  f.driver=Driver
  with self.mocks():
   result=t.prepare(self.project,self.source,'old','Synthetic autonomous tab authorization.',self.creator)
  self.inspection=Path(result['inspection']);self.root=self.inspection.parent
  m.evidence(self.root/'search-attempt.json',{'at':t.xp.legacy.now(),'inspection':m.pointer(self.inspection),
   'scope':'one_existing_query_health_check_only','task':'synthetic-query','url':'https://www.xiaohongshu.com/search_result?keyword=example&type=51'})
  m.evidence(self.root/'search-failure.json',{'at':t.xp.legacy.now(),'error':'TARGET_MISSING','collection_permitted':False})
  self.original={p:p.read_bytes() for p in self.root.iterdir() if p.is_file()}
  self.created=0;f.calls.clear()
 def creator(self,*args):
  self.created+=1
  if self.create_failure:raise t.bridge.BridgeError('PROJECT_TAB_CREATE_OUTCOME_UNKNOWN')
  tab=11 if not hasattr(self,'inspection') else 12
  return {'tabId':tab,'windowId':22,'url':t.HOME,'active':True}
 @contextlib.contextmanager
 def mocks(self):
  @contextlib.contextmanager
  def lease(*args):yield ['k']
  with self.fixture.mocks(),patch.object(t.bridge,'lease',lease):yield
 def run_replace(self):
  return t.replace_missing_presearch_home(self.project,self.source,'old','Synthetic autonomous tab authorization.',self.inspection,self.creator)
 def test_success_preserves_old_evidence_requires_fresh_search(self):
  with self.mocks():r=self.run_replace()
  self.assertTrue(r['replacement_home']);self.assertFalse(r['collection_permitted'])
  self.assertEqual(self.created,1)
  for p,b in self.original.items():self.assertEqual(p.read_bytes(),b)
  self.assertNotEqual(r['inspection'],str(self.inspection))
  record=next((self.fixture.p/'account-guard/project-tab-preparations').glob('*.done.json'))
  with self.mocks(),self.assertRaisesRegex(Exception,'FRESH_ATTESTATION'):
   t.completed_preparation(record.with_name(record.name.replace('.done.json','.json')),['k'])
 def test_second_replacement_and_lost_temporary_marker_cannot_recreate(self):
  with self.mocks():
   self.run_replace()
   for f in (self.fixture.p/'account-guard/project-tab-preparations').glob('*.replacement.json'):f.unlink()
   with self.assertRaisesRegex(Exception,'ALREADY_ATTEMPTED'):self.run_replace()
  self.assertEqual(self.created,1)
 def test_unknown_creation_consumes_single_attempt(self):
  self.create_failure=True
  with self.mocks():
   with self.assertRaisesRegex(Exception,'OUTCOME_UNKNOWN'):self.run_replace()
   with self.assertRaisesRegex(Exception,'ALREADY_ATTEMPTED'):self.run_replace()
  self.assertEqual(self.created,1)
 def test_any_search_progress_rejects_before_browser(self):
  for n in ['search-before.json','search-navigation.json','search-capture-0.json','search-metadata-0.json','grant.json','note-attempt.json']:
   f=self.root/n;f.write_text('{}')
   with self.mocks(),self.assertRaisesRegex(Exception,'MAY_HAVE_PROGRESS'):self.run_replace()
   f.unlink()
  self.assertEqual(self.fixture.calls,[]);self.assertEqual(self.created,0)
 def test_other_failure_types_cannot_replace(self):
  p=self.root/'search-failure.json';old=json.loads(p.read_text())
  for error in ['ACCESS_STOP','COMMAND_OUTCOME_UNKNOWN','MANUAL_QUERY_CHANGED']:
   p.write_text(json.dumps(dict(old,error=error)))
   with self.mocks(),self.assertRaisesRegex(Exception,'PRESEARCH_MISSING'):self.run_replace()
  self.assertEqual(self.fixture.calls,[])
 def test_existing_target_or_missing_lease_cannot_replace(self):
  self.mode='exists'
  with self.mocks(),self.assertRaisesRegex(Exception,'EXACT_ABSENCE'):self.run_replace()
  self.assertEqual(self.created,0)
 def test_new_stop_rejects_before_browser(self):
  self.fixture.stops=dict(self.fixture.stops,additional={'sha256':'f'*64})
  with self.mocks(),self.assertRaisesRegex(Exception,'STOP_CHANGED'):self.run_replace()
  self.assertEqual(self.created,0);self.assertEqual(self.fixture.calls,[])
 def test_invalid_current_parent_lease_cannot_replace(self):
  with self.mocks(),patch.object(t.bridge,'lease',side_effect=t.bridge.BridgeError('SHARED_STOP')),self.assertRaisesRegex(Exception,'SHARED_STOP'):self.run_replace()
  self.assertEqual(self.created,0)
 def test_live_access_warning_stops_shared_account(self):
  self.warn=True
  with self.mocks(),patch.object(t.bridge.identity,'stop_all') as stop,self.assertRaisesRegex(Exception,'ACCESS_STOP'):self.run_replace()
  stop.assert_called_once();self.assertEqual(self.created,0)
 def test_changed_frozen_stop_rejected(self):
  attempt=m.check_pointer(m.check_pointer(m.pointer(self.inspection))['attempt'])
  Path(next(iter(attempt['frozen_stops'].values()))['file']).write_text('{}')
  with self.mocks(),self.assertRaises(Exception):self.run_replace()
  self.assertEqual(self.created,0)
 def test_changed_parent_account_rejected(self):
  self.fixture.parent=dict(self.fixture.parent,account_uid='9'*24)
  with self.mocks(),self.assertRaisesRegex(Exception,'PARENT_CHANGED'):self.run_replace()
  self.assertEqual(self.created,0)
 def test_replacement_home_cannot_be_replaced_again(self):
  with self.mocks():r=self.run_replace()
  newer=Path(r['inspection']);self.inspection=newer
  with self.mocks(),self.assertRaises(Exception):self.run_replace()
  self.assertEqual(self.created,1)
 def test_full_fresh_probe_chain_allows_future_preparation(self):
  with self.mocks():r=self.run_replace()
  f=self.fixture;initial=Path(r['inspection']);root=initial.parent;inspected=m.check_pointer(m.pointer(initial))
  capture={'uid':f.uid,'url':'https://www.xiaohongshu.com/search_result?keyword=example&type=51',
   'cards':[{'url':'https://www.xiaohongshu.com/explore/'+'3'*24,'text':'Synthetic diary'}]}
  search=dict(inspected,initial_inspection=m.pointer(initial),capture=m.evidence(root/'actual-search.json',capture))
  note={'uid':f.uid,'url':capture['cards'][0]['url'],'title':'Synthetic diary','body':'Synthetic treatment narrative.',
   'authors':[{'name':'synthetic','url':'https://www.xiaohongshu.com/user/profile/'+'5'*24}]}
  b=copy.deepcopy(inspected['binding']);b['tabs']['detail']['last_url']=note['url'];b['tabs']['search']=b['tabs']['detail']
  probe={'kind':m.KIND,'binding':b,'inspection':m.evidence(root/'search-probe.json',search),
   'capture':m.evidence(root/'normal-note.json',note),'excluded_note_id':None,'collection_permitted':False}
  probe_file=root/'note-probe.json';m.evidence(probe_file,probe)
  review=f.put('review.json',{'reader':'codex','reviewed':True,'body_read':True,'capture_sha256':probe['capture']['sha256'],
   'summary':'Read the synthetic narrative and author.','positions':['/title','/body','/authors'],
   'comments_read':False,'media_read':False})
  with self.mocks():m.attest(probe_file,review,'new')
  record=next((f.p/'account-guard/project-tab-preparations').glob('*.done.json'))
  record=record.with_name(record.name.replace('.done.json','.json'))
  with patch.object(m.nr,'current_stops',return_value=f.stops):t.completed_preparation(record,['k'])
  # A later project enumerates the original registry and must ignore sidecars,
  # while the original record validates its completed replacement chain.
  original_load=t.bridge.load_binding
  def load(project,name):
   return f.parent if Path(project)==self.source else original_load(project,name)
  with self.mocks(),patch.object(t.bridge,'load_binding',side_effect=load),patch.object(t.manual,'inspect',return_value=r):
   t.prepare(f.p/'later',self.source,'old','Synthetic next project.',self.creator)
  (root/'normal-note.json').write_text('{}')
  with patch.object(m.nr,'current_stops',return_value=f.stops),self.assertRaises(Exception):t.completed_preparation(record,['k'])




class MigratedHistoricalRecordTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.base=Path(self.tmp.name).resolve();self.old=self.base/'old-guard';self.new=self.base/'durable-guard'
  self.old.mkdir(mode=0o700);self.new.mkdir(mode=0o700)
  self.relative=Path('project-tab-preparations')/('a'*64+'.json')
  self.source=self.old/self.relative;self.source.parent.mkdir();m.evidence(self.source,{'synthetic':'consumed-once'})
  self.ref=m.pointer(self.source);self.target=self.new/self.relative;self.target.parent.mkdir();self.target.write_bytes(self.source.read_bytes())
  self.report={'format':1,'source':str(self.old),'destination':str(self.new),'collection_permitted':False,
    'fresh_web_validation_required':True,'source_unchanged':True,'api_and_media_restrictions_preserved':True,
    'files':{str(self.relative):self.ref['sha256']},'source_files':{str(self.relative):self.ref['sha256']}}
  self.manifest=self.new/'state-migration.json';self.write_manifest()
  self.guard=patch.object(t.xp.legacy,'account_root',return_value=self.new);self.guard.start();self.addCleanup(self.guard.stop)
 def write_manifest(self):self.manifest.write_text(json.dumps(self.report))
 def check(self,ref=None):return t.historical_account_record(ref or self.ref,self.target)
 def test_registered_copy_preserves_consumption_when_source_was_cleaned(self):
  self.source.unlink();before=self.target.read_bytes();self.assertEqual(self.check(),{'synthetic':'consumed-once'});self.assertEqual(self.target.read_bytes(),before)
 def test_exact_current_pointer_needs_no_migration(self):
  self.manifest.unlink();self.assertEqual(self.check(m.pointer(self.target)),{'synthetic':'consumed-once'})
 def test_arbitrary_old_path_is_rejected_even_with_identical_bytes(self):
  other=self.base/'other.json';other.write_bytes(self.source.read_bytes())
  with self.assertRaisesRegex(Exception,'MIGRATION_SOURCE_CHANGED'):self.check(m.pointer(other))
 def test_changed_current_bytes_are_rejected(self):
  self.target.write_text('{}')
  with self.assertRaisesRegex(Exception,'MIGRATION_RECORD_CHANGED'):self.check()
 def test_still_present_but_changed_old_bytes_are_rejected(self):
  self.source.write_text('{}')
  with self.assertRaises(Exception):self.check()
 def test_missing_original_hash_registration_is_rejected(self):
  del self.report['source_files'][str(self.relative)];self.write_manifest()
  with self.assertRaisesRegex(Exception,'MIGRATION_RECORD_CHANGED'):self.check()
 def test_missing_copy_hash_registration_is_rejected(self):
  del self.report['files'][str(self.relative)];self.write_manifest()
  with self.assertRaisesRegex(Exception,'MIGRATION_RECORD_CHANGED'):self.check()
 def test_malformed_manifest_and_changed_destination_are_rejected(self):
  self.manifest.write_text('not-json')
  with self.assertRaises(Exception):self.check()
  self.report['destination']=str(self.old);self.write_manifest()
  with self.assertRaisesRegex(Exception,'MIGRATION_CHANGED'):self.check()
 def test_missing_manifest_is_not_an_implicit_migration(self):
  self.manifest.unlink()
  with self.assertRaisesRegex(Exception,'MIGRATION_REQUIRED'):self.check()
 def test_symlinked_manifest_is_rejected(self):
  actual=self.base/'actual-manifest.json';actual.write_bytes(self.manifest.read_bytes());self.manifest.unlink();self.manifest.symlink_to(actual)
  with self.assertRaisesRegex(Exception,'MIGRATION_REQUIRED'):self.check()
 def test_migration_cannot_claim_collection_permission(self):
  self.report['collection_permitted']=True;self.write_manifest()
  with self.assertRaisesRegex(Exception,'MIGRATION_CHANGED'):self.check()
 def test_current_record_symlink_cannot_bypass_missing_manifest(self):
  self.target.unlink();self.target.symlink_to(self.source);self.manifest.unlink()
  with self.assertRaisesRegex(Exception,'MIGRATION_RECORD_CHANGED'):self.check()
 def test_current_record_symlink_cannot_bypass_manifest_validation(self):
  self.target.unlink();self.target.symlink_to(self.source)
  with self.assertRaisesRegex(Exception,'MIGRATION_RECORD_CHANGED'):self.check()
 def test_parent_directory_symlink_cannot_bypass_missing_manifest(self):
  self.target.unlink();self.target.parent.rmdir();self.target.parent.symlink_to(self.source.parent,target_is_directory=True);self.manifest.unlink()
  with self.assertRaisesRegex(Exception,'MIGRATION_RECORD_CHANGED'):self.check()
 def test_current_record_outside_account_root_is_rejected_before_exact_match(self):
  with self.assertRaisesRegex(Exception,'MIGRATION_RECORD_CHANGED'):
   t.historical_account_record(self.ref,self.source)


class MigratedCompletedReplacementTests(PresearchReplacementTests):
 def completed_chain(self):
  with self.mocks():r=self.run_replace()
  f=self.fixture;initial=Path(r['inspection']);root=initial.parent;inspected=m.check_pointer(m.pointer(initial))
  capture={'uid':f.uid,'url':'https://www.xiaohongshu.com/search_result?keyword=example&type=51',
    'cards':[{'url':'https://www.xiaohongshu.com/explore/'+'3'*24,'text':'Synthetic diary'}]}
  search=dict(inspected,initial_inspection=m.pointer(initial),capture=m.evidence(root/'actual-search.json',capture))
  note={'uid':f.uid,'url':capture['cards'][0]['url'],'title':'Synthetic diary','body':'Synthetic treatment narrative.',
    'authors':[{'name':'synthetic','url':'https://www.xiaohongshu.com/user/profile/'+'5'*24}]}
  b=copy.deepcopy(inspected['binding']);b['tabs']['detail']['last_url']=note['url'];b['tabs']['search']=b['tabs']['detail']
  probe={'kind':m.KIND,'binding':b,'inspection':m.evidence(root/'search-probe.json',search),
    'capture':m.evidence(root/'normal-note.json',note),'excluded_note_id':None,'collection_permitted':False}
  probe_file=root/'note-probe.json';m.evidence(probe_file,probe)
  review=f.put('review.json',{'reader':'codex','reviewed':True,'body_read':True,'capture_sha256':probe['capture']['sha256'],
    'summary':'Read the synthetic narrative and author.','positions':['/title','/body','/authors'],
    'comments_read':False,'media_read':False})
  with self.mocks():result=m.attest(probe_file,review,'new')
  self.completed_grant=m.check_pointer(result['grant']);self.completed_root=root
  return next((f.p/'account-guard/project-tab-preparations').glob('*.done.json')).with_suffix('').with_suffix('.json')
 def migrate_records(self):
  import shutil,hashlib
  record=self.completed_chain();self.old_guard=self.fixture.p/'account-guard';self.old_guard.chmod(0o700)
  self.new_guard=self.fixture.p/'durable-guard';shutil.copytree(self.old_guard,self.new_guard);self.new_guard.chmod(0o700)
  hashes={str(p.relative_to(self.old_guard)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.old_guard.rglob('*.json')}
  self.migration={'format':1,'source':str(self.old_guard),'destination':str(self.new_guard),'collection_permitted':False,
    'fresh_web_validation_required':True,'source_unchanged':True,'api_and_media_restrictions_preserved':True,
    'files':hashes,'source_files':hashes}
  (self.new_guard/'state-migration.json').write_text(json.dumps(self.migration))
  current=self.new_guard/record.relative_to(self.old_guard);shutil.rmtree(self.old_guard)
  return current
 def test_registered_migrated_replacement_is_history_without_reviving_access(self):
  record=self.migrate_records();before={p:p.read_bytes() for p in self.new_guard.rglob('*.json')}
  changed=dict(self.fixture.stops,additional={'sha256':'f'*64})
  with patch.object(t.xp.legacy,'account_root',return_value=self.new_guard),patch.object(t.nr,'current_stops',return_value=changed):
   t.completed_preparation(record,['k'],historical_only=True)
   with self.assertRaises(Exception):t.completed_preparation(record,['k'])
   with self.assertRaises(Exception):m.validated_probe(self.completed_grant['probe'])
  for p,b in before.items():self.assertEqual(p.read_bytes(),b)
 def test_changed_inspection_is_rejected_in_migrated_history(self):
  record=self.migrate_records();marker=record.with_suffix('.replacement.json');mark=json.loads(marker.read_text());intent=m.check_pointer(mark['intent'])
  other=self.fixture.put('other-inspection.json',{});intent['inspection']=m.pointer(other);Path(mark['intent']['file']).write_text(json.dumps(intent));mark['intent']=m.pointer(mark['intent']['file']);marker.write_text(json.dumps(mark))
  with patch.object(t.xp.legacy,'account_root',return_value=self.new_guard),self.assertRaisesRegex(Exception,'REPLACEMENT_CHANGED'):
   t.completed_preparation(record,['k'],historical_only=True)
 def test_changed_review_still_rejects_migrated_history(self):
  record=self.migrate_records();Path(self.completed_grant['review']['file']).write_text('{}')
  with patch.object(t.xp.legacy,'account_root',return_value=self.new_guard),self.assertRaises(Exception):
   t.completed_preparation(record,['k'],historical_only=True)
 def test_migrated_history_cannot_bypass_current_parent_stop(self):
  self.migrate_records()
  with patch.object(t.xp.legacy,'account_root',return_value=self.new_guard),self.mocks(),patch.object(t.bridge,'lease',side_effect=t.bridge.BridgeError('SHARED_STOP')),patch.object(t,'completed_preparation') as history,self.assertRaisesRegex(Exception,'SHARED_STOP'):
   t.prepare(self.project/'later',self.source,'old','Synthetic subsequent research.',self.creator)
  history.assert_not_called()

if __name__=='__main__':unittest.main()
