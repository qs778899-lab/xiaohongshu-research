"""Synthetic same-target recovery: no browser, requests, cookies or live account."""
import copy
from datetime import datetime,timezone,timedelta
import hashlib,json
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch

import xhs_current_page_recovery as current
import xhs_note_recovery as nr
import xhs_bridge as bridge
import xhs_project as xp
import xhs_native as native
import xhs_account_identity as identity
import test_note_recovery as fixture
from test_bridge_binding_fork import ForkDriver

UID=fixture.UID;NOTE='5'*24;PROFILE=fixture.PROFILE;URL='https://www.xiaohongshu.com/explore/'+NOTE+'?xsec_token=observed'

class Fake:
    def __init__(self,fail=None):self.calls=[];self.fail=fail
    def call(self,action,b,role,**kwargs):
        self.calls.append(action)
        assert role=='detail'
        if action not in {'metadata','identity','capture'}:raise AssertionError('Unexpected action '+action)
        if self.fail==action:raise bridge.BridgeError('ACCESS_STOP')
        if self.fail=='timeout' and action=='identity':raise TimeoutError('synthetic timeout')
        if self.fail=='interrupt' and action=='identity':raise KeyboardInterrupt()
        url=URL if self.fail!='target' else 'https://www.xiaohongshu.com/explore/'+'9'*24
        if self.fail=='home':url='https://www.xiaohongshu.com/explore'
        page=b['tabs']['detail']['page']
        if action=='metadata':return {'url':url,'page':page}
        if action=='identity':return {'url':url,'page':page,'uid':UID if self.fail!='uid' else '9'*24}
        return {'url':url,'uid':UID,'title':'Actual synthetic patient note','body':'' if self.fail=='empty' else 'Synthetic complete current body','media':[]}
    def close(self):pass

