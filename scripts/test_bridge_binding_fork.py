"""Synthetic isolated binding delegation: no browser, network or real account data."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

import xhs_account_identity as identity
import xhs_bridge as bridge
import xhs_note_recovery as nr
import xhs_project as xp
import test_note_recovery as fixture
UID,PROFILE,CONTEXT,HOME=fixture.UID,fixture.PROFILE,fixture.CONTEXT,fixture.HOME


class ForkDriver:
    def __init__(self,fail=None,callback=None,page_offset=0):
        self.actions=[];self.pages={};self.fail=fail;self.callback=callback;self.page_offset=page_offset
    def call(self,action,b=None,role=None,**fields):
        self.actions.append((action,b.get('binding_name') if b else None,role))
        if action=='profiles':return {'daemonRunning':True,'profiles':[{'contextId':CONTEXT,'extensionVersion':'1.0.26'}]}
        if action=='create':
            if self.fail=='unknown':raise bridge.BridgeError('COMMAND_OUTCOME_UNKNOWN')
            page=hex((10 if role=='search' else 11)+self.page_offset)[2:]*32
            if self.fail=='reused':page='c'*32
            self.pages[role]=page
            if self.callback:self.callback(action,role)
            return {'page':page}
        if action=='metadata':return {'page':self.pages[role],'url':HOME}
        if action=='identity':
            if self.fail=='access':raise bridge.BridgeError('ACCESS_STOP')
            return {'page':self.pages[role] if self.fail!='wrong_target' else 'd'*32,
                    'url':HOME,'uid':UID if self.fail!='wrong_uid' else '9'*24,'visible':True}
        raise AssertionError('Unexpected browser action '+action)
    def close(self):pass


class InitializingDriver(ForkDriver):
    def __init__(self):super().__init__();self.ready=False;self.resume_url=HOME
    def call(self,action,b=None,role=None,**fields):
        result=super().call(action,b,role,**fields)
        if action=='metadata' and role=='search':result['url']=self.resume_url if self.ready else 'chrome://newtab/'
        return result


class BindingForkTests(unittest.TestCase):
    setUp=fixture.RecoveryTests.setUp
    tearDown=fixture.RecoveryTests.tearDown
    run_probe=fixture.RecoveryTests.run_probe
    run_attest=fixture.RecoveryTests.run_attest
    assert_stops_unchanged=fixture.RecoveryTests.assert_stops_unchanged

    def prepare(self,web_authorization=False):
        self.run_attest()
        if web_authorization:
            b=bridge.load_binding(self.p.path,'test')
            auth=self.root/'auth.json'
            auth.write_text(json.dumps({'scope':'rendered_public_web_only','approved':True,'context_id':CONTEXT,
                'account_uid':UID,'user_text':'Use separate research tabs','recorded_at':xp.legacy.now(),'preserved_media_stops':{}}))
            bridge.attach_web_authorization(self.p.path,b,auth);bridge.save_binding(self.p.path,'test',b)
        self.parent_before=bridge.binding_path(self.p.path,'test').read_bytes()
        b=bridge.load_binding(self.p.path,'test');self.grant_file=Path(b['note_unavailable_recovery']['file'])
        self.grant_before=self.grant_file.read_bytes()
        return b

    def fork(self,driver=None,name='isolated',intent='one-attempt'):
        self.fork_driver=driver or ForkDriver()
        return bridge.fork_binding(self.p.path,'test',name,intent,'Open independent public research tabs',self.fork_driver)

    def allowed(self,name='isolated',binding=None):
        with bridge.lease(self.p.path,binding or bridge.load_binding(self.p.path,name)):pass

    def blocked(self,name='isolated',binding=None):
        with self.assertRaises(xp.legacy.ResearchError):self.allowed(name,binding)

    def test_new_namespace_two_actual_home_ids_parent_and_stops_unchanged(self):
        parent=self.prepare(web_authorization=True);before=[self.p.task(x) for x in self.tasks]
        result=self.fork();b=bridge.load_binding(self.p.path,'isolated')
        self.assertTrue(result['parent_binding_unchanged']);self.assertNotEqual(parent['session'],b['session'])
        self.assertEqual(set(b['tabs']),{'search','detail'});self.assertNotIn('note_unavailable_recovery',b)
        self.assertNotIn('web_validation',b);self.assertTrue(result['body_validation_still_required'])
        self.assertEqual(b['web_channel_authorization'],parent['web_channel_authorization'])
        self.assertEqual(sum(a=='create' for a,_,_ in self.fork_driver.actions),2)
        self.assertTrue(all(name in {None,'isolated'} for _,name,_ in self.fork_driver.actions))
        self.assertEqual(bridge.binding_path(self.p.path,'test').read_bytes(),self.parent_before)
        self.assertEqual(self.grant_file.read_bytes(),self.grant_before);self.assert_stops_unchanged()
        self.assertEqual(before,[self.p.task(x) for x in self.tasks]);self.allowed()
        with self.assertRaises(xp.legacy.StopRun):
            with identity.account_lease(PROFILE,UID):pass

    def test_successful_intent_is_idempotent_without_browser_calls(self):
        self.prepare();self.fork();second=ForkDriver()
        result=self.fork(second);self.assertTrue(result['duplicate']);self.assertEqual(second.actions,[])
        with self.assertRaisesRegex(bridge.BridgeError,'FORK_INTENT_ALREADY_CONSUMED'):self.fork(name='other')

    def test_uncertain_create_cannot_retry_or_change_name(self):
        self.prepare();d=ForkDriver('unknown')
        with self.assertRaisesRegex(bridge.BridgeError,'COMMAND_OUTCOME_UNKNOWN'):self.fork(d)
        b=bridge.load_binding(self.p.path,'isolated');self.assertEqual(b['tabs']['search']['state'],'creation_pending')
        with self.assertRaisesRegex(bridge.BridgeError,'FORK_OUTCOME_REQUIRES_DIAGNOSIS'):self.fork(d)
        with self.assertRaisesRegex(bridge.BridgeError,'FORK_INTENT_ALREADY_CONSUMED'):self.fork(d,name='other')
        with self.assertRaisesRegex(bridge.BridgeError,'FORK_PRIOR_OUTCOME_REQUIRES_DIAGNOSIS'):self.fork(d,name='other',intent='different-intent')
        self.assertEqual(sum(a=='create' for a,_,_ in d.actions),1);self.assert_stops_unchanged()

    def test_mutable_parent_tabs_and_page_fault_do_not_change_authority(self):
        self.prepare();self.fork();b=bridge.load_binding(self.p.path,'test')
        b['tabs']['detail']['page']='e'*32;b['tabs']['detail']['last_url']=HOME+'/'+'e'*24
        b['last_error']={'code':'SCREENSHOT_TARGET_NOT_VISIBLE','at':xp.legacy.now()};b['state']='blocked'
        bridge.save_binding(self.p.path,'test',b);self.allowed();self.assert_stops_unchanged()

    def test_completed_fork_later_page_fault_does_not_block_independent_fork(self):
        self.prepare();self.fork();b=bridge.load_binding(self.p.path,'isolated')
        b['state']='blocked';b['last_error']={'code':'SCREENSHOT_TARGET_NOT_VISIBLE','at':xp.legacy.now()}
        bridge.save_binding(self.p.path,'isolated',b);old=bridge.binding_path(self.p.path,'isolated').read_bytes()
        result=self.fork(ForkDriver(page_offset=2),name='another',intent='another-explicit-intent')
        self.assertEqual(result['state'],'ready');self.allowed('another')
        self.assertEqual(bridge.binding_path(self.p.path,'isolated').read_bytes(),old);self.assert_stops_unchanged()

    def test_changed_parent_grant_or_authority_invalidates_child(self):
        self.prepare();self.fork();b=bridge.load_binding(self.p.path,'test')
        for key,value in [('context_id','other-context'),('runtime',{'other':'runtime'}),('authorization','changed'),
                          ('profile_guard_key','9'*64),('session','other-parent-session'),('account_uid','8'*24)]:
            with self.subTest(key=key):
                changed=copy.deepcopy(b);changed[key]=value;bridge.save_binding(self.p.path,'test',changed);self.blocked()
                bridge.save_binding(self.p.path,'test',b);self.allowed()
        self.grant_file.write_bytes(self.grant_before+b' ');self.blocked()
        self.grant_file.write_bytes(self.grant_before);self.allowed()

    def test_old_web_authorization_is_revalidated_not_regranted(self):
        self.prepare(web_authorization=True);self.fork()
        path=self.p.path/'bridge/web-channel-authorization.json';raw=path.read_bytes()
        path.write_bytes(raw+b' ');self.blocked();path.write_bytes(raw);self.allowed()

    def test_any_new_stop_blocks_both_paths_without_clear(self):
        self.prepare();self.fork();path=identity.guard_directory(self.keys[0])/'STOP.json';raw=path.read_bytes()
        path.write_bytes(raw+b' ');self.blocked();self.blocked('test');path.write_bytes(raw);self.allowed()
        identity.associate(UID,'8'*64);self.blocked();self.blocked('test')

    def test_invalid_parent_or_pause_has_zero_browser_calls(self):
        self.prepare();self.grant_file.write_bytes(self.grant_before+b' ');d=ForkDriver()
        with self.assertRaises(xp.legacy.ResearchError):self.fork(d)
        self.assertEqual(d.actions,[]);self.assertFalse(bridge.binding_path(self.p.path,'isolated').exists())
        self.grant_file.write_bytes(self.grant_before);(self.p.path/'bridge/PAUSE').write_text('paused')
        with self.assertRaisesRegex(bridge.BridgeError,'USER_PAUSED'):self.fork(d)
        self.assertEqual(d.actions,[]);self.assert_stops_unchanged()

    def test_wrong_uid_or_actual_target_never_gets_ready(self):
        self.prepare()
        d=ForkDriver('wrong_uid')
        with self.assertRaisesRegex(bridge.BridgeError,'ACCOUNT_MISMATCH'):self.fork(d)
        self.blocked();self.assert_stops_unchanged()

    def test_wrong_actual_target_never_gets_ready(self):
        self.prepare();d=ForkDriver('wrong_target')
        with self.assertRaisesRegex(bridge.BridgeError,'FORK_ACTUAL_HOME_TARGET_REQUIRED'):self.fork(d)
        self.blocked();self.assert_stops_unchanged()

    def test_reused_registered_page_rejected_before_reading_that_page(self):
        self.prepare();b=bridge.load_binding(self.p.path,'test');b['tabs']['search']={'page':'C'*32,'session':'old-search'}
        bridge.save_binding(self.p.path,'test',b);d=ForkDriver('reused')
        with self.assertRaisesRegex(bridge.BridgeError,'FORK_TARGET_OR_SESSION_REUSED'):self.fork(d)
        self.assertEqual([a for a,_,_ in d.actions],['profiles','create']);self.assert_stops_unchanged()

    def test_new_access_warning_keeps_original_evidence_but_writes_new_stop(self):
        self.prepare()
        with self.assertRaises(xp.legacy.StopRun):self.fork(ForkDriver('access'))
        self.blocked();self.blocked('test')
        self.assertEqual(self.grant_file.read_bytes(),self.grant_before)
        for key,raw in self.stops.items():self.assertNotEqual((identity.guard_directory(key)/'STOP.json').read_bytes(),raw)

    def test_child_home_or_auth_tampering_and_stripped_marker_fail_closed(self):
        self.prepare();self.fork();original=bridge.load_binding(self.p.path,'isolated')
        for key in ('web_parent_authorization','binding_kind','fork_home_verification'):
            changed=copy.deepcopy(original);changed.pop(key);bridge.save_binding(self.p.path,'isolated',changed)
            self.blocked();bridge.save_binding(self.p.path,'isolated',original);self.allowed()
        changed=copy.deepcopy(original);changed['tabs']['detail']['page']='d'*32
        bridge.save_binding(self.p.path,'isolated',changed);self.blocked()
        bridge.save_binding(self.p.path,'isolated',original)
        changed=copy.deepcopy(original);changed['note_unavailable_recovery']=bridge.load_binding(self.p.path,'test')['note_unavailable_recovery']
        bridge.save_binding(self.p.path,'isolated',changed);self.blocked()
        bridge.save_binding(self.p.path,'isolated',original)
        home=Path(original['fork_home_verification']['file']);home.write_bytes(home.read_bytes()+b' ');self.blocked()

    def test_in_memory_context_or_target_cannot_bypass_disk_chain(self):
        self.prepare();self.fork();b=bridge.load_binding(self.p.path,'isolated')
        for key,value in [('session','foreign-session'),('runtime',{'foreign':True})]:
            changed=copy.deepcopy(b);changed[key]=value;self.blocked(binding=changed)
        changed=copy.deepcopy(b);changed['tabs']['search']['page']='e'*32;self.blocked(binding=changed)

    def test_recursive_delegation_forbidden(self):
        self.prepare();self.fork();d=ForkDriver()
        with self.assertRaisesRegex(bridge.BridgeError,'FORK_RECURSION_FORBIDDEN'):
            bridge.fork_binding(self.p.path,'isolated','grandchild','second-attempt','Separate public tabs',d)
        self.assertEqual(d.actions,[]);self.assert_stops_unchanged()

    def test_missing_child_role_cannot_create_with_old_home_evidence(self):
        self.prepare();self.fork();b=bridge.load_binding(self.p.path,'isolated');b['tabs'].pop('detail');d=ForkDriver()
        with self.assertRaisesRegex(bridge.BridgeError,'FORK_HOME_REVERIFICATION_REQUIRED'):
            bridge.provision_role(self.p.path,'isolated',b,d,'detail')
        self.assertEqual(d.actions,[]);self.assert_stops_unchanged()

    def failed_initialization(self):
        self.prepare();d=InitializingDriver()
        with self.assertRaisesRegex(bridge.BridgeError,'SCOPE_CHANGED'):self.fork(d)
        b=bridge.load_binding(self.p.path,'isolated')
        self.assertNotIn('verified_at',b['tabs']['search']);self.assertEqual(set(b['tabs']),{'search'})
        return d,b

    def test_same_intent_resumes_known_home_without_recreating_search(self):
        d,b=self.failed_initialization();old_page=b['tabs']['search']['page'];old_session=b['session']
        pointer=copy.deepcopy(b['web_parent_authorization']);record_bytes=Path(pointer['file']).read_bytes()
        diagnostics=[json.loads(Path(x['file']).read_text()) for x in b['initialization_observations']]
        self.assertTrue(any(x.get('result',{}).get('url')=='chrome://newtab/' for x in diagnostics))
        d.ready=True;result=self.fork(d);current=bridge.load_binding(self.p.path,'isolated')
        self.assertTrue(result['resumed_initialization']);self.assertEqual(current['session'],old_session)
        self.assertEqual(current['tabs']['search']['page'],old_page)
        self.assertEqual(sum(a=='create' and r=='search' for a,_,r in d.actions),1)
        self.assertEqual(sum(a=='create' and r=='detail' for a,_,r in d.actions),1)
        self.assertEqual(current['web_parent_authorization'],pointer);self.assertEqual(Path(pointer['file']).read_bytes(),record_bytes)
        self.assertEqual(current['resolved_initialization_errors'][0]['code'],'SCOPE_CHANGED');self.allowed()
        self.assertEqual(bridge.binding_path(self.p.path,'test').read_bytes(),self.parent_before);self.assert_stops_unchanged()

    def test_resume_different_current_url_stops_before_identity_or_create(self):
        d,b=self.failed_initialization();d.ready=True;d.resume_url=HOME+'/'+'e'*24;before=len(d.actions)
        with self.assertRaisesRegex(bridge.BridgeError,'FORK_BOOTSTRAP_TARGET_CHANGED'):self.fork(d)
        self.assertEqual([a for a,_,_ in d.actions[before:]],['profiles','metadata'])
        current=bridge.load_binding(self.p.path,'isolated')
        last=json.loads(Path(current['initialization_observations'][-1]['file']).read_text())
        self.assertEqual(last['result']['url'],d.resume_url);self.blocked();self.assert_stops_unchanged()

    def test_resume_wrong_uid_does_not_create_detail_or_clear_failure(self):
        d,b=self.failed_initialization();d.ready=True;d.fail='wrong_uid';before=len(d.actions)
        with self.assertRaisesRegex(bridge.BridgeError,'ACCOUNT_MISMATCH'):self.fork(d)
        self.assertNotIn('create',[a for a,_,_ in d.actions[before:]]);self.blocked();self.assert_stops_unchanged()
        self.assertEqual(bridge.load_binding(self.p.path,'isolated')['last_error']['code'],'ACCOUNT_MISMATCH')

    def test_resume_recorded_target_change_rejected_without_browser(self):
        d,b=self.failed_initialization();b['tabs']['search']['page']='f'*32;bridge.save_binding(self.p.path,'isolated',b)
        d.ready=True;before=len(d.actions)
        with self.assertRaisesRegex(bridge.BridgeError,'FORK_BOOTSTRAP_TARGET_CHANGED'):self.fork(d)
        self.assertEqual(len(d.actions),before);self.assert_stops_unchanged()

    def test_resume_rechecks_stops_before_any_browser_call(self):
        d,b=self.failed_initialization();d.ready=True;before=len(d.actions)
        path=identity.guard_directory(self.keys[0])/'STOP.json';path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(xp.legacy.StopRun):self.fork(d)
        self.assertEqual(len(d.actions),before)
        self.assertEqual(bridge.load_binding(self.p.path,'isolated')['last_error']['code'],'SCOPE_CHANGED')


if __name__=='__main__':unittest.main()
