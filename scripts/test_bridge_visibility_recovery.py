"""Offline one-shot recovery of legacy visibility holds, never foregrounding."""
import copy
import json
from contextlib import contextmanager
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bridge as fixture
import xhs_bridge as b
import xhs_project as xp
import xhs_research as legacy

ROOT='3'*24
REPLY='4'*24


class HiddenDriver(fixture.VisibilityFake):
    def __init__(self,project=None,task_id=None):
        super().__init__();self.metrics={};self.project=project;self.task_id=task_id
        self.after_capture=None;self.deep_gap=False;self.intent_observed=False

    def close(self):pass

    def call(self,action,binding=None,role=None,**fields):
        if action in {'capture','deep'} and self.project:
            records=self.project.task(self.task_id)['payload']['bridge']['visibility_repair_probes']
            self.intent_observed=any(r['status']=='started' for r in records.values())
        if action=='deep':
            self.calls.append((action,binding['context_id'],role,fields))
            thread={'rootKey':'id:'+ROOT,'commentId':ROOT,'state':'not_processed',
                    'contentCaptured':True,'replyContentCaptured':True,'replyIdentityComplete':True,'expanders':[]}
            if self.deep_gap:
                gap={'commentId':REPLY,'reason':'reply_content_not_rendered',
                     'renderedReplyPrefixOnly':True,'retainedPriorBody':False,'renderedText':'回复 test :'}
                thread.update(state='stalled',replyContentCaptured=False,replyBodyGaps=[gap])
            return {'url':self.url,'results':[{'commentId':ROOT,'text':'Actual synthetic public root',
                    'is_reply':False}], 'deepCoverage':{'threads':[thread],'remainingThreads':[thread],
                    'observedRootIds':[ROOT],'rootPaginationState':'complete' if self.deep_gap else 'pending',
                    'explicitEnd':self.deep_gap}}
        value=super().call(action,binding,role,**fields)
        if action=='capture' and self.after_capture:self.after_capture()
        return value


