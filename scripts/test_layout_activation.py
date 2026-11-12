"""Offline behavioral checks for one bounded owned-tab layout recovery."""
import copy
import json
import unittest
from unittest.mock import patch
from test_bridge import BridgeTests, Fake, UID
import xhs_bridge as b

class LayoutFake(Fake):
    def __init__(self, resolves=True, uid=UID, access_stop=False):
        super().__init__(uid=uid)
        self.resolves=resolves;self.activated=False;self.access_stop=access_stop
    def call(self, action, binding=None, role=None, **fields):
        if action=='capture':
            self.calls.append((action,None,role,fields))
            if self.access_stop:raise b.BridgeError('ACCESS_STOP')
            if not self.activated or not self.resolves:raise b.BridgeError('BACKGROUND_LAYOUT_UNSETTLED')
            return {'url':self.url,'uid':self.uid,'cards':[{'text':'new evidence'}]}
        if action=='activate':
            self.calls.append((action,None,role,fields));self.activated=True
            return {'activated':binding['tabs'][role]['page'],'windowFocusRequested':False}
        return super().call(action,binding,role,**fields)

class LayoutRecoveryTests(BridgeTests):
    def run_capture(self, driver, binding=None):
        with patch.object(b.time,'sleep',lambda _:None):
            return b.ready_capture(self.project,binding or self.binding,driver,'detail',{'backgroundText':True,'stage':'search'})
    def test_new_layout_can_be_read_only_after_bounded_activation(self):
        driver=LayoutFake();result=self.run_capture(driver)
        self.assertEqual(result['cards'][0]['text'],'new evidence')
        self.assertEqual(sum(x[0]=='activate' for x in driver.calls),1)
        self.assertNotIn('reload',[x[0] for x in driver.calls])
        self.assertEqual(sum(x[0]=='identity' for x in driver.calls),2)
    def test_unsettled_layout_remains_gap_and_persisted_attempt_is_not_replayed(self):
        driver=LayoutFake(resolves=False)
        with self.assertRaisesRegex(b.BridgeError,'BACKGROUND_LAYOUT_UNSETTLED'):self.run_capture(driver)
        saved=json.loads((self.project.path/'bridge/bindings/test.json').read_text())
        second=LayoutFake(resolves=False)
        with self.assertRaisesRegex(b.BridgeError,'BACKGROUND_LAYOUT_UNSETTLED'):self.run_capture(second,saved)
        self.assertNotIn('activate',[x[0] for x in second.calls])
        self.assertNotIn('reload',[x[0] for x in driver.calls+second.calls])
    def test_wrong_account_is_not_activated(self):
        driver=LayoutFake(uid='3'*24)
        with self.assertRaisesRegex(b.BridgeError,'ACCOUNT_MISMATCH'):self.run_capture(driver)
        self.assertNotIn('activate',[x[0] for x in driver.calls])
    def test_access_stop_does_not_trigger_recovery(self):
        driver=LayoutFake(access_stop=True)
        with self.assertRaisesRegex(b.BridgeError,'ACCESS_STOP'):self.run_capture(driver)
        self.assertNotIn('activate',[x[0] for x in driver.calls])
    def test_non_owned_tab_is_not_activated(self):
        self.binding['tabs']['detail']['owned']=False;driver=LayoutFake()
        with self.assertRaisesRegex(b.BridgeError,'BACKGROUND_LAYOUT_UNSETTLED'):self.run_capture(driver)
        self.assertNotIn('activate',[x[0] for x in driver.calls])
    def test_target_change_is_not_activated(self):
        self.binding['tabs']['detail']['last_url']='https://www.xiaohongshu.com/explore/'+'9'*24
        driver=LayoutFake()
        with self.assertRaisesRegex(b.BridgeError,'UNEXPECTED_TARGET_CHANGE'):self.run_capture(driver)
        self.assertNotIn('activate',[x[0] for x in driver.calls])

if __name__=='__main__':unittest.main()