class Tests(unittest.TestCase):
    FORK=False
    def setUp(self):
        # Actual old grant graph: original 300031, guarded normal capture and read receipt.
        self.old=fixture.RecoveryTests(methodName='runTest');self.old.setUp();self.old.run_attest()
        self.root=self.old.root;self.guards=self.old.guards;self.p=self.old.p;self.keys=self.old.keys
        parent=bridge.load_binding(self.p.path,'test');parent['authorization']='existing explicit public research authorization'
        bridge.save_binding(self.p.path,'test',parent)
        self.name='isolated' if self.FORK else 'test'
        if self.FORK:
            bridge.fork_binding(self.p.path,'test',self.name,'current-test','Open independent public research tabs',ForkDriver())
        self.binding=bridge.load_binding(self.p.path,self.name)
        now=datetime.now(timezone.utc)-timedelta(seconds=5)
        self.binding.update(state='blocked',last_error={'code':'ACCESS_STOP','at':now.isoformat()})
        slot=self.binding['tabs']['detail'];slot['navigation_intent']={'url':URL,'at':(now-timedelta(seconds=1)).isoformat()}
        if not self.FORK:
            slot['page']='A'*32
            self.binding['tabs']['search']={'page':'B'*32,'session':self.binding['session']+'-search','owned':True,'last_url':'https://www.xiaohongshu.com/search_result?keyword=topic'}
        bridge.save_binding(self.p.path,self.name,self.binding)
        self.stops={}
        for i,key in enumerate(self.keys):
            f=identity.guard_directory(key)/'STOP.json';f.write_text(json.dumps({'at':(now+timedelta(milliseconds=i+1)).isoformat(),'reason':nr.STOP_REASON,'channel':'bridge','credential_free':True}));self.stops[key]=f.read_bytes()
        self.confirm=self.root/'confirmation.json';self.confirm.write_text(json.dumps({'target_note_id':NOTE,'user_statement':'正文和评论均正常','processed_at':(now+timedelta(seconds=2)).isoformat()}))
        source=self.root/'current-source.json';source.write_text(json.dumps({'cards':[{'url':URL,'text':'Actual synthetic patient note'}]}));manifest=self.root/'current-manifest.json';manifest.write_text(json.dumps({'units':[{'stage':'body','target':NOTE,'sources':[str(source)],'next':'read'}]}));self.task=native.import_manifest(self.p,manifest)['task_ids'][0]
        self.driver=Fake();self.review=self.root/'current-review.json'
    def tearDown(self):self.old.tearDown()
    def probe(self,driver=None):return current.probe(self.p.path,self.name,self.task,self.confirm,'continue authorized public research',driver or self.driver)
    def attest(self,probe=None):
        probe=probe or self.probe();self.review.write_text(json.dumps({'probe_id':probe['probe_id'],'normal_sha256':probe['normal_sha256'],'reader':'codex','reviewed':True,'body_read':True,'summary':'Actually read the current exact note body; verified account and target','positions':['/capture/title','/capture/body']}));result=current.attest(self.p.path,self.name,probe['probe_file'],self.review);return probe,result
    def allowed(self):
        with bridge.lease(self.p.path,bridge.load_binding(self.p.path,self.name)):pass
    def blocked(self):
        with self.assertRaises(xp.legacy.ResearchError):self.allowed()
    def unchanged(self):
        for key,raw in self.stops.items():self.assertEqual((identity.guard_directory(key)/'STOP.json').read_bytes(),raw)
    def failed_original(self,fail='home'):
        with self.assertRaises(BaseException):self.probe(Fake(fail))
        failure=next((self.p.path/'bridge/current-page-recoveries').glob('*/failure.json'))
        return failure.parent
    def manual_authorization(self,directory):
        ref=lambda name:{'file':str(directory/name),'sha256':hashlib.sha256((directory/name).read_bytes()).hexdigest()}
        value={'kind':current.MANUAL_KIND,'project':str(self.p.path.resolve()),'binding_name':self.name,
               'target_note_id':NOTE,'original_attempt':ref('attempt.json'),'original_failure':ref('failure.json'),
               'processed_at':datetime.now(timezone.utc).isoformat(),'user_statement':'已恢复原帖，正文评论正常，允许再次复核'}
        path=self.root/'manual-restoration.json';path.write_text(json.dumps(value));return path
    def manual_probe(self,file,driver=None):
        return current.probe(self.p.path,self.name,self.task,self.confirm,'continue authorized public research',
                             driver or self.driver,manual_restoration=file)
    def test_bounded_probe_no_navigation_no_collection_grant(self):
        before=bridge.load_binding(self.p.path,self.name);r=self.probe();self.assertEqual(self.driver.calls,['metadata','identity','capture']);self.assertFalse(r['collection_permitted']);self.assertEqual(before,bridge.load_binding(self.p.path,self.name));self.unchanged();self.blocked();self.assertEqual(self.p.task(self.task)['status'],'pending')
    def test_probe_one_shot(self):
        self.probe()
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.probe()
        self.assertEqual(len(self.driver.calls),3)
    def test_access_timeout_and_interrupt_consumed(self):
        for fail in ['metadata','identity','capture','timeout','interrupt']:
            with self.subTest(fail=fail):
                # One independent synthetic context per failure.
                test=Tests();test.setUp()
                try:
                    with self.assertRaises(BaseException):test.probe(Fake(fail))
                    with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):test.probe()
                    test.blocked();test.unchanged()
                finally:test.tearDown()
    def test_wrong_uid_wrong_target_and_empty_fail(self):
        for fail in ['uid','target','empty']:
            test=Tests();test.setUp()
            try:
                with self.assertRaises(bridge.BridgeError):test.probe(Fake(fail))
                test.blocked();test.unchanged()
            finally:test.tearDown()
    def test_attest_bridge_only_preserves_old_authority_and_stops(self):
        old=bridge.load_binding(self.p.path,self.name);_,r=self.attest();self.allowed();self.unchanged();new=bridge.load_binding(self.p.path,self.name);self.assertEqual(new.get('note_unavailable_recovery'),old.get('note_unavailable_recovery'));self.assertEqual(new.get('web_parent_authorization'),old.get('web_parent_authorization'));self.assertEqual(new['tabs'],old['tabs']);self.assertTrue(r['api_and_cdn_still_stopped'])
        with self.assertRaises(xp.legacy.StopRun):
            with identity.account_lease(PROFILE,UID):pass
    def test_attest_idempotent_no_more_browser_calls(self):
        p,_=self.attest();before=bridge.load_binding(self.p.path,self.name);r=current.attest(self.p.path,self.name,p['probe_file'],self.review);self.assertTrue(r['duplicate']);self.assertEqual(before,bridge.load_binding(self.p.path,self.name));self.assertEqual(len(self.driver.calls),3)
    def test_review_and_capture_tamper_invalidate(self):
        p,_=self.attest();self.review.write_text('{}');self.blocked()
    def test_capture_tamper_invalidate(self):
        p,_=self.attest();Path(p['normal_file']).write_text('{}');self.blocked()
    def test_missing_grant_pointer_hash_cannot_authorize(self):
        self.attest();b=bridge.load_binding(self.p.path,self.name);b[current.POINTER].pop('sha256')
        bridge.save_binding(self.p.path,self.name,b);self.blocked()
    def test_old_authority_file_tamper_invalidates(self):
        self.attest();Path(self.binding.get('note_unavailable_recovery',self.binding.get('web_parent_authorization'))['file']).write_text('{}');self.blocked()
    def test_authority_binding_changes_invalidate(self):
        self.attest();original=bridge.load_binding(self.p.path,self.name)
        changes=[('account_uid','9'*24),('session','different'),('context_id','different'),('profile_label','different'),('runtime',{'different':True})]
        for key,value in changes:
            b=copy.deepcopy(original);b[key]=value;bridge.save_binding(self.p.path,self.name,b);self.blocked()
        for role in ['detail','search']:
            b=copy.deepcopy(original);b['tabs'][role]['page']='C'*32;bridge.save_binding(self.p.path,self.name,b);self.blocked()
        bridge.save_binding(self.p.path,self.name,original)
    def test_legitimate_url_progress_does_not_invalidate(self):
        self.attest();b=bridge.load_binding(self.p.path,self.name);b['tabs']['detail']['last_url']='https://www.xiaohongshu.com/explore/'+'8'*24;b['tabs']['detail'].pop('navigation_intent');b['tabs']['detail']['verified_at']=datetime.now(timezone.utc).isoformat();bridge.save_binding(self.p.path,self.name,b);self.allowed()
    def test_new_stop_invalidates(self):
        self.attest();key=self.keys[0];f=identity.guard_directory(key)/'STOP.json';d=json.loads(f.read_text());d['at']=datetime.now(timezone.utc).isoformat();f.write_text(json.dumps(d));self.blocked()
    def test_new_account_alias_invalidates(self):
        self.attest();identity.associate(UID,'6'*64);self.blocked()
    def test_confirmation_must_follow_stop(self):
        d=json.loads(self.confirm.read_text());d['processed_at']='2020-01-01T00:00:00+00:00';self.confirm.write_text(json.dumps(d))
        with self.assertRaisesRegex(bridge.BridgeError,'CONFIRMATION_NOT_NEWER'):self.probe()
        self.assertEqual(self.driver.calls,[])
    def test_negative_hearsay_and_question_confirmation_rejected_before_probe(self):
        original=json.loads(self.confirm.read_text())
        for text in ['正文和评论不正常','正文和评论均不正常','正文和评论没有正常','听说正文和评论均正常','他说正文和评论均正常','正文和评论均正常吗','正文和评论均正常？','正文和评论均正常?','正文正常但评论不正常','正文和评论都不是正常的']:
            value=dict(original,user_statement=text);self.confirm.write_text(json.dumps(value))
            with self.subTest(text=text),self.assertRaisesRegex(bridge.BridgeError,'NORMAL_PAGE_CONFIRMATION'):self.probe()
        self.assertEqual(self.driver.calls,[])
        self.confirm.write_text(json.dumps(original));self.probe()

    def test_non_body_or_other_target_rejected(self):
        t=self.p.task(self.task);payload=t['payload'];payload['stage']='roots';self.p.db.execute('update tasks set payload=? where id=?',(json.dumps(payload),self.task));self.p.db.commit()
        with self.assertRaisesRegex(bridge.BridgeError,'FAILED_BODY_TASK'):self.probe()
        self.assertEqual(self.driver.calls,[])
    def test_copied_grant_cannot_authorize_other_binding(self):
        self.attest();b=bridge.load_binding(self.p.path,self.name);b['binding_name']='copy';bridge.save_binding(self.p.path,'copy',b)
        with self.assertRaises(xp.legacy.ResearchError):
            with bridge.lease(self.p.path,b):pass
    def test_historical_stop_ref_uses_archive_not_overwritten_active_file(self):
        old_stops=json.loads(Path(bridge.load_binding(self.p.path,'test')['note_unavailable_recovery']['file']).read_text())
        record=json.loads(Path(old_stops['probe_file']).read_text())
        for key,stop in record['stops'].items():
            self.assertNotEqual(hashlib.sha256(self.stops[key]).hexdigest(),stop['sha256'])
            self.assertEqual(hashlib.sha256((self.p.path/stop['archive']).read_bytes()).hexdigest(),stop['sha256'])
        self.attest();self.allowed();self.unchanged()
    def test_kind_scope_marker_alone_is_not_prior_authority(self):
        b=bridge.load_binding(self.p.path,self.name)
        if self.FORK:
            b=bridge.load_binding(self.p.path,'test')
        f=self.root/'invalid-grant.json';f.write_text(json.dumps({'kind':nr.KIND,'scope':'rendered_public_web_only','historical':True}))
        b['note_unavailable_recovery']={'file':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest()}
        bridge.save_binding(self.p.path,b['binding_name'],b)
        with self.assertRaises(bridge.BridgeError):self.probe()
        self.assertEqual(self.driver.calls,[])
    def test_attest_before_read_does_not_authorize(self):
        p=self.probe();self.review.write_text(json.dumps({'probe_id':p['probe_id'],'normal_sha256':p['normal_sha256'],'reviewed':True,'summary':'not a reading','positions':['body']}))
        with self.assertRaisesRegex(bridge.BridgeError,'ACTUAL_CODEX_BODY_REVIEW'):current.attest(self.p.path,self.name,p['probe_file'],self.review)
        self.blocked()
    def test_failed_original_saves_metadata_before_target_validation(self):
        directory=self.failed_original();failure=json.loads((directory/'failure.json').read_text())
        self.assertEqual(failure['failed_stage'],'metadata');self.assertEqual(set(failure['observations']),{'metadata'})
        self.assertEqual(nr.check_ref(self.p,failure['observations']['metadata'])['result']['url'],'https://www.xiaohongshu.com/explore')
        self.unchanged()
    def test_failed_capture_keeps_both_previous_stage_observations(self):
        directory=self.failed_original('capture');failure=json.loads((directory/'failure.json').read_text())
        self.assertEqual(failure['failed_stage'],'capture');self.assertEqual(set(failure['observations']),{'metadata','identity'})
        for ref in failure['observations'].values():nr.check_ref(self.p,ref)
    def test_manual_restoration_one_child_attestable_preserves_original(self):
        directory=self.failed_original();original={x.name:x.read_bytes() for x in directory.iterdir() if x.is_file()}
        file=self.manual_authorization(directory)
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.probe()
        result=self.manual_probe(file);self.assertEqual(self.driver.calls,['metadata','identity','capture'])
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.manual_probe(file)
        self.assertEqual(len(self.driver.calls),3)
        for name,raw in original.items():self.assertEqual((directory/name).read_bytes(),raw)
        self.attest(result);self.allowed();self.unchanged()
        duplicate=current.attest(self.p.path,self.name,result['probe_file'],self.review);self.assertTrue(duplicate['duplicate'])
    def test_manual_failed_preflight_is_single_use_and_never_identity(self):
        directory=self.failed_original();file=self.manual_authorization(directory);driver=Fake('home')
        with self.assertRaisesRegex(bridge.BridgeError,'EXACT_NOTE'):self.manual_probe(file,driver)
        self.assertEqual(driver.calls,['metadata'])
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.manual_probe(file,driver)
        self.assertEqual(driver.calls,['metadata']);self.blocked();self.unchanged()
    def test_changed_original_failure_hash_cannot_create_second_preflight(self):
        directory=self.failed_original();file=self.manual_authorization(directory);driver=Fake('home')
        with self.assertRaises(bridge.BridgeError):self.manual_probe(file,driver)
        failure=directory/'failure.json';failure.write_bytes(failure.read_bytes()+b' ')
        value=json.loads(file.read_text());value['original_failure']['sha256']=hashlib.sha256(failure.read_bytes()).hexdigest()
        value['processed_at']=datetime.now(timezone.utc).isoformat();file.write_text(json.dumps(value))
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.manual_probe(file,driver)
        self.assertEqual(driver.calls,['metadata']);self.blocked();self.unchanged()
    def test_manual_identity_timeout_or_capture_failure_consumes_child(self):
        for fail in ['identity','timeout','interrupt','capture','uid','target']:
            test=Tests();test.setUp()
            try:
                file=test.manual_authorization(test.failed_original());driver=Fake(fail)
                with self.subTest(fail=fail),self.assertRaises(BaseException):test.manual_probe(file,driver)
                with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):test.manual_probe(file)
                test.blocked();test.unchanged()
            finally:test.tearDown()
    def test_manual_negative_or_stale_confirmation_rejected_without_driver(self):
        directory=self.failed_original();file=self.manual_authorization(directory);original=json.loads(file.read_text())
        for text in ['已恢复原帖，正文评论不正常，允许再次复核','听说已恢复原帖，正文评论正常，允许再次复核','已恢复原帖，正文评论正常，允许再次复核？']:
            file.write_text(json.dumps(dict(original,user_statement=text)))
            with self.assertRaisesRegex(bridge.BridgeError,'EXPLICIT_USER'):self.manual_probe(file)
        file.write_text(json.dumps(dict(original,processed_at='2020-01-01T00:00:00+00:00')))
        with self.assertRaisesRegex(bridge.BridgeError,'CONFIRMATION_NOT_NEWER'):self.manual_probe(file)
        self.assertEqual(self.driver.calls,[])
    def test_manual_wrong_original_sha_target_or_binding_rejected(self):
        directory=self.failed_original();file=self.manual_authorization(directory);original=json.loads(file.read_text())
        for field,value in [('target_note_id','9'*24),('binding_name','elsewhere'),('project','/elsewhere')]:
            file.write_text(json.dumps(dict(original,**{field:value})))
            with self.assertRaisesRegex(bridge.BridgeError,'MANUAL_SCOPE'):self.manual_probe(file)
        for field in ('original_attempt','original_failure'):
            altered=copy.deepcopy(original);altered[field]['sha256']='0'*64;file.write_text(json.dumps(altered))
            with self.assertRaisesRegex(bridge.BridgeError,'EVIDENCE_CHANGED'):self.manual_probe(file)
        self.assertEqual(self.driver.calls,[])
    def test_manual_new_stop_rejected_before_driver(self):
        directory=self.failed_original();file=self.manual_authorization(directory)
        path=identity.guard_directory(self.keys[0])/'STOP.json';value=json.loads(path.read_text())
        value['at']=datetime.now(timezone.utc).isoformat();path.write_text(json.dumps(value))
        with self.assertRaises(bridge.BridgeError):self.manual_probe(file)
        self.assertEqual(self.driver.calls,[])
    def test_manual_sources_or_stage_tampering_invalidates_attestation(self):
        directory=self.failed_original();file=self.manual_authorization(directory);result=self.manual_probe(file)
        self.attest(result);self.allowed()
        path=Path(result['normal_file']).with_name('metadata.json');path.write_bytes(path.read_bytes()+b' ');self.blocked()
    def test_manual_confirmation_tampering_invalidates_lease(self):
        directory=self.failed_original();file=self.manual_authorization(directory);result=self.manual_probe(file)
        self.attest(result);self.allowed();file.write_bytes(file.read_bytes()+b' ');self.blocked()
    def test_manual_changing_confirmation_does_not_create_second_child(self):
        directory=self.failed_original();file=self.manual_authorization(directory);self.manual_probe(file)
        value=json.loads(file.read_text());value['processed_at']=datetime.now(timezone.utc).isoformat();file.write_text(json.dumps(value))
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.manual_probe(file)
        self.assertEqual(self.driver.calls,['metadata','identity','capture'])
    def test_manual_original_failure_tampering_invalidates_lease(self):
        directory=self.failed_original();file=self.manual_authorization(directory);result=self.manual_probe(file)
        self.attest(result);self.allowed();path=directory/'failure.json';path.write_bytes(path.read_bytes()+b' ');self.blocked()

