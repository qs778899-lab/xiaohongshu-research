#!/usr/bin/env python3
"""Offline bounded comment repair verification; all evidence/accounts are synthetic."""
import contextlib
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bridge as fixture
import xhs_bridge as b
import xhs_project as xp
import xhs_account_identity as identity
import xhs_research as legacy


ROOT='3'*24
REPLY='4'*24


class CommentLocalFix(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BridgeTests();self.f.setUp();self.p=self.f.project
        task=self.p.task(self.f.tid);task['payload']['stage']='replies'
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task['id']));self.p.db.commit()
        self.binding={**self.f.binding,'runtime':{},'format':b.VERSION,
            'project':str(self.p.path.resolve()),'profile_label':'test','profile_guard_key':'a'*64}
        b.save_binding(self.p.path,'test',self.binding)
        self.snapshot={'url':fixture.URL,'results':[{'commentId':ROOT,'text':'原有父楼全文','authorId':'5'*24}],
            'deepCoverage':{'threads':[{'rootKey':'id:'+ROOT,'commentId':ROOT,'state':'pending',
                'contentCaptured':True,'replyContentCaptured':True,'replyIdentityComplete':True,
                'visibleReplies':0,'expanders':['展开回复'],
                'pendingReplyExpansion':{'state':'click_submitted','beforeVisibleReplies':0,
                    'beforeReplyIds':[],'pendingObservations':1,'clickedAt':100}}],
                'observedRootIds':[ROOT],'rootPaginationState':'pending','explicitEnd':False,
                'checkpoint':{'reason':'reply_render_pending','at':100,'requestedWaitMs':400},
                'waitHandedToScheduler':True}}
        b.store_capture(self.p,self.p.task(self.f.tid),self.snapshot)
        b.hold_task(self.p,self.f.tid,'DEEP_READ_FAILED')
        self.original_fault=copy.deepcopy(self.progress()['fault'])
        class Driver:
            def __init__(self,*args):self.metrics={'calls':0}
            def close(self):pass
        self.driver=patch.object(b,'Driver',Driver);self.driver.start()
        self.lease_calls=[];self.acquisitions=[];self.sleeps=[]
        @contextlib.contextmanager
        def lease(*args,**kwargs):
            self.lease_calls.append(1);yield []
        self.lease=patch.object(b,'lease',lease);self.lease.start()
        self.sleep=patch.object(b.time,'sleep',self.sleeps.append);self.sleep.start()

    def tearDown(self):
        self.sleep.stop();self.lease.stop();self.driver.stop();self.f.tearDown()

    def progress(self):return self.p.task(self.f.tid)['payload']['bridge']

    def invoke(self,sequence,basis='bounded renderer yields to outer scheduler'):
        sequence=iter(sequence)
        def collect(db,binding,driver,task,**kwargs):
            self.acquisitions.append(task['id'])
            self.assertFalse(kwargs.get('force_advance',False))
            value=next(sequence)
            if isinstance(value,BaseException):raise value
            return b.store_capture(db,task,value)
        with patch.object(b,'collect_one',collect):
            return b.verify_local_fix(self.p.path,'test',self.f.tid,basis)

    def changed(self):
        value=copy.deepcopy(self.snapshot)
        value['results'].append({'commentId':REPLY,'parentCommentId':ROOT,'text':'新渲染的回复全文','authorId':'6'*24,'is_reply':True})
        return value

    def assert_archives(self):
        attempts=self.progress()['repair_probes']
        for attempt in attempts.values():
            self.assertEqual(attempt['prior_fault'],self.original_fault)
            for ref in [attempt['archive'],attempt.get('result_archive'),attempt.get('failure_archive')]+[r['archive'] for r in attempt['observations']]:
                if ref:
                    source=Path(ref['file'])
                    self.assertTrue(source.is_file())
                    self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),ref['sha256'])

    def test_real_new_comment_clears_hold_but_requires_review_and_keeps_pending(self):
        result=self.invoke([self.changed()])
        self.assertTrue(result['repair_verified']);self.assertEqual(result['new_comment_ids'],1)
        self.assertEqual(result['collection_attempts'],1);self.assertEqual(self.sleeps,[])
        self.assertNotIn('fault',self.progress());self.assertTrue(self.progress()['awaiting_review'])
        self.assertTrue(result['requires_codex_review']);self.assertTrue(result['awaiting_review'])
        self.assertEqual(self.p.task(self.f.tid)['status'],'pending');self.assert_archives()

    def test_root_scope_uses_same_bounded_comment_verification(self):
        task=self.p.task(self.f.tid);task['payload']['stage']='roots'
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task['id']));self.p.db.commit()
        value=self.changed();value['results'][-1].pop('parentCommentId');value['results'][-1]['is_reply']=False
        result=self.invoke([value]);self.assertTrue(result['repair_verified'])
        self.assertEqual(result['new_comment_ids'],1);self.assertEqual(self.p.task(self.f.tid)['status'],'pending')

    def test_yield_only_and_relative_time_changes_preserve_original_hold_after_two(self):
        second=copy.deepcopy(self.snapshot);second['results'][0]['time']='4小时前'
        second['deepCoverage']['checkpoint']['at']=200
        second['deepCoverage']['elapsedMs']=999
        second['deepCoverage']['threads'][0]['pendingReplyExpansion']['pendingObservations']=2
        result=self.invoke([self.snapshot,second])
        self.assertFalse(result['repair_verified']);self.assertEqual(self.progress()['fault'],self.original_fault)
        self.assertEqual(result['new_comment_ids'],0);self.assertEqual(result['new_rendered_versions'],0)
        self.assertFalse(result['checkpoint_advanced']);self.assertEqual(result['collection_attempts'],2)
        self.assertEqual(len(self.lease_calls),2);self.assertEqual(self.sleeps,[10]);self.assert_archives()

    def test_second_acquisition_continues_after_pure_yield(self):
        result=self.invoke([self.snapshot,self.changed()])
        self.assertTrue(result['repair_verified']);self.assertEqual(result['collection_attempts'],2)
        self.assertEqual(len(self.lease_calls),2);self.assertEqual(self.sleeps,[10])
        observations=next(iter(self.progress()['repair_probes'].values()))['observations']
        self.assertEqual([x['verified'] for x in observations],[False,True]);self.assert_archives()

    def test_same_ids_new_full_text_is_real_rendered_progress(self):
        value=copy.deepcopy(self.snapshot);value['results'][0]['text']+='，刚加载的后半句。'
        result=self.invoke([value]);self.assertTrue(result['repair_verified'])
        self.assertEqual(result['new_comment_ids'],0);self.assertEqual(result['new_rendered_versions'],1)

    def test_real_completed_thread_is_checkpoint_progress_without_new_ids(self):
        value=copy.deepcopy(self.snapshot);thread=value['deepCoverage']['threads'][0]
        thread.update(state='visible_replies_read',expanders=[],scanIncomplete=False)
        thread.pop('pendingReplyExpansion')
        result=self.invoke([value]);self.assertTrue(result['repair_verified'])
        self.assertTrue(result['checkpoint_advanced']);self.assertEqual(result['new_comment_ids'],0)
        self.assertFalse(self.progress()['collection_end'])

    def test_partial_or_unidentified_thread_cannot_fake_checkpoint_progress(self):
        value=copy.deepcopy(self.snapshot);thread=value['deepCoverage']['threads'][0]
        thread.update(state='visible_replies_read',expanders=[],scanIncomplete=True,replyIdentityComplete=False)
        thread.pop('pendingReplyExpansion')
        result=self.invoke([value,value]);self.assertFalse(result['repair_verified'])
        self.assertEqual(self.progress()['fault'],self.original_fault)

    def test_access_stop_aborts_first_acquisition_without_second_or_wait(self):
        with self.assertRaisesRegex(b.BridgeError,'ACCESS_STOP'):
            self.invoke([b.BridgeError('ACCESS_STOP')])
        self.assertEqual(len(self.acquisitions),1);self.assertEqual(self.sleeps,[])
        self.assertEqual(self.progress()['fault'],self.original_fault)
        attempt=next(iter(self.progress()['repair_probes'].values()))
        self.assertEqual(attempt['status'],'failed');self.assertEqual(attempt['error'],'ACCESS_STOP');self.assert_archives()

    def test_real_account_stop_between_units_is_rechecked_and_preserved(self):
        self.lease.stop()
        key=self.binding['profile_guard_key'];identity.associate(fixture.UID,key)
        def sleep(seconds):
            self.sleeps.append(seconds)
            legacy.write_json(identity.guard_directory(key)/'STOP.json',{'reason':'access restricted between units'})
        with patch.object(b.time,'sleep',sleep):
            with self.assertRaises(legacy.StopRun):self.invoke([self.snapshot])
        self.assertEqual(len(self.acquisitions),1);self.assertEqual(self.progress()['fault'],self.original_fault)
        self.assertTrue((identity.guard_directory(key)/'STOP.json').is_file());self.assert_archives()

    def test_unknown_action_never_retried(self):
        with self.assertRaisesRegex(b.BridgeError,'COMMAND_OUTCOME_UNKNOWN'):
            self.invoke([b.BridgeError('COMMAND_OUTCOME_UNKNOWN')])
        self.assertEqual(len(self.acquisitions),1);self.assertEqual(self.sleeps,[])
        self.assertEqual(self.progress()['fault'],self.original_fault)

    def test_same_code_version_cannot_repeat_probe(self):
        self.invoke([self.snapshot,self.snapshot])
        with self.assertRaisesRegex(b.BridgeError,'REPAIR_PROBE_ALREADY_USED'):
            self.invoke([])
        self.assertEqual(len(self.acquisitions),2)

    def test_deep_source_change_creates_new_version_without_changing_files(self):
        self.invoke([self.snapshot,self.snapshot]);original=Path.read_bytes
        def changed_source(path):
            raw=original(path)
            return raw+b'\n// synthetic changed renderer' if path.name=='deep_comments.mjs' else raw
        with patch.object(Path,'read_bytes',changed_source):
            result=self.invoke([self.snapshot,self.changed()])
        self.assertTrue(result['repair_verified']);self.assertEqual(len(self.progress()['repair_probes']),2)
        self.assertEqual(len(self.acquisitions),4);self.assert_archives()

    def test_only_original_held_deep_fault_is_eligible(self):
        b.hold_task(self.p,self.f.tid,'ACCESS_STOP')
        with self.assertRaisesRegex(b.BridgeError,'LOCAL_COMMENT_FAULT_REQUIRED'):self.invoke([])
        self.assertNotIn('repair_probes',self.progress());self.assertEqual(self.acquisitions,[])

    def test_mutated_immutable_snapshot_blocks_before_any_probe_or_collection(self):
        source=Path(self.progress()['last_snapshot']);source.write_text('{}')
        with self.assertRaisesRegex(b.BridgeError,'IMMUTABLE_SOURCE_CHANGED'):self.invoke([])
        self.assertNotIn('repair_probes',self.progress());self.assertEqual(self.acquisitions,[])

    def test_actual_collector_still_checks_uid_before_deep_read(self):
        class WrongAccount(fixture.Fake):
            def __init__(self,*args):super().__init__(uid='9'*24);self.metrics={'calls':0}
            def close(self):pass
        driver=WrongAccount()
        with patch.object(b,'Driver',lambda *args:driver):
            with self.assertRaisesRegex(b.BridgeError,'ACCOUNT_MISMATCH'):
                b.verify_local_fix(self.p.path,'test',self.f.tid,'bounded renderer fix')
        self.assertEqual([call[0] for call in driver.calls],['metadata','identity'])
        self.assertEqual(self.progress()['fault'],self.original_fault);self.assertEqual(self.sleeps,[])

    def test_actual_collector_still_rejects_unregistered_target_before_identity(self):
        binding=copy.deepcopy(self.binding);binding['tabs']['detail']['last_url']=fixture.URL
        b.save_binding(self.p.path,'test',binding)
        class OtherTarget(fixture.Fake):
            def __init__(self,*args):super().__init__(url=fixture.URL.replace(fixture.NOTE,'9'*24));self.metrics={'calls':0}
            def close(self):pass
        driver=OtherTarget()
        with patch.object(b,'Driver',lambda *args:driver):
            with self.assertRaisesRegex(b.BridgeError,'UNEXPECTED_TARGET_CHANGE'):
                b.verify_local_fix(self.p.path,'test',self.f.tid,'bounded renderer fix')
        self.assertEqual([call[0] for call in driver.calls],['metadata'])
        self.assertEqual(self.progress()['fault'],self.original_fault);self.assertEqual(self.sleeps,[])


if __name__=='__main__':unittest.main()
