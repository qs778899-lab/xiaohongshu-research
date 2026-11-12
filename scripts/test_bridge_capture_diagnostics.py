"""Synthetic local-only failures never become content reads or completion."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import xhs_bridge as b
import xhs_native as native
import xhs_project as xp
import xhs_rounds as rounds

NOTE='a'*24
URL='https://www.xiaohongshu.com/explore/'+NOTE+'?xsec_token=observed-synthetic'
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6kU8AAAAASUVORK5CYII=')


def sha(file):return hashlib.sha256(Path(file).read_bytes()).hexdigest()


class CaptureDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.p=xp.Project(self.root/'project',create=True);rounds.schema(self.p)
        source=self.root/'source.json';source.write_text(json.dumps({'url':URL}))
        manifest=self.root/'manifest.json';manifest.write_text(json.dumps({'units':[
            {'stage':'images','target':NOTE,'sources':[str(source)],'next':'Read comments attachments too.'}]}))
        self.tid=native.import_manifest(self.p,manifest)['task_ids'][0]
        self.old=self.capture('old-blank');self.good=self.capture('later-readable')
        record=self.good['data']['media_captures'][0]
        self.p.db.execute('INSERT INTO media_inventory VALUES(?,?,?,?,?)',
                          (NOTE,'body:1','image',xp.dumps(record),'captured_unreviewed'));self.p.db.commit()
        self.visual={'target':NOTE,'action':'media','outcome':'ready','observed_at':'2026-11-05T10:20:00+00:00',
            'summary':'Synthetic fixture attests actually read replacement image, comments still pending.',
            'source_file':record['file'],'url':URL,'target_verified':True,'content_ready':True,
            'view_consistent':True,'content_observed':True,'reviewed':True,'complete':False,
            'media_verified':True,'media_reviewed_keys':['body:1'],'positions':['body:1'],
            'bridge_snapshot_hashes':[self.good['hash']],'gaps':['comment attachments not scanned']}
        vf=self.root/'visual.json';vf.write_text(json.dumps(self.visual))
        self.event=b.review(self.p,self.tid,vf)['event_id']
        task=self.p.task(self.tid);task['payload']['bridge']['fault']={'code':'MEDIA_EMPTY_OR_UNLOADED','hold':True}
        task['payload']['pending_action']='media'
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),self.tid));self.p.db.commit()
        self.p.set_meta('stop',{'reason':'Original API STOP remains','at':'2026-11-05T00:00:00+00:00'})
        self.p.db.commit()
        old=self.old['data']['media_captures'][0]
        self.receipt={'kind':'failed_media_capture_diagnostic_v1','task_id':self.tid,'target':NOTE,
            'snapshot_hash':self.old['hash'],'classification':'target_image_region_blank',
            'reviewer':'codex','inspection_method':'view_image','visual_checked':True,
            'target_context_verified':True,'media_content_readable':False,
            'visual_observation':'Synthetic fixture: title visible, target image region entirely blank.',
            'inspected_at':'2026-11-05T11:00:00+00:00','media_key':'body:1','position':1,
            'png_file':old['file'],'png_sha256':old['sha256'],
            'sidecar_file':str(Path(old['file']).with_suffix('.json')),
            'sidecar_sha256':sha(Path(old['file']).with_suffix('.json')),
            'replacement_event_id':self.event,'content_observed':False,'reviewed':False,
            'complete':False,'comment_media_scan_complete':False}
        self.rf=self.root/'diagnosis.json';self.write()

    def tearDown(self):self.p.close();self.tmp.cleanup()

    def write(self,receipt=None):self.rf.write_text(json.dumps(receipt or self.receipt));return self.rf

    def capture(self,name):
        d=self.p.path/'bridge/media'/NOTE;d.mkdir(parents=True,exist_ok=True)
        png=d/(name+'.png');png.write_bytes(PNG+name.encode())
        cap={'position':1,'count':1,'kind':'image','file':str(png),'url':URL,
             'observed_at':'2026-11-05T10:00:00+00:00','reviewed':False}
        png.with_suffix('.json').write_text(json.dumps(cap));cap['sha256']=sha(png)
        data={'url':URL,'body_carousel_only':True,'captured_positions':[1],'carousel_count':1,
              'comment_media_scan_complete':False,'media_captures':[cap],'video_requires_segments':False}
        r=b.store_capture(self.p,self.p.task(self.tid),data)
        return {'data':data,'file':Path(r['snapshot']),'hash':Path(r['snapshot']).stem}

    def preserved(self):
        task=self.p.task(self.tid);payload=copy.deepcopy(task['payload'])
        payload['bridge'].pop('awaiting_review',None)
        return {'payload':payload,'status':task['status'],'review_status':task['review_status'],
            'turn':task['turn'],'meta':[tuple(r) for r in self.p.db.execute('SELECT * FROM meta ORDER BY key')],
            'events':[tuple(r) for r in self.p.db.execute('SELECT * FROM native_events ORDER BY id')],
            'media':[tuple(r) for r in self.p.db.execute('SELECT * FROM media_inventory ORDER BY media_key')],
            'coverage':native.coverage(self.p)}

    def test_acknowledgement_changes_only_ledger_and_handoff_not_content_or_stops(self):
        before=self.preserved();files={f:f.read_bytes() for f in (self.p.path/'native-sources').iterdir()}
        result=b.confirm_capture_diagnostic(self.p,self.tid,self.rf)
        self.assertFalse(result['awaiting_review']);self.assertEqual(result['media_read_count_added'],0)
        self.assertEqual(before,self.preserved())
        self.assertEqual(files,{f:f.read_bytes() for f in files})
        self.assertEqual(b.capture_diagnostic_status(self.p,self.p.task(self.tid))['confirmed_hashes'],[self.old['hash']])
        self.assertEqual(self.p.task(self.tid)['payload']['bridge']['reviewed_hashes'],[self.good['hash']])
        self.assertTrue(b.confirm_capture_diagnostic(self.p,self.tid,self.rf)['duplicate'])
        self.p.close();self.p=xp.Project(self.root/'project')
        self.assertEqual(b.capture_diagnostic_status(self.p,self.p.task(self.tid))['confirmed_hashes'],[self.old['hash']])

    def test_missing_or_false_visual_diagnosis_and_scope_forgery_rejected_without_writes(self):
        variations=[('visual_checked',False),('inspection_method','metadata'),('reviewer','automatic'),
            ('media_content_readable',True),('classification','success'),('target','b'*24),
            ('task_id','not-this-task'),('content_observed',True),('reviewed',True),
            ('complete',True),('comment_media_scan_complete',True),('position',2),
            ('media_key','body:2'),('sidecar_file',str(self.root/'unrelated.json')),
            ('replacement_event_id','missing'),('inspected_at','2026-11-05T11:00:00')]
        before=self.preserved();changes=self.p.db.total_changes
        for key,value in variations:
            with self.subTest(key=key):
                changed=dict(self.receipt);changed[key]=value
                with self.assertRaises((b.BridgeError,ValueError)):b.confirm_capture_diagnostic(self.p,self.tid,self.write(changed))
                self.assertEqual(changes,self.p.db.total_changes);self.assertEqual(before,self.preserved())
        self.assertFalse((self.p.path/'bridge/capture-diagnostics').exists())

    def test_raw_png_sidecar_and_replacement_tampering_fail_before_any_write(self):
        event=self.p.db.execute('SELECT * FROM native_events WHERE id=?',(self.event,)).fetchone();value=json.loads(event['receipt'])
        files=[self.old['file'],Path(self.receipt['png_file']),Path(self.receipt['sidecar_file']),
               Path(self.good['data']['media_captures'][0]['file']),self.p.path/event['artifact'],
               self.p.path/value['receipt_archive']]
        before=self.preserved();changes=self.p.db.total_changes
        for file in files:
            with self.subTest(file=file.name):
                original=file.read_bytes();file.write_bytes(original+b'changed')
                with self.assertRaises((b.BridgeError,ValueError)):b.confirm_capture_diagnostic(self.p,self.tid,self.rf)
                file.write_bytes(original)
                self.assertEqual(changes,self.p.db.total_changes);self.assertEqual(before,self.preserved())

    def test_successfully_read_image_is_never_diagnosed_as_failed(self):
        r=dict(self.receipt);cap=self.good['data']['media_captures'][0]
        r.update(snapshot_hash=self.good['hash'],png_file=cap['file'],png_sha256=cap['sha256'],
                 sidecar_file=str(Path(cap['file']).with_suffix('.json')),
                 sidecar_sha256=sha(Path(cap['file']).with_suffix('.json')))
        with self.assertRaisesRegex(b.BridgeError,'NOT_UNREAD'):b.confirm_capture_diagnostic(self.p,self.tid,self.write(r))
        # Forging the handoff cache cannot erase the actual prior reading event.
        task=self.p.task(self.tid);task['payload']['bridge']['reviewed_hashes']=[]
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),self.tid));self.p.db.commit()
        with self.assertRaisesRegex(b.BridgeError,'ALREADY_VISUALLY_REVIEWED'):b.confirm_capture_diagnostic(self.p,self.tid,self.write(r))

    def test_post_confirmation_tampering_restores_pending_diagnostic_without_mutating_rows(self):
        result=b.confirm_capture_diagnostic(self.p,self.tid,self.rf);archive=Path(result['source_archive'])
        for file in [archive,self.old['file'],Path(self.receipt['png_file']),Path(self.receipt['sidecar_file'])]:
            with self.subTest(file=file.name):
                original=file.read_bytes();file.write_bytes(original+b'changed');changes=self.p.db.total_changes
                status=b.capture_diagnostic_status(self.p,self.p.task(self.tid))
                self.assertEqual(status['confirmed_hashes'],[]);self.assertEqual(len(status['invalid']),1)
                self.assertEqual(b.summary(self.p)['awaiting_reading_scopes'],1)
                self.assertEqual(changes,self.p.db.total_changes);file.write_bytes(original)

    def test_same_capture_and_later_partial_review_do_not_resurrect_diagnosed_hashes(self):
        b.confirm_capture_diagnostic(self.p,self.tid,self.rf)
        b.store_capture(self.p,self.p.task(self.tid),self.good['data'])
        self.assertFalse(self.p.task(self.tid)['payload']['bridge']['awaiting_review'])
        vf=self.root/'visual-repeat.json';vf.write_text(json.dumps(dict(self.visual,summary='Repeat existing visual source, no new image.')))
        self.assertFalse(b.review(self.p,self.tid,vf)['awaiting_review'])
        new=self.capture('new-unread-content');self.assertTrue(self.p.task(self.tid)['payload']['bridge']['awaiting_review'])
        self.assertNotIn(self.old['hash'],self.p.task(self.tid)['payload']['bridge']['reviewed_hashes'])
        self.assertNotIn(new['hash'],b.capture_diagnostic_status(self.p,self.p.task(self.tid))['confirmed_hashes'])

    def test_malformed_replacement_receipt_fails_closed_without_reclassifying_content(self):
        b.confirm_capture_diagnostic(self.p,self.tid,self.rf)
        self.p.db.execute('UPDATE native_events SET receipt=? WHERE id=?',('[]',self.event));self.p.db.commit()
        # Deliberately corrupt a synthetic DB row after confirmation. A reader
        # must restore the diagnostic gap rather than crash or accept the cache.
        task={'id':self.tid,'kind':'native','payload':json.loads(self.p.db.execute(
            'SELECT payload FROM tasks WHERE id=?',(self.tid,)).fetchone()[0])}
        changes=self.p.db.total_changes
        status=b.capture_diagnostic_status(self.p,task)
        self.assertEqual(status['confirmed_hashes'],[]);self.assertEqual(len(status['invalid']),1)
        self.assertEqual(changes,self.p.db.total_changes)
        self.assertEqual(task['payload']['bridge']['reviewed_hashes'],[self.good['hash']])

    def test_diagnostic_hash_cannot_be_falsely_added_to_content_review(self):
        b.confirm_capture_diagnostic(self.p,self.tid,self.rf);before=self.preserved()
        bad=dict(self.visual,bridge_snapshot_hashes=[self.old['hash']]);vf=self.root/'bad-review.json';vf.write_text(json.dumps(bad))
        with self.assertRaisesRegex(b.BridgeError,'DIAGNOSTIC_SNAPSHOT_IS_NOT_CONTENT'):b.review(self.p,self.tid,vf)
        self.assertEqual(before,self.preserved())

    def test_diagnostic_does_not_allow_completion_without_comment_media_scan(self):
        b.confirm_capture_diagnostic(self.p,self.tid,self.rf);before=self.preserved()
        bad=dict(self.visual,complete=True,end='all_positions_read',gaps=[]);vf=self.root/'bad-complete.json';vf.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError,'Comment attachment scan'):b.review(self.p,self.tid,vf)
        self.assertEqual(before,self.preserved())

    def test_round_packet_excludes_only_valid_diagnostics_and_returns_them_if_proof_changes(self):
        b.confirm_capture_diagnostic(self.p,self.tid,self.rf)
        packet=rounds.review_next(self.p,self.tid)
        self.assertEqual(packet['snapshots'],[])
        self.assertEqual(packet['capture_diagnostics']['confirmed_hashes'],[self.old['hash']])
        self.assertEqual(self.p.task(self.tid)['payload']['bridge']['reviewed_hashes'],[self.good['hash']])
        side=Path(self.receipt['sidecar_file']);old=side.read_bytes();side.write_bytes(old+b' ')
        packet=rounds.review_next(self.p,self.tid)
        self.assertEqual([s['hash'] for s in packet['snapshots']],[self.old['hash']])
        self.assertTrue(packet['capture_diagnostics']['invalid']);side.write_bytes(old)

    def test_same_key_or_inspection_time_cannot_be_rebound_by_rehashing_sidecar(self):
        side=Path(self.receipt['sidecar_file']);old=side.read_bytes();data=json.loads(old)
        data['url']=URL.replace(NOTE,'b'*24);side.write_text(json.dumps(data))
        receipt=dict(self.receipt,sidecar_sha256=sha(side))
        with self.assertRaisesRegex(b.BridgeError,'SIDECAR_MISMATCH'):b.confirm_capture_diagnostic(self.p,self.tid,self.write(receipt))
        side.write_bytes(old)
        receipt=dict(self.receipt,inspected_at='2026-11-04T11:00:00+00:00')
        with self.assertRaisesRegex(b.BridgeError,'TIME_REQUIRED'):b.confirm_capture_diagnostic(self.p,self.tid,self.write(receipt))

    def test_interrupted_confirmation_rolls_back_acknowledgement_and_is_retryable(self):
        before=self.preserved()
        with patch.object(b,'capture_diagnostic_status',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):b.confirm_capture_diagnostic(self.p,self.tid,self.rf)
        self.assertEqual(before,self.preserved())
        self.assertFalse(self.p.db.execute("SELECT 1 FROM sqlite_master WHERE name='bridge_capture_diagnostics'").fetchone())
        self.assertTrue(self.p.task(self.tid)['payload']['bridge']['awaiting_review'])
        self.assertTrue(b.confirm_capture_diagnostic(self.p,self.tid,self.rf)['diagnostic_confirmed'])

    def test_concurrent_capture_after_first_validation_preserves_new_hash_and_progress(self):
        validate=b._validate_capture_diagnostic;published=[];new_hash='f'*64
        def validate_then_capture(project,task,receipt):
            result=validate(project,task,receipt)
            if not published:
                other=sqlite3.connect(self.p.path/'project.sqlite3')
                try:
                    payload=json.loads(other.execute('SELECT payload FROM tasks WHERE id=?',(self.tid,)).fetchone()[0])
                    payload['bridge']['hashes'].append(new_hash)
                    payload['bridge']['cursor']={'last_media_key':'comment:real:1','position':2}
                    payload['next']='New concurrent comment attachment remains unread.'
                    other.execute('UPDATE tasks SET payload=?,turn=turn+1 WHERE id=?',(xp.dumps(payload),self.tid))
                    other.commit();published.append(copy.deepcopy(payload))
                finally:other.close()
            return result
        old_turn=self.p.task(self.tid)['turn']
        with patch.object(b,'_validate_capture_diagnostic',side_effect=validate_then_capture):
            result=b.confirm_capture_diagnostic(self.p,self.tid,self.rf)
        self.assertTrue(result['awaiting_review'])
        task=self.p.task(self.tid)
        self.assertEqual(task['payload'],published[0]);self.assertEqual(task['turn'],old_turn+1)
        self.assertEqual(task['payload']['bridge']['reviewed_hashes'],[self.good['hash']])
        self.assertEqual(b.capture_diagnostic_status(self.p,task)['confirmed_hashes'],[self.old['hash']])

    def test_concurrent_completion_after_first_validation_rejects_diagnostic_without_reverting_completion(self):
        validate=b._validate_capture_diagnostic;published=[]
        def validate_then_complete(project,task,receipt):
            result=validate(project,task,receipt)
            if not published:
                other=sqlite3.connect(self.p.path/'project.sqlite3')
                try:
                    payload=json.loads(other.execute('SELECT payload FROM tasks WHERE id=?',(self.tid,)).fetchone()[0])
                    payload['next']='Concurrent reviewer completed the range.'
                    other.execute("UPDATE tasks SET payload=?,status='complete',review_status='reviewed' WHERE id=?",
                                  (xp.dumps(payload),self.tid));other.commit();published.append(copy.deepcopy(payload))
                finally:other.close()
            return result
        with patch.object(b,'_validate_capture_diagnostic',side_effect=validate_then_complete):
            with self.assertRaisesRegex(b.BridgeError,'PENDING_TASK_REQUIRED'):
                b.confirm_capture_diagnostic(self.p,self.tid,self.rf)
        task=self.p.task(self.tid)
        self.assertEqual(task['status'],'complete');self.assertEqual(task['review_status'],'reviewed')
        self.assertEqual(task['payload'],published[0])
        self.assertFalse(self.p.db.execute("SELECT 1 FROM sqlite_master WHERE name='bridge_capture_diagnostics'").fetchone())

    def test_explicit_cli_is_local_only_and_keeps_original_meta_events_and_media(self):
        before=self.preserved()
        cmd=[sys.executable,'-B',str(Path(b.__file__)),'--project',str(self.p.path),
             'confirm-capture-diagnostic','--task',self.tid,'--receipt',str(self.rf)]
        result=subprocess.run(cmd,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertEqual(before,self.preserved());self.assertFalse(json.loads(result.stdout)['data']['awaiting_review'])
        missing=self.root/'does-not-exist';cmd[cmd.index('--project')+1]=str(missing)
        result=subprocess.run(cmd,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertFalse(missing.exists())


if __name__=='__main__':unittest.main()
