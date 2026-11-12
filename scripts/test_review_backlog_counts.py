"""Cumulative read-only backlog remains separate from collection checkpoints."""
import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_bridge as fixture
import xhs_bridge as bridge
import xhs_project as xp
import xhs_rounds as rounds


class ReviewBacklogTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BridgeTests();self.f.setUp();self.p=self.f.project
        rounds.schema(self.p);self.tid=self.f.tid
        t=self.p.task(self.tid);t['payload']['stage']='roots'
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(t['payload']),self.tid))
        self.p.db.commit()
        rounds.create(self.p,'counts',{'cutoff':'2026-11-05T00:00:00+08:00',
            'doctors':['Synthetic doctor'],'employment_rule':'explicit source required','task_ids':[self.tid]})

    def tearDown(self):self.f.tearDown()

    def task(self):return self.p.task(self.tid)

    def capture(self,rows,**extra):
        return bridge.store_capture(self.p,self.task(),dict(url=fixture.URL,results=rows,explicitEnd=False,**extra))

    def row(self,cid,text='ordinary words',**extra):
        return dict(commentId=cid,text=text,parentCommentId='',author='Same nickname',**extra)

    def pending(self,**kwargs):return rounds.review_backlog(self.p,self.task(),**kwargs)

    def persisted(self):
        return {name:[tuple(r) for r in self.p.db.execute('SELECT * FROM '+name+' ORDER BY rowid')]
                for name in ['tasks','native_events','entity_versions','meta']}

    def test_unindexed_new_text_counts_while_batch_handoff_stays_false(self):
        for i in range(3):self.capture([self.row(str(i))])
        before=self.persisted();backlog=self.pending()
        self.assertEqual(backlog['pending_review_hash_count'],3)
        self.assertEqual(backlog['pending_review_entity_count'],3)
        self.assertEqual(backlog['pending_review_entity_version_count'],3)
        self.assertFalse(backlog['review_handoff_due'])
        self.assertFalse(self.task()['payload']['bridge']['awaiting_review'])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM entity_versions').fetchone()[0],0)
        self.assertEqual(self.persisted(),before)

    def test_smaller_snapshot_retains_earlier_unread_versions(self):
        self.capture([self.row('one'),self.row('two')])
        self.capture([self.row('one','changed words')])
        backlog=self.pending()
        self.assertEqual((backlog['pending_review_hash_count'],backlog['pending_review_entity_count'],
                          backlog['pending_review_entity_version_count']),(2,2,3))

    def test_partial_packet_read_reduces_entities_without_marking_hashes_read(self):
        self.capture([self.row(str(i)) for i in range(5)])
        packet=rounds.review_next(self.p,self.tid,2)
        rounds.commit_packet(self.p,self.tid,{'review_packet_id':packet['id'],
            'reviewed':True,'summary':'Actually read these two synthetic versions'},apply=True)
        backlog=self.pending()
        self.assertEqual(backlog['pending_review_entity_version_count'],3)
        self.assertEqual(backlog['pending_review_hash_count'],1)
        self.assertEqual(self.task()['payload']['bridge'].get('reviewed_hashes',[]),[])

    def test_metadata_change_does_not_mark_hashes_or_changed_words_read(self):
        self.capture([self.row('one',time='3 hours ago',likes=1)])
        packet=rounds.review_next(self.p,self.tid)
        rounds.commit_packet(self.p,self.tid,{'review_packet_id':packet['id'],
            'reviewed':True,'summary':'Actually read synthetic version'},apply=True)
        self.capture([self.row('one',time='4 hours ago',likes=2)])
        backlog=self.pending()
        self.assertEqual(backlog['pending_review_hash_count'],2)
        self.assertEqual(backlog['pending_review_entity_version_count'],0)
        self.capture([self.row('one','later words differ',time='4 hours ago',likes=2)])
        self.assertEqual(self.pending()['pending_review_entity_version_count'],1)

    def test_missing_ids_are_not_forcibly_merged_across_snapshots(self):
        self.capture([{'text':'same words','time':'one'}])
        self.capture([{'text':'same words','time':'two'}])
        self.assertEqual(self.pending()['pending_review_entity_count'],2)

    def test_same_nickname_different_public_accounts_are_distinct_versions(self):
        self.capture([self.row('one',authorHrefRaw='/user/profile/'+'a'*24)])
        self.capture([self.row('one',authorHrefRaw='/user/profile/'+'b'*24)])
        self.assertEqual(self.pending()['pending_review_entity_count'],1)
        self.assertEqual(self.pending()['pending_review_entity_version_count'],2)

    def test_empty_snapshot_is_pending_observation_not_proof_of_no_comments(self):
        self.capture([])
        backlog=self.pending()
        self.assertEqual(backlog['pending_review_hash_count'],1)
        self.assertEqual(backlog['pending_review_entity_count'],0)
        self.assertFalse(self.task()['payload']['bridge']['collection_end'])

    def test_helper_can_report_before_entity_index_schema_without_creating_it(self):
        self.capture([self.row('one')]);self.p.db.execute('DROP TABLE entity_versions');self.p.db.commit()
        self.assertEqual(self.pending()['pending_review_entity_version_count'],1)
        self.assertFalse(self.p.db.execute("SELECT 1 FROM sqlite_master WHERE name='entity_versions'").fetchone())

    def test_diagnostics_excluded_exactly_like_review_next_not_marked_reviewed(self):
        first=self.capture([])
        second=self.capture([self.row('two')])
        t=self.task();t['payload']['stage']='images'
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(t['payload']),self.tid));self.p.db.commit()
        h=Path(first['snapshot']).stem
        with patch.object(bridge,'capture_diagnostic_status',return_value={'confirmed_hashes':[h],'invalid':[]}):
            backlog=self.pending();packet=rounds.review_next(self.p,self.tid)
        self.assertEqual(backlog['pending_review_hash_count'],1)
        self.assertEqual(backlog['pending_review_entity_version_count'],1)
        self.assertEqual([s['hash'] for s in packet['snapshots']],[Path(second['snapshot']).stem])
        self.assertEqual(self.task()['payload']['bridge'].get('reviewed_hashes',[]),[])

    def test_invalid_diagnostic_is_not_excluded(self):
        self.capture([self.row('one')]);t=self.task();t['payload']['stage']='images'
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(t['payload']),self.tid));self.p.db.commit()
        with patch.object(bridge,'capture_diagnostic_status',return_value={'confirmed_hashes':[],
                'invalid':[{'reason':'changed diagnostic source'}]}):
            backlog=self.pending()
        self.assertEqual(backlog['pending_review_hash_count'],1)
        self.assertTrue(backlog['pending_review_diagnostic_gaps'])

    def test_changed_source_is_explicit_gap_and_later_report_rechecks(self):
        source=Path(self.capture([self.row('one')])['snapshot'])
        self.assertEqual(self.pending()['pending_review_entity_version_count'],1)
        source.write_text('changed source')
        backlog=self.pending()
        self.assertEqual(backlog['pending_review_hash_count'],1)
        self.assertFalse(backlog['pending_review_entity_counts_complete'])
        self.assertTrue(backlog['pending_review_source_gaps'])
        self.assertEqual(self.task()['payload']['bridge'].get('reviewed_hashes',[]),[])

    def test_symlink_source_is_gap_even_if_target_bytes_match(self):
        source=Path(self.capture([self.row('one')])['snapshot'])
        copy=source.with_name('separate-test-copy.json');copy.write_bytes(source.read_bytes())
        source.unlink();source.symlink_to(copy)
        self.assertTrue(self.pending()['pending_review_source_gaps'])

    def test_changed_previous_read_source_is_gap_not_silently_validated(self):
        source=Path(self.capture([self.row('one')])['snapshot']);t=self.task()
        t['payload']['bridge']['reviewed_hashes']=[source.stem]
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(t['payload']),self.tid));self.p.db.commit()
        source.write_text('tampered after prior reading')
        backlog=self.pending()
        self.assertEqual(backlog['pending_review_hash_count'],0)
        self.assertFalse(backlog['pending_review_entity_counts_complete'])
        self.assertTrue(backlog['pending_review_source_gaps'])

    def test_report_local_source_memo_reuses_hash_but_never_global_cache(self):
        source=Path(self.capture([self.row('one')])['snapshot']);memo={}
        with patch.object(Path,'read_bytes',autospec=True,side_effect=Path.read_bytes) as read:
            first=self.pending(memo=memo);second=self.pending(memo=memo)
        self.assertEqual(first,second)
        self.assertEqual(sum(call.args[0]==source for call in read.call_args_list),1)
        source.write_text('changed after the first report')
        self.assertTrue(self.pending()['pending_review_source_gaps'])

    def test_audit_status_summary_show_unread_without_changing_gate_or_stop(self):
        self.capture([self.row('one'),self.row('two')])
        self.p.set_meta('stop',{'reason':'synthetic stop retained'});self.p.db.commit()
        before=self.persisted()
        with patch.object(bridge,'Driver',side_effect=AssertionError('Report must not control browser')):
            audit=rounds.audit(self.p,'counts');status=rounds.status(self.p,'counts');summary=bridge.summary(self.p)
        row=audit['items'][0]
        self.assertEqual(row['category'],'acquired_needs_review')
        self.assertFalse(row['review_handoff_due'])
        self.assertEqual(status['stages']['roots']['pending_review_entity_version_count'],2)
        self.assertEqual(status['stages']['roots']['awaiting_review'],0)
        self.assertEqual(summary['awaiting_reading_scopes'],1)
        self.assertEqual(summary['review_handoff_scopes'],0)
        self.assertEqual(summary['pending_review_hash_count'],1)
        self.assertEqual(self.persisted(),before)

    def test_hold_and_stall_survive_backlog_reporting(self):
        self.capture([self.row('one')]);t=self.task();b=t['payload']['bridge']
        b.update(fault={'code':'COMMENT_PAGINATION_STALLED','hold':True},stalled_count=3)
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(t['payload']),self.tid));self.p.db.commit()
        before=copy.deepcopy(self.task())
        self.assertEqual(rounds.audit(self.p,'counts')['items'][0]['category'],'execution_gap')
        bridge.summary(self.p)
        self.assertEqual(self.task(),before)


if __name__=='__main__':unittest.main()
