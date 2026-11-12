"""Bounded read-only recovery of a newly created exact owned target."""
import copy
import unittest
from unittest.mock import patch
import xhs_bridge as bridge

HOME='https://www.xiaohongshu.com/explore'
PAGE='owned-page'

class Driver:
    def __init__(self,meta=None):
        self.actions=[];self.meta=meta or {'page':PAGE,'url':HOME}
    def call(self,action,b=None,role=None,**kwargs):
        self.actions.append(action)
        if action=='create':return {'page':PAGE}
        if action=='metadata':return copy.deepcopy(self.meta)
        raise AssertionError('Unexpected mutation/content operation: '+action)

class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.binding={'session':'project-role','tabs':{'detail':{'page':PAGE,'owned':True,'state':'created','last_url':HOME}}}
        self.driver=Driver()
        self.save=patch.object(bridge,'save_binding');self.save.start()
        self.sleep=patch.object(bridge.time,'sleep');self.sleeper=self.sleep.start()
    def tearDown(self):self.sleep.stop();self.save.stop()
    def run_case(self,effects):
        with patch.object(bridge,'verify_tab',side_effect=effects) as verify:
            result=bridge.provision_role('/unused','test',self.binding,self.driver,'detail')
        return result,verify.call_count
    def test_existing_unverified_scope_transition_rechecks_same_id(self):
        result,count=self.run_case([bridge.BridgeError('SCOPE_CHANGED'),{'page':PAGE}])
        self.assertEqual(count,2);self.assertEqual(result,{'page':PAGE})
        self.assertEqual(self.driver.actions,['metadata']);self.sleeper.assert_called_once_with(1)
    def test_new_target_created_only_once_then_waits(self):
        self.binding['tabs']={}
        _,count=self.run_case([bridge.BridgeError('SCOPE_CHANGED'),{'page':PAGE}])
        self.assertEqual(count,2);self.assertEqual(self.driver.actions,['create','metadata'])
        self.assertTrue(self.binding['tabs']['detail']['owned'])
    def test_initial_blank_and_root_urls_only_wait(self):
        for url in ['about:blank','https://www.xiaohongshu.com','https://www.xiaohongshu.com/',HOME]:
            with self.subTest(url=url):
                self.driver=Driver({'page':PAGE,'url':url})
                _,count=self.run_case([bridge.BridgeError('SCOPE_CHANGED'),{'page':PAGE}])
                self.assertEqual(count,2);self.assertEqual(self.driver.actions,['metadata'])
    def test_four_checks_is_hard_limit_and_no_recreation(self):
        with patch.object(bridge,'verify_tab',side_effect=bridge.BridgeError('SCOPE_CHANGED')) as verify:
            with self.assertRaisesRegex(bridge.BridgeError,'SCOPE_CHANGED'):
                bridge.provision_role('/unused','test',self.binding,self.driver,'detail')
        self.assertEqual(verify.call_count,4);self.assertEqual(self.driver.actions,['metadata']*3)
        self.assertEqual(self.sleeper.call_count,3)
        self.assertNotIn('verified_at',self.binding['tabs']['detail'])
    def test_private_or_other_public_url_never_retried(self):
        for url in ['https://example.com/private','https://www.xiaohongshu.com/chat','https://www.xiaohongshu.com/explore/'+'a'*24,HOME+'?unexpected=1']:
            with self.subTest(url=url):
                self.driver=Driver({'page':PAGE,'url':url})
                with patch.object(bridge,'verify_tab',side_effect=bridge.BridgeError('SCOPE_CHANGED')) as verify:
                    with self.assertRaisesRegex(bridge.BridgeError,'SCOPE_CHANGED'):
                        bridge.provision_role('/unused','test',self.binding,self.driver,'detail')
                self.assertEqual(verify.call_count,1);self.assertEqual(self.driver.actions,['metadata'])
        self.sleeper.assert_not_called()
    def test_different_exact_target_never_retried(self):
        self.driver=Driver({'page':'other','url':HOME})
        with patch.object(bridge,'verify_tab',side_effect=bridge.BridgeError('SCOPE_CHANGED')) as verify:
            with self.assertRaises(bridge.BridgeError):bridge.provision_role('/unused','test',self.binding,self.driver,'detail')
        self.assertEqual(verify.call_count,1);self.sleeper.assert_not_called()
    def test_verified_or_not_owned_or_navigation_intent_not_initialized(self):
        for update in [{'verified_at':'earlier'},{'owned':False},{'state':'creation_pending'},{'navigation_intent':{'url':HOME}},{'last_url':'https://www.xiaohongshu.com/explore/'+'a'*24}]:
            with self.subTest(update=update):
                original=copy.deepcopy(self.binding);self.binding['tabs']['detail'].update(update)
                with patch.object(bridge,'verify_tab',side_effect=bridge.BridgeError('SCOPE_CHANGED')) as verify:
                    with self.assertRaises(bridge.BridgeError):bridge.provision_role('/unused','test',self.binding,self.driver,'detail')
                self.assertEqual(verify.call_count,1);self.binding=original
        self.assertEqual(self.driver.actions,[]);self.sleeper.assert_not_called()
    def test_access_account_missing_or_target_failures_are_not_retried(self):
        for code in ['ACCESS_STOP','ACCOUNT_MISMATCH','TARGET_MISSING','TARGET_MISMATCH','UNEXPECTED_TARGET_CHANGE']:
            with self.subTest(code=code):
                with patch.object(bridge,'verify_tab',side_effect=bridge.BridgeError(code)) as verify:
                    with self.assertRaisesRegex(bridge.BridgeError,code):bridge.provision_role('/unused','test',self.binding,self.driver,'detail')
                self.assertEqual(verify.call_count,1)
        self.assertEqual(self.driver.actions,[]);self.sleeper.assert_not_called()
    def test_identity_or_content_initial_wait_is_bounded(self):
        _,count=self.run_case([bridge.BridgeError('CONTENT_LOADING'),bridge.BridgeError('IDENTITY_UNCONFIRMED'),{'page':PAGE}])
        self.assertEqual(count,3);self.assertEqual(self.driver.actions,[])
    def test_pending_unknown_creation_is_not_repeated(self):
        self.binding['tabs']['detail'].pop('page')
        with patch.object(bridge,'verify_tab') as verify:
            with self.assertRaisesRegex(bridge.BridgeError,'TAB_CREATE_OUTCOME_UNKNOWN'):
                bridge.provision_role('/unused','test',self.binding,self.driver,'detail')
        verify.assert_not_called();self.assertEqual(self.driver.actions,[])

if __name__=='__main__':unittest.main()
