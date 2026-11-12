#!/usr/bin/env python3
"""Offline regression tests; no browser, credentials or external network."""
import hashlib
import json
import multiprocessing as mp
import copy
import io
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_bridge as b
import xhs_account_identity as identity
import xhs_research as legacy
import xhs_project as xp
import xhs_native as native

UID='1'*24
NOTE='2'*24
URL='https://www.xiaohongshu.com/explore/'+NOTE+'?xsec_token=observed&xsec_source=pc_search'


def attempt_lock(root,key,uid,queue):
    with patch.object(legacy,'account_root',lambda:Path(root)):
        try:
            with identity.account_lease(key,uid):queue.put('acquired')
        except legacy.CollectionBusy:queue.put('busy')


class Fake:
    def __init__(self,url=URL,uid=UID):self.calls=[];self.url=url;self.uid=uid
    def call(self,action,binding=None,role=None,**fields):
        self.calls.append((action,binding and binding['context_id'],role,fields))
        if action=='metadata':return {'page':binding['tabs'][role]['page'],'url':self.url,'windowId':7}
        if action=='identity':return {'uid':self.uid,'url':self.url,'visible':True}
        if action=='capture':return {'url':self.url,'uid':UID,'body':'实际正文','title':'题名','media':[]}
        if action=='profiles':return {'daemonRunning':True,'profiles':[{'contextId':'profile_alpha-context','extensionVersion':'1.0.26'}]}
        if action=='navigate':self.url=fields['url'];return {'navigationSubmitted':True}
        if action=='create':self.url='https://www.xiaohongshu.com/explore';return {'page':'p-'+role}
        raise AssertionError(action)


