"""Offline behavior regressions for parent END with a missing reply body."""
import copy
import json
from contextlib import nullcontext
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bridge as fixture
import xhs_bridge as b
import xhs_native as native
import xhs_project as xp
import xhs_research as legacy

ROOT='3'*24
REPLY='4'*24


class ReplyBodyEnd(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BridgeTests();self.f.setUp();self.p=self.f.project
        task=self.p.task(self.f.tid);task['payload']['stage']='roots'
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task['id']));self.p.db.commit()
        self.gap={'commentId':REPLY,'reason':'reply_content_not_rendered','renderedText':'回复 test :',
                  'renderedReplyPrefixOnly':True,'retainedPriorBody':False}
        thread={'rootKey':'id:'+ROOT,'commentId':ROOT,'state':'stalled','reason':'reply_content_not_rendered',
                'contentCaptured':True,'replyContentCaptured':False,'replyIdentityComplete':True,
                'replyBodyGaps':[self.gap],'currentReplyBodyGaps':[self.gap],'expanders':[],
                'unsafeExpanders':0,'scanIncomplete':False}
        self.snapshot={'url':fixture.URL,'results':[{'commentId':ROOT,'text':'Synthetic parent text','is_reply':False}],
            'deepCoverage':{'threads':[thread],'remainingThreads':[thread],'observedRootIds':[ROOT],
                'rootPaginationState':'complete','explicitEnd':True,'rootEndBasis':'rendered_end_signal'}}

    def tearDown(self):self.f.tearDown()

    def capture(self,value=None):
        return b.store_capture(self.p,self.p.task(self.f.tid),value or copy.deepcopy(self.snapshot))

    def progress(self):return self.p.task(self.f.tid)['payload']['bridge']

    def receipt(self,complete=False):
        pr=self.progress()
        receipt={'target':fixture.NOTE,'action':'read','outcome':'ready','observed_at':legacy.now(),
            'summary':'Synthetic saved text reading','gaps':[] if complete else ['Reply body unavailable'],
            'source_file':pr['last_snapshot'],'url':fixture.URL,'target_verified':True,'content_ready':True,
            'view_consistent':True,'reviewed':True,'complete':complete,'end':'platform_end',
            'positions':['saved rendered text'],'bridge_snapshot_hashes':pr['hashes']}
        file=self.f.root/'review.json';file.write_text(json.dumps(receipt));return file

    def valid_snapshot(self):
        value=copy.deepcopy(self.snapshot);thread=value['deepCoverage']['threads'][0]
        thread.update(state='visible_replies_read',reason='',replyContentCaptured=True,
                      replyBodyGaps=[],currentReplyBodyGaps=[])
        thread['replyBodyCaptures']={REPLY:{'commentId':REPLY,'parentCommentId':ROOT,'authorId':'5'*24,
                                          'text':'Actual synthetic reply body','textSource':'rendered_text'}}
        value['deepCoverage']['remainingThreads']=[]
        value['results'].append({'commentId':REPLY,'parentCommentId':ROOT,'authorHrefRaw':'/user/profile/'+'5'*24,
                                 'text':'Actual synthetic reply body','textSource':'rendered_text','is_reply':True})
        return value

    def test_parent_end_preserved_but_missing_body_held_without_overwriting_raw(self):
        before=json.dumps(self.snapshot,sort_keys=True);result=self.capture();pr=self.progress()
        self.assertTrue(pr['root_collection_end']);self.assertTrue(b.root_pagination_ended(pr))
        self.assertFalse(pr['collection_end']);self.assertEqual(pr['fault']['code'],b.COMMENT_BODY_GAP_FAULT)
        self.assertEqual(pr['fault']['threads'][0]['commentIds'],[REPLY])
        self.assertEqual(json.dumps(self.snapshot,sort_keys=True),before)
        self.assertEqual(json.loads(Path(result['snapshot']).read_text())['deepCoverage']['remainingThreads'][0]['replyBodyGaps'],[self.gap])
        self.assertEqual(self.p.task(self.f.tid)['status'],'pending')
        self.assertFalse(list(self.f.guard.rglob('STOP.json')))

    def test_complete_refused_before_record_even_if_old_end_flag_and_comments_are_wrong(self):
        self.capture();task=self.p.task(self.f.tid);task['payload']['bridge'].update(collection_end=True,comments={})
        task['payload']['bridge'].pop('fault');self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task['id']));self.p.db.commit()
        count=self.p.db.execute('SELECT COUNT(*) FROM native_events').fetchone()[0]
        with self.assertRaisesRegex(b.BridgeError,'COMMENT_SCOPE_INCOMPLETE'):
            b.review(self.p,self.f.tid,self.receipt(True))
        self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM native_events').fetchone()[0],count)
        self.assertEqual(self.p.task(self.f.tid)['status'],'pending')

    def test_partial_review_does_not_release_hold(self):
        self.capture();fault=copy.deepcopy(self.progress()['fault'])
        result=b.review(self.p,self.f.tid,self.receipt())
        self.assertFalse(result['awaiting_review']);self.assertEqual(self.progress()['fault'],fault)
        self.assertFalse(self.progress()['collection_end'])

    def test_valid_completed_scope_still_completes(self):
        self.capture(self.valid_snapshot());self.assertTrue(self.progress()['collection_end'])
        result=b.review(self.p,self.f.tid,self.receipt(True));self.assertEqual(result['status'],'complete')

    def test_legacy_end_with_complete_thread_and_optional_flags_missing_still_works(self):
        value=copy.deepcopy(self.snapshot);value['deepCoverage']['threads']=[{'rootKey':'id:'+ROOT,'commentId':ROOT,'state':'visible_replies_read'}]
        value['deepCoverage']['remainingThreads']=[];self.capture(value)
        self.assertTrue(self.progress()['collection_end'])
        self.assertEqual(b.review(self.p,self.f.tid,self.receipt(True))['status'],'complete')

    def test_retained_previously_verified_body_not_blocked_by_current_blank_render(self):
        value=self.valid_snapshot();thread=value['deepCoverage']['threads'][0]
        thread['currentReplyBodyGaps']=[dict(self.gap,retainedPriorBody=True)]
        self.capture(value);self.assertTrue(self.progress()['collection_end']);self.assertNotIn('fault',self.progress())

    def test_other_runnable_floors_are_not_prematurely_held(self):
        value=copy.deepcopy(self.snapshot);other={'rootKey':'id:'+'6'*24,'commentId':'6'*24,'state':'pending','expanders':['展开回复']}
        value['deepCoverage']['threads'].append(other);value['deepCoverage']['remainingThreads'].append(other)
        self.capture(value);self.assertFalse(self.progress()['collection_end']);self.assertNotIn('fault',self.progress())

    def test_legacy_pending_task_is_held_offline_before_rescheduling(self):
        self.capture();task=self.p.task(self.f.tid);task['payload']['bridge'].pop('fault');task['payload']['bridge']['collection_end']=True
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task['id']));self.p.db.commit()
        updated=b.sync_shared_comment_stall(self.p,self.p.task(self.f.tid))
        self.assertEqual(updated['payload']['bridge']['fault']['code'],b.COMMENT_BODY_GAP_FAULT)
        self.assertFalse(updated['payload']['bridge']['collection_end'])

    def work_fixture(self):
        binding={**self.f.binding,'format':b.VERSION,'project':str(self.p.path.resolve()),
                 'profile_label':'synthetic','profile_guard_key':'a'*64,'runtime':{}}
        b.save_binding(self.p.path,'test',binding)
        return type('FakeDriver',(),{'__init__':lambda self,*a:setattr(self,'metrics',{}),'close':lambda self:None})

    def import_stage(self,stage,target):
        file=self.f.root/(stage+'-manifest.json')
        file.write_text(json.dumps({'units':[{'stage':stage,'target':target,'sources':[str(self.f.source)],'next':'read'}]}))
        return native.import_manifest(self.p,file)['task_ids'][0]

    def test_partial_read_held_task_is_skipped_and_other_note_runs(self):
        self.capture();b.review(self.p,self.f.tid,self.receipt());healthy=self.import_stage('body','8'*24);calls=[]
        def collect(project,binding,driver,task):
            calls.append(task['id'])
            return b.store_capture(project,task,{'url':fixture.URL.replace(fixture.NOTE,'8'*24),'body':'Other synthetic note'})
        with patch.object(b,'Driver',self.work_fixture()),patch.object(b,'lease',lambda *a:nullcontext([])),patch.object(b,'collect_one',collect),patch.object(b.time,'sleep'):
            b.work(self.p.path,'test',units=2,max_seconds=30,interval=3,task_ids=[self.f.tid,healthy])
        self.assertEqual(calls,[healthy]);self.assertEqual(self.progress()['fault']['code'],b.COMMENT_BODY_GAP_FAULT)

    def test_separate_reply_task_can_run_after_parent_pagination_ended(self):
        self.capture();sibling=self.import_stage('replies',fixture.NOTE);calls=[]
        def collect(project,binding,driver,task):
            calls.append(task['id']);return b.store_capture(project,task,self.valid_snapshot())
        with patch.object(b,'Driver',self.work_fixture()),patch.object(b,'lease',lambda *a:nullcontext([])),patch.object(b,'collect_one',collect),patch.object(b.time,'sleep'):
            b.work(self.p.path,'test',units=1,max_seconds=30,interval=3,task_ids=[sibling])
        self.assertEqual(calls,[sibling]);self.assertFalse(self.progress()['collection_end'])
        self.assertEqual(self.progress()['fault']['code'],b.COMMENT_BODY_GAP_FAULT)

    def probe(self,value):
        self.capture();binding=copy.deepcopy(self.f.binding)
        def collect(project,binding,driver,task,**kwargs):return b.store_capture(project,task,copy.deepcopy(value))
        with patch.object(b,'lease',lambda *a:nullcontext([])),patch.object(b,'collect_one',collect),patch.object(b.time,'sleep'):
            driver=type('FakeDriver',(),{'metrics':{}})()
            return b.verify_comment_local_fix(self.p,binding,driver,self.p.task(self.f.tid),'Explicit synthetic renderer fix probe')

    def test_explicit_probe_can_release_only_after_missing_body_is_really_captured(self):
        result=self.probe(self.valid_snapshot());self.assertTrue(result['repair_verified'])
        self.assertNotIn('fault',self.progress());self.assertTrue(self.progress()['awaiting_review'])

    def test_new_text_elsewhere_does_not_release_held_body_gap(self):
        value=copy.deepcopy(self.snapshot);value['results'].append({'commentId':'7'*24,'text':'Unrelated newly rendered text'})
        result=self.probe(value);self.assertFalse(result['repair_verified'])
        self.assertEqual(self.progress()['fault']['code'],b.COMMENT_BODY_GAP_FAULT)


if __name__=='__main__':unittest.main()