class ForkTests(Tests):
    FORK=True
    def test_current_parent_authority_projection_rechecked_each_lease(self):
        self.attest();original=bridge.load_binding(self.p.path,'test')
        for field,value in [('authorization','changed'),('context_id','other'),('runtime',{'other':True}),('session','changed')]:
            changed=copy.deepcopy(original);changed[field]=value;bridge.save_binding(self.p.path,'test',changed)
            self.blocked();bridge.save_binding(self.p.path,'test',original);self.allowed()
    def test_current_parent_grant_pointer_rechecked_each_lease(self):
        self.attest();b=bridge.load_binding(self.p.path,'test');b['note_unavailable_recovery']['file']+='-other'
        bridge.save_binding(self.p.path,'test',b);self.blocked()
    def test_current_name_index_rechecked_each_lease(self):
        self.attest();index=nr.fork_path(self.p.path,'names',self.name);index.write_text('{}');self.blocked()
    def test_current_completed_home_proof_rechecked_each_lease(self):
        self.attest();pointer=self.binding['web_parent_authorization'];ready=Path(pointer['file']).with_suffix('.ready.json')
        ready.write_text('{}');self.blocked()
    def test_parent_normal_navigation_and_page_fault_remain_mutable(self):
        self.attest();b=bridge.load_binding(self.p.path,'test')
        b['tabs']['detail']['last_url']='https://www.xiaohongshu.com/explore/'+'8'*24
        b['tabs']['detail']['page']='E'*32;b['last_success']={'at':xp.legacy.now()};b['state']='blocked'
        b['last_error']={'code':'SCREENSHOT_TARGET_NOT_VISIBLE','at':xp.legacy.now()}
        bridge.save_binding(self.p.path,'test',b);self.allowed()
    def test_invalid_parent_authority_rejected_before_probe_calls(self):
        b=bridge.load_binding(self.p.path,'test');b['authorization']='changed';bridge.save_binding(self.p.path,'test',b)
        with self.assertRaisesRegex(bridge.BridgeError,'PARENT_AUTHORITY'):self.probe()
        self.assertEqual(self.driver.calls,[])

if __name__=='__main__':unittest.main()
