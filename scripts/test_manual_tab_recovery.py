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
        self.error=None
        self.identity_uid=self.uid
        case=self
        class FakeDriver:
            def __init__(self,*args):pass
            def close(self):pass
            def call(self,action,b=None,role=None,**kw):
                case.calls.append(action)
                if action=='active_user_tab':return {'tabId':11,'windowId':22,'url':case.url,'active':True}
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

    def test_access_warning_url_is_never_a_normalization_exception(self):
        with self.assertRaises(bridge.BridgeError):
            m.proof_args('https://www.xiaohongshu.com/website-login/captcha?redirectPath='+self.url)

    def test_note_search_and_explore_routes_match_only_same_id(self):
        a='https://www.xiaohongshu.com/search_result/'+'3'*24+'?xsec_source='
        b='https://www.xiaohongshu.com/explore/'+'3'*24+'?xsec_source=pc_feed'
        self.assertEqual(m.proof_args(a),m.proof_args(b))
        self.assertNotEqual(m.proof_args(a),m.proof_args(b.replace('3'*24,'4'*24)))

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

if __name__=='__main__':unittest.main()
