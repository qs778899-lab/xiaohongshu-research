"""Synthetic real manual-grant delegation; no network or personal data."""
import copy,json,unittest
from pathlib import Path
import xhs_bridge as bridge
import xhs_note_recovery as nr
import xhs_manual_tab_recovery as manual
import xhs_account_identity as identity
import xhs_project as xp
import test_source404_recovery as fixtures
from test_bridge_binding_fork import ForkDriver

class ManualForkDriver(ForkDriver):
    def __init__(self,parent,fail=None):super().__init__(fail,page_offset=4);self.parent=parent
    def call(self,action,b=None,role=None,**kw):
        if action=='profiles':
            self.actions.append((action,None,role));return {'daemonRunning':True,'profiles':[{'contextId':self.parent['context_id'],'extensionVersion':self.parent['extension_version']}]}
        result=super().call(action,b,role,**kw)
        if action=='identity':result['uid']='9'*24 if self.fail=='wrong_uid' else self.parent['account_uid']
        return result

class Tests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ManualTests(methodName='runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.project=self.f.p.path;self.parent=bridge.load_binding(self.project,self.f.name)
        self.before=bridge.binding_path(self.project,self.f.name).read_bytes()
    def fork(self,driver=None,name='independent',intent='one-explicit-intent'):
        self.driver=driver or ManualForkDriver(self.parent)
        return bridge.fork_binding(self.project,self.f.name,name,intent,'Authorized independent public research',self.driver)
    def allowed(self):
        with bridge.lease(self.project,bridge.load_binding(self.project,'independent')):pass
    def test_two_owned_homes_keep_parent_stops_and_exclusions(self):
        result=self.fork();child=bridge.load_binding(self.project,'independent')
        self.assertEqual(child['binding_kind'],nr.MANUAL_FORK_KIND)
        self.assertTrue(result['body_validation_still_required']);self.assertNotIn('web_validation',child)
        self.assertEqual(child['manual_excluded_note_ids'],self.parent['manual_excluded_note_ids'])
        self.assertEqual(sum(a=='create' for a,_,_ in self.driver.actions),2)
        self.assertTrue(all(s['owned'] for s in child['tabs'].values()))
        self.assertNotIn(self.f.name,[name for _,name,_ in self.driver.actions]);self.allowed()
        self.assertEqual(bridge.binding_path(self.project,self.f.name).read_bytes(),self.before);self.f.unchanged()
        with self.assertRaises(xp.legacy.StopRun):
            with identity.account_lease(child['profile_guard_key'],child['account_uid']):pass
    def test_original_restricted_note_is_blocked_before_browser(self):
        self.fork();child=bridge.load_binding(self.project,'independent');calls=[]
        class NoRead:
            def call(self,*a,**kw):calls.append(a)
        with self.assertRaisesRegex(bridge.BridgeError,'ORIGINAL_NOTE_ACCESS_UNRESOLVED'):
            bridge.prepare_target(None,child,NoRead(),{'payload':{'target':child['manual_excluded_note_ids'][0]}})
        self.assertEqual(calls,[])
    def test_child_exclusion_or_account_change_revokes_grant(self):
        self.fork();child=bridge.load_binding(self.project,'independent')
        for field,value in [('manual_excluded_note_ids',[]),('account_uid','9'*24),('runtime',{'changed':True})]:
            changed=copy.deepcopy(child);changed[field]=value;bridge.save_binding(self.project,'independent',changed)
            with self.assertRaises(xp.legacy.ResearchError):self.allowed()
            bridge.save_binding(self.project,'independent',child);self.allowed()
    def test_changed_parent_source_revokes_child(self):
        self.fork();grant=manual.check_pointer(self.parent['manual_tab_revalidation']);probe=manual.check_pointer(grant['probe'])
        Path(probe['capture']['file']).write_text('{}')
        with self.assertRaises(xp.legacy.ResearchError):self.allowed()
    def test_new_stop_rejects_creation_without_browser_calls(self):
        file=identity.guard_directory(self.f.keys[0])/'STOP.json';file.write_bytes(file.read_bytes()+b' ')
        driver=ManualForkDriver(self.parent)
        with self.assertRaises(xp.legacy.ResearchError):self.fork(driver)
        self.assertEqual(driver.actions,[])
    def test_unknown_create_cannot_replay_or_change_intent(self):
        driver=ManualForkDriver(self.parent,'unknown')
        with self.assertRaisesRegex(bridge.BridgeError,'COMMAND_OUTCOME_UNKNOWN'):self.fork(driver)
        count=len(driver.actions)
        with self.assertRaises(xp.legacy.ResearchError):self.fork(driver)
        with self.assertRaises(xp.legacy.ResearchError):self.fork(driver,name='another',intent='another-intent')
        self.assertEqual(len(driver.actions),count);self.f.unchanged()
    def test_wrong_home_account_never_gets_ready(self):
        with self.assertRaisesRegex(bridge.BridgeError,'ACCOUNT_MISMATCH'):self.fork(ManualForkDriver(self.parent,'wrong_uid'))
        self.assertNotEqual(bridge.load_binding(self.project,'independent')['state'],'ready');self.f.unchanged()
    def test_successful_intent_is_idempotent_and_children_cannot_fork(self):
        self.fork();driver=ManualForkDriver(self.parent);result=self.fork(driver)
        self.assertTrue(result['duplicate']);self.assertEqual(driver.actions,[])
        with self.assertRaisesRegex(bridge.BridgeError,'FORK_RECURSION_FORBIDDEN'):
            bridge.fork_binding(self.project,'independent','grandchild','another','Authorized',driver)
        self.assertEqual(driver.actions,[])

if __name__=='__main__':unittest.main()
