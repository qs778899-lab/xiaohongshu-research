"""Unique comment metrics use a project-note ID union, independent of review stages."""
import json
import unittest
from contextlib import nullcontext
from unittest.mock import patch

import test_bridge as fixture
import xhs_bridge as bridge
import xhs_native as native
import xhs_rounds as rounds


class CommentMetricTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BridgeTests();self.f.setUp();self.p=self.f.project
        rounds.schema(self.p)
        self.roots=self.add('roots',fixture.NOTE)
        self.replies=self.add('replies',fixture.NOTE)

    def tearDown(self):self.f.tearDown()

    def add(self,stage,note):
        f=self.f.root/(stage+'-'+note+'.json')
        f.write_text(json.dumps({'units':[{'stage':stage,'target':note,'sources':[str(self.f.source)],'next':'read'}]}))
        return native.import_manifest(self.p,f)['task_ids'][0]

    def capture(self,tid,ids,text='text'):
        return bridge.store_capture(self.p,self.p.task(tid),{'url':fixture.URL.replace(fixture.NOTE,self.p.task(tid)['payload']['target']),'results':[{'commentId':cid,'text':text,'author':'同昵称','parentCommentId':''} for cid in ids],
            'deepCoverage':{'rootPaginationState':'complete','threads':[],'observedRootIds':list(ids)}})

    def test_two_stages_overlap_then_only_new_reply_counts(self):
        ids=[f'{i:024x}' for i in range(17)]
        self.capture(self.roots,ids);before=bridge.observed_comment_ids(self.p,fixture.NOTE)
        self.capture(self.replies,ids+['f'*24])
        self.assertEqual(len(bridge.observed_comment_ids(self.p,fixture.NOTE)-before),1)

    def test_same_id_text_change_is_not_new_unique(self):
        self.capture(self.roots,['a'*24]);before=bridge.observed_comment_ids(self.p,fixture.NOTE)
        self.capture(self.replies,['a'*24],text='后来改善')
        self.assertEqual(bridge.observed_comment_ids(self.p,fixture.NOTE)-before,set())

    def test_another_note_same_id_does_not_suppress_this_note(self):
        other=self.add('roots','9'*24);self.capture(other,['a'*24])
        self.assertEqual(bridge.observed_comment_ids(self.p,fixture.NOTE),set())
        self.capture(self.replies,['a'*24])
        self.assertEqual(bridge.observed_comment_ids(self.p,fixture.NOTE),{'a'*24})

    def test_smaller_snapshot_keeps_cumulative_union(self):
        self.capture(self.roots,['a'*24,'b'*24,'c'*24]);self.capture(self.roots,['a'*24])
        self.capture(self.replies,['b'*24])
        self.assertEqual(bridge.observed_comment_ids(self.p,fixture.NOTE),{'a'*24,'b'*24,'c'*24})

    def test_work_metric_unions_note_and_preserves_legacy_rows(self):
        ids=[f'{i:024x}' for i in range(17)]
        self.capture(self.roots,ids)
        self.p.db.execute("UPDATE tasks SET status='complete' WHERE id=?",(self.roots,))
        legacy={'new_unique_comments':17,'stage':'roots'}
        self.p.db.execute('INSERT INTO execution_metrics(task_id,body,at) VALUES(?,?,?)',(self.roots,json.dumps(legacy),'old'));self.p.db.commit()
        class Driver:
            def __init__(self,*args):self.metrics={'calls':0,'process_starts':1}
            def close(self):pass
        def collect(db,b,d,t):
            return bridge.store_capture(db,t,{'url':fixture.URL,'results':[{'commentId':cid,'text':'text','parentCommentId':''} for cid in ids+['f'*24]],'deepCoverage':{'rootPaginationState':'complete','threads':[],'observedRootIds':ids}})
        binding=dict(self.f.binding,runtime={})
        with patch.object(bridge,'load_binding',return_value=binding),patch.object(bridge,'Driver',Driver),patch.object(bridge,'lease',return_value=nullcontext([])),patch.object(bridge,'save_binding'),patch.object(bridge,'collect_one',side_effect=collect):
            result=bridge.work(self.p.path,'test',units=1,max_seconds=30,task_ids=[self.replies])
        metrics=[json.loads(r[0]) for r in self.p.db.execute('SELECT body FROM execution_metrics ORDER BY id')]
        self.assertEqual(result['processed_units'],1)
        self.assertEqual(metrics[0],legacy)
        self.assertEqual(metrics[-1]['new_unique_comments'],1)
        self.assertEqual(metrics[-1]['metric_scope'],'project_note_comment_ids_v2')
        self.assertEqual(metrics[-1]['note_id'],fixture.NOTE)


if __name__=='__main__':unittest.main()
