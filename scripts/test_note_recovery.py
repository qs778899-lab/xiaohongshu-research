"""Synthetic 300031 recovery uses no browser/network and preserves all STOPs."""
import copy
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import xhs_note_recovery as nr
import xhs_bridge as bridge
import xhs_account_identity as identity
import xhs_native as native
import xhs_project as xp

UID='1'*24
FAILED='2'*24
NORMAL='3'*24
PROFILE='4'*64
CONTEXT='exact-context'
HOME='https://www.xiaohongshu.com/explore'
URL=HOME+'/'+NORMAL+'?xsec_token=actually-observed&xsec_source=pc_search'


class FakeDriver:
    def __init__(self,fail=None):self.actions=[];self.urls={};self.fail=fail
    def call(self,action,b=None,role=None,**fields):
        self.actions.append((action,role))
        if action=='profiles':return {'profiles':[{'contextId':CONTEXT,'extensionVersion':'1.0.26'}]}
        if action=='create':
            if self.fail=='create_unknown':raise bridge.BridgeError('COMMAND_OUTCOME_UNKNOWN')
            self.urls[role]=HOME;return {'page':'normal-page'}
        url=self.urls[role]
        if action=='metadata':return {'page':'normal-page','url':url}
        if action=='identity':
            if self.fail=='access':raise bridge.BridgeError('ACCESS_STOP')
            return {'url':url,'uid':UID if self.fail!='wrong_uid' else '9'*24,'page':'normal-page'}
        if action=='navigate':self.urls[role]=fields['url'];return {'navigationSubmitted':True}
        if action=='capture':return {'url':url,'uid':UID,'body':'' if self.fail=='empty' else 'Actual complete synthetic body','media':[]}
        raise AssertionError(action)
    def close(self):pass


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.guards=self.root/'guards';self.guards.mkdir(mode=0o700)
        self.guard_patch=patch.object(identity.legacy,'account_root',return_value=self.guards);self.guard_patch.start()
        self.sleep_patch=patch.object(bridge.time,'sleep');self.sleep_patch.start()
        self.p=xp.Project(self.root/'project',create=True)
        identity.associate(UID,PROFILE)
        self.keys=identity.related_keys(PROFILE,UID)
        t=datetime.now(timezone.utc)-timedelta(seconds=20)
        self.binding={'format':bridge.VERSION,'project':str(self.p.path.resolve()),'binding_name':'test',
                      'profile_label':'profile_alpha','context_id':CONTEXT,'account_uid':UID,'profile_guard_key':PROFILE,
                      'session':'this-project-session','extension_version':'1.0.26','runtime':{},'state':'blocked',
                      'last_error':{'code':'ACCESS_STOP','at':t.isoformat()},
                      'tabs':{'detail':{'page':'old-page','session':'this-project-detail','owned':True,
                                       'last_url':HOME+'/old','navigation_intent':{'url':HOME+'/'+FAILED,'at':(t-timedelta(seconds=1)).isoformat()}}}}
        bridge.save_binding(self.p.path,'test',self.binding)
        err='https://www.xiaohongshu.com/404?'+urlencode({'source':'/404/sec_xyz?'+urlencode({'redirectPath':HOME+'/'+FAILED}),
             'error_code':'300031','error_msg':'当前笔记暂时无法浏览','verifyMsg':'','uuid':'actual'})
        self.metadata=self.root/'error-metadata.json';self.metadata.write_text(json.dumps({'page':'old-page','url':err}))
        self.original=self.root/'original.json';self.original.write_text(json.dumps({'observed_at':(t+timedelta(seconds=1)).isoformat(),
            'metadata':{'page':'old-page','url':err,'source_file':str(self.metadata),'source_sha256':hashlib.sha256(self.metadata.read_bytes()).hexdigest()},
            'binding_snapshot':copy.deepcopy(self.binding)}))
        self.card=self.root/'card.json';self.card.write_text(json.dumps({'cards':[{'url':URL,'text':'observed card'}]}))
        self.target=self.root/'target.json';self.target.write_text(json.dumps({'scope':'one_normal_public_research_note_health_check_only',
            'context_id':CONTEXT,'account_uid':UID,'profile_label':'profile_alpha','target_note_id':NORMAL,'url':URL,
            'source_file':str(self.card),'source_sha256':hashlib.sha256(self.card.read_bytes()).hexdigest(),'source_position':'/cards/0',
            'user_request':{'text':'你自己打开浏览器继续','processed_at':(t+timedelta(seconds=2)).isoformat()}}))
        self.stops={}
        for index,key in enumerate(self.keys):
            f=identity.guard_directory(key)/'STOP.json';f.write_text(json.dumps({'at':(t+timedelta(milliseconds=index+1)).isoformat(),
                 'reason':nr.STOP_REASON,'channel':'bridge','credential_free':True}));self.stops[key]=f.read_bytes()
        manifest=self.root/'manifest.json';manifest.write_text(json.dumps({'units':[{'stage':stage,'target':FAILED,'next':'read','sources':[str(self.metadata)]} for stage in ['body','roots','replies']]}))
        self.tasks=native.import_manifest(self.p,manifest)['task_ids']
        self.driver=FakeDriver();self.review=self.root/'review.json'
    def tearDown(self):self.p.close();self.sleep_patch.stop();self.guard_patch.stop();self.tmp.cleanup()
    def run_probe(self):return nr.probe(self.p.path,'test',self.original,self.target,self.driver)
    def run_attest(self,probe=None):
        probe=probe or self.run_probe()
        self.review.write_text(json.dumps({'probe_id':probe['probe_id'],'normal_sha256':probe['normal_sha256'],
            'reviewed':True,'summary':'Read the actual synthetic body, checked identity and page readiness.','positions':['body and identity']}))
        return probe,nr.attest(self.p.path,'test',probe['probe_file'],self.review)
    def assert_stops_unchanged(self):
        for key,raw in self.stops.items():self.assertEqual((identity.guard_directory(key)/'STOP.json').read_bytes(),raw)
    def bridge_allowed(self):
        with bridge.lease(self.p.path,bridge.load_binding(self.p.path,'test')):pass
    def assert_blocked(self):
        with self.assertRaises(xp.legacy.ResearchError):self.bridge_allowed()
    def test_probe_is_bounded_preserves_stops_and_does_not_complete_tasks(self):
        before=[self.p.task(tid) for tid in self.tasks];result=self.run_probe()
        self.assertTrue(result['review_required']);self.assertFalse(result['collection_permitted'])
        self.assertEqual([a for a,_ in self.driver.actions].count('create'),1)
        self.assertEqual([a for a,_ in self.driver.actions].count('navigate'),1)
        self.assertEqual([a for a,_ in self.driver.actions].count('capture'),1)
        self.assertEqual(before,[self.p.task(tid) for tid in self.tasks]);self.assert_stops_unchanged();self.assert_blocked()
    def test_reviewed_grant_allows_only_bridge_and_quarantines_failed_note(self):
        _,result=self.run_attest();self.assertEqual(result['scope'],'rendered_public_web_only')
        self.bridge_allowed();self.assert_stops_unchanged()
        with self.assertRaises(xp.legacy.StopRun):
            with identity.account_lease(PROFILE,UID):pass
        for tid in self.tasks:
            t=self.p.task(tid);self.assertEqual(t['status'],'pending');self.assertEqual(t['payload']['bridge']['fault']['code'],'NOTE_UNAVAILABLE')
        b=bridge.load_binding(self.p.path,'test');self.assertEqual(b['tabs']['detail']['page'],'normal-page')
        self.assertEqual(b['quarantined_tabs'][0]['tab']['page'],'old-page')
    def test_attest_is_idempotent_without_duplicate_fault_or_tab_history(self):
        probe,result=self.run_attest();before=bridge.load_binding(self.p.path,'test')
        tasks=[self.p.task(tid)['payload'] for tid in self.tasks]
        duplicate=nr.attest(self.p.path,'test',probe['probe_file'],self.review)
        self.assertTrue(duplicate['duplicate']);self.assertEqual(before,bridge.load_binding(self.p.path,'test'))
        self.assertEqual(tasks,[self.p.task(tid)['payload'] for tid in self.tasks]);self.assert_stops_unchanged()
    def test_retry_after_commit_before_binding_save_finishes_same_grant(self):
        probe=self.run_probe();self.review.write_text(json.dumps({'probe_id':probe['probe_id'],'normal_sha256':probe['normal_sha256'],
             'reviewed':True,'summary':'Actually read body','positions':['body']}))
        real=bridge.save_binding
        with patch.object(bridge,'save_binding',side_effect=OSError('interrupted after commit')):
            with self.assertRaises(OSError):nr.attest(self.p.path,'test',probe['probe_file'],self.review)
        self.assert_blocked()
        result=nr.attest(self.p.path,'test',probe['probe_file'],self.review)
        self.assertFalse(result['duplicate']);self.bridge_allowed();self.assert_stops_unchanged()
        self.assertTrue(all(self.p.task(t)['payload']['bridge']['fault']['prior_fault'] is None for t in self.tasks))
    def test_success_without_actual_review_cannot_grant(self):
        result=self.run_probe();self.review.write_text(json.dumps({'probe_id':result['probe_id'],'normal_sha256':result['normal_sha256'],
            'reviewed':False,'summary':'not yet read','positions':['body']}))
        with self.assertRaisesRegex(bridge.BridgeError,'RECOVERY_ACTUAL_CODEX_REVIEW_REQUIRED'):
            nr.attest(self.p.path,'test',result['probe_file'],self.review)
        self.assert_blocked();self.assert_stops_unchanged()
    def test_user_pause_prevents_even_profile_query(self):
        (self.p.path/'bridge/PAUSE').write_text('paused')
        with self.assertRaisesRegex(bridge.BridgeError,'USER_PAUSED'):self.run_probe()
        self.assertEqual(self.driver.actions,[]);self.assert_stops_unchanged()
    def test_unknown_creation_consumes_probe_budget_no_second_tab(self):
        self.driver.fail='create_unknown'
        with self.assertRaisesRegex(bridge.BridgeError,'COMMAND_OUTCOME_UNKNOWN'):self.run_probe()
        with self.assertRaisesRegex(bridge.BridgeError,'RECOVERY_PROBE_ALREADY_ATTEMPTED'):self.run_probe()
        self.assertEqual([a for a,_ in self.driver.actions].count('create'),1);self.assert_stops_unchanged()
    def test_account_mismatch_or_missing_body_never_grants(self):
        self.driver.fail='wrong_uid'
        with self.assertRaisesRegex(bridge.BridgeError,'ACCOUNT_MISMATCH'):self.run_probe()
        self.assertNotIn('navigate',[a for a,_ in self.driver.actions]);self.assert_stops_unchanged();self.assert_blocked()
    def test_empty_actual_body_remains_blocked(self):
        self.driver.fail='empty'
        with self.assertRaisesRegex(bridge.BridgeError,'RECOVERY_ACTUAL_BODY_REQUIRED'):self.run_probe()
        self.assert_stops_unchanged();self.assert_blocked()
    def test_new_access_warning_writes_new_stop_and_no_grant(self):
        self.driver.fail='access'
        with self.assertRaises(xp.legacy.StopRun):self.run_probe()
        self.assert_blocked()
        for key,raw in self.stops.items():self.assertNotEqual((identity.guard_directory(key)/'STOP.json').read_bytes(),raw)
        # The old STOP evidence remains byte-identical in the immutable archive.
        for raw in self.stops.values():self.assertEqual((self.p.path/'native-sources'/hashlib.sha256(raw).hexdigest()).read_bytes(),raw)
    def test_changed_new_deleted_or_added_associated_stop_invalidates_grant(self):
        self.run_attest();self.bridge_allowed();key=self.keys[0];path=identity.guard_directory(key)/'STOP.json';raw=path.read_bytes()
        path.write_bytes(raw+b' ');self.assert_blocked();path.write_bytes(raw);self.bridge_allowed()
        path.unlink();self.assert_blocked();path.write_bytes(raw);self.bridge_allowed()
        identity.associate(UID,'8'*64);self.assert_blocked()
    def test_each_original_or_recovery_proof_change_invalidates_grant(self):
        probe,_=self.run_attest();record=json.loads(Path(probe['probe_file']).read_text())
        paths=[self.original,self.metadata,self.target,self.card,self.review,Path(probe['normal_file']),Path(probe['probe_file'])]
        for path in paths:
            with self.subTest(path=path.name):
                raw=path.read_bytes();path.write_bytes(raw+b' ');self.assert_blocked();path.write_bytes(raw);self.bridge_allowed()
    def test_missing_grant_hash_cannot_disable_evidence_binding(self):
        self.run_attest();b=bridge.load_binding(self.p.path,'test')
        b['note_unavailable_recovery'].pop('sha256');bridge.save_binding(self.p.path,'test',b)
        self.assert_blocked();self.assert_stops_unchanged()
    def test_old_other_project_stop_is_not_excused(self):
        self.p.set_meta('stop',{'reason':'unresolved previous API access limit'});self.p.db.commit()
        with self.assertRaises(xp.legacy.StopRun):self.run_probe()
        self.assertEqual(self.driver.actions,[]);self.assert_stops_unchanged()
    def test_forged_media_exception_cannot_excuse_generic_bridge_stop(self):
        exceptions={key:hashlib.sha256(raw).hexdigest() for key,raw in self.stops.items()}
        with self.assertRaises(xp.legacy.StopRun):
            with identity.account_lease(PROFILE,UID,web_stop_exceptions=exceptions):pass
    def test_changed_original_error_or_unobserved_target_fails_before_network(self):
        target=json.loads(self.target.read_text());target['url']=target['url'].replace('actually-observed','invented')
        self.target.write_text(json.dumps(target))
        with self.assertRaisesRegex(bridge.BridgeError,'RECOVERY_NORMAL_URL_NOT_ACTUALLY_OBSERVED'):self.run_probe()
        self.assertEqual(self.driver.actions,[]);self.assert_stops_unchanged()
    def test_older_normal_observation_cannot_attest(self):
        probe=self.run_probe();record=json.loads(Path(probe['probe_file']).read_text());normal=json.loads(Path(probe['normal_file']).read_text())
        normal['observed_at']='2000-01-01T00:00:00+00:00'
        with self.assertRaisesRegex(bridge.BridgeError,'RECOVERY_NORMAL_OBSERVATION_NOT_NEWER'):
            nr.validate_normal(self.binding,json.loads(self.target.read_text()),normal,record['stops'])

if __name__=='__main__':unittest.main()
