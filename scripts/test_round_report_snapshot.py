"""Report-local native health reuse never grants access or caches across reports."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bridge as fixture
import xhs_bridge as bridge
import xhs_native as native
import xhs_native_recovery as recovery
import xhs_project as xp
import xhs_rounds as rounds


class ReportSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BridgeTests();self.f.setUp();self.p=self.f.project
        self.ids=[self.f.tid];self.completed=[]
        for i in range(24):
            target=f'{i+1:024x}'
            payload={'stage':'roots' if i%2 else 'body','target':target,'next':'read original source',
                     'bridge':{'stalled_count':i%4,'cursor':{'scrollTop':i*100}}}
            if i==1:payload['bridge']['fault']={'code':'COMMENT_PAGINATION_STALLED','hold':True}
            if i==2:payload['bridge']['awaiting_review']=True
            if i==3:payload['bridge']['collection_end']=True
            tid=self.p.add_task('native',xp.dumps([payload['stage'],target,'']),payload)
            self.ids.append(tid)
            if i%5==0:
                event=f'event-{i}'
                source=self.p.path/f'event-{i}.json';source.write_text(json.dumps({'fixture':i}))
                artifact,sha=native.archive(self.p,source)
                receipt_file=self.p.path/f'receipt-{i}.json';receipt_file.write_text(json.dumps({'reviewed':True,'fixture':i}))
                receipt_archive,receipt_sha=native.archive(self.p,receipt_file)
                receipt={'receipt_archive':receipt_archive,'receipt_sha256':receipt_sha,
                         'reviewed':True,'corrects_observation_event':f'old-synthetic-{i}'}
                self.p.db.execute('INSERT INTO native_events VALUES(?,?,?,?,?,?,?,?)',
                    (event,tid,'offline','offline',xp.dumps(receipt),artifact,sha,'2026-11-01T00:00:00+00:00'))
                payload['completion_event']=event
                self.p.db.execute("UPDATE tasks SET status='complete',review_status='reviewed',payload=? WHERE id=?",(xp.dumps(payload),tid))
                self.completed.append((tid,self.p.path/receipt_archive))
        # Real channel holds are computed from event rows, never a fake snapshot.
        for i in range(2):
            self.p.db.execute('INSERT INTO native_events VALUES(?,?,?,?,?,?,?,?)',
                (f'failure-{i}',self.ids[0],'scroll','no_effect','{}','','','2026-11-01T00:00:01+00:00'))
        self.p.db.commit()
        rounds.create(self.p,'snapshot',{'cutoff':'2026-11-05T00:00:00+08:00',
            'doctors':['合成医生'],'employment_rule':'explicit source required','task_ids':self.ids})
        self.no_driver=patch.object(bridge,'Driver',side_effect=AssertionError('No browser calls in a report'))
        self.no_driver.start()

    def tearDown(self):self.no_driver.stop();self.f.tearDown()

    def persisted(self):
        return {'tasks':[tuple(r) for r in self.p.db.execute('SELECT * FROM tasks ORDER BY id')],
                'events':[tuple(r) for r in self.p.db.execute('SELECT * FROM native_events ORDER BY id')],
                'stop':self.p.get_meta('stop')}

    def measured(self,operation):
        before=self.persisted()
        with patch.object(native,'channel_health',wraps=native.channel_health) as health, \
             patch.object(recovery,'native_project_stop_applies',wraps=recovery.native_project_stop_applies) as stop, \
             patch.object(self.p,'task',wraps=self.p.task) as task:
            result=operation()
        self.assertEqual(health.call_count,1);self.assertEqual(stop.call_count,1)
        self.assertEqual(task.call_count,len(self.ids))
        self.assertTrue(all(call.kwargs.get('native_dispatch') is not None for call in task.call_args_list))
        self.assertEqual(self.persisted(),before)
        return result

    def test_audit_uses_one_real_snapshot_with_original_gap_categories(self):
        report=self.measured(lambda:rounds.audit(self.p,'snapshot'))
        categories={row['category'] for row in report['items']}
        self.assertTrue({'completed_record','unprocessed','execution_gap','acquired_needs_review','acquisition_end_needs_receipt'}<=categories)
        self.assertTrue(report['dry_run']);self.assertTrue(report['semantic_decisions_required'])

    def test_project_wide_audit_also_uses_one_snapshot(self):
        report=self.measured(lambda:rounds.audit(self.p))
        self.assertEqual(report['count'],len(self.ids))

    def test_status_keeps_completion_checks_independent_of_health(self):
        with patch.object(native,'valid_completion',wraps=native.valid_completion) as completion:
            report=self.measured(lambda:rounds.status(self.p,'snapshot'))
        self.assertEqual(completion.call_count,len(self.completed))
        self.assertEqual(report['invalid_completion_tasks'],[])
        self.assertEqual(report['pending'],len(self.ids)-len(self.completed))
        self.assertEqual(report['outcome'],'remaining_gaps')
        self.assertEqual(report['stages']['roots']['execution_gaps'],1)

    def test_export_reuses_same_task_and_audit_snapshot(self):
        report=self.measured(lambda:rounds.export(self.p,'snapshot'))
        out=Path(report['directory'])
        audit=json.loads((out/'队列审计.json').read_text())
        status=json.loads((out/'覆盖与缺口.json').read_text())
        self.assertEqual(audit,rounds.audit(self.p,'snapshot'))
        self.assertEqual(status,rounds.status(self.p,'snapshot'))

    def test_following_report_rechecks_new_stop_and_keeps_channel_hold(self):
        def actions():
            seen=[];original=self.p.task
            def capture(*args,**kwargs):
                task=original(*args,**kwargs);seen.append(task['next_action']);return task
            with patch.object(self.p,'task',side_effect=capture):rounds.audit(self.p,'snapshot')
            return seen
        self.assertIn('channel_probe_required_no_repeat_action',actions())
        self.p.set_meta('stop',{'reason':'new account access restriction'});self.p.db.commit()
        self.assertEqual(set(actions()),{'preserve_access_stop'})
        self.assertEqual(self.p.get_meta('stop'),{'reason':'new account access restriction'})

    def test_following_report_rechecks_tampered_completion_archive(self):
        self.assertEqual(rounds.status(self.p,'snapshot')['invalid_completion_tasks'],[])
        tid,source=self.completed[0];source.write_text('changed after earlier report')
        report=self.measured(lambda:rounds.status(self.p,'snapshot'))
        self.assertIn(tid,report['invalid_completion_tasks'])
        self.assertEqual(self.p.task(tid)['status'],'complete')
        self.assertEqual(report['outcome'],'remaining_gaps')

    def test_export_still_marks_changed_evidence_source_invalid(self):
        source=self.p.path/'evidence.txt';source.write_text('真实来源仍须逐项核对')
        manifest=self.p.path/'evidence.json';manifest.write_text(json.dumps({'evaluation_entries':[{
            'id':'S001','note_id':fixture.NOTE,'source':source.name,'url':fixture.URL,
            'summary':'年龄归属待核，不得自动完成'}]}))
        rounds.import_evidence(self.p,manifest)
        source.write_text('changed')
        self.assertEqual(rounds.export(self.p,'snapshot')['invalid_evidence_sources'],[])
        evidence=json.loads(self.p.db.execute('SELECT body FROM evidence_versions WHERE id=?',('S001',)).fetchone()[0])
        Path(evidence['source_file']).write_text('changed archived evidence')
        result=self.measured(lambda:rounds.export(self.p,'snapshot'))
        self.assertEqual(result['invalid_evidence_sources'],['S001'])
        index=json.loads((Path(result['directory'])/'原帖索引.json').read_text())
        self.assertTrue(index[0]['source_invalid'])

    @unittest.skipUnless(os.environ.get('XHS_BASELINE_ROUNDS_MODULE'),'optional immutable baseline module not supplied')
    def test_outputs_equal_unchanged_baseline_module(self):
        path=Path(os.environ['XHS_BASELINE_ROUNDS_MODULE'])
        spec=importlib.util.spec_from_file_location('original_rounds_snapshot_test',path)
        original=importlib.util.module_from_spec(spec);spec.loader.exec_module(original)
        self.assertEqual(rounds.audit(self.p,'snapshot'),original.audit(self.p,'snapshot'))
        self.assertEqual(rounds.status(self.p,'snapshot'),original.status(self.p,'snapshot'))
        before=self.persisted()
        old_result=original.export(self.p,'snapshot');out=Path(old_result['directory'])
        old_files={p.name:p.read_bytes() for p in out.iterdir() if p.is_file()}
        self.assertEqual(rounds.export(self.p,'snapshot'),old_result)
        self.assertEqual({p.name:p.read_bytes() for p in out.iterdir() if p.is_file()},old_files)
        self.assertEqual(self.persisted(),before)


if __name__=='__main__':unittest.main()
