"""Synthetic same-search recovery, no real browser/account or network."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import unittest

import xhs_bridge as bridge
import xhs_current_search_recovery as search
import xhs_current_page_recovery as current
import xhs_native as native
import xhs_account_identity as identity
import xhs_project as xp
import test_current_page_recovery as bodyfixture

QUERY='示例机构 测试乙 主题'
URL='https://www.xiaohongshu.com/search_result?keyword=%E7%A4%BA%E4%BE%8B%E6%9C%BA%E6%9E%84%20%E6%B5%8B%E8%AF%95%E4%B9%99%20%E4%B8%BB%E9%A2%98&type=51'

class Fake:
    def __init__(self, fail=None): self.fail=fail; self.calls=[]
    def call(self,action,b,role,**kwargs):
        self.calls.append((action,role,kwargs))
        assert role=='search' and action in {'metadata','identity','capture'}
        if self.fail==action: raise bridge.BridgeError('ACCESS_STOP')
        if self.fail=='timeout' and action=='identity':raise TimeoutError('synthetic')
        if self.fail=='interrupt' and action=='capture':raise KeyboardInterrupt()
        url=URL if self.fail!='query' else URL.replace('type=51','type=53')
        page=b['tabs']['search']['page'] if self.fail!='page' else 'F'*32
        if action=='metadata':return {'url':url,'page':page}
        if action=='identity':return {'url':url,'page':page,'uid':b['account_uid'] if self.fail!='uid' else 'f'*24}
        assert kwargs==dict(stage='search',query=QUERY,expectedPath='/search_result')
        r={'url':url,'uid':b['account_uid'],'stage':'search','cards':[{'text':'真实候选标题','url':'https://www.xiaohongshu.com/explore/'+'4'*24+'?xsec_token=observed'}],
           'selected':[],'sortControls':[],'visible':True,'position':{'scrollHeight':1500,'viewportHeight':800,'loading':False}}
        if self.fail=='empty':r['cards']=[]
        if self.fail=='warning':r['securityBlock']=True
        if self.fail=='geometry':r['position']['viewportHeight']=0
        if self.fail=='loading':r['position']['loading']=True
        if self.fail=='stage':r['stage']='body'
        return r
    def close(self):pass

class Tests(unittest.TestCase):
    FORK=False
    def setUp(self):
        self.base=(bodyfixture.ForkTests() if self.FORK else bodyfixture.Tests());self.base.setUp()
        self.p=self.base.p;self.root=self.base.root;self.name=self.base.name;self.keys=self.base.keys
        self.b=bridge.load_binding(self.p.path,self.name)
        now=datetime.now(timezone.utc)-timedelta(seconds=5)
        self.b['last_error']={'code':'ACCESS_STOP','at':now.isoformat()}
        self.b['tabs']['search']['navigation_intent']={'url':URL,'at':(now-timedelta(seconds=1)).isoformat()}
        bridge.save_binding(self.p.path,self.name,self.b)
        self.stops={}
        for key in self.keys:
            file=identity.guard_directory(key)/'STOP.json';old=json.loads(file.read_text());old['at']=(now+timedelta(milliseconds=1)).isoformat();file.write_text(json.dumps(old));self.stops[key]=file.read_bytes()
        source=self.root/'search-source.json';source.write_text(json.dumps({'query':QUERY}))
        manifest=self.root/'search-manifest.json';manifest.write_text(json.dumps({'units':[{'stage':'search','target':QUERY,'sort':'popular','sources':[str(source)],'next':'read'}]}))
        self.task=native.import_manifest(self.p,manifest)['task_ids'][0]
        error=self.p.path/'bridge/last-driver-error.json';error.write_text(json.dumps({'role':'search','action':'identity','error':{'code':'ACCESS_STOP'},'at':now.isoformat()}))
        handoff=self.p.path/'bridge/handoff.json';handoff.write_text(json.dumps({'event':'blocked','reason':'ACCESS_STOP','task_id':self.task,'processed_units':0}))
        self.confirm=self.root/'search-confirm.json';self.confirm.write_text(json.dumps({'user_confirmation':'页面均正常','known_failed_task':self.task,'binding':self.name,'profile':self.b['profile_label'],'confirmation_reference':'actual user answer','processed_at':(now+timedelta(seconds=2)).isoformat()}))
        self.driver=Fake();self.review=self.root/'search-review.json'
    def tearDown(self):self.base.tearDown()
    def probe(self,driver=None):return search.probe(self.p.path,self.name,self.task,self.confirm,'continue existing research',driver or self.driver)
    def attest(self):
        p=self.probe();self.review.write_text(json.dumps({'probe_id':p['probe_id'],'normal_sha256':p['normal_sha256'],'reader':'codex','reviewed':True,'query_read':True,'search_cards_read':True,'body_read':False,'sort_verified':False,'pagination_verified':False,'summary':'Actually checked same query/account and read observed candidate titles; sorting still unverified.','positions':['/capture/cards','/capture/url']}));return p,search.attest(self.p.path,self.name,p['probe_file'],self.review)
    def allowed(self):
        with bridge.lease(self.p.path,bridge.load_binding(self.p.path,self.name)):pass
    def blocked(self):
        with self.assertRaises(xp.legacy.ResearchError):self.allowed()
    def unchanged(self):
        for key,raw in self.stops.items():self.assertEqual((identity.guard_directory(key)/'STOP.json').read_bytes(),raw)
    def test_only_three_original_role_reads_and_no_probe_grant(self):
        before=bridge.load_binding(self.p.path,self.name);r=self.probe();self.assertEqual([x[:2] for x in self.driver.calls],[('metadata','search'),('identity','search'),('capture','search')]);self.assertEqual(before,bridge.load_binding(self.p.path,self.name));self.assertFalse(r['collection_permitted']);self.assertFalse(r['sort_verified']);self.unchanged();self.blocked()
    def test_probe_cannot_repeat_or_reset_with_new_confirmation(self):
        self.probe();d=json.loads(self.confirm.read_text());d['processed_at']=datetime.now(timezone.utc).isoformat();self.confirm.write_text(json.dumps(d))
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.probe()
        self.assertEqual(len(self.driver.calls),3)
    def test_failures_are_consumed_and_keep_stops(self):
        for fail in ['metadata','identity','capture','query','page','uid','empty','warning','geometry','loading','stage','timeout','interrupt']:
            t=Tests();t.setUp()
            try:
                with self.subTest(fail=fail):
                    with self.assertRaises(BaseException):t.probe(Fake(fail))
                    with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):t.probe()
                    t.unchanged();t.blocked()
                    failure=next((t.p.path/'bridge/current-page-recoveries').glob('*/failure.json'));self.assertTrue(json.loads(failure.read_text())['attempt_consumed'])
            finally:t.tearDown()
    def test_failure_role_action_time_task_must_match_before_reads(self):
        file=self.p.path/'bridge/last-driver-error.json';orig=json.loads(file.read_text())
        for change in [{'role':'detail'},{'action':'capture'},{'at':'2020-01-01T00:00:00+00:00'},{'error':{'code':'TARGET_MISSING'}}]:
            file.write_text(json.dumps(dict(orig,**change)))
            with self.assertRaises(bridge.BridgeError):self.probe()
        file.write_text(json.dumps(orig));file=self.p.path/'bridge/handoff.json';d=json.loads(file.read_text());d['task_id']=self.base.task;file.write_text(json.dumps(d))
        with self.assertRaisesRegex(bridge.BridgeError,'FAILED_HANDOFF'):self.probe()
        self.assertEqual(self.driver.calls,[])
    def test_bad_confirmations_rejected_before_reads(self):
        d=json.loads(self.confirm.read_text())
        for text in ['不正常','页面均正常吗','听说页面均正常','登录正常但搜索不正常']:
            self.confirm.write_text(json.dumps(dict(d,user_confirmation=text)))
            with self.assertRaisesRegex(bridge.BridgeError,'CONFIRMATION'):self.probe()
        self.confirm.write_text(json.dumps(dict(d,processed_at='2020-01-01T00:00:00+00:00')))
        with self.assertRaisesRegex(bridge.BridgeError,'NOT_NEWER'):self.probe()
        self.assertEqual(self.driver.calls,[])
    def test_wrong_binding_intent_task_stage_or_query_rejected(self):
        original=bridge.load_binding(self.p.path,self.name)
        for change in ['unowned','query','session']:
            b=copy.deepcopy(original)
            if change=='unowned':b['tabs']['search']['owned']=False
            elif change=='query':b['tabs']['search']['navigation_intent']['url']=URL.replace('type=51','type=53')+'&keyword=other'
            else:b['tabs']['search']['session']='other'
            bridge.save_binding(self.p.path,self.name,b)
            with self.assertRaises(bridge.BridgeError):self.probe()
        bridge.save_binding(self.p.path,self.name,original)
        t=self.p.task(self.task);pl=t['payload'];pl['stage']='body';self.p.db.execute('update tasks set payload=? where id=?',(json.dumps(pl),self.task));self.p.db.commit()
        with self.assertRaises(bridge.BridgeError):self.probe()
        self.assertEqual(self.driver.calls,[])
    def test_attest_restores_only_old_web_scope_and_no_tasks(self):
        old=bridge.load_binding(self.p.path,self.name);p,r=self.attest();self.allowed();self.unchanged();new=bridge.load_binding(self.p.path,self.name)
        self.assertEqual(old['tabs'],new['tabs']);self.assertEqual(old.get('note_unavailable_recovery'),new.get('note_unavailable_recovery'));self.assertEqual(old.get('web_parent_authorization'),new.get('web_parent_authorization'));self.assertTrue(r['no_tasks_completed']);self.assertFalse(r['sort_verified']);self.assertEqual(self.p.task(self.task)['status'],'pending')
        with self.assertRaises(xp.legacy.StopRun):
            with identity.account_lease(self.b['profile_guard_key'],self.b['account_uid']):pass
        self.assertTrue(search.attest(self.p.path,self.name,p['probe_file'],self.review)['duplicate']);self.assertEqual(len(self.driver.calls),3)
    def test_mutable_handoff_updates_do_not_replace_frozen_failure(self):
        self.attest();(self.p.path/'bridge/handoff.json').write_text('{}');(self.p.path/'bridge/last-driver-error.json').write_text('{}');self.allowed()
    def test_new_stop_and_alias_invalidate(self):
        self.attest();file=identity.guard_directory(self.keys[0])/'STOP.json';d=json.loads(file.read_text());d['at']=datetime.now(timezone.utc).isoformat();file.write_text(json.dumps(d));self.blocked()
    def test_new_alias_invalidates(self):
        self.attest();identity.associate(self.b['account_uid'],'6'*64);self.blocked()
    def test_proof_capture_confirmation_review_tamper_revoke(self):
        for target in ['normal','confirmation','review','failure_source']:
            t=Tests();t.setUp()
            try:
                p,r=t.attest();record=json.loads(Path(p['probe_file']).read_text())
                file={'normal':p['normal_file'],'confirmation':record['confirmation']['file'],'review':str(t.review),'failure_source':record['search_scope']['failure_sources']['driver_error']['file']}[target]
                Path(file).write_text('{}');t.blocked()
            finally:t.tearDown()
    def test_changed_authority_page_uid_context_invalidates(self):
        self.attest();original=bridge.load_binding(self.p.path,self.name)
        for key,value in [('context_id','other'),('account_uid','9'*24),('session','other')]:
            b=copy.deepcopy(original);b[key]=value;bridge.save_binding(self.p.path,self.name,b);self.blocked()
        for role in ['detail','search']:
            b=copy.deepcopy(original);b['tabs'][role]['page']='E'*32;bridge.save_binding(self.p.path,self.name,b);self.blocked()
    def test_claiming_sort_or_body_verified_cannot_attest(self):
        p=self.probe()
        r={'probe_id':p['probe_id'],'normal_sha256':p['normal_sha256'],'reader':'codex','reviewed':True,'query_read':True,'search_cards_read':True,'body_read':True,'sort_verified':True,'pagination_verified':False,'summary':'bad','positions':['/capture/cards']};self.review.write_text(json.dumps(r))
        with self.assertRaisesRegex(bridge.BridgeError,'ACTUAL_CODEX'):search.attest(self.p.path,self.name,p['probe_file'],self.review)
        self.blocked();self.unchanged()

class ForkTests(Tests):
    FORK=True

class ManualTests(unittest.TestCase):
    FORK = False
    def setUp(self):
        self.e = ForkTests() if self.FORK else Tests()
        self.e.setUp()
    def tearDown(self): self.e.tearDown()
    def root_failure(self, fail='identity'):
        with self.assertRaises(BaseException): self.e.probe(Fake(fail))
        failure = next((self.e.p.path/'bridge/current-page-recoveries').glob('*/failure.json'))
        return failure.parent
    def manual(self, directory):
        ref=lambda name:{'file':str(directory/name),'sha256':hashlib.sha256((directory/name).read_bytes()).hexdigest()}
        path=self.e.root/'manual-search-login.json'
        path.write_text(json.dumps({'kind':search.MANUAL_KIND,'project':str(self.e.p.path.resolve()),
            'binding_name':self.e.name,'task_id':self.e.task,'user_statement':'我已重新登陆好了，你继续',
            'confirmation_reference':'actual later user login report','processed_at':datetime.now(timezone.utc).isoformat(),
            'original_attempt':ref('attempt.json'),'original_failure':ref('failure.json')}))
        return path
    def probe(self, manual, driver=None):
        return search.probe(self.e.p.path,self.e.name,self.e.task,self.e.confirm,'continue existing research',driver or Fake(),manual_restoration=manual)
    def attest(self, result):
        self.e.review.write_text(json.dumps({'probe_id':result['probe_id'],'normal_sha256':result['normal_sha256'],
            'reader':'codex','reviewed':True,'query_read':True,'search_cards_read':True,'body_read':False,
            'sort_verified':False,'pagination_verified':False,'summary':'Actually read same query and cards after user manual login; no sorting proof',
            'positions':['/capture/url','/capture/cards']}))
        return search.attest(self.e.p.path,self.e.name,result['probe_file'],self.e.review)
    def test_manual_login_uses_root_frozen_failure_after_live_error_changed(self):
        root=self.root_failure();before={n:(root/n).read_bytes() for n in ['attempt.json','failure.json']}
        (self.e.p.path/'bridge/last-driver-error.json').write_text(json.dumps({'role':'search','action':'identity','at':datetime.now(timezone.utc).isoformat(),'error':{'code':'ACCESS_STOP'}}))
        (self.e.p.path/'bridge/handoff.json').write_text('{}')
        file=self.manual(root);driver=Fake();result=self.probe(file,driver)
        self.assertNotEqual(root,Path(result['probe_file']).parent)
        self.assertEqual([x[0] for x in driver.calls],['metadata','identity','capture'])
        self.e.blocked();self.e.unchanged();self.assertFalse(result['collection_permitted'])
        self.attest(result);self.e.allowed();self.e.unchanged()
        for name,raw in before.items():self.assertEqual((root/name).read_bytes(),raw)
        self.assertEqual(self.e.p.task(self.e.task)['status'],'pending')
    def test_local_manual_preflight_does_not_consume_child_or_call_driver(self):
        root=self.root_failure();file=self.manual(root)
        directory=self.e.p.path/'bridge/current-page-recoveries'
        before={str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        scope,_,token,_,_=search.manual_context(self.e.p,bridge.load_binding(self.e.p.path,self.e.name),
            self.e.task,current.nr.current_stops(self.e.keys),json.loads(file.read_text()))
        self.assertEqual(scope['query'],QUERY);self.assertFalse((directory/token).exists())
        self.assertEqual(before,{str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()})
        self.e.unchanged()
    def test_manual_login_report_must_be_explicit_newer_and_scoped(self):
        root=self.root_failure();file=self.manual(root);orig=json.loads(file.read_text());driver=Fake()
        for change in [{'user_statement':'页面均正常'},{'user_statement':'听说我已重新登陆好了，你继续'},
                       {'processed_at':'2020-01-01T00:00:00+00:00'},{'binding_name':'other'},{'task_id':self.e.base.task}]:
            file.write_text(json.dumps(dict(orig,**change)))
            with self.assertRaises(bridge.BridgeError):self.probe(file,driver)
        self.assertEqual(driver.calls,[])
    def test_manual_child_fixed_opportunity_after_failed_or_interrupted_read(self):
        for fail in ['metadata','identity','capture','query','page','uid','timeout','interrupt']:
            t=ManualForkTests() if self.FORK else ManualTests();t.setUp()
            try:
                root=t.root_failure();file=t.manual(root);driver=Fake(fail)
                with self.assertRaises(BaseException):t.probe(file,driver)
                calls=len(driver.calls);d=json.loads(file.read_text());d['processed_at']=datetime.now(timezone.utc).isoformat()
                copy_file=t.e.root/'different-input.json';copy_file.write_text(json.dumps(d))
                with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):t.probe(copy_file,driver)
                self.assertEqual(len(driver.calls),calls);t.e.unchanged();t.e.blocked()
            finally:t.tearDown()
    def test_root_failure_must_be_identity_access_with_verified_metadata(self):
        for fail in ['metadata','capture','timeout']:
            t=ManualForkTests() if self.FORK else ManualTests();t.setUp()
            try:
                root=t.root_failure(fail);file=t.manual(root);driver=Fake()
                with self.assertRaisesRegex(bridge.BridgeError,'IDENTITY_ACCESS_FAILURE'):t.probe(file,driver)
                self.assertEqual(driver.calls,[])
            finally:t.tearDown()
    def test_child_cannot_become_a_parent_and_root_cannot_be_reset(self):
        root=self.root_failure();file=self.manual(root)
        with self.assertRaises(bridge.BridgeError):self.probe(file,Fake('identity'))
        child=next(d for d in (self.e.p.path/'bridge/current-page-recoveries').iterdir() if d!=root and (d/'failure.json').exists())
        file=self.manual(child);driver=Fake()
        with self.assertRaisesRegex(bridge.BridgeError,'ROOT_REFERENCE'):self.probe(file,driver)
        self.assertEqual(driver.calls,[])
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.e.probe()
    def test_new_stop_or_fixed_identity_change_reject_before_child_read(self):
        root=self.root_failure();file=self.manual(root);original=bridge.load_binding(self.e.p.path,self.e.name);driver=Fake()
        for field,value in [('account_uid','9'*24),('session','other'),('context_id','other')]:
            b=copy.deepcopy(original);b[field]=value;bridge.save_binding(self.e.p.path,self.e.name,b)
            with self.assertRaises(xp.legacy.ResearchError):self.probe(file,driver)
        b=copy.deepcopy(original);b['tabs']['search']['page']='F'*32;bridge.save_binding(self.e.p.path,self.e.name,b)
        with self.assertRaises(xp.legacy.ResearchError):self.probe(file,driver)
        bridge.save_binding(self.e.p.path,self.e.name,original)
        stop=identity.guard_directory(self.e.keys[0])/'STOP.json';d=json.loads(stop.read_text());d['at']=datetime.now(timezone.utc).isoformat();stop.write_text(json.dumps(d))
        with self.assertRaises(xp.legacy.ResearchError):self.probe(file,driver)
        self.assertEqual(driver.calls,[])
    def test_original_source_hashes_are_bound_and_cannot_be_substituted(self):
        root=self.root_failure();file=self.manual(root);d=json.loads(file.read_text());d['original_failure']['sha256']='f'*64;file.write_text(json.dumps(d));driver=Fake()
        with self.assertRaisesRegex(bridge.BridgeError,'EVIDENCE_CHANGED'):self.probe(file,driver)
        self.assertEqual(driver.calls,[])
    def test_successful_child_manual_source_tamper_and_new_stop_revoke(self):
        root=self.root_failure();file=self.manual(root);result=self.probe(file);self.attest(result);self.e.allowed()
        file.write_text('{}');self.e.blocked();self.e.unchanged()

class ManualForkTests(ManualTests):
    FORK = True

if __name__=='__main__':unittest.main()
