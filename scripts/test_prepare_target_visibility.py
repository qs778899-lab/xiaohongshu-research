"""Same-role selection is bounded and never substitutes for actual visibility."""
import copy
import unittest
from unittest.mock import patch

import test_bridge as fixture
import xhs_bridge as bridge
import xhs_project as xp

MISSING=object()


class VisibilityDriver(fixture.Fake):
    def __init__(self,visibility,**kwargs):
        super().__init__(**kwargs);self.visibility=list(visibility);self.activation_error=None
        self.after_activation_url=None;self.after_activation_uid=None

    def call(self,action,binding=None,role=None,**fields):
        if action=='activate':
            self.calls.append((action,binding['context_id'],role,fields))
            if self.activation_error:raise bridge.BridgeError(self.activation_error)
            if self.after_activation_url:self.url=self.after_activation_url
            if self.after_activation_uid:self.uid=self.after_activation_uid
            return {'activated':binding['tabs'][role]['page'],'windowFocusRequested':False}
        value=super().call(action,binding,role,**fields)
        if action=='identity':
            if not self.visibility:raise AssertionError('Unexpected repeated verification')
            visible=self.visibility.pop(0)
            if isinstance(visible,Exception):raise visible
            if visible is MISSING:value.pop('visible')
            else:value['visible']=visible
        return value


class PrepareVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BridgeTests();self.f.setUp();self.p=self.f.project
        self.b=self.f.binding;self.b['tabs']['detail']['last_url']=fixture.URL
        self.task=self.p.task(self.f.tid)
        self.save=patch.object(bridge,'save_binding');self.save.start()
        self.sleep=patch.object(bridge.time,'sleep');self.sleep.start()

    def tearDown(self):self.sleep.stop();self.save.stop();self.f.tearDown()
    def prepare(self,driver):return bridge.prepare_target(self.p,self.b,driver,self.task)
    def actions(self,driver):return [c[0] for c in driver.calls]

    def test_visible_role_needs_no_activation(self):
        driver=VisibilityDriver([True]);url,role,args=self.prepare(driver)
        self.assertEqual((url,role),(fixture.URL,'detail'))
        self.assertEqual(self.actions(driver),['metadata','identity'])

    def test_hidden_role_selects_exact_existing_role_once_then_rechecks(self):
        driver=VisibilityDriver([False,True]);self.prepare(driver)
        self.assertEqual(self.actions(driver),['metadata','identity','activate','metadata','identity'])
        self.assertEqual(driver.calls[2][1:3],(self.b['context_id'],'detail'))
        self.assertEqual(self.b['tabs']['detail']['page'],'p1')

    def test_still_hidden_is_local_gap_without_collect_scroll_reload(self):
        driver=VisibilityDriver([False,False])
        with self.assertRaisesRegex(bridge.BridgeError,'^TARGET_NOT_VISIBLE$'):self.prepare(driver)
        self.assertEqual(self.actions(driver),['metadata','identity','activate','metadata','identity'])
        self.assertIn('TARGET_NOT_VISIBLE',bridge.LOCAL_FAULTS)

    def test_unknown_visibility_never_activates_or_becomes_true(self):
        for visible in [MISSING,None,0,1,'false','true',{},[]]:
            with self.subTest(value=repr(visible)):
                driver=VisibilityDriver([visible])
                with self.assertRaisesRegex(bridge.BridgeError,'^TARGET_VISIBILITY_UNCONFIRMED$'):self.prepare(driver)
                self.assertEqual(self.actions(driver),['metadata','identity'])
        self.assertIn('TARGET_VISIBILITY_UNCONFIRMED',bridge.LOCAL_FAULTS)

    def test_post_selection_unknown_is_not_success(self):
        driver=VisibilityDriver([False,MISSING])
        with self.assertRaisesRegex(bridge.BridgeError,'^TARGET_VISIBILITY_UNCONFIRMED$'):self.prepare(driver)
        self.assertEqual(self.actions(driver).count('activate'),1)

    def test_account_mismatch_before_selection_stops_immediately(self):
        driver=VisibilityDriver([False],uid='9'*24)
        with self.assertRaisesRegex(bridge.BridgeError,'^ACCOUNT_MISMATCH$'):self.prepare(driver)
        self.assertNotIn('activate',self.actions(driver))

    def test_account_mismatch_after_selection_does_not_retry(self):
        driver=VisibilityDriver([False,True]);driver.after_activation_uid='9'*24
        with self.assertRaisesRegex(bridge.BridgeError,'^ACCOUNT_MISMATCH$'):self.prepare(driver)
        self.assertEqual(self.actions(driver).count('activate'),1)

    def test_other_target_after_selection_fails_before_second_identity(self):
        driver=VisibilityDriver([False]);driver.after_activation_url='https://www.xiaohongshu.com/explore/'+'9'*24
        with self.assertRaisesRegex(bridge.BridgeError,'^UNEXPECTED_TARGET_CHANGE$'):self.prepare(driver)
        self.assertEqual(self.actions(driver),['metadata','identity','activate','metadata'])

    def test_access_stop_after_selection_is_preserved(self):
        driver=VisibilityDriver([False,bridge.BridgeError('ACCESS_STOP')])
        with self.assertRaisesRegex(bridge.BridgeError,'^ACCESS_STOP$'):self.prepare(driver)
        self.assertEqual(self.actions(driver).count('activate'),1)
        self.assertNotIn('ACCESS_STOP',bridge.LOCAL_FAULTS)

    def test_uncertain_selection_is_not_replayed(self):
        driver=VisibilityDriver([False]);driver.activation_error='COMMAND_OUTCOME_UNKNOWN'
        with self.assertRaisesRegex(bridge.BridgeError,'^COMMAND_OUTCOME_UNKNOWN$'):self.prepare(driver)
        self.assertEqual(self.actions(driver),['metadata','identity','activate'])

    def query_change(self,visibility):
        old='https://www.xiaohongshu.com/search_result?keyword=old&type=51'
        new='https://www.xiaohongshu.com/search_result?keyword=new&type=51'
        self.b['tabs']['search']={'page':'s1','owned':True,'session':'xhs-test-search','last_url':old}
        self.task={'payload':{'stage':'search','target':'new','sort':'popular'}}
        driver=VisibilityDriver(visibility,url=old)
        url,role,args=self.prepare(driver)
        self.assertEqual((url,role,args['query']),(new,'search','new'))
        self.assertTrue(all(call[2]=='search' for call in driver.calls))
        return driver

    def test_navigation_uses_final_hidden_document_then_selects(self):
        driver=self.query_change([True,False,True])
        self.assertEqual(self.actions(driver),['metadata','identity','navigate','metadata','identity','activate','metadata','identity'])

    def test_navigation_old_hidden_new_visible_does_not_select(self):
        driver=self.query_change([False,True])
        self.assertEqual(self.actions(driver),['metadata','identity','navigate','metadata','identity'])

    def test_navigation_readiness_retry_uses_final_verified_meta(self):
        driver=self.query_change([True,bridge.BridgeError('CONTENT_LOADING'),False,True])
        self.assertEqual(self.actions(driver).count('navigate'),1)
        self.assertEqual(self.actions(driver).count('activate'),1)
        self.assertEqual(self.actions(driver).count('identity'),4)

    def test_navigation_unknown_final_visibility_is_gap(self):
        old='https://www.xiaohongshu.com/explore/'+'3'*24
        self.b['tabs']['detail']['last_url']=old
        driver=VisibilityDriver([True,MISSING],url=old)
        with self.assertRaisesRegex(bridge.BridgeError,'^TARGET_VISIBILITY_UNCONFIRMED$'):self.prepare(driver)
        self.assertNotIn('activate',self.actions(driver))

    def test_prepare_preserves_fault_cursor_stall_task_and_stop(self):
        task=self.p.task(self.f.tid);task['payload']['bridge']={
            'fault':{'code':'COMMENT_PAGINATION_STALLED','hold':True,'recovery':{'activate':'old','reload':'old','exhausted':True}},
            'stalled_count':3,'cursor':{'scrollTop':500},'repair_probes':{'prior':{'verified':False}}}
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task['id']))
        self.p.set_meta('stop',{'reason':'retain prior account stop'});self.p.db.commit()
        before=copy.deepcopy(self.p.task(task['id']))
        self.task=before;driver=VisibilityDriver([False,True]);self.prepare(driver)
        self.assertEqual(self.p.task(task['id']),before)
        self.assertEqual(self.p.get_meta('stop'),{'reason':'retain prior account stop'})


if __name__=='__main__':unittest.main()
