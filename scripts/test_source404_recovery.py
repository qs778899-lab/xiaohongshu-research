"""Synthetic owned-home recovery with the real typed lease and ordinary guards."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_page_presence as fixture
import xhs_bridge as bridge
import xhs_native as native
import xhs_project as xp
import xhs_account_identity as identity
import xhs_source404_recovery as helper
import test_manual_tab_recovery as manual_fixture
import xhs_manual_tab_recovery as manual

BAD='9'*24
BAD_URL='https://www.xiaohongshu.com/explore/'+BAD+'?xsec_token=original-observed'


class Driver:
    def __init__(self,fail=None):self.calls=[];self.fail=fail;self.url=helper.HOME
    def call(self,action,b,role,**kwargs):
        self.calls.append(action);assert role=='detail';page=b['tabs'][role]['page']
        if self.fail=='access' and action=='identity':raise bridge.BridgeError('ACCESS_STOP')
        if action=='metadata':return {'page':page,'url':self.url if self.fail!='other_url' else helper.HOME+'&extra=1'}
        if action=='identity':return {'page':page,'url':self.url,'uid':b['account_uid'] if self.fail!='uid' else '8'*24,'visible':True}
        if action=='lease_status':return {'expectedPage':page,'matching':1,'leaseTabCount':2 if self.fail=='multiple' else 1,'tabs':[{'page':page,'url':self.url}]}
        if action=='navigate':
            self.url=kwargs['url']
            if self.fail=='unknown':raise bridge.BridgeError('COMMAND_OUTCOME_UNKNOWN')
            if self.fail=='timeout':raise TimeoutError('synthetic timeout')
            if self.fail=='interrupt':raise KeyboardInterrupt()
            return {'navigationSubmitted':True}
        raise AssertionError('Forbidden '+action)


class Tests(unittest.TestCase):
    def setUp(self):
        self.old=fixture.ForkPresenceTests(methodName='runTest');self.old.setUp();self.old.attest()
        self.p=self.old.p;self.root=self.old.root;self.name=self.old.name
        source=self.root/'source404-observed.json';source.write_text(json.dumps({'url':BAD_URL,'title':'Observed original candidate'}))
        manifest=self.root/'source404-manifest.json';manifest.write_text(json.dumps({'units':[{'stage':stage,'target':BAD,'sources':[str(source)],'next':'Read original observed note'} for stage in ['roots','replies']]}))
        self.tasks=native.import_manifest(self.p,manifest)['task_ids'];self.task=self.tasks[0]
        b=bridge.load_binding(self.p.path,self.name);b['state']='blocked';b['last_error']={'code':'SCOPE_CHANGED','at':xp.legacy.now()}
        b['tabs']['detail']['last_url']=fixture.fixture.URL;b['tabs']['detail']['verified_at']=xp.legacy.now()
        b['tabs']['detail']['navigation_intent']={'url':BAD_URL,'at':xp.legacy.now()}
        bridge.save_binding(self.p.path,self.name,b);self.before=copy.deepcopy(b);self.driver=Driver()
    def tearDown(self):self.old.tearDown()
    def restore(self,driver=None):return helper.restore(self.p.path,self.name,self.task,driver or self.driver)
    def failed(self,driver):
        with self.assertRaises(BaseException):self.restore(driver)
    def test_success_one_navigation_same_owned_target_and_source_hold(self):
        before_task=self.p.task(self.old.base.task);result=self.restore();b=bridge.load_binding(self.p.path,self.name)
        self.assertEqual(self.driver.calls,['metadata','identity','lease_status','navigate','metadata','identity'])
        self.assertEqual(b['state'],'ready');self.assertNotIn('navigation_intent',b['tabs']['detail'])
        self.assertEqual(set(result['held_tasks']),set(self.tasks));self.old.base.allowed();self.old.base.unchanged()
        self.assertEqual(before_task,self.p.task(self.old.base.task))
        for task_id in self.tasks:
            t=self.p.task(task_id);fault=t['payload']['bridge']['fault'];self.assertEqual(t['status'],'pending')
            self.assertTrue(fault['hold']);self.assertEqual(fault['cause'],'unknown');self.assertEqual(fault['source_url'],BAD_URL)
        attempt=next((self.p.path/'bridge/source404-recoveries').glob('*/attempt.json'));saved=json.loads(attempt.read_text())
        self.assertEqual(saved['binding']['tabs']['detail']['navigation_intent'],self.before['tabs']['detail']['navigation_intent'])
        self.assertEqual(saved['binding']['last_error'],self.before['last_error'])
    def test_fixed_attempt_not_reset_by_failure_timestamp(self):
        driver=Driver('other_url');self.failed(driver);self.assertEqual(driver.calls,['metadata'])
        b=copy.deepcopy(self.before);b['last_error']['at']=xp.legacy.now();bridge.save_binding(self.p.path,self.name,b)
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.restore(driver)
        self.assertEqual(driver.calls,['metadata'])
    def test_identity_mismatch_and_multiple_lease_never_navigate(self):
        for fail in ['uid','multiple']:
            test=Tests();test.setUp()
            try:
                driver=Driver(fail);test.failed(driver);self.assertNotIn('navigate',driver.calls);test.old.base.unchanged()
                self.assertFalse(test.p.task(test.task)['payload'].get('bridge',{}).get('fault',{}).get('hold',False))
            finally:test.tearDown()
    def test_uncertain_navigation_timeout_and_interrupt_never_repeat(self):
        for fail in ['unknown','timeout','interrupt']:
            test=Tests();test.setUp()
            try:
                driver=Driver(fail);test.failed(driver);self.assertEqual(driver.calls.count('navigate'),1)
                test.failed(driver);self.assertEqual(driver.calls.count('navigate'),1)
                self.assertTrue(test.p.task(test.task)['payload']['bridge']['fault']['hold']);test.old.base.unchanged()
            finally:test.tearDown()
    def test_real_access_guard_stops_account_and_preserves_old_stop_archive(self):
        driver=Driver('access');self.failed(driver);self.assertNotIn('navigate',driver.calls)
        self.assertEqual(bridge.load_binding(self.p.path,self.name)['last_error']['code'],'ACCESS_STOP')
        for key,old in self.old.base.stops.items():
            self.assertNotEqual((identity.guard_directory(key)/'STOP.json').read_bytes(),old)
        self.old.base.blocked()
    def test_new_stop_before_helper_fails_before_browser(self):
        file=identity.guard_directory(self.old.base.keys[0])/'STOP.json';file.write_bytes(file.read_bytes()+b' ')
        self.failed(self.driver);self.assertEqual(self.driver.calls,[])
    def test_missing_observed_source_or_unowned_role_fails_before_browser(self):
        b=bridge.load_binding(self.p.path,self.name);b['tabs']['detail']['owned']=False;bridge.save_binding(self.p.path,self.name,b)
        self.failed(self.driver);self.assertEqual(self.driver.calls,[])
    def test_unobserved_restore_target_fails_before_browser(self):
        b=bridge.load_binding(self.p.path,self.name);b['tabs']['detail']['last_url']='https://www.xiaohongshu.com/explore/'+'6'*24
        bridge.save_binding(self.p.path,self.name,b);self.failed(self.driver);self.assertEqual(self.driver.calls,[])
    def test_exact_home_variants_do_not_qualify(self):
        self.failed(Driver('other_url'))
        self.assertFalse(self.p.task(self.task)['payload'].get('bridge',{}).get('fault',{}).get('hold',False))

class ManualDriver(Driver):
    def call(self,action,b,role,**kwargs):
        if action=='locate_registered_url':
            self.calls.append(action)
            slot=b['tabs'][role]
            return {'matches':[{'tabId':slot['chrome_tab_id']+(1 if self.fail=='tab' else 0),
                'windowId':slot['window_id']+(1 if self.fail=='window' else 0),'url':self.url}]}
        if action=='capture':
            self.calls.append(action)
            if self.fail=='capture_access':raise bridge.BridgeError('ACCESS_STOP')
            return {'uid':b['account_uid'],'url':self.url,'title':'Synthetic restored note',
                    'body':'' if self.fail=='empty' else 'Actual synthetic narrative.',
                    'authors':[{'name':'Example author'}]}
        return super().call(action,b,role,**kwargs)


class ManualTests(unittest.TestCase):
    def setUp(self):
        self.fixture=manual_fixture.ManualTabTests(methodName='runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        # Build an actual typed manual grant and actual account STOPs, not a mocked lease.
        f.parent['profile_guard_key']='c'*64
        xp.legacy.account_root().mkdir(exist_ok=True)
        self.keys=identity.related_keys(f.parent['profile_guard_key'],f.uid)
        self.stops={}
        f.stops={}
        for key in self.keys:
            path=identity.guard_directory(key)/'STOP.json';path.write_bytes(f.stop.read_bytes())
            self.stops[key]=path.read_bytes()
            f.stops[key]=dict(manual.pointer(path),at='2026-11-11T00:00:00+00:00')
        _,b=f.create_grant_fixture()
        self.p=xp.Project(f.p,create=True);self.addCleanup(self.p.close);self.name='manual'
        src=f.put('failed-source.json',{'url':BAD_URL})
        manifest=f.put('tasks.json',{'units':[{'stage':s,'target':BAD,'sources':[str(src)],'next':'Read'} for s in ['roots','replies']]})
        self.tasks=native.import_manifest(self.p,manifest)['task_ids'];self.task=self.tasks[0]
        b.update(state='blocked',last_error={'code':'UNEXPECTED_TARGET_CHANGE','at':xp.legacy.now()})
        b['tabs']['detail'].update(verified_at=xp.legacy.now(),navigation_intent={'url':BAD_URL})
        bridge.save_binding(f.p,self.name,b);self.before=copy.deepcopy(b);self.driver=ManualDriver()
        sleeper=patch.object(helper.time,'sleep');sleeper.start();self.addCleanup(sleeper.stop)
    def restore(self):return helper.restore(self.p.path,self.name,self.task,self.driver)
    def unchanged(self):
        for key,raw in self.stops.items():self.assertEqual((identity.guard_directory(key)/'STOP.json').read_bytes(),raw)
    def test_manual_same_tab_navigates_once_captures_body_without_marking_read(self):
        result=self.restore();b=bridge.load_binding(self.p.path,self.name)
        self.assertEqual(self.driver.calls,['metadata','identity','locate_registered_url','navigate','metadata','identity','locate_registered_url','capture'])
        self.assertEqual(b['state'],'ready');self.assertEqual(b['tabs']['detail'],b['tabs']['search'])
        self.assertEqual(manual.authority(b),manual.authority(self.before));self.unchanged()
        self.assertFalse(result['body_read']);self.assertTrue(result['no_tasks_completed'])
        self.assertEqual(manual.check_pointer(result['capture'])['result']['body'],'Actual synthetic narrative.')
        for t in self.tasks:self.assertTrue(self.p.task(t)['payload']['bridge']['fault']['hold'])
        with bridge.lease(self.p.path,b):pass
        with self.assertRaises(xp.legacy.StopRun):
            with identity.account_lease(b['profile_guard_key'],b['account_uid']):pass
    def test_changed_tab_window_or_uid_never_navigates(self):
        for fail in ['tab','window','uid']:
            with self.subTest(fail=fail):
                case=ManualTests();case.setUp()
                try:
                    case.driver.fail=fail
                    with self.assertRaises(bridge.BridgeError):case.restore()
                    self.assertNotIn('navigate',case.driver.calls);case.unchanged()
                finally:case.doCleanups()
    def test_empty_restored_body_keeps_blocked_and_cannot_renavigate(self):
        self.driver.fail='empty'
        with self.assertRaisesRegex(bridge.BridgeError,'NOTE_CONTENT'):self.restore()
        self.assertEqual(bridge.load_binding(self.p.path,self.name)['state'],'blocked')
        with self.assertRaises(bridge.BridgeError):self.restore()
        self.assertEqual(self.driver.calls.count('navigate'),1);self.unchanged()
    def test_new_stop_or_tampered_grant_blocks_before_browser(self):
        path=identity.guard_directory(self.keys[0])/'STOP.json';path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(xp.legacy.StopRun):self.restore()
        self.assertEqual(self.driver.calls,[])
    def test_tampered_grant_blocks_before_browser(self):
        path=Path(self.before['manual_tab_revalidation']['file']);path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(xp.legacy.StopRun):self.restore()
        self.assertEqual(self.driver.calls,[])
    def test_actual_access_error_stops_account(self):
        self.driver.fail='capture_access'
        with self.assertRaises(xp.legacy.StopRun):self.restore()
        self.assertEqual(bridge.load_binding(self.p.path,self.name)['last_error']['code'],'ACCESS_STOP')
        with self.assertRaises(xp.legacy.StopRun):
            with bridge.lease(self.p.path,bridge.load_binding(self.p.path,self.name)):pass
    def test_unknown_navigation_is_not_replayed(self):
        self.driver.fail='unknown'
        with self.assertRaises(bridge.BridgeError):self.restore()
        with self.assertRaises(bridge.BridgeError):self.restore()
        self.assertEqual(self.driver.calls.count('navigate'),1);self.unchanged()
    def test_other_scope_redirect_cannot_navigate(self):
        self.driver.url='https://www.xiaohongshu.com/explore'
        with self.assertRaisesRegex(bridge.BridgeError,'EXACT_HOME'):self.restore()
        self.assertNotIn('navigate',self.driver.calls)
    def imported_target(self,uid):
        b=bridge.load_binding(self.p.path,self.name)
        url='https://www.xiaohongshu.com/explore/'+'4'*24+'?xsec_token=synthetic'
        b['tabs']['detail']['last_url']=url;bridge.save_binding(self.p.path,self.name,b)
        raw=json.dumps({'stage':'body','url':url,'uid':uid,'title':'Archived synthetic note','body':'Original text.'}).encode()
        directory=self.p.path/'native-sources';directory.mkdir(exist_ok=True)
        (directory/hashlib.sha256(raw).hexdigest()).write_bytes(raw)
    def test_imported_hash_bound_same_account_capture_is_valid_restore_source(self):
        self.imported_target(self.before['account_uid']);self.restore();self.unchanged()
        self.assertEqual(self.driver.calls.count('navigate'),1)
    def test_imported_other_account_capture_is_not_restore_source(self):
        self.imported_target('8'*24)
        with self.assertRaisesRegex(bridge.BridgeError,'RESTORE_SOURCE'):self.restore()
        self.assertEqual(self.driver.calls,[])
    def test_mixed_imported_archives_do_not_hide_valid_capture(self):
        self.imported_target(self.before['account_uid'])
        for raw in [b'plain archived text',b'\xff',b'[]',b'null',b'42']:
            (self.p.path/'native-sources'/hashlib.sha256(raw).hexdigest()).write_bytes(raw)
        self.restore();self.assertEqual(self.driver.calls.count('navigate'),1);self.unchanged()

if __name__=='__main__':unittest.main()
