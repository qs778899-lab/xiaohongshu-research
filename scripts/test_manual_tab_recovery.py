import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_bridge as bridge
import xhs_manual_tab_recovery as m
import xhs_account_identity as identity
import xhs_project as xp

class ManualTabTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p=Path(self.tmp.name).resolve()
        guard=patch.object(xp.legacy,'account_root',return_value=self.p/'account-guard')
        guard.start();self.addCleanup(guard.stop)
        self.url='https://www.xiaohongshu.com/search_result/?keyword=test&type=51'
        self.uid='1'*24
        self.parent={'format':'xhs-bridge-v1','project':str(self.p),'profile_label':'test',
          'context_id':'test-context','account_uid':self.uid,'profile_guard_key':'k',
          'runtime':{},'extension_version':'1.0.27','binding_name':'old','session':'xhs-old',
          'tabs':{'detail':{'navigation_intent':{'url':'https://www.xiaohongshu.com/explore/'+'2'*24}}}}
        self.stop=self.put('STOP.json',{'at':'2026-11-11T00:00:00+00:00','reason':m.nr.STOP_REASON,
                                      'channel':'bridge','credential_free':True})
        self.stops={'k':dict(m.pointer(self.stop),at='2026-11-11T00:00:00+00:00')}
        self.meta=self.put('meta.json',{'metadata':{'tabs':[{'tabId':11,'windowId':22,'url':self.url,'active':True}]}})
        self.user=self.put('user.json',{'user_statement':'Continue in the manually restored tab.',
          'normal_other_posts_statement':'Other posts work normally.', 'processed_at':'2026-11-11T01:00:00+00:00'})
        self.calls=[]
        self.capture={'uid':self.uid,'url':self.url,'stage':'search',
          'cards':[{'url':'https://www.xiaohongshu.com/explore/'+'3'*24,'text':'example'}]}
        self.adopt_error=None
        self.error=None
        self.identity_uid=self.uid
        case=self
        class FakeDriver:
            def __init__(self,*args):pass
            def close(self):pass
            def call(self,action,b=None,role=None,**kw):
                case.calls.append(action)
                if action=='diagnose_active_user_tab':
                    if case.error:raise bridge.BridgeError(case.error)
                    return {'tabId':11,'windowId':22,'url':case.url,'active':True,
                            'pageKind':'public_page_metadata_only','pageContentRead':False}
                if action=='active_user_tab':return {'tabId':11,'windowId':22,'url':case.url,'active':True}
                if action=='adopt_user_tab' and case.adopt_error:raise bridge.BridgeError(case.adopt_error)
                if action=='adopt_user_tab':return {'page':'A'*32,'url':case.url,'owned':False}
                if action=='identity':return {'uid':case.identity_uid,'url':case.url,'page':'A'*32}
                if action=='capture':
                    if case.error:raise bridge.BridgeError(case.error)
                    return case.capture
                raise AssertionError(action)
        self.driver=FakeDriver

    def put(self,name,data):
        p=self.p/name;p.write_text(json.dumps(data));return p

    @contextlib.contextmanager
    def mocks(self):
        @contextlib.contextmanager
        def lock(*args):yield ['k']
        with patch.object(bridge,'load_binding',return_value=self.parent),patch.object(bridge,'Driver',self.driver),\
             patch.object(m.nr,'locked_stops',lock),patch.object(m.nr,'current_stops',return_value=self.stops):
            yield

    def run_inspect(self,destination=None):
        with self.mocks():return m.inspect(destination or self.p,'old',self.meta,self.user,
                                         parent_project=self.p if destination else None)

    def test_ai_search_capture_remains_card_based_and_route_specific(self):
        self.url='https://www.xiaohongshu.com/search_result_ai?keyword=Synthetic%2520query'
        self.meta=self.put('meta-ai.json',{'tabId':11,'windowId':22,'url':self.url,'active':True})
        self.capture['url']=self.url
        result=self.run_inspect()
        inspected=m.check_pointer(m.pointer(result['inspection']))
        self.assertFalse(result['collection_permitted'])
        self.assertEqual(m.proof_args(self.url),{'stage':'search','expectedPath':'/search_result_ai','query':'Synthetic%20query','backgroundText':True})
        self.assertEqual(m.check_pointer(inspected['capture'])['cards'],self.capture['cards'])
        with self.assertRaisesRegex(bridge.BridgeError,'MANUAL_SEARCH_CONTENT_REQUIRED'):
            m.require_capture(dict(self.capture,cards=[],body='Generated summary'),inspected['binding'],m.proof_args(self.url))
        with self.assertRaisesRegex(bridge.BridgeError,'MANUAL_CAPTURE_TARGET_CHANGED'):
            m.require_capture(dict(self.capture,url=self.url.replace('search_result_ai','search_result')),inspected['binding'],m.proof_args(self.url))

    def test_diagnosis_needs_no_restoration_claim_and_consumes_no_attempt(self):
        before=self.stop.read_bytes()
        with self.mocks():result=m.diagnose_active(self.p,'old','Diagnose the verification interface only.')
        self.assertEqual(self.calls,['diagnose_active_user_tab'])
        self.assertFalse(result['collection_permitted']);self.assertFalse(result['send_permitted'])
        self.assertFalse(result['verification_completed']);self.assertFalse(result['restoration_attempt_consumed'])
        self.assertEqual(self.stop.read_bytes(),before)
        self.assertFalse((self.p/'bridge/manual-tab-recoveries').exists())
        self.assertFalse((self.p/'account-guard/manual-tab-attempts').exists())
        self.assertEqual(m.check_pointer(result['evidence'])['status'],'observed')

    def test_diagnosis_distinguishes_execution_permission_from_browser_disconnect(self):
        self.error='CONNECTOR_PERMISSION_DENIED'
        with self.mocks():result=m.diagnose_active(self.p,'old','Diagnose only.')
        self.assertEqual(result['next_action'],'execution_permission_review_not_browser_restart')
        self.assertEqual(self.calls,['diagnose_active_user_tab'])
        self.assertFalse(result['restoration_attempt_consumed'])

    def test_diagnosis_requires_authorization_before_driver(self):
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'AUTHORIZATION_REQUIRED'):
            m.diagnose_active(self.p,'old','')
        self.assertEqual(self.calls,[])

    def test_diagnosis_rejects_concurrent_stop_change(self):
        with self.mocks(),patch.object(m.nr,'current_stops',side_effect=[self.stops,{}]),\
             self.assertRaisesRegex(bridge.BridgeError,'STOP_CHANGED'):
            m.diagnose_active(self.p,'old','Diagnose only.')
        self.assertFalse((self.p/'bridge/manual-tab-diagnostics').exists())

    def test_diagnosis_rejects_repository_destination_before_driver(self):
        with self.mocks(),self.assertRaises(Exception):
            m.diagnose_active(Path(m.__file__).resolve().parent.parent,'old','Diagnose only.')
        self.assertEqual(self.calls,[])

    def failed_focus_fixture(self):
        self.adopt_error='USER_TAB_NOT_FOCUSED'
        with self.assertRaisesRegex(bridge.BridgeError,'USER_TAB_NOT_FOCUSED'):self.run_inspect()
        self.adopt_error=None
        return next((self.p/'bridge/manual-tab-recoveries').glob('*/attempt.json'))

    def test_finish_prebind_focus_rejects_copied_attempt_directory(self):
        import shutil
        f=self.failed_focus_fixture();copy=f.parent.parent/'copy'
        shutil.copytree(f.parent,copy)
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'LOCATION_CHANGED'):m.finish_inspection(copy/'attempt.json')
        self.assertEqual(self.calls,['adopt_user_tab'])

    def test_finish_prebind_focus_rejects_candidate_account_or_context_change(self):
        f=self.failed_focus_fixture();original=f.read_text()
        for key in ['account_uid','context_id','runtime','extension_version']:
            value=json.loads(original);value['candidate'][key]='changed';f.write_text(json.dumps(value))
            with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'CANDIDATE_CHANGED'):m.finish_inspection(f)
        self.assertEqual(self.calls,['adopt_user_tab'])

    def test_finish_prebind_focus_rejects_changed_target_or_args(self):
        f=self.failed_focus_fixture();original=f.read_text()
        for key in ['target','args']:
            value=json.loads(original);value[key]['url']='https://www.xiaohongshu.com/explore';f.write_text(json.dumps(value))
            with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'ATTEMPT_CHANGED'):m.finish_inspection(f)
        self.assertEqual(self.calls,['adopt_user_tab'])

    def test_finish_prebind_focus_rejects_copied_reservation(self):
        f=self.failed_focus_fixture();value=json.loads(f.read_text())
        copied=self.p/'copy-reservation.json';copied.write_bytes(Path(value['account_attempt']['file']).read_bytes())
        value['account_attempt']=m.pointer(copied);f.write_text(json.dumps(value))
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'RESERVATION_LOCATION_CHANGED'):m.finish_inspection(f)
        self.assertEqual(self.calls,['adopt_user_tab'])

    def test_finish_prebind_focus_preserves_original_and_requires_full_probe(self):
        f=self.failed_focus_fixture();original=f.read_bytes();stop=self.stop.read_bytes()
        with self.mocks():result=m.finish_inspection(f)
        self.assertFalse(result['collection_permitted'])
        self.assertEqual(f.read_bytes(),original);self.assertEqual(self.stop.read_bytes(),stop)
        self.assertTrue((f.parent/'inspection-failure.json').exists())
        self.assertEqual(self.calls,['adopt_user_tab','adopt_user_tab','identity','capture'])

    def test_finish_prebind_focus_is_single_use_even_after_second_focus_failure(self):
        f=self.failed_focus_fixture();self.adopt_error='USER_TAB_NOT_FOCUSED'
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'USER_TAB_NOT_FOCUSED'):m.finish_inspection(f)
        calls=list(self.calls)
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):m.finish_inspection(f)
        self.assertEqual(self.calls,calls)

    def test_finish_prebind_focus_rejects_unknown_or_access_failure(self):
        f=self.failed_focus_fixture()
        for error in ['ACCESS_STOP','COMMAND_OUTCOME_UNKNOWN','USER_TAB_CHANGED']:
            (f.parent/'inspection-failure.json').write_text(json.dumps({'error':error}))
            with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'PREBIND_FOCUS'):m.finish_inspection(f)
        self.assertEqual(self.calls,['adopt_user_tab'])

    def test_finish_prebind_focus_rejects_existing_binding_progress(self):
        f=self.failed_focus_fixture();(f.parent/'adopted.json').write_text('{}')
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'MAY_HAVE_PROGRESS'):m.finish_inspection(f)
        self.assertEqual(self.calls,['adopt_user_tab'])

    def test_finish_prebind_focus_rejects_changed_parent(self):
        f=self.failed_focus_fixture();self.parent['context_id']='other-context'
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'PARENT_CHANGED'):m.finish_inspection(f)
        self.assertEqual(self.calls,['adopt_user_tab'])

    def test_finish_prebind_focus_rejects_new_stop(self):
        f=self.failed_focus_fixture();self.stops['k']['sha256']='changed'
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'STOP_CHANGED'):m.finish_inspection(f)
        self.assertEqual(self.calls,['adopt_user_tab'])

    def test_finish_prebind_focus_rejects_wrong_account_after_adoption(self):
        f=self.failed_focus_fixture();self.identity_uid='9'*24
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'IDENTITY_CHANGED'):m.finish_inspection(f)
        self.assertFalse((f.parent/'inspection.json').exists())

    def second_focus_failure(self):
        f=self.failed_focus_fixture();self.adopt_error='USER_TAB_NOT_FOCUSED'
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'USER_TAB_NOT_FOCUSED'):m.finish_inspection(f)
        self.adopt_error=None
        return f

    def test_verified_focus_checks_live_metadata_and_preserves_failures(self):
        f=self.second_focus_failure()
        saved={name:(f.parent/name).read_bytes() for name in ['attempt.json','inspection-failure.json','finish-inspection-attempt.json','finish-inspection-failure.json']}
        with self.mocks():result=m.finish_inspection(f,verify_focus=True)
        self.assertFalse(result['collection_permitted'])
        self.assertEqual(self.calls[-4:],['active_user_tab','adopt_user_tab','identity','capture'])
        for name,data in saved.items():self.assertEqual((f.parent/name).read_bytes(),data)

    def test_verified_focus_rejects_unknown_outcome_and_access_errors(self):
        f=self.second_focus_failure();before=list(self.calls)
        for error in ['ACCESS_STOP','COMMAND_OUTCOME_UNKNOWN','USER_TAB_CHANGED','USER_TAB_BIND_MISMATCH']:
            (f.parent/'finish-inspection-failure.json').write_text(json.dumps({'error':error}))
            with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'PREBIND_FOCUS'):m.finish_inspection(f,True)
        self.assertEqual(self.calls,before)

    def test_verified_focus_rejects_different_active_tab_before_adoption(self):
        f=self.second_focus_failure();self.url='https://www.xiaohongshu.com/explore'
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'FOCUSED_TARGET_CHANGED'):m.finish_inspection(f,True)
        self.assertEqual(self.calls[-1],'active_user_tab')
        self.assertFalse((f.parent/'verified-focus-attempt.json').exists())

    def test_failed_preflight_does_not_hide_later_unknown_binding_outcome(self):
        f=self.second_focus_failure();original_url=self.url;self.url='https://www.xiaohongshu.com/explore'
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'FOCUSED_TARGET_CHANGED'):m.finish_inspection(f,True)
        self.assertFalse((f.parent/'verified-focus-failure.json').exists())
        self.assertEqual(len(list((f.parent/'focus-diagnostics').glob('*.json'))),1)
        self.url=original_url;self.adopt_error='COMMAND_OUTCOME_UNKNOWN'
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'COMMAND_OUTCOME_UNKNOWN'):m.finish_inspection(f,True)
        self.assertEqual(json.loads((f.parent/'verified-focus-failure.json').read_text())['error'],'COMMAND_OUTCOME_UNKNOWN')
        before=list(self.calls)
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):m.finish_inspection(f,True)
        self.assertEqual(before,self.calls)

    def test_verified_focus_marker_cannot_be_reused_after_failure(self):
        f=self.second_focus_failure();self.adopt_error='USER_TAB_NOT_FOCUSED'
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'USER_TAB_NOT_FOCUSED'):m.finish_inspection(f,True)
        before=list(self.calls)
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):m.finish_inspection(f,True)
        self.assertEqual(self.calls,before)

    def test_verified_focus_rejects_changed_previous_attempt(self):
        f=self.second_focus_failure();v=json.loads((f.parent/'finish-inspection-attempt.json').read_text())
        v['attempt']['sha256']='tampered';(f.parent/'finish-inspection-attempt.json').write_text(json.dumps(v))
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'ATTEMPT_CHANGED'):m.finish_inspection(f,True)
        self.assertEqual(self.calls,['adopt_user_tab','adopt_user_tab'])

    def test_cross_project_inherits_prior_exclusions_in_new_grant(self):
        self.parent['manual_excluded_note_ids']=['7'*24]
        destination=self.p/'separate-project'
        probe,review=self.complete_probe_fixture(destination)
        with self.mocks():result=m.attest(probe,review,'manual-destination')
        self.assertEqual(result['excluded_note_ids'],['2'*24,'7'*24])

    def test_inherited_excluded_card_is_not_navigated(self):
        self.parent['manual_excluded_note_ids']=['3'*24]
        inspected=self.run_inspect(self.p/'separate-project')['inspection']
        before=list(self.calls)
        with self.assertRaisesRegex(bridge.BridgeError,'ORIGINAL_RESTRICTED_NOTE_EXCLUDED'):
            m.probe_note(inspected,self.capture['cards'][0]['url'])
        self.assertEqual(before,self.calls)

    def test_explicit_destination_keeps_capture_out_of_source_project(self):
        destination=self.p/'separate-project'
        result=self.run_inspect(destination)
        self.assertTrue(Path(result['inspection']).is_relative_to(destination))
        saved=m.check_pointer(m.pointer(result['inspection']))
        attempt=m.check_pointer(saved['attempt'])
        self.assertEqual(saved['binding']['project'],str(destination))
        self.assertEqual(attempt['parent_project'],str(self.p))
        self.assertFalse((self.p/'bridge').exists())
        self.assertFalse(result['collection_permitted'])

    def test_switching_destination_cannot_retry_consumed_probe(self):
        self.error='ACCESS_STOP'
        with self.assertRaises(bridge.BridgeError):self.run_inspect(self.p/'first')
        calls=list(self.calls)
        with self.assertRaisesRegex(bridge.BridgeError,'MANUAL_INSPECT_ALREADY_ATTEMPTED'):
            self.run_inspect(self.p/'second')
        self.assertEqual(calls,self.calls)

    def test_destination_grant_preserves_account_stop_and_own_authority(self):
        destination=self.p/'separate-project'
        probe,review=self.complete_probe_fixture(destination)
        before=self.stop.read_bytes()
        with self.mocks():result=m.attest(probe,review,'manual-destination')
        b=bridge.load_binding(destination,'manual-destination')
        self.assertEqual(before,self.stop.read_bytes())
        with patch.object(m.nr,'current_stops',return_value=self.stops):
            self.assertEqual(m.web_exceptions(destination,b,['k']),{'k':self.stops['k']['sha256']})
            with self.assertRaisesRegex(bridge.BridgeError,'MANUAL_PROJECT_CHANGED'):
                m.web_exceptions(self.p,b,['k'])
        self.assertFalse((self.p/'bridge/bindings/manual-destination.json').exists())

    def test_cross_project_active_entrypoint_only_records_destination(self):
        destination=self.p/'separate-project'
        with self.mocks():result=m.inspect_active(destination,'old',self.user,parent_project=self.p)
        self.assertTrue(Path(result['inspection']).is_relative_to(destination))
        self.assertEqual(len(list((destination/'bridge/manual-active-observations').glob('*.json'))),1)
        self.assertFalse((self.p/'bridge').exists())

    def test_cross_project_account_mismatch_never_reads_content(self):
        self.identity_uid='4'*24
        with self.assertRaisesRegex(bridge.BridgeError,'MANUAL_IDENTITY_CHANGED'):
            self.run_inspect(self.p/'separate-project')
        self.assertNotIn('capture',self.calls)

    def test_repository_destination_is_rejected_before_browser_calls(self):
        with self.assertRaises(xp.legacy.ResearchError):
            self.run_inspect(Path(m.__file__).resolve().parents[1])
        self.assertEqual(self.calls,[])

    def test_homepage_cannot_validate_research_access(self):
        self.put('meta.json',{'tabId':11,'windowId':22,'url':'https://www.xiaohongshu.com/explore','active':True})
        self.url='https://www.xiaohongshu.com/explore'
        result=self.run_inspect()
        self.assertFalse(result['collection_permitted'])
        self.assertNotIn('capture',self.calls)
        with self.assertRaises(bridge.BridgeError):m.probe_note(result['inspection'],'https://www.xiaohongshu.com/explore/'+'3'*24)

    def test_account_mismatch_does_not_read_content(self):
        self.identity_uid='4'*24
        with self.assertRaises(bridge.BridgeError):self.run_inspect()
        self.assertNotIn('capture',self.calls)
        self.assertFalse(list(self.p.rglob('inspection.json')))

    def test_success_is_still_probe_only_and_stop_unchanged(self):
        before=self.stop.read_bytes()
        result=self.run_inspect()
        self.assertFalse(result['collection_permitted'])
        self.assertEqual(before,self.stop.read_bytes())
        self.assertFalse((self.p/'bridge'/'bindings').exists())
        saved=json.loads(Path(result['inspection']).read_text())
        self.assertFalse(saved['binding']['tabs']['detail']['owned'])

    def test_active_tab_entrypoint_archives_metadata_and_stays_probe_only(self):
        before=self.stop.read_bytes()
        with self.mocks():result=m.inspect_active(self.p,'old',self.user)
        self.assertEqual(self.calls,['active_user_tab','adopt_user_tab','identity','capture'])
        self.assertFalse(result['collection_permitted'])
        self.assertEqual(before,self.stop.read_bytes())
        self.assertEqual(len(list((self.p/'bridge/manual-active-observations').glob('*.json'))),1)

    def body_search_fixture(self):
        self.url='https://www.xiaohongshu.com/explore/'+'5'*24
        self.meta.write_text(json.dumps({'metadata':{'tabs':[{'tabId':11,'windowId':22,'url':self.url,'active':True}]}}))
        self.capture={'uid':self.uid,'url':self.url,'stage':'body','title':'Normal source',
                      'body':'Saved source text','authors':[{'name':'Example'}]}
        inspected=self.run_inspect()['inspection']
        case=self
        class Driver:
            url=case.url
            def __init__(self,*args):pass
            def close(self):pass
            def call(self,action,b=None,role=None,**kw):
                case.calls.append(action)
                if action=='metadata':return {'url':self.url,'page':'A'*32}
                if action=='identity':return {'uid':case.identity_uid}
                if action=='navigate':self.url=kw['url'];return {'url':self.url,'navigationSubmitted':True}
                if action=='capture':return {'uid':case.uid,'url':self.url,'cards':[{'url':'https://www.xiaohongshu.com/explore/'+'6'*24}]}
                raise AssertionError(action)
        class Project:
            def __init__(self,*args):pass
            def task(self,*args):return {'kind':'native','payload':{'stage':'search'}}
            def close(self):pass
        return inspected,Driver,Project

    def test_existing_normal_body_can_start_bounded_search_revalidation(self):
        inspected,driver,project=self.body_search_fixture()
        with self.mocks(),patch.object(bridge,'Driver',driver),patch.object(xp,'Project',project),\
             patch.object(bridge,'observed_url',return_value='https://www.xiaohongshu.com/search_result?keyword=test'),\
             patch.object(m.time,'sleep'):
            result=m.probe_search(inspected,'existing')
            self.assertFalse(result['collection_permitted'])
            with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):
                m.probe_search(inspected,'existing')
        self.assertEqual(self.calls.count('navigate'),1)

    def test_account_change_before_search_prevents_navigation(self):
        inspected,driver,project=self.body_search_fixture();self.identity_uid='9'*24
        with self.mocks(),patch.object(bridge,'Driver',driver),patch.object(xp,'Project',project),\
             patch.object(bridge,'observed_url',return_value='https://www.xiaohongshu.com/search_result?keyword=test'):
            with self.assertRaisesRegex(bridge.BridgeError,'IDENTITY_CHANGED'):
                m.probe_search(inspected,'existing')
        self.assertNotIn('navigate',self.calls)

    def test_unready_body_cannot_be_used_for_search_revalidation(self):
        inspected,_,_=self.body_search_fixture()
        obj=json.loads(Path(inspected).read_text())
        raw=json.loads(Path(obj['capture']['file']).read_text());raw['loading']=True
        capture=self.put('unready.json',raw);obj['capture']=m.pointer(capture)
        bad=self.put('unready-inspection.json',obj);before=list(self.calls)
        with self.assertRaisesRegex(bridge.BridgeError,'CAPTURE_FAILED'):
            m.probe_search(bad,'existing')
        self.assertEqual(self.calls,before)

    def test_captcha_is_a_failure_and_not_retryable(self):
        self.error='ACCESS_STOP'
        with self.assertRaises(bridge.BridgeError):self.run_inspect()
        calls=list(self.calls)
        with self.assertRaises(bridge.BridgeError):self.run_inspect()
        self.assertEqual(calls,self.calls)
        self.assertFalse(list(self.p.rglob('inspection.json')))

    def test_unobserved_note_cannot_be_probed(self):
        inspected=self.run_inspect()['inspection']
        calls=list(self.calls)
        with self.assertRaises(bridge.BridgeError):m.probe_note(inspected,'https://www.xiaohongshu.com/explore/'+'4'*24)
        self.assertEqual(calls,self.calls)

    def test_original_restricted_note_remains_excluded(self):
        url='https://www.xiaohongshu.com/explore/'+'2'*24
        self.capture['cards']=[{'url':url}]
        inspected=self.run_inspect()['inspection']
        with self.assertRaises(bridge.BridgeError):m.probe_note(inspected,url)
        self.assertFalse(list(self.p.rglob('note-attempt.json')))

    def test_shared_tab_state_survives_save_load(self):
        slot={'page':'A'*32,'session':'xhs-test','owned':False,'last_url':self.url}
        b=dict(self.parent,manual_single_tab=True,binding_name='manual',tabs={'search':slot,'detail':dict(slot)})
        bridge.save_binding(self.p,'manual',b)
        loaded=bridge.load_binding(self.p,'manual')
        loaded['tabs']['detail']['last_url']='https://www.xiaohongshu.com/explore/'+'3'*24
        self.assertEqual(loaded['tabs']['search']['last_url'],loaded['tabs']['detail']['last_url'])
        bridge.save_binding(self.p,'manual',loaded)
        self.assertEqual(bridge.load_binding(self.p,'manual')['tabs']['search'],loaded['tabs']['detail'])

    def test_search_trailing_slash_is_equivalent_but_query_change_is_not(self):
        a='https://www.xiaohongshu.com/search_result?keyword=test&type=51'
        self.assertEqual(m.proof_args(a),m.proof_args(self.url))
        self.assertNotEqual(m.proof_args(a),m.proof_args(a.replace('test','different')))

    def layout_failure_fixture(self):
        inspected,driver,project=self.body_search_fixture()
        class Loading(driver):
            def call(self,action,*args,**kwargs):
                if action=='capture':raise bridge.BridgeError('BACKGROUND_LAYOUT_UNSETTLED')
                return super().call(action,*args,**kwargs)
        query='https://www.xiaohongshu.com/search_result?keyword=test'
        with self.mocks(),patch.object(bridge,'Driver',Loading),patch.object(xp,'Project',project),\
             patch.object(bridge,'observed_url',return_value=query),patch.object(m.time,'sleep'):
            with self.assertRaisesRegex(bridge.BridgeError,'LAYOUT_UNSETTLED'):
                m.probe_search(inspected,'existing')
        driver.url=query
        observed=self.put('layout-observation.json',{'reader':'codex','account_uid':self.uid,'url':query,
            'normal_search_visible':True,'window_restored':True,'no_access_warning':True,
            'summary':'Original query cards visible after selecting the exact task window.',
            'observed_at':'2099-01-01T00:00:00+00:00'})
        return inspected,driver,observed

    def test_observed_layout_recovery_reads_same_search_once_without_navigation(self):
        inspected,driver,observed=self.layout_failure_fixture()
        before=self.calls.count('navigate')
        with self.mocks(),patch.object(bridge,'Driver',driver):
            result=m.finish_search(inspected,observed)
            self.assertFalse(result['collection_permitted'])
            with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_CONTINUED'):
                m.finish_search(inspected,observed)
        self.assertEqual(self.calls.count('navigate'),before)

    def test_layout_recovery_requires_current_matching_observation(self):
        inspected,_,observed=self.layout_failure_fixture()
        original=json.loads(observed.read_text())
        for change in [{'account_uid':'9'*24},{'url':'https://www.xiaohongshu.com/search_result?keyword=other'},
                       {'observed_at':'2000-01-01T00:00:00Z'},{'normal_search_visible':False}]:
            observed.write_text(json.dumps(original|change))
            with self.assertRaises(bridge.BridgeError):m.finish_search(inspected,observed)
        self.assertFalse((Path(inspected).parent/'search-read-continuation.json').exists())

    def test_layout_observation_cannot_override_captcha_failure(self):
        inspected,_,observed=self.layout_failure_fixture()
        failure=Path(inspected).parent/'search-failure.json'
        data=json.loads(failure.read_text());data['error']='ACCESS_STOP';failure.write_text(json.dumps(data))
        with self.assertRaisesRegex(bridge.BridgeError,'NOT_APPLICABLE'):
            m.finish_search(inspected,observed)

    def test_access_warning_url_is_never_a_normalization_exception(self):
        with self.assertRaises(bridge.BridgeError):
            m.proof_args('https://www.xiaohongshu.com/website-login/captcha?redirectPath='+self.url)

    def test_note_search_and_explore_routes_match_only_same_id(self):
        a='https://www.xiaohongshu.com/search_result/'+'3'*24+'?xsec_source='
        b='https://www.xiaohongshu.com/explore/'+'3'*24+'?xsec_source=pc_feed'
        self.assertEqual(m.proof_args(a),m.proof_args(b))
        self.assertNotEqual(m.proof_args(a),m.proof_args(b.replace('3'*24,'4'*24)))

    def saved_titleless_fixture(self):
        inspection=Path(self.run_inspect()['inspection']);root=inspection.parent
        url=self.capture['cards'][0]['url']
        note={'uid':self.uid,'url':url,'stage':'body','title':'',
              'body':'A complete synthetic account of a public exhibition.',
              'authors':[{'name':'Example writer','url':'https://www.xiaohongshu.com/user/profile/'+'5'*24}]}
        files={'note-attempt.json':{'at':'2026-11-11T02:00:00+00:00','inspection':m.pointer(inspection),
                  'url':url,'note_id':'3'*24,'scope':'one_observed_public_note_health_check_only'},
               'note-failure.json':{'at':'2026-11-11T02:01:00+00:00','error':'MANUAL_NOTE_CONTENT_REQUIRED','collection_permitted':False},
               'note-before.json':{'page':'A'*32,'url':self.url},
               'note-navigation.json':{'navigationSubmitted':True},
               'note-metadata-0.json':{'page':'A'*32,'url':url},'note-capture-0.json':note}
        for name,value in files.items():m.evidence(root/name,value)
        return inspection

    def test_titleless_saved_note_finishes_without_browser_and_preserves_source(self):
        inspection=self.saved_titleless_fixture();root=inspection.parent
        originals={f.name:f.read_bytes() for f in root.iterdir() if f.is_file()}
        before=list(self.calls)
        with self.mocks(),patch.object(bridge,'Driver',side_effect=AssertionError('No browser')):
            result=m.finish_note(inspection)
            with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_FINISHED'):m.finish_note(inspection)
        self.assertEqual(self.calls,before);self.assertFalse(result['collection_permitted'])
        for name,value in originals.items():self.assertEqual((root/name).read_bytes(),value)
        probe=m.check_pointer(m.pointer(result['probe']));self.assertEqual(m.check_pointer(probe['capture'])['title'],'')
        self.assertEqual(m.validated_probe(m.pointer(result['probe']))[2]['body'],
                         'A complete synthetic account of a public exhibition.')
        review=self.put('titleless-review.json',{'reader':'codex','reviewed':True,'body_read':True,
            'capture_sha256':probe['capture']['sha256'],'summary':'Read the empty title field, full narrative and named author.',
            'positions':['/title','/body','/authors'],'comments_read':False,'media_read':False})
        with self.mocks():granted=m.attest(result['probe'],review,'titleless')
        self.assertEqual(granted['state'],'ready');self.assertTrue(granted['stops_unchanged'])

    def saved_image_only_fixture(self):
        inspection=self.saved_titleless_fixture();root=inspection.parent
        capture=json.loads((root/'note-capture-0.json').read_text())
        capture.update(title='Synthetic image question',body='',stage='body',
                       media=[{'tag':'IMG','url':'https://sns-webpic-qc.xhscdn.com/synthetic-note.webp'}])
        self.put(str(root/'note-capture-0.json'),capture)
        return inspection

    def test_image_only_first_capture_finishes_without_reading_pixels_or_navigating(self):
        inspection=self.saved_image_only_fixture();root=inspection.parent
        before={p.name:p.read_bytes() for p in root.glob('*.json')}
        with self.mocks(),patch.object(bridge,'Driver',side_effect=AssertionError('No browser')):
            result=m.finish_note(inspection)
        self.assertFalse(result['collection_permitted'])
        probe=m.check_pointer(m.pointer(result['probe']))
        self.assertIn('saved_image_only_note_history',probe)
        for name,data in before.items():self.assertEqual((root/name).read_bytes(),data)
        m.require_capture(m.check_pointer(probe['capture']),probe['binding'],m.proof_args(m.check_pointer(probe['capture'])['url']))
        review=self.put('image-note-review.json',{'reader':'codex','reviewed':True,'body_read':True,
            'capture_sha256':probe['capture']['sha256'],'summary':'Read title and author; body is explicitly empty. Image remains unread.',
            'positions':['/title','/body','/authors'],'comments_read':False,'media_read':False})
        with self.mocks():grant=m.attest(result['probe'],review,'image-note')
        self.assertEqual(grant['scope'],'rendered_public_web_only')
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'ALREADY_FINISHED'):m.finish_note(inspection)

    def test_image_only_capture_rejects_empty_placeholder_author_emoji_and_access_states(self):
        inspection=self.saved_image_only_fixture();root=inspection.parent
        capture=json.loads((root/'note-capture-0.json').read_text());b=json.loads(inspection.read_text())['binding'];args=m.proof_args(capture['url'])
        variants=[dict(capture,title=''),dict(capture,title='加载中'),dict(capture,body=None),dict(capture,authors=[]),
            dict(capture,media=[]),dict(capture,media=[{'tag':'IMG','url':'https://picasso-static.xiaohongshu.com/emoji.png'}]),
            dict(capture,media=[{'tag':'IMG','url':'https://xhscdn.com.evil/synthetic.webp'}]),dict(capture,loading=True),
            dict(capture,securityBlock=True),dict(capture,loginWall=True),dict(capture,notFound=True)]
        for value in variants:
            with self.subTest(value=value),self.assertRaises(bridge.BridgeError):m.require_capture(value,b,args)

    def test_titleless_capture_requires_observed_title_body_author_and_normal_state(self):
        inspection=self.saved_titleless_fixture();b=json.loads(inspection.read_text())['binding']
        capture=json.loads((inspection.parent/'note-capture-0.json').read_text());args=m.proof_args(capture['url'])
        changes=[{'title':None},{'title':False},{'body':''},{'body':'  '},{'body':123},{'body':'[图片]'},{'body':'加载中…'},{'body':'Loading...'},
                 {'authors':[]},{'authors':[{'name':'Example'}]},
                 {'authors':[{'name':'Example','url':'https://example.org/user/profile/'+'5'*24}]},
                 {'authors':[{'name':'','url':'https://www.xiaohongshu.com/user/profile/'+'5'*24}]},
                 {'stage':'search'},{'loading':True},{'loginWall':True},{'securityBlock':True},{'notFound':True},
                 {'uid':'9'*24},{'url':capture['url'].replace('3'*24,'4'*24)}]
        for change in changes:
            with self.subTest(change=change),self.assertRaises(bridge.BridgeError):
                m.require_capture(dict(capture,**change),b,args)
        missing=dict(capture);del missing['title']
        with self.assertRaisesRegex(bridge.BridgeError,'NOTE_CONTENT_REQUIRED'):m.require_capture(missing,b,args)

    def test_titleless_saved_note_rejects_changed_or_unsubmitted_history(self):
        inspection=self.saved_titleless_fixture();root=inspection.parent
        changes=[('note-failure.json','error','ACCESS_STOP'),('note-failure.json','error','COMMAND_OUTCOME_UNKNOWN'),
                 ('note-attempt.json','note_id','4'*24),('note-attempt.json','inspection',{}),
                 ('note-attempt.json','url','https://www.xiaohongshu.com/explore/'+'4'*24),
                 ('note-navigation.json','navigationSubmitted',False),('note-before.json','url','https://www.xiaohongshu.com/explore'),
                 ('note-before.json','securityBlock',True),('note-metadata-0.json','securityBlock',True),
                 ('note-navigation.json','error','ACCESS_STOP'),('note-metadata-0.json','loading',True),
                 ('note-metadata-0.json','page','B'*32),('note-metadata-0.json','windowId',99),
                 ('note-capture-0.json','title','A nonempty title'),('note-capture-0.json','body',''),
                 ('note-capture-0.json','url','https://www.xiaohongshu.com/explore/'+'4'*24)]
        for name,key,value in changes:
            path=root/name;original=path.read_bytes();changed=json.loads(original);changed[key]=value;path.write_text(json.dumps(changed))
            try:
                with self.subTest(name=name,key=key,value=value),self.mocks(),self.assertRaises(bridge.BridgeError):m.finish_note(inspection)
                self.assertFalse((root/'note-probe.json').exists())
            finally:path.write_bytes(original)
        (root/'note-capture-1.json').write_text('{}')
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'HISTORY_CHANGED'):m.finish_note(inspection)

    def test_titleless_saved_note_rejects_new_stop_without_grant(self):
        inspection=self.saved_titleless_fixture();self.stops['k']['sha256']='changed'
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'STOP_CHANGED'):m.finish_note(inspection)
        self.assertFalse((inspection.parent/'note-probe.json').exists())

    def test_saved_canonical_url_validation_remains_supported(self):
        inspection=self.saved_titleless_fixture();root=inspection.parent
        failure=json.loads((root/'note-failure.json').read_text());failure['error']='MANUAL_CAPTURE_TARGET_CHANGED'
        (root/'note-failure.json').write_text(json.dumps(failure))
        capture=json.loads((root/'note-capture-0.json').read_text());capture['title']='Exhibition diary'
        (root/'note-capture-0.json').write_text(json.dumps(capture))
        with self.mocks(),patch.object(bridge,'Driver',side_effect=AssertionError('No browser')):result=m.finish_note(inspection)
        self.assertFalse(result['collection_permitted'])
        self.assertEqual(m.validated_probe(m.pointer(result['probe']))[2]['title'],'Exhibition diary')

    def test_titleless_saved_note_grant_chain_rejects_later_history_edit(self):
        inspection=self.saved_titleless_fixture()
        with self.mocks():result=m.finish_note(inspection)
        (inspection.parent/'note-navigation.json').write_text('{"navigationSubmitted":false}')
        with self.assertRaisesRegex(bridge.BridgeError,'EVIDENCE_CHANGED'):m.validated_probe(m.pointer(result['probe']))

    def test_titleless_recheck_keeps_original_failure_history_in_new_grant(self):
        inspection=self.saved_titleless_fixture()
        with self.mocks():finished=m.finish_note(inspection)
        probe=m.check_pointer(m.pointer(finished['probe']));capture=m.check_pointer(probe['capture'])
        review={'reader':'codex','reviewed':True,'body_read':True,'capture_sha256':probe['capture']['sha256'],
                'summary':'Actually read the empty title, author and full narrative.',
                'positions':['/title','/body','/authors'],'comments_read':False,'media_read':False}
        review_path=self.put('first-review.json',review)
        with self.mocks():m.attest(finished['probe'],review_path,'first-titleless')
        class Driver:
            def __init__(self,*args):pass
            def close(self):pass
            def call(self,action,*args,**kwargs):
                if action in {'metadata','identity'}:return {'page':'A'*32,'url':capture['url'],'uid':capture['uid'],'visible':True}
                if action=='capture':return capture
                raise AssertionError(action)
        @contextlib.contextmanager
        def lock(*args):yield ['k']
        with patch.object(bridge,'Driver',Driver),patch.object(m.nr,'locked_stops',lock),patch.object(m.nr,'current_stops',return_value=self.stops):
            checked=m.recheck(self.p,'first-titleless')
            review['capture_sha256']=checked['capture']['sha256']
            second_review=self.put('second-review.json',review)
            m.attest(checked['probe'],second_review,'second-titleless')
            binding=bridge.load_binding(self.p,'second-titleless')
            m.web_exceptions(self.p,binding,['k'])
            (inspection.parent/'note-navigation.json').write_text('{"navigationSubmitted":false}')
            with self.assertRaisesRegex(bridge.BridgeError,'EVIDENCE_CHANGED'):m.web_exceptions(self.p,binding,['k'])

    def complete_probe_fixture(self,destination=None):
        inspected_path=Path(self.run_inspect(destination)['inspection'])
        root=inspected_path.parent;inspected=json.loads(inspected_path.read_text())
        note={'uid':self.uid,'url':'https://www.xiaohongshu.com/explore/'+'3'*24,
              'title':'Treatment diary','body':'Personal treatment narrative.',
              'authors':[{'name':'patient','url':'https://www.xiaohongshu.com/user/profile/'+'5'*24}]}
        capture=m.evidence(root/'actual-note.json',note)
        b=inspected['binding'];b['tabs']['detail']['last_url']=note['url'];b['tabs']['search']=b['tabs']['detail']
        probe={'kind':m.KIND,'binding':b,'inspection':m.pointer(inspected_path),
               'capture':capture,'excluded_note_id':'2'*24,'collection_permitted':False}
        probe_path=root/'note-probe.json';m.evidence(probe_path,probe)
        review={'reader':'codex','reviewed':True,'body_read':True,'capture_sha256':capture['sha256'],
                'summary':'Read title, author and full narrative. Comments and media not read.',
                'positions':['/title','/body','/authors'],'comments_read':False,'media_read':False}
        return probe_path,self.put('review.json',review)

    def create_grant_fixture(self):
        probe,review=self.complete_probe_fixture()
        @contextlib.contextmanager
        def lock(*args):yield ['k']
        with patch.object(m.nr,'locked_stops',lock),patch.object(m.nr,'current_stops',return_value=self.stops):
            result=m.attest(probe,review,'manual')
        return result,bridge.load_binding(self.p,'manual')

    def test_attest_only_enables_typed_web_binding(self):
        before=self.stop.read_bytes();result,b=self.create_grant_fixture()
        self.assertEqual(before,self.stop.read_bytes())
        self.assertEqual(result['excluded_note_ids'],['2'*24])
        with patch.object(m.nr,'current_stops',return_value=self.stops):
            self.assertEqual(m.web_exceptions(self.p,b,['k']),{'k':self.stops['k']['sha256']})
        with patch.object(identity,'guard_directory',return_value=self.p):
            with self.assertRaises(xp.legacy.StopRun):identity.check_stops(['k'])

    def test_new_stop_and_authority_change_revoke_web_grant(self):
        _,b=self.create_grant_fixture()
        changed={'k':dict(self.stops['k'],sha256='f'*64)}
        with patch.object(m.nr,'current_stops',return_value=changed):
            with self.assertRaises(bridge.BridgeError):m.web_exceptions(self.p,b,['k'])
        b['account_uid']='8'*24
        with patch.object(m.nr,'current_stops',return_value=self.stops):
            with self.assertRaises(bridge.BridgeError):m.web_exceptions(self.p,b,['k'])

    def test_changed_original_observation_revokes_web_grant(self):
        result,b=self.create_grant_fixture()
        grant=m.check_pointer(result['grant']);probe=m.check_pointer(grant['probe'])
        Path(probe['capture']['file']).write_text('{}')
        with patch.object(m.nr,'current_stops',return_value=self.stops):
            with self.assertRaises(bridge.BridgeError):m.web_exceptions(self.p,b,['k'])

    def test_original_restricted_note_stops_before_any_driver_action(self):
        calls=[]
        class Driver:
            def call(self,*args,**kwargs):calls.append(args)
        with self.assertRaises(bridge.BridgeError):
            bridge.prepare_target(None,{'manual_excluded_note_ids':['2'*24]},Driver(),{'payload':{'target':'2'*24}})
        self.assertEqual(calls,[])

    def test_two_verifications_keep_window_when_cdp_omits_it(self):
        _,b=self.create_grant_fixture()
        url=b['tabs']['detail']['last_url'];uid=b['account_uid']
        class Driver:
            def call(self,action,*args,**kwargs):
                return {'page':'A'*32,'url':url,'uid':uid,'visible':True}
        for _ in range(2):
            bridge.verify_tab(Driver(),b,'detail')
            bridge.save_binding(self.p,'manual',b)
            b=bridge.load_binding(self.p,'manual')
            self.assertEqual(b['tabs']['detail']['window_id'],22)
            with patch.object(m.nr,'current_stops',return_value=self.stops):
                m.web_exceptions(self.p,b,['k'])

    def test_observed_different_window_stops(self):
        _,b=self.create_grant_fixture();url=b['tabs']['detail']['last_url'];uid=b['account_uid']
        class Driver:
            def call(self,action,*args,**kwargs):
                return {'page':'A'*32,'url':url,'uid':uid,'windowId':99}
        with self.assertRaisesRegex(bridge.BridgeError,'TARGET_MISMATCH'):
            bridge.verify_tab(Driver(),b,'detail')
        self.assertEqual(b['tabs']['detail']['window_id'],22)

    def test_recheck_repairs_only_missing_window_and_reads_same_normal_note(self):
        result,b=self.create_grant_fixture();before=Path(result['grant']['file']).read_bytes()
        g=m.check_pointer(result['grant']);probe=m.check_pointer(g['probe']);capture=m.check_pointer(probe['capture'])
        b['tabs']['detail']['window_id']=None;bridge.save_binding(self.p,'manual',b)
        calls=[]
        class Driver:
            def __init__(self,*args):pass
            def close(self):pass
            def call(self,action,*args,**kwargs):
                calls.append(action)
                if action in {'metadata','identity'}:
                    return {'page':'A'*32,'url':capture['url'],'uid':capture['uid']}
                if action=='capture':return capture
                raise AssertionError(action)
        @contextlib.contextmanager
        def lock(*args):yield ['k']
        with patch.object(bridge,'Driver',Driver),patch.object(m.nr,'locked_stops',lock),patch.object(m.nr,'current_stops',return_value=self.stops):
            checked=m.recheck(self.p,'manual')
            with self.assertRaisesRegex(bridge.BridgeError,'MANUAL_RECHECK_ALREADY_ATTEMPTED'):
                m.recheck(self.p,'manual')
        self.assertEqual(calls,['metadata','identity','capture'])
        self.assertFalse(checked['collection_permitted'])
        self.assertEqual(before,Path(result['grant']['file']).read_bytes())
        self.assertIsNone(bridge.load_binding(self.p,'manual')['tabs']['detail']['window_id'])

    def test_recheck_does_not_repair_changed_account(self):
        _,b=self.create_grant_fixture();b['account_uid']='8'*24;bridge.save_binding(self.p,'manual',b)
        with self.assertRaisesRegex(bridge.BridgeError,'MANUAL_AUTHORITY_CHANGED'):
            m.recheck(self.p,'manual')

    def test_author_profile_is_identity_only_and_cannot_validate_research_access_alone(self):
        author_url='https://www.xiaohongshu.com/user/profile/'+'5'*24+'?xsec_token=synthetic&xsec_source=pc_user'
        self.url=author_url
        self.put('meta.json',{'tabId':11,'windowId':22,'url':author_url,'active':True})
        before=self.stop.read_bytes()
        result=self.run_inspect()
        self.assertFalse(result['collection_permitted'])
        self.assertNotIn('capture',self.calls)
        self.assertEqual(before,self.stop.read_bytes())
        self.assertEqual(m.proof_args(author_url),
                         {'stage':'author','expectedPath':'/user/profile/'+'5'*24,'backgroundText':True})
        with self.assertRaises(bridge.BridgeError):
            m.probe_note(result['inspection'],'https://www.xiaohongshu.com/explore/'+'3'*24)

    def test_author_profile_inspection_starts_bounded_search_and_attests_with_gemini_reader(self):
        author_url='https://www.xiaohongshu.com/user/profile/'+'5'*24
        self.url=author_url
        self.put('meta.json',{'tabId':11,'windowId':22,'url':author_url,'active':True})
        inspected=self.run_inspect()['inspection']
        note_url='https://www.xiaohongshu.com/explore/'+'6'*24
        case=self
        class Driver:
            url=author_url
            def __init__(self,*args):pass
            def close(self):pass
            def call(self,action,b=None,role=None,**kw):
                case.calls.append(action)
                if action=='metadata':return {'url':self.url,'page':'A'*32}
                if action=='identity':return {'uid':case.identity_uid,'url':self.url,'page':'A'*32}
                if action=='navigate':Driver.url=kw['url'];return {'url':Driver.url,'navigationSubmitted':True}
                if action=='capture':
                    if kw['stage']=='search':
                        return {'uid':case.uid,'url':self.url,'cards':[{'url':note_url,'text':'Synthetic card'}]}
                    return {'uid':case.uid,'url':self.url,'stage':'body','title':'Synthetic post',
                            'body':'Normal narrative','authors':[{'name':'Author','url':'https://www.xiaohongshu.com/user/profile/'+'8'*24}]}
                raise AssertionError(action)
        class Project:
            def __init__(self,*args):pass
            def task(self,*args):return {'kind':'native','payload':{'stage':'search'}}
            def close(self):pass
        with self.mocks(),patch.object(bridge,'Driver',Driver),patch.object(xp,'Project',Project),\
             patch.object(bridge,'observed_url',return_value='https://www.xiaohongshu.com/search_result?keyword=test'),\
             patch.object(m.time,'sleep'):
            searched=m.probe_search(inspected,'existing')
            self.assertFalse(searched['collection_permitted'])
            with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):
                m.probe_search(inspected,'existing')
            probed=m.probe_note(searched['inspection'],note_url)
            self.assertFalse(probed['collection_permitted'])
            review=self.put('gemini-review.json',{'reader':'gemini','reviewed':True,'body_read':True,
                'capture_sha256':probed['capture']['sha256'],'summary':'Read title, author and body.',
                'positions':['/title','/body','/authors'],'comments_read':False,'media_read':False})
            granted=m.attest(probed['probe'],review,'author-to-gemini')
        self.assertEqual(granted['state'],'ready');self.assertTrue(granted['stops_unchanged'])
        self.assertEqual(granted['scope'],'rendered_public_web_only')

    def test_author_profile_and_home_reject_source404_invalid_profile_and_access_walls(self):
        with self.assertRaisesRegex(bridge.BridgeError,'SOURCE404'):
            m.proof_args('https://www.xiaohongshu.com/explore?source=404')
        with self.assertRaises(bridge.BridgeError):
            m.proof_args('https://www.xiaohongshu.com/user/profile/not-hex')
        author_url='https://www.xiaohongshu.com/user/profile/'+'5'*24
        args=m.proof_args(author_url)
        b={'account_uid':self.uid}
        for bad in [{'loginWall':True},{'securityBlock':True},{'notFound':True},{'loading':True}]:
            with self.subTest(bad=bad),self.assertRaisesRegex(bridge.BridgeError,'CAPTURE_FAILED'):
                m.require_capture({'uid':self.uid,'url':author_url,**bad},b,args)

    def test_profile_qualified_note_url_normalizes_to_explore_note_id(self):
        a='https://www.xiaohongshu.com/user/profile/'+'5'*24+'/'+'3'*24+'?xsec_source=pc_user'
        b='https://www.xiaohongshu.com/explore/'+'3'*24
        self.assertEqual(m.note_id(a),'3'*24)
        self.assertEqual(m.proof_args(a),m.proof_args(b))
        self.assertNotEqual(m.proof_args(a),m.proof_args(b.replace('3'*24,'4'*24)))

    def test_review_accepts_codex_and_gemini_only(self):
        probe_path,review_path=self.complete_probe_fixture()
        probe=m.check_pointer(m.pointer(probe_path))
        review=json.loads(review_path.read_text())
        for reader in ('codex','gemini'):
            m.validate_review(dict(review,reader=reader),probe)
        for bad in ('','other',None,'CODEX'):
            with self.subTest(bad=bad),self.assertRaisesRegex(bridge.BridgeError,'REVIEW_REQUIRED'):
                m.validate_review(dict(review,reader=bad),probe)

    def test_inspect_active_reuses_identical_archived_observation_without_mutation(self):
        # Simulate a prior pre-attempt failure that already archived the same active-tab metadata.
        with self.mocks(),patch.object(m,'proof_args',side_effect=bridge.BridgeError('MANUAL_NOTE_OR_SEARCH_REQUIRED')):
            with self.assertRaisesRegex(bridge.BridgeError,'MANUAL_NOTE_OR_SEARCH_REQUIRED'):
                m.inspect_active(self.p,'old',self.user)
        obs=list((self.p/'bridge/manual-active-observations').glob('*.json'))
        self.assertEqual(len(obs),1);before_bytes=obs[0].read_bytes()
        with self.mocks():result=m.inspect_active(self.p,'old',self.user)
        self.assertFalse(result['collection_permitted'])
        self.assertEqual(obs[0].read_bytes(),before_bytes)
        # Subsequent call still fails closed on already-consumed attempt reservation.
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):
            m.inspect_active(self.p,'old',self.user)

if __name__=='__main__':unittest.main()