class VisibilityFake(Fake):
    def __init__(self,uid=UID):
        super().__init__(uid=uid);self.visible=False
    def call(self,action,binding=None,role=None,**fields):
        if action=='activate':
            self.calls.append((action,binding['context_id'],role,fields))
            return {'activated':True}
        result=super().call(action,binding,role,**fields)
        if action=='identity':result['visible']=self.visible
        return result


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.guard=self.root/'guards';self.guard.mkdir(mode=0o700)
        self.patch=patch.object(legacy,'account_root',lambda:self.guard);self.patch.start()
        self.project=xp.Project(self.root/'project',create=True)
        self.source=self.root/'source.json';self.source.write_text(json.dumps({'url':URL}))
        manifest=self.root/'manifest.json';manifest.write_text(json.dumps({'units':[{'stage':'body','target':NOTE,'next':'read','sources':[str(self.source)]}]}))
        self.tid=native.import_manifest(self.project,manifest)['task_ids'][0]
        self.binding={'binding_name':'test','context_id':'profile_alpha-context','session':'xhs-test','account_uid':UID,'tabs':{'detail':{'page':'p1','owned':True}}}

    def tearDown(self):self.project.close();self.patch.stop();self.tmp.cleanup()

    def test_scope_private_and_tricky_hosts_rejected(self):
        for url in ['https://www.xiaohongshu.com/chat','https://www.xiaohongshu.com.evil/explore','https://www.xiaohongshu.com@evil/explore','http://www.xiaohongshu.com/explore','https://www.xiaohongshu.com:444/explore']:
            with self.assertRaises(b.BridgeError):b.safe_url(url)
        self.assertEqual(b.safe_url(URL),URL)

    def test_observed_author_note_route_preserves_note_identity(self):
        author_url='https://www.xiaohongshu.com/user/profile/'+'4'*24+'/'+NOTE+'?xsec_token=observed'
        self.assertEqual(b.safe_url(author_url),author_url)
        self.binding['tabs']['detail']['navigation_intent']={'url':author_url}
        result=b.verify_tab(Fake(URL),self.binding,'detail')
        self.assertEqual(result['url'],URL)
        self.assertNotIn('navigation_intent',self.binding['tabs']['detail'])
        self.assertNotEqual(b.target_path('/user/profile/'+'4'*24),b.target_path('/explore/'+NOTE))

    def test_scope_change_reads_no_identity_or_body(self):
        driver=Fake('https://example.com/private')
        with self.assertRaises(b.BridgeError):b.verify_tab(driver,self.binding,'detail')
        self.assertEqual([x[0] for x in driver.calls],['metadata'])

    def test_unexpected_other_public_note_stops_before_identity(self):
        self.binding['tabs']['detail']['last_url']=URL
        driver=Fake('https://www.xiaohongshu.com/explore/'+'9'*24)
        with self.assertRaisesRegex(b.BridgeError,'UNEXPECTED_TARGET_CHANGE'):
            b.verify_tab(driver,self.binding,'detail')
        self.assertEqual([x[0] for x in driver.calls],['metadata'])

    def test_login_redirect_is_a_shared_access_stop(self):
        with self.assertRaisesRegex(b.BridgeError,'ACCESS_STOP'):
            b.safe_url('https://www.xiaohongshu.com/website-login/captcha')

    def test_driver_routes_each_role_to_its_persisted_lease(self):
        driver=b.Driver.__new__(b.Driver)
        driver.runtime={'package_root':'pinned','node':'node','config_dir':'config','cache_dir':'cache'}
        self.binding.update(extension_version='1.0.26')
        self.binding['tabs']={'search':{'page':'search-page','session':'xhs-test-search'},
                              'detail':{'page':'detail-page','session':'xhs-test-detail'}}
        calls=[]
        def run(args,env):
            calls.append(args)
            return {'ok':True,'data':{}}
        driver.metrics={'calls':0,'elapsed_seconds':0}
        with patch.object(driver,'exchange',run):
            driver.call('metadata',self.binding,'search');driver.call('metadata',self.binding,'detail')
        self.assertEqual([c['session'] for c in calls],['xhs-test-search','xhs-test-detail'])
        self.assertEqual([c['tab'] for c in calls],['search-page','detail-page'])
        self.assertTrue(all(c['contextId']=='profile_alpha-context' for c in calls))

    def test_account_mismatch_reads_no_body(self):
        driver=Fake(uid='3'*24)
        with self.assertRaises(b.BridgeError):b.collect_one(self.project,self.binding,driver,self.project.task(self.tid))
        self.assertEqual([x[0] for x in driver.calls],['metadata','identity'])

    def test_invisible_target_without_authorization_never_activates_os_app(self):
        for authorization in (None,'','   ',True):
            with self.subTest(authorization=authorization):
                self.binding.pop('app_foreground_authorization',None)
                if authorization is not None:self.binding['app_foreground_authorization']=authorization
                driver=VisibilityFake()
                with patch.object(b.sys,'platform','darwin'),patch.object(b.subprocess,'run') as run:
                    with self.assertRaisesRegex(b.BridgeError,'TARGET_NOT_VISIBLE'):
                        b.collect_one(self.project,self.binding,driver,self.project.task(self.tid))
                run.assert_not_called()
                self.assertEqual([x[0] for x in driver.calls],['metadata','identity','activate','metadata','identity'])

    def test_opt_in_foreground_requires_fresh_same_target_identity_before_collection(self):
        self.binding['app_foreground_authorization']='Explicit synthetic Chrome research authorization'
        driver=VisibilityFake()
        def activate_app(*args,**kwargs):
            self.assertEqual([x[0] for x in driver.calls],['metadata','identity','activate','metadata','identity'])
            driver.visible=True
        with patch.object(b.sys,'platform','darwin'),patch.object(b.subprocess,'run',side_effect=activate_app) as run,patch.object(b.time,'sleep') as sleep:
            result=b.collect_one(self.project,self.binding,driver,self.project.task(self.tid))
        run.assert_called_once_with(['/usr/bin/osascript','-e','tell application "Google Chrome" to activate'],
            shell=False,check=True,timeout=5,stdout=b.subprocess.DEVNULL,stderr=b.subprocess.DEVNULL)
        sleep.assert_called_once_with(1)
        self.assertEqual([x[0] for x in driver.calls],['metadata','identity','activate','metadata','identity','metadata','identity','capture'])
        self.assertTrue(Path(result['snapshot']).is_file())

    def test_opt_in_foreground_is_not_available_on_other_platforms(self):
        self.binding['app_foreground_authorization']='Explicit synthetic Chrome research authorization'
        driver=VisibilityFake()
        with patch.object(b.sys,'platform','linux'),patch.object(b.subprocess,'run') as run:
            with self.assertRaisesRegex(b.BridgeError,'TARGET_NOT_VISIBLE'):
                b.collect_one(self.project,self.binding,driver,self.project.task(self.tid))
        run.assert_not_called();self.assertNotIn('capture',[x[0] for x in driver.calls])

    def test_foreground_os_failure_stops_without_collection_or_retry(self):
        self.binding['app_foreground_authorization']='Explicit synthetic Chrome research authorization'
        for error in (OSError('Synthetic OS failure'),b.subprocess.TimeoutExpired('synthetic',5),
                      b.subprocess.CalledProcessError(1,'synthetic')):
            with self.subTest(error=type(error).__name__):
                driver=VisibilityFake()
                with patch.object(b.sys,'platform','darwin'),patch.object(b.subprocess,'run',side_effect=error) as run,patch.object(b.time,'sleep') as sleep:
                    with self.assertRaisesRegex(b.BridgeError,'TARGET_NOT_VISIBLE'):
                        b.collect_one(self.project,self.binding,driver,self.project.task(self.tid))
                run.assert_called_once();sleep.assert_not_called()
                self.assertEqual([x[0] for x in driver.calls],['metadata','identity','activate','metadata','identity'])

    def test_foreground_success_without_observed_visibility_still_refuses(self):
        self.binding['app_foreground_authorization']='Explicit synthetic Chrome research authorization'
        driver=VisibilityFake()
        with patch.object(b.sys,'platform','darwin'),patch.object(b.subprocess,'run') as run,patch.object(b.time,'sleep'):
            with self.assertRaisesRegex(b.BridgeError,'TARGET_NOT_VISIBLE'):
                b.collect_one(self.project,self.binding,driver,self.project.task(self.tid))
        run.assert_called_once()
        self.assertEqual([x[0] for x in driver.calls],['metadata','identity','activate','metadata','identity','metadata','identity'])

    def test_wrong_account_is_refused_before_any_os_activation(self):
        self.binding['app_foreground_authorization']='Explicit synthetic Chrome research authorization'
        driver=VisibilityFake(uid='3'*24)
        with patch.object(b.sys,'platform','darwin'),patch.object(b.subprocess,'run') as run:
            with self.assertRaisesRegex(b.BridgeError,'ACCOUNT_MISMATCH'):
                b.collect_one(self.project,self.binding,driver,self.project.task(self.tid))
        run.assert_not_called();self.assertEqual([x[0] for x in driver.calls],['metadata','identity'])

    def test_foreground_does_not_suppress_new_scope_or_account_errors(self):
        self.binding['app_foreground_authorization']='Explicit synthetic Chrome research authorization'
        for code in ('SCOPE_CHANGED','ACCOUNT_MISMATCH'):
            with self.subTest(code=code):
                driver=VisibilityFake()
                def activate_app(*args,**kwargs):
                    driver.visible=True
                    if code=='SCOPE_CHANGED':driver.url='https://example.com/private'
                    else:driver.uid='3'*24
                with patch.object(b.sys,'platform','darwin'),patch.object(b.subprocess,'run',side_effect=activate_app) as run,patch.object(b.time,'sleep'):
                    with self.assertRaisesRegex(b.BridgeError,code):
                        b.collect_one(self.project,self.binding,driver,self.project.task(self.tid))
                run.assert_called_once();self.assertNotIn('capture',[x[0] for x in driver.calls])

    def test_empty_lease_does_not_prove_target_destroyed(self):
        fake=Fake()
        def call(action,*args,**kwargs):
            if action=='metadata':raise b.BridgeError('TARGET_MISSING')
            if action=='probe_registered_target':return {'state':'registered_target_exists_but_lease_missing','page':'p1'}
            raise AssertionError('Unexpected action '+action)
        fake.call=call
        with self.assertRaisesRegex(b.BridgeError,'TARGET_RELATION_UNCONFIRMED'):
            b.recover_owned_role(self.project.path,'test',self.binding,fake,'detail')
        self.assertEqual(self.binding['tabs']['detail']['page'],'p1')
        self.assertNotIn('retired_targets',self.binding)

    def test_proven_destroyed_owned_target_recreated_in_same_role(self):
        fake=Fake();original=fake.call
        def call(action,binding=None,role=None,**fields):
            if action=='metadata' and binding['tabs'][role].get('page')=='p1':raise b.BridgeError('TARGET_MISSING')
            if action=='probe_registered_target':return {'state':'registered_target_absent','page':'p1'}
            return original(action,binding,role,**fields)
        fake.call=call
        b.recover_owned_role(self.project.path,'test',self.binding,fake,'detail')
        self.assertEqual(self.binding['retired_targets'][0]['tab']['page'],'p1')
        self.assertEqual(self.binding['tabs']['detail']['session'],'xhs-test-detail')
        self.assertEqual([x[0] for x in fake.calls],['create','metadata','identity'])

    def test_destroyed_target_replacement_wrong_account_stops_before_content(self):
        fake=Fake(uid='3'*24);original=fake.call
        def call(action,binding=None,role=None,**fields):
            if action=='metadata' and binding['tabs'][role].get('page')=='p1':raise b.BridgeError('TARGET_MISSING')
            if action=='probe_registered_target':return {'state':'registered_target_absent','page':'p1'}
            return original(action,binding,role,**fields)
        fake.call=call
        with self.assertRaisesRegex(b.BridgeError,'ACCOUNT_MISMATCH'):
            b.recover_owned_role(self.project.path,'test',self.binding,fake,'detail')
        self.assertEqual([x[0] for x in fake.calls],['create','metadata','identity'])

    def test_destroyed_fork_role_requires_home_proof_before_replacement(self):
        self.binding['fork_home_verification']={'file':'preserved-home','sha256':'preserved-sha'}
        calls=[]
        class Absent:
            def call(self,action,*args,**kwargs):
                calls.append(action)
                if action=='probe_registered_target':
                    return {'state':'registered_target_absent','page':'p1'}
                raise AssertionError('No creation or content before a fork home proof')
        with patch.object(b,'provision_role',side_effect=b.BridgeError('TARGET_MISSING')):
            with self.assertRaisesRegex(b.BridgeError,'FORK_REPLACEMENT_HOME_REVERIFICATION_REQUIRED'):
                b.recover_owned_role(self.project.path,'test',self.binding,Absent(),'detail')
        self.assertEqual(calls,['probe_registered_target'])
        self.assertNotIn('detail',self.binding['tabs'])
        saved=json.loads((self.project.path/'bridge/bindings/test.json').read_text())
        self.assertEqual(saved['retired_targets'][0]['tab']['page'],'p1')
        self.assertEqual(saved['fork_home_verification']['sha256'],'preserved-sha')

    def test_explicit_identity_and_acquired_is_not_reviewed(self):
        driver=Fake();r=b.collect_one(self.project,self.binding,driver,self.project.task(self.tid))
        self.assertTrue(all(x[1]=='profile_alpha-context' for x in driver.calls))
        self.assertTrue(r['awaiting_review'])
        task=self.project.task(self.tid);self.assertEqual(task['status'],'pending');self.assertEqual(task['review_status'],'pending')
        self.assertEqual(len(task['payload']['bridge']['hashes']),1)

    def test_same_snapshot_is_idempotent_and_smaller_keeps_ids(self):
        task=self.project.task(self.tid);data={'url':URL,'body':'body','cards':[{'url':URL}]}
        b.store_capture(self.project,task,data)
        second=b.store_capture(self.project,self.project.task(self.tid),data)
        self.assertTrue(second['duplicate'])
        b.store_capture(self.project,self.project.task(self.tid),{'url':URL,'body':'b','cards':[]})
        progress=self.project.task(self.tid)['payload']['bridge']
        self.assertEqual(progress['observed_note_ids'],[NOTE]);self.assertEqual(len(progress['hashes']),2)

    def test_search_preserves_actual_sort_selection_before_panel_closes(self):
        manifest=self.root/'search-sort.json'
        manifest.write_text(json.dumps({'units':[{'stage':'search','target':'正畸','sort':'latest',
            'sources':[str(self.source)],'next':'page'}]}))
        tid=native.import_manifest(self.project,manifest)['task_ids'][0]
        url='https://www.xiaohongshu.com/search_result/?keyword=%E6%AD%A3%E7%95%B8&type=51'
        binding={**self.binding,'tabs':{'search':{'page':'search','owned':True}}}
        driver=Fake(url);original_call=driver.call;state={'panel_open':True};advances=[]
        def call(action,*args,**fields):
            if action=='advance':
                advances.append(fields)
                if fields.get('dismissFilters'):
                    state['panel_open']=False
                    return {'action':'filter_closed'}
                if 'sort' in fields:raise AssertionError('Already selected sort must not be clicked again')
                return {'after':500}
            if action=='capture':
                return {'url':url,'uid':UID,'stage':'search',
                    'selected':['最新'] if state['panel_open'] else [],
                    'cards':[{'url':URL,'text':'Synthetic search result'}],
                    'position':{'scrollTop':0,'scrollHeight':2000}}
            return original_call(action,*args,**fields)
        driver.call=call
        with patch.object(b.time,'sleep',lambda n:None):
            b.collect_one(self.project,binding,driver,self.project.task(tid))
        progress=self.project.task(tid)['payload']['bridge']
        raw=json.loads(Path(progress['last_snapshot']).read_text())
        self.assertEqual(raw['selected'],[])
        self.assertEqual(raw['sortVerification']['label'],'最新')
        self.assertEqual(raw['sortVerification']['selected_before_close'],['最新'])
        self.assertIs(raw['sortVerification']['verified_before_close'],True)
        self.assertFalse(state['panel_open'])
        self.assertEqual(sum(bool(a.get('dismissFilters')) for a in advances),1)
        self.assertFalse(any('sort' in a for a in advances))

    def test_two_profiles_same_uid_serialize_across_processes(self):
        first='a'*64;second='b'*64
        identity.associate(UID,first);identity.associate(UID,second)
        ctx=mp.get_context('fork');q=ctx.Queue()
        with identity.account_lease(first,UID):
            proc=ctx.Process(target=attempt_lock,args=(str(self.guard),second,UID,q));proc.start();proc.join(3)
            self.assertFalse(proc.is_alive());self.assertEqual(q.get(timeout=1),'busy')

    def test_legacy_stop_on_other_profile_blocks_bridge(self):
        a='a'*64;c='b'*64;identity.associate(UID,a);identity.associate(UID,c)
        legacy.write_json(identity.guard_directory(a)/'STOP.json',{'reason':'old'})
        with self.assertRaises(legacy.StopRun):
            with identity.account_lease(c,UID):pass

    def test_scoped_media_exception_preserves_stop_and_blocks_new_stops(self):
        key='a'*64;path=identity.guard_directory(key)/'STOP.json'
        legacy.write_json(path,{'reason':'Public media access restriction: HTTP_403'})
        raw=path.read_bytes();exceptions={key:hashlib.sha256(raw).hexdigest()}
        with identity.account_lease(key,UID,web_stop_exceptions=exceptions):self.assertEqual(path.read_bytes(),raw)
        with self.assertRaises(legacy.StopRun):
            with identity.account_lease(key,UID):pass
        legacy.write_json(path,{'reason':'Observed browser login/verification/access restriction'})
        with self.assertRaises(legacy.StopRun):
            with identity.account_lease(key,UID,web_stop_exceptions=exceptions):pass

    def test_bind_preflight_stop_is_recorded_without_creating_tabs(self):
        key='a'*64;legacy.write_json(identity.guard_directory(key)/'STOP.json',{'reason':'old media 403'})
        driver=Fake();old=driver.call
        def call(action,*args,**kwargs):
            if action=='doctor':return {}
            return old(action,*args,**kwargs)
        driver.call=call
        with patch.object(b,'Driver',lambda runtime:driver):
            with self.assertRaises(legacy.StopRun):
                b.bind(self.project.path,'profile_alpha',{},'profile_alpha-context','profile_alpha',UID,key,'explicit user request')
        saved=b.load_binding(self.project.path,'profile_alpha')
        self.assertEqual(saved['state'],'blocked');self.assertEqual(saved['last_error']['code'],'SHARED_STOP')
        self.assertEqual(saved['tabs'],{});self.assertFalse(any(c[0]=='create' for c in driver.calls))

    def test_only_never_attempted_role_can_be_provisioned_on_resume(self):
        binding={**self.binding,'binding_name':'profile_alpha','tabs':{}}
        driver=Fake()
        b.provision_role(self.project.path,'profile_alpha',binding,driver,'search')
        b.provision_role(self.project.path,'profile_alpha',binding,driver,'search')
        self.assertEqual(sum(c[0]=='create' for c in driver.calls),1)
        binding['tabs']['detail']={'session':'xhs-test-detail','state':'creation_pending'}
        with self.assertRaisesRegex(b.BridgeError,'TAB_CREATE_OUTCOME_UNKNOWN'):
            b.provision_role(self.project.path,'profile_alpha',binding,driver,'detail')
        self.assertEqual(sum(c[0]=='create' for c in driver.calls),1)

    def test_project_stop_precedes_capture(self):
        self.project.set_meta('stop',{'reason':'old'});self.project.db.commit()
        with self.assertRaises(legacy.StopRun):
            with b.lease(self.project.path,{'profile_guard_key':'a'*64,'account_uid':UID}):self.fail('must not run')

    def test_runtime_tampering_detected(self):
        runtime=self.root/'runtime';(runtime/'dist').mkdir(parents=True);(runtime/'clis').mkdir();(runtime/'package.json').write_text('{}')
        node=runtime/'node';node.write_bytes(b'node')
        cfg={'package_root':str(runtime),'package_tree_sha256':b.digest_tree(runtime),'node':str(node),'node_sha256':hashlib.sha256(b'node').hexdigest()}
        (runtime/'dist'/'file.js').write_text('changed')
        with self.assertRaises(b.BridgeError):b.Driver(cfg)

    def test_no_url_invention_for_an_unseen_note(self):
        task=self.project.task(self.tid);task['payload']['target']='f'*24
        with self.assertRaises(b.BridgeError):b.observed_url(self.project.path,task)

    def test_review_receipt_resumes_only_read_snapshots(self):
        data={'url':URL,'body':'body','cards':[]}
        b.store_capture(self.project,self.project.task(self.tid),data)
        progress=self.project.task(self.tid)['payload']['bridge']
        receipt={'target':NOTE,'action':'read','outcome':'ready','observed_at':legacy.now(),
                 'summary':'Read the archived body; no attribution confirmed.',
                 'gaps':['image review pending'],'source_file':progress['last_snapshot'],
                 'url':URL,'target_verified':True,'content_ready':True,'view_consistent':True,
                 'reviewed':True,'complete':False,'positions':['body'],
                 'bridge_snapshot_hashes':progress['hashes']}
        file=self.root/'review.json';legacy.write_json(file,receipt)
        result=b.review(self.project,self.tid,file)
        self.assertFalse(result['awaiting_review']);self.assertEqual(result['status'],'pending')
        b.store_capture(self.project,self.project.task(self.tid),{'url':URL,'body':'new'})
        receipt.update(complete=True,end='body_text_read',gaps=[]);legacy.write_json(file,receipt)
        with self.assertRaisesRegex(b.BridgeError,'UNREAD_SNAPSHOTS_REMAIN'):b.review(self.project,self.tid,file)

    def test_merged_comments_end_and_new_unread_thread(self):
        task=self.project.task(self.tid);p=task['payload'];p['stage']='replies'
        self.project.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(p),self.tid));self.project.db.commit()
        first={'url':URL,'results':[],'deepCoverage':{'rootPaginationState':'complete','explicitEnd':True,'threads':[
            {'rootKey':'id:one','state':'visible_replies_read'}]}}
        b.store_capture(self.project,self.project.task(self.tid),first)
        self.assertTrue(self.project.task(self.tid)['payload']['bridge']['collection_end'])
        small={'url':URL,'results':[],'deepCoverage':{'rootPaginationState':'pending','threads':[]}}
        b.store_capture(self.project,self.project.task(self.tid),small)
        self.assertTrue(self.project.task(self.tid)['payload']['bridge']['collection_end'])
        more={'url':URL,'results':[],'deepCoverage':{'rootPaginationState':'pending','threads':[
            {'rootKey':'id:two','state':'not_processed'}]}}
        b.store_capture(self.project,self.project.task(self.tid),more)
        self.assertFalse(self.project.task(self.tid)['payload']['bridge']['collection_end'])

    def test_scroll_is_captured_before_role_can_navigate_away(self):
        manifest=self.root/'search.json'
        manifest.write_text(json.dumps({'units':[{'stage':'search','target':'正畸','sort':'general','sources':[str(self.source)],'next':'page'}]}))
        tid=native.import_manifest(self.project,manifest)['task_ids'][0]
        url='https://www.xiaohongshu.com/search_result/?keyword=%E6%AD%A3%E7%95%B8&type=51'
        binding={**self.binding,'tabs':{'search':{'page':'search','owned':True}}}
        driver=Fake(url);old=driver.call;state={'top':0}
        def call(action,*args,**fields):
            if action=='advance':
                if fields.get('dismissFilters'):return {'action':'filter_already_closed'}
                state['top']=500;return {'after':500}
            if action=='capture':return {'url':url,'uid':UID,'stage':'search','selected':['综合'],'cards':[{'url':URL,'text':'A'}]+([{'url':URL.replace(NOTE,'3'*24),'text':'B'}] if state['top'] else []),'position':{'scrollTop':state['top'],'scrollHeight':2000}}
            return old(action,*args,**fields)
        driver.call=call
        with patch.object(b.time,'sleep',lambda n:None):b.collect_one(self.project,binding,driver,self.project.task(tid))
        progress=self.project.task(tid)['payload']['bridge']
        self.assertEqual(progress['cursor']['scrollTop'],500)
        self.assertEqual(len(progress['observed_note_ids']),2)
        self.assertEqual(len(progress['hashes']),2)

    def test_same_cards_with_scroll_progress_do_not_stall(self):
        task=self.project.task(self.tid);task['payload']['stage']='author'
        self.project.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),self.tid));self.project.db.commit()
        for top in (0,500,1000,1500):
            b.store_capture(self.project,self.project.task(self.tid),{'url':'https://www.xiaohongshu.com/user/profile/'+NOTE,'cards':[{'url':URL}],'position':{'scrollTop':top,'scrollHeight':3000}})
        progress=self.project.task(self.tid)['payload']['bridge']
        self.assertFalse(progress['awaiting_review']);self.assertEqual(len(progress['hashes']),1)
        self.assertEqual(progress['cursor']['scrollTop'],1500)

    def test_missing_owned_tab_does_not_create_replacement(self):
        driver=Fake()
        def call(action,*args,**kwargs):
            driver.calls.append((action,));raise b.BridgeError('TARGET_MISSING')
        driver.call=call
        with self.assertRaisesRegex(b.BridgeError,'TARGET_MISSING'):b.verify_tab(driver,self.binding,'detail')
        self.assertEqual(driver.calls,[('metadata',)])

    def test_reload_budget_survives_process_restart(self):
        driver=Fake();original=driver.call;self.binding['binding_name']='test'
        def call(action,*args,**kwargs):
            if action=='capture':raise b.BridgeError('CONTENT_LOADING')
            if action=='reload':driver.calls.append(('reload',));return {}
            return original(action,*args,**kwargs)
        driver.call=call
        with patch.object(b.time,'sleep',lambda n:None):
            with self.assertRaisesRegex(b.BridgeError,'CONTENT_LOADING'):
                b.ready_capture(self.project,self.binding,driver,'detail',{})
            saved=legacy.read_json(b.binding_path(self.project.path,'test'))
            with self.assertRaisesRegex(b.BridgeError,'CONTENT_LOADING_AFTER_SINGLE_RELOAD'):
                b.ready_capture(self.project,saved,driver,'detail',{})
        self.assertEqual(sum(x[0]=='reload' for x in driver.calls),1)


class WindowStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.project=Path(self.temp.name).resolve()
        self.binding={'binding_name':'test','context_id':'synthetic-context','session':'xhs-synthetic',
            'account_uid':UID,'runtime':{},'state':'blocked','last_error':{'code':'TARGET_NOT_VISIBLE'},
            'tabs':{role:{'page':page*32,'session':'xhs-synthetic-'+role,'owned':True,'last_url':URL}
                    for role,page in [('search','A'),('detail','B')]}}
        self.locks={'project':False,'lease':False};self.expected_role='detail';test=self
        @contextmanager
        def project_lock(project):
            self.assertEqual(project,self.project);self.locks['project']=True
            try:yield
            finally:self.locks['project']=False
        @contextmanager
        def account_lease(project,binding):
            self.assertTrue(self.locks['project']);self.assertEqual(project,self.project)
            self.assertEqual(binding['account_uid'],UID);self.locks['lease']=True
            try:yield ['synthetic-account-lock']
            finally:self.locks['lease']=False
        class DiagnosticDriver:
            def __init__(self):self.calls=[];self.closed=0;self.url=URL;self.uid=UID;self.drift=None;self.error=None
            def call(self,action,binding,role):
                test.assertTrue(all(test.locks.values()));test.assertEqual(role,test.expected_role)
                self.calls.append((action,role,binding['tabs'][role]['session']))
                page=binding['tabs'][role]['page']
                if action=='metadata':return {'page':page,'url':self.url}
                if action=='identity':return {'page':page,'url':self.url,'uid':self.uid,'visible':False}
                test.assertEqual(action,'registered_window_state','No UI or content action is permitted')
                if self.error:raise b.BridgeError(self.error)
                result={'page':page,'url':self.url,'uid':self.uid,'readOnly':True,'targetVerified':True,
                    'windowFocusRequested':False,'windowRestoreRequested':False,
                    'window':{'id':47,'state':'normal','focused':False},
                    'tab':{'id':31,'windowId':47,'url':self.url,'active':True}}
                if self.drift=='uid':self.uid='3'*24
                if self.drift=='url':self.url=URL.replace(NOTE,'4'*24)
                return result
            def close(self):
                test.assertTrue(test.locks['project']);test.assertFalse(test.locks['lease']);self.closed+=1
        self.driver=DiagnosticDriver();self.stack=ExitStack()
        self.driver_factory=self.stack.enter_context(patch.object(b,'Driver',return_value=self.driver))
        self.stack.enter_context(patch.object(b,'load_binding',side_effect=lambda project,name:copy.deepcopy(self.binding)))
        self.stack.enter_context(patch.object(b.xp,'project_lock',project_lock))
        self.stack.enter_context(patch.object(b,'lease',account_lease))
        self.saved=self.stack.enter_context(patch.object(b,'save_binding'))
        self.stopped=self.stack.enter_context(patch.object(b.identity,'stop_all'))
        self.stack.enter_context(patch.object(b,'prepare_target',side_effect=AssertionError('No prepare/activation')))
        self.stack.enter_context(patch.object(b.xp,'Project',side_effect=AssertionError('No database opening')))
        self.stack.enter_context(patch.object(b.sqlite3,'connect',side_effect=AssertionError('No direct SQLite access')))

    def tearDown(self):self.stack.close();self.temp.cleanup()

    def test_same_registered_role_lease_and_before_after_identity_without_normal_save(self):
        self.expected_role='search';original=copy.deepcopy(self.binding)
        result=b.window_state(self.project,'test','search')
        self.assertEqual(result['role'],'search');self.assertEqual(result['binding'],'test')
        self.assertEqual(result['window'],{'id':47,'state':'normal','focused':False})
        self.assertEqual([c[0] for c in self.driver.calls],['metadata','identity','registered_window_state','metadata','identity'])
        self.assertTrue(all(c[1:] == ('search','xhs-synthetic-search') for c in self.driver.calls))
        self.assertEqual(self.binding,original);self.saved.assert_not_called();self.stopped.assert_not_called()
        self.assertEqual(self.driver.closed,1)

    def test_wrong_original_url_refuses_before_native_diagnostic(self):
        self.driver.url=URL.replace(NOTE,'4'*24)
        with self.assertRaisesRegex(b.BridgeError,'UNEXPECTED_TARGET_CHANGE'):
            b.window_state(self.project,'test','detail')
        self.assertEqual([c[0] for c in self.driver.calls],['metadata'])
        self.saved.assert_called_once();self.stopped.assert_not_called();self.assertEqual(self.driver.closed,1)

    def test_recorded_navigation_intent_can_be_verified_without_persisting_it(self):
        self.driver.url=URL.replace(NOTE,'4'*24)
        self.binding['tabs']['detail']['navigation_intent']={'url':self.driver.url}
        result=b.window_state(self.project,'test','detail')
        self.assertEqual(result['url'],self.driver.url)
        self.assertIn('navigation_intent',self.binding['tabs']['detail'])
        self.saved.assert_not_called();self.assertEqual(self.driver.closed,1)

    def test_wrong_uid_before_diagnostic_and_after_diagnostic_are_rejected(self):
        self.driver.uid='3'*24
        with self.assertRaisesRegex(b.BridgeError,'ACCOUNT_MISMATCH'):
            b.window_state(self.project,'test','detail')
        self.assertEqual([c[0] for c in self.driver.calls],['metadata','identity'])
        self.driver.calls.clear();self.driver.uid=UID;self.driver.drift='uid'
        with self.assertRaisesRegex(b.BridgeError,'ACCOUNT_MISMATCH'):
            b.window_state(self.project,'test','detail')
        self.assertEqual([c[0] for c in self.driver.calls],['metadata','identity','registered_window_state','metadata','identity'])
        self.assertEqual(self.saved.call_count,2);self.stopped.assert_not_called();self.assertEqual(self.driver.closed,2)

    def test_url_change_after_diagnostic_is_rejected(self):
        self.driver.drift='url'
        with self.assertRaisesRegex(b.BridgeError,'UNEXPECTED_TARGET_CHANGE'):
            b.window_state(self.project,'test','detail')
        self.assertEqual([c[0] for c in self.driver.calls],['metadata','identity','registered_window_state','metadata'])
        self.saved.assert_called_once();self.stopped.assert_not_called();self.assertEqual(self.driver.closed,1)

    def test_only_access_stop_failure_uses_original_stop_boundary(self):
        self.driver.error='ACCESS_STOP'
        with self.assertRaisesRegex(b.BridgeError,'ACCESS_STOP'):
            b.window_state(self.project,'test','detail')
        self.saved.assert_called_once();self.stopped.assert_called_once_with(
            ['synthetic-account-lock'],'Observed browser login/verification/access restriction')
        self.assertEqual(self.driver.closed,1)

    def test_driver_is_closed_even_when_account_lease_cannot_be_entered(self):
        with patch.object(b,'lease',side_effect=b.BridgeError('USER_PAUSED')):
            with self.assertRaisesRegex(b.BridgeError,'USER_PAUSED'):
                b.window_state(self.project,'test','detail')
        self.assertEqual(self.driver.calls,[]);self.assertEqual(self.driver.closed,1)
        self.saved.assert_not_called();self.stopped.assert_not_called()

    def test_window_state_cli_routes_only_explicit_binding_and_role(self):
        result={'readOnly':True,'synthetic':True};output=io.StringIO()
        with patch.object(b,'window_state',return_value=result) as probe,patch.object(b.sys,'argv',
                ['xhs_bridge.py','--project',str(self.project),'window-state','--binding','test','--role','search']),redirect_stdout(output):
            b.main()
        probe.assert_called_once_with(self.project,'test','search')
        self.assertEqual(json.loads(output.getvalue()),{'ok':True,'data':result})
        self.saved.assert_not_called();self.stopped.assert_not_called()

    def test_text_mode_only_changes_the_explicit_boolean_without_driver_or_authority_changes(self):
        original=copy.deepcopy(self.binding)
        for mode,value in [('background',True),('foreground',False)]:
            with self.subTest(mode=mode):
                result=b.text_mode(self.project,'test',mode)
                expected=copy.deepcopy(original);expected['background_text_collection']=value
                self.assertEqual(self.saved.call_args.args,(self.project,'test',expected))
                self.assertEqual(result,{'binding':'test','mode':mode,'background_text_collection':value})
                self.assertEqual(self.binding,original)
        self.driver_factory.assert_not_called();self.stopped.assert_not_called()
        self.assertEqual(self.driver.calls,[]);self.assertEqual(self.driver.closed,0)

    def test_text_mode_invalid_modes_do_not_load_save_or_create_driver(self):
        for mode in (None,'','auto',True,{},[]):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(b.BridgeError,'TEXT_MODE_REQUIRED'):
                    b.text_mode(self.project,'test',mode)
        self.driver_factory.assert_not_called();self.saved.assert_not_called();self.stopped.assert_not_called()

    def test_text_mode_lease_failure_preserves_binding_and_stop(self):
        original=copy.deepcopy(self.binding)
        with patch.object(b,'lease',side_effect=b.BridgeError('USER_PAUSED')):
            with self.assertRaisesRegex(b.BridgeError,'USER_PAUSED'):
                b.text_mode(self.project,'test','background')
        self.driver_factory.assert_not_called();self.saved.assert_not_called();self.stopped.assert_not_called()
        self.assertEqual(self.binding,original)

    def test_text_mode_cli_uses_explicit_binding_and_mode(self):
        output=io.StringIO()
        with patch.object(b.sys,'argv',['xhs_bridge.py','--project',str(self.project),
                'text-mode','--binding','test','--mode','background']),redirect_stdout(output):
            b.main()
        self.assertEqual(json.loads(output.getvalue()),{'ok':True,'data':{
            'binding':'test','mode':'background','background_text_collection':True}})
        self.saved.assert_called_once();self.driver_factory.assert_not_called();self.stopped.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