class VisibilityRecovery(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BridgeTests();self.f.setUp();self.p=self.f.project;self.tid=self.f.tid
        self.binding={**self.f.binding,'format':b.VERSION,'project':str(self.p.path.resolve()),
                      'runtime':{},'profile_guard_key':'a'*64,'background_text_collection':True}
        self.fault={'code':'TARGET_NOT_VISIBLE','hold':True,'at':'original-visibility-stop',
                    'recovery':{'exhausted':True}}
        self.set_progress({'fault':copy.deepcopy(self.fault)})
        self.driver=HiddenDriver(self.p,self.tid);self.lease_depth=0

    def tearDown(self):self.f.tearDown()

    def task(self):return self.p.task(self.tid)
    def progress(self):return self.task()['payload'].get('bridge',{})

    def set_progress(self,progress,stage=None):
        task=self.p.task(self.tid)
        task['payload']['bridge']=progress
        if stage:task['payload']['stage']=stage
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),self.tid));self.p.db.commit()

    @contextmanager
    def lease(self,*args):
        self.lease_depth+=1
        try:yield []
        finally:self.lease_depth-=1

    def run_work(self,collect=None):
        b.save_binding(self.p.path,'test',self.binding)
        actual=b.collect_one
        def under_lease(*args,**kwargs):
            self.assertEqual(self.lease_depth,1)
            return (collect or actual)(*args,**kwargs)
        with patch.object(b,'Driver',lambda *args:self.driver),patch.object(b,'lease',self.lease),\
             patch.object(b,'collect_one',under_lease),patch.object(b.time,'sleep'),\
             patch.object(b.subprocess,'run') as os_focus:
            result=b.work(self.p.path,'test',units=1,max_seconds=30,interval=3,task_ids=[self.tid])
        os_focus.assert_not_called()
        return result

    def test_actual_hidden_body_is_acquired_then_verified_before_hold_is_archived(self):
        result=self.run_work();pr=self.progress()
        self.assertEqual(result['processed_units'],1);self.assertTrue(self.driver.intent_observed)
        self.assertNotIn('fault',pr);self.assertTrue(pr['awaiting_review']);self.assertEqual(pr.get('reviewed_hashes',[]),[])
        self.assertEqual(self.task()['status'],'pending');self.assertEqual(pr['resolved_visibility_faults'][0]['prior_fault'],self.fault)
        names=[x[0] for x in self.driver.calls]
        self.assertEqual(names,['metadata','identity','capture','metadata','identity'])
        self.assertFalse(self.driver.visible);self.assertEqual(len(pr['visibility_repair_probes']),1)

    def test_fresh_roots_without_hashes_can_receive_real_acquisition(self):
        self.set_progress({'fault':copy.deepcopy(self.fault)},'roots')
        self.run_work();pr=self.progress()
        self.assertTrue(self.driver.intent_observed);self.assertTrue(pr['hashes']);self.assertNotIn('fault',pr)
        self.assertTrue(pr['awaiting_review']);self.assertFalse(pr['collection_end'])
        self.assertIn('deep',[x[0] for x in self.driver.calls]);self.assertNotIn('activate',[x[0] for x in self.driver.calls])

    def test_unconfirmed_visibility_fault_uses_same_actual_verification(self):
        fault=copy.deepcopy(self.fault);fault['code']='TARGET_VISIBILITY_UNCONFIRMED'
        self.set_progress({'fault':fault});self.run_work()
        self.assertNotIn('fault',self.progress())
        self.assertEqual(self.progress()['resolved_visibility_faults'][0]['prior_fault'],fault)

    def test_mode_change_alone_does_not_clear_any_hold(self):
        self.assertTrue(b.background_visibility_candidate(self.task(),self.binding))
        self.assertEqual(self.progress()['fault'],self.fault)

    def test_all_text_stages_are_eligible_but_other_stages_and_faults_are_not(self):
        for stage in b.DOM_TEXT_STAGES:
            task=copy.deepcopy(self.task());task['payload']['stage']=stage
            self.assertTrue(b.background_visibility_candidate(task,self.binding),stage)
        for code in ['COMMENT_PAGINATION_STALLED',b.COMMENT_BODY_GAP_FAULT,'DEEP_READ_FAILED','ACCESS_STOP','CONTENT_LOADING']:
            task=copy.deepcopy(self.task());task['payload']['bridge']['fault']['code']=code
            self.assertFalse(b.background_visibility_candidate(task,self.binding),code)
        for stage in ['images','video']:
            task=copy.deepcopy(self.task());task['payload']['stage']=stage
            self.assertFalse(b.background_visibility_candidate(task,self.binding),stage)

    def test_mode_must_be_explicit_true(self):
        for value in [False,None,'true',1]:
            binding={**self.binding,'background_text_collection':value}
            self.assertFalse(b.background_visibility_candidate(self.task(),binding))
        self.binding['background_text_collection']=False
        self.run_work(lambda *args: self.fail('disabled mode must not collect'))
        self.assertEqual(self.progress()['fault'],self.fault)

    def test_unread_hash_or_review_handoff_blocks_automatic_probe(self):
        for pr in [{'fault':copy.deepcopy(self.fault),'hashes':['f'*64],'awaiting_review':False},
                   {'fault':copy.deepcopy(self.fault),'awaiting_review':True}]:
            self.set_progress(pr);self.run_work(lambda *args:self.fail('unread review must take precedence'))
            self.assertNotIn('visibility_repair_probes',self.progress())

    def test_two_schedules_do_not_retry_a_failed_probe_of_same_version(self):
        calls=[]
        def fail(*args,**kwargs):calls.append(1);raise b.BridgeError('TARGET_NOT_VISIBLE')
        self.run_work(fail);self.run_work(fail)
        self.assertEqual(len(calls),1);pr=self.progress();self.assertEqual(pr['fault']['code'],'TARGET_NOT_VISIBLE')
        record=next(iter(pr['visibility_repair_probes'].values()))
        self.assertEqual(record['status'],'failed');self.assertTrue(record['no_automatic_retry'])
        self.assertEqual(record['prior_fault'],self.fault);self.assertNotIn('resolved_visibility_faults',pr)

    def test_unknown_submission_failure_keeps_intent_and_is_not_replayed(self):
        calls=[]
        def fail(*args,**kwargs):calls.append(1);raise b.BridgeError('COMMAND_OUTCOME_UNKNOWN')
        self.run_work(fail);self.run_work(fail)
        self.assertEqual(len(calls),1);self.assertEqual(self.progress()['fault'],self.fault)
        self.assertEqual(next(iter(self.progress()['visibility_repair_probes'].values()))['error'],'COMMAND_OUTCOME_UNKNOWN')

    def test_uid_change_on_post_acquisition_verification_never_clears_old_hold(self):
        self.driver.after_capture=lambda:setattr(self.driver,'uid','9'*24)
        result=self.run_work();pr=self.progress()
        self.assertEqual(result['reason'],'ACCOUNT_MISMATCH');self.assertEqual(pr['fault'],self.fault)
        self.assertTrue(pr['awaiting_review']);self.assertNotIn('resolved_visibility_faults',pr)

    def test_target_change_on_post_acquisition_verification_is_not_restored_or_replayed(self):
        self.driver.after_capture=lambda:setattr(self.driver,'url',fixture.URL.replace(fixture.NOTE,'8'*24))
        result=self.run_work();self.assertEqual(result['reason'],'UNEXPECTED_TARGET_CHANGE')
        self.assertEqual(self.progress()['fault'],self.fault);self.assertNotIn('resolved_visibility_faults',self.progress())

    def test_new_prefix_only_gap_is_preserved_instead_of_masked_by_old_visibility(self):
        self.set_progress({'fault':copy.deepcopy(self.fault)},'roots');self.driver.deep_gap=True
        self.run_work();pr=self.progress()
        self.assertEqual(pr['fault']['code'],b.COMMENT_BODY_GAP_FAULT);self.assertTrue(pr['fault']['hold'])
        self.assertFalse(pr['collection_end']);self.assertTrue(pr['awaiting_review'])
        self.assertNotIn('resolved_visibility_faults',pr)
        self.assertEqual(next(iter(pr['visibility_repair_probes'].values()))['status'],'new_fault_preserved')

    def test_explicit_local_fix_uses_same_single_probe_for_body(self):
        b.save_binding(self.p.path,'test',self.binding)
        with patch.object(b,'Driver',lambda *args:self.driver),patch.object(b,'lease',self.lease),patch.object(b.time,'sleep'):
            result=b.verify_local_fix(self.p.path,'test',self.tid,'Explicit background renderer verification')
        self.assertTrue(result['repair_verified']);self.assertTrue(self.progress()['awaiting_review'])

    def test_foreground_search_and_author_keep_their_old_explicit_repair_path(self):
        self.binding['background_text_collection']=False;b.save_binding(self.p.path,'test',self.binding)
        for stage in ['search','author']:
            self.set_progress({'fault':copy.deepcopy(self.fault)},stage)
            if stage=='search':
                task=self.task();task['payload']['sort']='general'
                self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),self.tid));self.p.db.commit()
            calls=[]
            def collect(project,binding,driver,task,**kwargs):
                calls.append(kwargs)
                url=('https://www.xiaohongshu.com/search_result?keyword='+fixture.NOTE if stage=='search'
                     else 'https://www.xiaohongshu.com/user/profile/'+fixture.NOTE)
                return b.store_capture(project,task,{'url':url,'cards':[], 'explicitEnd':True})
            with patch.object(b,'Driver',lambda *args:self.driver),patch.object(b,'lease',self.lease),patch.object(b,'collect_one',collect):
                result=b.verify_local_fix(self.p.path,'test',self.tid,'Legacy explicit foreground repair')
            self.assertTrue(result['repair_verified']);self.assertEqual(calls,[{'force_advance':True}])
            self.assertNotIn('visibility_repair_probes',self.progress())

    def test_account_stop_is_not_a_visibility_probe_or_mode_bypass(self):
        @contextmanager
        def stopped(*args):raise legacy.StopRun('Synthetic account stop');yield
        b.save_binding(self.p.path,'test',self.binding)
        with patch.object(b,'Driver',lambda *args:self.driver),patch.object(b,'lease',stopped),patch.object(b,'collect_one') as collect:
            result=b.work(self.p.path,'test',units=1,max_seconds=30,interval=3,task_ids=[self.tid])
        collect.assert_not_called();self.assertEqual(result['event'],'blocked')
        self.assertEqual(self.progress()['fault'],self.fault);self.assertNotIn('visibility_repair_probes',self.progress())


if __name__=='__main__':unittest.main()
