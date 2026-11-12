"""Synthetic receipts: frames may be observed but cannot mark whole video reviewed."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import xhs_bridge as bridge
import xhs_native as native
import xhs_project as xp
import xhs_rounds as rounds

NOTE='a'*24
URL='https://www.xiaohongshu.com/explore/'+NOTE+'?xsec_token=synthetic-observed'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class MediaReviewScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.p=xp.Project(self.root/'project',create=True)
        rounds.schema(self.p)
        source=self.root/'source.json';source.write_text(json.dumps({'url':URL}))
        manifest=self.root/'manifest.json';manifest.write_text(json.dumps({'units':[
            {'stage':stage,'target':NOTE,'sources':[str(source)],'next':'Read actual media type and retain partial gaps'} for stage in ['images','video']]}))
        ids=native.import_manifest(self.p,manifest)['task_ids'];self.ids={self.p.task(t)['payload']['stage']:t for t in ids}
        self.image=self.root/'image.png';self.image.write_bytes(b'synthetic static image already visually checked by fixture')
        self.frame=self.root/'video-frame.png';self.frame.write_bytes(b'synthetic one frame only, not a video timeline')
        self.records=[]
        for i,(kind,file) in enumerate([('image',self.image),('video',self.frame)],1):
            record={'position':i,'count':2,'kind':kind,'file':str(file),'sha256':sha(file),'reviewed':False}
            self.records.append(record)
            self.p.db.execute('INSERT INTO media_inventory VALUES(?,?,?,?,?)',(NOTE,f'body:{i}',kind,rounds.packed(record),'captured_unreviewed'))
        self.p.db.commit();self.hashes={}
        for stage,tid in self.ids.items():
            result=bridge.store_capture(self.p,self.p.task(tid),{'url':URL,'carousel_count':2,'media_captures':self.records,
                'captured_positions':[1,2],'video_requires_segments':True,'body_carousel_only':True,'comment_media_scan_complete':False})
            self.hashes[stage]=Path(result['snapshot']).stem
        self.scan=self.root/'comment-scan.json';self.scan.write_text('{"synthetic_actual_comment_attachment_scan":0}')
        self.scan_proof={'source_file':str(self.scan),'sha256':sha(self.scan),'complete':True}
    def tearDown(self):self.p.close();self.tmp.cleanup()
    def task(self,stage):return self.p.task(self.ids[stage])
    def receipt(self,stage,keys=None,complete=False):
        keys=keys if keys is not None else ['body:1' if stage=='images' else 'body:2']
        return {'target':NOTE,'action':'media','outcome':'ready','observed_at':'2026-11-08T12:00:00+00:00',
                'summary':'Synthetic fixture: exact static picture or a partial video frame; unviewed scope remains.',
                'source_file':str(self.image if stage=='images' else self.frame),'url':URL,
                'target_verified':True,'content_ready':True,'view_consistent':True,'content_observed':True,
                'reviewed':True,'complete':complete,'media_verified':True,'media_reviewed_keys':keys,
                'positions':keys or ['absence-scan'],'bridge_snapshot_hashes':[self.hashes[stage]],
                'gaps':[] if complete else ['Remaining media or video timeline not reviewed'],
                **({'end':'all_relevant_segments_verified' if stage=='video' else 'all_positions_read',
                    'comment_media_scan':self.scan_proof} if complete else {})}
    def submit(self,stage,receipt):
        file=self.root/'receipt.json';file.write_text(json.dumps(receipt));return bridge.review(self.p,self.ids[stage],file)
    def states(self):return dict(self.p.db.execute('SELECT media_key,state FROM media_inventory WHERE note_id=? ORDER BY media_key',(NOTE,)))
    def ledger(self):
        return {'tasks':[tuple(r) for r in self.p.db.execute('SELECT * FROM tasks ORDER BY id')],
                'events':[tuple(r) for r in self.p.db.execute('SELECT * FROM native_events ORDER BY id')],
                'media':[tuple(r) for r in self.p.db.execute('SELECT * FROM media_inventory ORDER BY media_key')]}
    def full_video(self):
        receipt=self.receipt('video',complete=True);segments=[]
        for i,(start,end) in enumerate([(0,4),(4,12)]):
            file=self.root/f'actual-verified-segment-{i}.mp4';file.write_bytes(('synthetic local full segment '+str(i)).encode())
            segments.append({'start':start,'end':end,'source_file':str(file),'sha256':sha(file),'verified':True})
        receipt['video_review']={'body:2':{'duration':12,'segments':segments}}
        receipt['source_file']=segments[0]['source_file'];return receipt
    def test_mixed_image_video_partial_receipt_rejected_atomically_before_native_event(self):
        before=self.ledger();receipt=self.receipt('images',['body:1','body:2'])
        with self.assertRaisesRegex(ValueError,'Images receipt cannot review video'):self.submit('images',receipt)
        self.assertEqual(before,self.ledger())
    def test_video_partial_frame_receipt_can_record_event_without_whole_video_inventory_review(self):
        before=self.states();events=self.p.db.execute('SELECT count(*) FROM native_events').fetchone()[0]
        result=self.submit('video',self.receipt('video'))
        self.assertTrue(result['event_id']);self.assertEqual(self.states(),before)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM native_events').fetchone()[0],events+1)
        self.assertEqual(self.task('video')['status'],'pending')
    def test_video_task_cannot_review_static_picture_key(self):
        before=self.ledger()
        with self.assertRaisesRegex(ValueError,'Video receipt cannot review non-video'):self.submit('video',self.receipt('video',['body:1']))
        self.assertEqual(before,self.ledger())
    def test_images_only_partial_remains_supported_and_does_not_review_paired_video(self):
        result=self.submit('images',self.receipt('images'))
        self.assertTrue(result['event_id']);self.assertEqual(self.states(),{'body:1':'reviewed','body:2':'captured_unreviewed'})
        self.assertEqual(self.task('images')['status'],'pending')
    def test_images_completion_can_leave_video_for_separate_scope(self):
        self.submit('images',self.receipt('images',complete=True))
        self.assertEqual(self.states(),{'body:1':'reviewed','body:2':'captured_unreviewed'})
        self.assertEqual(self.task('images')['status'],'complete');self.assertEqual(self.task('video')['status'],'pending')
    def test_original_valid_complete_video_timeline_can_review_whole_video(self):
        self.submit('video',self.full_video())
        self.assertEqual(self.states(),{'body:1':'captured_unreviewed','body:2':'reviewed'})
        self.assertEqual(self.task('video')['status'],'complete')
    def test_png_frame_and_complete_flag_without_timeline_cannot_complete_video(self):
        before=self.ledger()
        with self.assertRaisesRegex(ValueError,'Video review incomplete'):self.submit('video',self.receipt('video',complete=True))
        self.assertEqual(before,self.ledger())
    def test_missing_unverified_changed_and_gapped_segments_do_not_review_whole_video(self):
        for change in ['missing','gap','unverified','changed']:
            with self.subTest(change=change):
                receipt=self.full_video();segments=receipt['video_review']['body:2']['segments']
                if change=='missing':segments.pop()
                elif change=='gap':segments[1]['start']=5
                elif change=='unverified':segments[1]['verified']=False
                else:Path(segments[1]['source_file']).write_bytes(b'changed segment')
                before=self.ledger()
                with self.assertRaises(ValueError):self.submit('video',receipt)
                self.assertEqual(before,self.ledger())
    def test_video_partial_with_complete_timeline_still_does_not_claim_whole_video(self):
        receipt=self.full_video();receipt.update(complete=False,gaps=['Only partial reading is attested'])
        before=self.states();self.submit('video',receipt);self.assertEqual(self.states(),before)
        self.assertEqual(self.task('video')['status'],'pending')
    def test_truthy_nonboolean_complete_does_not_mark_whole_video_reviewed(self):
        for value in [1,'true']:
            with self.subTest(value=value):
                receipt=self.full_video();receipt['complete']=value
                before=self.states();rounds.review_media(self.p,self.task('video'),receipt,apply=True)
                self.assertEqual(self.states(),before)
    def test_explicit_zero_no_video_compatibility(self):
        self.p.db.execute("DELETE FROM media_inventory WHERE kind='video'");self.p.db.commit()
        proof=self.root/'absence-video.json';proof.write_text('{"visible_video_count":0}')
        receipt={'complete':True,'media_reviewed_keys':[],'end':'explicit_zero',
                 'absence_scan':{'kind':'video','count':0,'complete':True,'reviewer':'codex','source_file':str(proof),'sha256':sha(proof)},
                 'comment_media_scan':self.scan_proof}
        before=self.states();rounds.review_media(self.p,self.task('video'),receipt,apply=True);self.assertEqual(before,self.states())
    def test_explicit_zero_no_images_does_not_change_existing_video_state(self):
        self.p.db.execute("DELETE FROM media_inventory WHERE kind='image'");self.p.db.commit()
        proof=self.root/'absence-image.json';proof.write_text('{"visible_image_count":0}')
        receipt={'complete':True,'media_reviewed_keys':[],'end':'explicit_zero',
                 'absence_scan':{'kind':'images','count':0,'complete':True,'reviewer':'codex','source_file':str(proof),'sha256':sha(proof)},
                 'comment_media_scan':self.scan_proof}
        before=self.states();rounds.review_media(self.p,self.task('images'),receipt,apply=True);self.assertEqual(before,self.states())
    def test_preexisting_reviewed_video_is_not_automatically_downgraded(self):
        self.p.db.execute("UPDATE media_inventory SET state='reviewed' WHERE kind='video'");self.p.db.commit()
        before=self.states();rounds.review_media(self.p,self.task('video'),self.receipt('video'),apply=True)
        self.assertEqual(before,self.states())
    def test_apply_false_valid_partial_never_updates_inventory(self):
        before=self.ledger();rounds.review_media(self.p,self.task('images'),self.receipt('images'),apply=False)
        self.assertEqual(before,self.ledger())


if __name__=='__main__':unittest.main()
