"""An uncollected stage cannot commit another stage's shared semantic packet."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_bridge as bridge
import xhs_native as native
import xhs_project as xp
import xhs_rounds as rounds

NOTE='a'*24
URL='https://www.xiaohongshu.com/explore/'+NOTE


class ReviewContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.p=xp.Project(self.root/'project',create=True)
        source=self.root/'source.json';source.write_text(json.dumps({'url':URL}))
        manifest=self.root/'manifest.json';manifest.write_text(json.dumps({'units':[
            {'stage':stage,'target':NOTE,'next':'read','sources':[str(source)]} for stage in ['roots','replies']]}))
        self.roots,self.replies=native.import_manifest(self.p,manifest)['task_ids']
        self.rows=[{'commentId':f'{i:024x}','text':f'Complete actual synthetic comment {i}',
                    'authorId':'b'*24,'parentCommentId':''} for i in range(36)]
        bridge.store_capture(self.p,self.p.task(self.roots),{'url':URL,'results':self.rows,'explicitEnd':False})
        rounds.index_task(self.p,self.p.task(self.roots))
        self.packet=rounds.review_next(self.p,self.replies,40)
        self.receipt=self.root/'review.json';self.write_receipt(self.packet)
    def tearDown(self):self.p.close();self.tmp.cleanup()
    def write_receipt(self,packet,hashes=None):
        source=packet['items'][0]['source'] if packet['items'] else packet['snapshots'][0]['file']
        value={'target':NOTE,'action':'read','outcome':'ready','observed_at':bridge.legacy.now(),
               'summary':'Actually read the synthetic packet with full parent context.',
               'gaps':['collection not complete'],'source_file':source,'url':URL,'target_verified':True,
               'content_ready':True,'view_consistent':True,'reviewed':True,'complete':False,'positions':['packet items'],
               'review_packet_id':packet['id'],'bridge_snapshot_hashes':hashes or []}
        self.receipt.write_text(json.dumps(value));return value
    def snapshot(self):
        return {name:[tuple(row) for row in self.p.db.execute(f'SELECT * FROM {name} ORDER BY rowid')]
                for name in ['native_events','entity_versions','review_packets','tasks']}
    def test_36_shared_entities_zero_snapshots_missing_task_context_rejected_without_writes(self):
        self.assertEqual(len(self.packet['items']),36);self.assertEqual(self.packet['snapshots'],[])
        self.assertNotIn('bridge',self.p.task(self.replies)['payload'])
        before=self.snapshot();changes=self.p.db.total_changes
        with patch.object(rounds,'commit_packet',wraps=rounds.commit_packet) as packet, \
             patch.object(native,'record',wraps=native.record) as record:
            with self.assertRaisesRegex(bridge.BridgeError,'BRIDGE_COLLECTION_CONTEXT_REQUIRED'):
                bridge.review(self.p,self.replies,self.receipt)
        packet.assert_not_called();record.assert_not_called()
        self.assertEqual(before,self.snapshot());self.assertEqual(changes,self.p.db.total_changes)
        self.assertEqual(self.p.db.execute('SELECT sum(reviewed) FROM entity_versions').fetchone()[0],0)
        self.assertEqual(self.p.db.execute('SELECT committed FROM review_packets WHERE id=?',(self.packet['id'],)).fetchone()[0],0)
    def test_empty_or_invalid_bridge_shell_is_not_collection_context(self):
        for shell in [None,{},[],{'hashes':[]},{'hashes':None},{'fault':{'code':'CONTENT_LOADING','hold':True}}]:
            with self.subTest(shell=shell):
                task=self.p.task(self.replies);task['payload']['bridge']=shell
                self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),self.replies));self.p.db.commit()
                before=self.snapshot();changes=self.p.db.total_changes
                with self.assertRaisesRegex(bridge.BridgeError,'BRIDGE_COLLECTION_CONTEXT_REQUIRED'):
                    bridge.review(self.p,self.replies,self.receipt)
                self.assertEqual(before,self.snapshot());self.assertEqual(changes,self.p.db.total_changes)
    def test_hashes_from_another_stage_do_not_create_missing_context(self):
        hashes=self.p.task(self.roots)['payload']['bridge']['hashes'];self.write_receipt(self.packet,hashes)
        before=self.snapshot()
        with self.assertRaisesRegex(bridge.BridgeError,'BRIDGE_COLLECTION_CONTEXT_REQUIRED'):
            bridge.review(self.p,self.replies,self.receipt)
        self.assertEqual(before,self.snapshot())
    def test_collected_task_still_accepts_partial_packet_with_empty_receipt_hashes(self):
        packet=rounds.review_next(self.p,self.roots,10);self.assertTrue(packet['more_entities']);self.write_receipt(packet)
        before=self.p.db.execute('select count(*) from native_events').fetchone()[0]
        result=bridge.review(self.p,self.roots,self.receipt)
        self.assertEqual(result['status'],'pending');self.assertTrue(result['awaiting_review'])
        self.assertEqual(self.p.db.execute('select count(*) from native_events').fetchone()[0],before+1)
        self.assertEqual(self.p.db.execute('select sum(reviewed) from entity_versions').fetchone()[0],10)
        self.assertEqual(self.p.db.execute('SELECT committed FROM review_packets WHERE id=?',(packet['id'],)).fetchone()[0],1)
    def test_collected_and_previously_read_task_may_read_new_shared_entities_without_snapshot_hashes(self):
        # This task has its own real acquisition, already read; a sibling stage
        # then discovers new comments. Empty task snapshots remain legitimate.
        own=self.p.task(self.roots);own['payload']['bridge']['reviewed_hashes']=list(own['payload']['bridge']['hashes'])
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(own['payload']),self.roots));self.p.db.commit()
        rounds.index_task(self.p,self.p.task(self.roots))
        extra={'commentId':'f'*24,'text':'New sibling-stage comment','authorId':'b'*24,'parentCommentId':''}
        bridge.store_capture(self.p,self.p.task(self.replies),{'url':URL,'results':self.rows+[extra],'explicitEnd':False})
        rounds.index_task(self.p,self.p.task(self.replies))
        packet=rounds.review_next(self.p,self.roots,40)
        self.assertEqual([item['entity_id'] for item in packet['items']],['f'*24]);self.assertEqual(packet['snapshots'],[])
        self.write_receipt(packet);result=bridge.review(self.p,self.roots,self.receipt)
        self.assertEqual(result['status'],'pending');self.assertFalse(result['awaiting_review'])
        self.assertEqual(self.p.db.execute('select sum(reviewed) from entity_versions').fetchone()[0],37)
    def test_direct_native_record_remains_available_without_bridge_acquisition(self):
        before=self.p.db.execute('select count(*) from native_events').fetchone()[0]
        result=native.record(self.p,self.replies,self.receipt)
        self.assertIn('event_id',result)
        self.assertEqual(self.p.db.execute('select count(*) from native_events').fetchone()[0],before+1)
        self.assertNotIn('bridge',self.p.task(self.replies)['payload'])
        self.assertEqual(self.p.db.execute('select sum(reviewed) from entity_versions').fetchone()[0],0)

if __name__=='__main__':unittest.main()
