import copy
import unittest
from unittest.mock import patch
import xhs_bridge as bridge
import xhs_note_link_return as helper
import xhs_manual_tab_recovery as manual
import test_source404_recovery as fixture


class Tests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ManualTests(methodName='runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.p=self.f.p.path;self.name=self.f.name;self.driver=self.f.driver
        self.b=bridge.load_binding(self.p,self.name);self.b['last_error']['code']='SCOPE_CHANGED'
        bridge.save_binding(self.p,self.name,self.b)
        self.error='https://www.xiaohongshu.com/404?source=note&noteId='+fixture.BAD+'&errorCode=-510001&xsec_token=original-observed'
        self.driver.url=self.error
        self.observation=self.f.fixture.put('link-failure.json',dict(status='failed',requested_url=fixture.BAD_URL,metadata={'page':self.b['tabs']['detail']['page'],'url':self.error}))
        self.navigations=[]
        sleeper=patch.object(helper.time,'sleep');sleeper.start();self.addCleanup(sleeper.stop)
    def navigate(self,b,error,failed,target):
        self.navigations.append(target);self.driver.url=target;return {'navigationSubmitted':True}
    def run_return(self,navigator=None):return helper.restore(self.p,self.name,self.observation,'Authorized continued public reading',navigator or self.navigate,self.driver)
    def test_success_retains_authority_stops_and_failed_source(self):
        result=self.run_return();self.assertEqual(len(self.navigations),1);self.f.unchanged()
        b=bridge.load_binding(self.p,self.name);self.assertEqual(b['state'],'ready');self.assertEqual(manual.authority(b),manual.authority(self.b))
        self.assertIn(fixture.BAD_URL,b['failed_note_links']);self.assertFalse(result['body_read'])
        with bridge.lease(self.p,b):pass
    def test_unknown_navigation_not_replayed(self):
        def nav(*args):self.navigations.append('unknown');raise bridge.BridgeError('COMMAND_OUTCOME_UNKNOWN')
        with self.assertRaises(bridge.BridgeError):self.run_return(nav)
        with self.assertRaises(bridge.BridgeError):self.run_return(nav)
        self.assertEqual(len(self.navigations),1);self.f.unchanged()
    def test_access_error_keeps_blocked(self):
        def nav(*args):raise bridge.BridgeError('ACCESS_STOP')
        with self.assertRaises(bridge.legacy.StopRun):self.run_return(nav)
        self.assertEqual(bridge.load_binding(self.p,self.name)['state'],'blocked')
        self.assertEqual(self.navigations,[])
    def test_error_changed_no_navigation(self):
        self.driver.url='https://www.xiaohongshu.com/explore'
        with self.assertRaisesRegex(bridge.BridgeError,'CURRENT_ERROR_CHANGED'):self.run_return()
        self.assertEqual(self.navigations,[])
    def test_current_exact_home_uses_ordinary_identity_and_navigation(self):
        self.driver.url='https://www.xiaohongshu.com/explore?source=404'
        result=self.run_return();self.assertFalse(result['body_read']);self.assertEqual(self.navigations,[])
        self.assertEqual(self.driver.calls.count('navigate'),1);self.f.unchanged()
    def test_bounded_home_continuation_only_after_no_navigation(self):
        self.driver.url='https://www.xiaohongshu.com/explore'
        with self.assertRaises(bridge.BridgeError):self.run_return()
        self.driver.url='https://www.xiaohongshu.com/explore?source=404'
        helper.restore(self.p,self.name,self.observation,'Already authorized',self.navigate,self.driver,resume_home=True)
        self.assertEqual(self.driver.calls.count('navigate'),1);self.f.unchanged()


if __name__=='__main__':unittest.main()
