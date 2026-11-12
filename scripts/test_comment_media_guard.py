"""Synthetic whole-comment media gates; no patient data or network."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import xhs_media as media
import xhs_project as xp

NID, CID, UID = 'a'*24, 'b'*24, 'c'*24
URL = 'https://sns-webpic.xhscdn.com/comment/synthetic-ordinary'


class CommentMediaGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.p=xp.Project(Path(self.temp.name)/'project',create=True);self.addCleanup(self.p.close)
        self.record={'operation':'browser_comments','ok':True,'note_id':NID,'captured_at':'2026-10-31T00:00:00Z',
            'data':[{'commentId':CID,'userId':UID,'authorHrefRaw':'/user/profile/'+UID,'is_reply':False,
                     'parentCommentId':'','text':'Synthetic complete visible words','images':[URL+'1',URL+'2']}],
            'coverage':{'rootPaginationState':'complete','threads':[]}}
        self.ingest(self.record)
        self.task=self.p.db.execute("SELECT id FROM tasks WHERE kind='browser'").fetchone()[0]
        self.comments=self.p.add_task('comments',NID,{'note_id':NID},15)
        self.p.db.execute("UPDATE tasks SET status='complete' WHERE id=?",(self.comments,));self.p.db.commit()
        self.folder=self.p.path/'media';self.folder.mkdir();self.manifest_path=self.folder/'manifest.json'

    def ingest(self,record):
        f=self.p.path/'input.json';f.write_text(json.dumps(record));v=self.p.ingest(f);self.p.db.commit()
        self.source=self.p.path/'snapshots'/(v['snapshot']+'.json')

    def manifest(self,partial=False):
        items={}
        for i,item in enumerate(media.extract_media(self.record,str(self.source))):
            f=self.folder/(item['media_id']+'.data');f.write_bytes(b'synthetic original media bytes')
            item.update(file=str(f),sha256=hashlib.sha256(f.read_bytes()).hexdigest(),acquisition_status='downloaded',
                acquired_at='2026-10-31T00:30:00Z',reviewed_at='2026-10-31T01:00:00Z',review_status='partial' if partial and i==1 else 'reviewed',
                observation='Synthetic credentials card: identity gray-redacted; only visible text read' if partial and i==1 else 'Synthetic complete visible image reading')
            if item['kind']=='video':item['checked_segments']=[{'full_video':True,'observation':'Synthetic entire silent video reviewed'}]
            items[item['media_id']]=item
        value={'format':'xiaohongshu-media-v2','items':items};self.save(value);return value

    def save(self,value):self.manifest_path.write_text(json.dumps(value))

    def assert_blocked(self):
        for task in [self.task,self.comments]:
            for decision in ['reviewed','relevant']:
                with self.assertRaisesRegex(xp.legacy.ResearchError,'Comment media remain unresolved'):
                    self.p.review(task,decision,'Synthetic whole comment completion request')

    def test_partial_ordinary_picture_blocks_whole_review_but_not_observed_text(self):
        self.manifest(partial=True);self.p.import_media(self.folder);self.assert_blocked()
        receipt=self.p.observed_review_plan(self.task);receipt.update(reviewer='Synthetic text reader',all_current_documents_read=True)
        self.p.observed_review(self.task,receipt,'Synthetic collected text read; ordinary image partial remains')
        self.assertEqual(self.p.task(self.task)['review_status'],'pending')
        self.assertTrue(self.p._inline_comment_media_gaps(NID), 'old helper remains a compatible strict alias')
        value=json.loads(self.manifest_path.read_text())
        for x in value['items'].values():x.update(review_status='reviewed',observation='Synthetic now all visible image content reviewed')
        self.save(value);self.p.import_media(self.folder)
        for task in [self.task,self.comments]:self.p.review(task,'reviewed','Synthetic complete text and media')
        self.assertEqual(self.p.task(self.task)['review_status'],'reviewed')

    def test_missing_queue_entry_is_detected_from_current_ordinary_metadata(self):
        self.p.db.execute("DELETE FROM tasks WHERE kind='media'")
        self.assertTrue(all('missing_media_task' in x for x in self.p._comment_media_gaps(NID)))
        self.assert_blocked()

    def test_api_pictures_and_nested_reply_media_cannot_escape_missing_queue_gate(self):
        record={'operation':'comments','ok':True,'note_id':NID,'page':1,'captured_at':'2026-10-31T01:00:00Z',
            'data':{'comments':[{'id':CID,'content':'Synthetic API words','user_info':{'user_id':UID},
              'pictures':[{'url':URL+'1'}],'sub_comment_count':1,'sub_comments':[{'id':'d'*24,'content':'Synthetic reply',
              'user_info':{'user_id':'e'*24},'pictures':[{'url':URL+'3'}]}]}],'has_more':False}}
        self.ingest(record);self.p.db.execute("DELETE FROM tasks WHERE kind='media'")
        gaps=self.p._comment_media_gaps(NID);self.assertEqual(len(gaps),2)
        self.assertTrue(all('missing_media_task' in x for x in gaps))

    def test_same_text_new_picture_and_partial_reimport_reopen_generic_review(self):
        self.manifest();self.p.import_media(self.folder);self.p.review(self.task,'reviewed','Synthetic initially read')
        before=self.p.db.execute('SELECT count(*) FROM reviews').fetchone()[0]
        value=json.loads(self.manifest_path.read_text());next(iter(value['items'].values()))['review_status']='partial'
        self.save(value);self.p.import_media(self.folder)
        self.assertEqual(self.p.task(self.task)['review_status'],'pending')
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM reviews').fetchone()[0],before)
        self.manifest();self.p.import_media(self.folder);self.p.review(self.task,'reviewed','Synthetic reread')
        self.record['captured_at']='2026-10-31T02:00:00Z';self.record['data'][0]['images'].append(URL+'3');self.ingest(self.record)
        self.assertEqual(self.p.task(self.task)['review_status'],'pending');self.assert_blocked()

    def test_file_content_missing_file_manifest_version_and_review_changes_are_stale(self):
        value=self.manifest();self.p.import_media(self.folder);item=next(iter(value['items'].values()));file=Path(item['file']);raw=file.read_bytes()
        file.write_bytes(b'changed');self.assert_blocked();file.write_bytes(raw)
        file.unlink();self.assert_blocked();file.write_bytes(raw)
        for change in [lambda x:x.update(resource_version=2),lambda x:x.update(review_status='partial'),lambda x:x.update(source_path='unrelated.json')]:
            changed=deepcopy(value);change(changed['items'][item['media_id']]);self.save(changed);self.assert_blocked()
        for malformed in [[],{'items':[]},{'format':'xiaohongshu-media-v2','items':{item['media_id']:[]}}]:
            self.save(malformed);self.assert_blocked()
        self.save(value);self.assertEqual(self.p._comment_media_gaps(NID),[])

    def test_paired_live_photo_requires_its_own_full_video_review(self):
        self.record['data'][0]['images']=[{'url':URL,'live_photo':True,'stream':{'h264':[{'master_url':'https://sns-video-bd.xhscdn.com/synthetic.mp4'}]}}]
        self.record['captured_at']='2026-10-31T00:10:00Z';self.ingest(self.record)
        # Retained old slots remain required; exclude them explicitly in this fixture.
        self.p.db.execute("UPDATE tasks SET status='excluded',review_status='excluded' WHERE kind='media' AND entity_key NOT IN ("+','.join('?' for _ in media.extract_media(self.record,str(self.source)))+")",tuple(x['media_id'] for x in media.extract_media(self.record,str(self.source))))
        value=self.manifest();vid=next(x for x in value['items'].values() if x['kind']=='video');vid['checked_segments']=[];self.save(value)
        self.p.import_media(self.folder);self.assert_blocked()
        vid['checked_segments']=[{'full_video':True,'observation':'Synthetic full video checked'}];self.save(value);self.p.import_media(self.folder)
        self.assertEqual(self.p._comment_media_gaps(NID),[])
        self.p.review(self.task,'reviewed','Synthetic ordinary image and paired full video read')

    def test_smaller_snapshot_does_not_erase_unread_retained_media(self):
        self.manifest(partial=True);self.p.import_media(self.folder)
        self.record['data']=[];self.record['captured_at']='2026-10-31T02:00:00Z';self.ingest(self.record)
        self.assert_blocked()

    def test_read_only_status_invalidates_old_whole_review_after_image_changes(self):
        value=self.manifest();self.p.import_media(self.folder)
        for task in [self.task,self.comments]:self.p.review(task,'reviewed','Synthetic fully read')
        self.p.db.execute("UPDATE tasks SET status='excluded',review_status='excluded' WHERE kind NOT IN ('comments','browser','media')")
        self.p.db.commit();self.assertEqual(self.p.status()['state'],'defined_visible_scope_processed')
        item=next(iter(value['items'].values()));Path(item['file']).write_bytes(b'changed after full review')
        before=[tuple(x) for x in self.p.db.execute('SELECT * FROM tasks ORDER BY id')];raw=self.manifest_path.read_bytes()
        status=self.p.status()
        self.assertEqual(status['state'],'work_remaining')
        invalid={x['task_id'] for x in status['invalid_review_bindings']}
        self.assertTrue({self.task,self.comments}<=invalid)
        self.assertEqual(sum(x['count'] for x in status['task_counts'] if x['kind']=='media' and x['review_status']=='pending'),1)
        self.assertEqual(before,[tuple(x) for x in self.p.db.execute('SELECT * FROM tasks ORDER BY id')])
        self.assertEqual(raw,self.manifest_path.read_bytes())

    def test_current_manifest_video_reading_revocation_cannot_use_old_db_claim(self):
        self.record['data'][0]['images']=[{'url':URL,'live_photo':True,'stream':{'h264':[{'master_url':'https://sns-video-bd.xhscdn.com/synthetic.mp4'}]}}]
        self.record['captured_at']='2026-10-31T00:10:00Z';self.ingest(self.record)
        current_ids={x['media_id'] for x in media.extract_media(self.record,str(self.source))}
        for row in self.p.db.execute("SELECT id,entity_key FROM tasks WHERE kind='media'").fetchall():
            if row['entity_key'] not in current_ids:self.p.db.execute("UPDATE tasks SET status='excluded',review_status='excluded' WHERE id=?",(row['id'],))
        value=self.manifest();self.p.import_media(self.folder)
        self.assertEqual(self.p._comment_media_gaps(NID),[])
        vid=next(x for x in value['items'].values() if x['kind']=='video');vid['checked_segments']=[];self.save(value)
        self.assert_blocked()
        self.assertTrue(self.p.status()['invalid_review_bindings'])

    def test_pending_parent_does_not_hide_invalid_reviewed_image_in_status(self):
        value=self.manifest();self.p.import_media(self.folder)
        self.assertEqual(self.p.task(self.task)['review_status'],'pending')
        item=next(iter(value['items'].values()));Path(item['file']).write_bytes(b'changed while text is pending')
        before=[tuple(x) for x in self.p.db.execute('SELECT * FROM tasks ORDER BY id')]
        status=self.p.status()
        self.assertEqual([x['media_id'] for x in status['invalid_review_bindings']],[item['media_id']])
        self.assertEqual(sum(x['count'] for x in status['task_counts'] if x['kind']=='media' and x['review_status']=='pending'),1)
        self.assertEqual(before,[tuple(x) for x in self.p.db.execute('SELECT * FROM tasks ORDER BY id')])

    def test_unknown_identity_keeps_source_bound_gap_without_inventing_media_ids(self):
        self.record['data'][0].pop('commentId');self.record['captured_at']='2026-10-31T02:00:00Z';self.ingest(self.record)
        known={x[0] for x in self.p.db.execute("SELECT entity_key FROM tasks WHERE kind='media'").fetchall()}
        gaps=self.p._comment_media_gaps(NID)
        self.assertTrue(any(x.startswith('source_bound_comment_media:') and x.endswith(':unknown_comment_identity') for x in gaps))
        self.assertTrue(all(x.startswith('source_bound_comment_media:') or x.split(':',1)[0] in known for x in gaps))


if __name__=='__main__':unittest.main()
