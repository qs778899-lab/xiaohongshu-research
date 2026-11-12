"""Offline scope/media/incremental review invariants; only temporary fixtures."""
import json
import hashlib
from pathlib import Path
import tempfile
import unittest

import xhs_project as xp

NID, CID, UID = 'a'*24, 'b'*24, 'c'*24


class ReviewStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.p=xp.Project(Path(self.temp.name)/'project',create=True);self.addCleanup(self.p.close)
        self.counter=0

    def ingest(self,operation,data,nid=NID,**extra):
        self.counter+=1
        raw={'operation':operation,'note_id':nid,'data':data,'captured_at':f'2026-10-31T00:{self.counter:02d}:00Z',**extra}
        path=self.p.path/f'raw{self.counter}.json';path.write_text(json.dumps(raw))
        self.p.ingest(path);self.p.db.commit();return path

    def note(self,images=None):
        self.ingest('note',{'items':[{'id':NID,'note_card':{'note_id':NID,'title':'Treatment','desc':'Saved full text','image_list':images or []}}]})
        return self.task('note')

    def task(self,kind,nid=NID):
        return self.p.task(self.p.db.execute('select id from tasks where kind=? and entity_key=?',(kind,nid)).fetchone()[0])

    def comments(self,nid=NID):
        self.ingest('comments',{'comments':[{'id':CID,'content':'Initial text','user_info':{'user_id':UID},'sub_comments':[]}],'has_more':False},nid,page=1)

    def browser(self,text='Initial text',nid=NID,**metadata):
        return self.ingest('browser_comments',[{'commentId':CID,'text':text,'userId':UID,'is_reply':False,**metadata}],nid,ok=True,
            coverage={'rootPaginationState':'complete','threads':[{'commentId':CID,'rootKey':'id:'+CID,'state':'visible_replies_read','processed':True,'expanders':0,'expectedReplies':0}]})

    def reviewed_pair(self,nid=NID):
        self.comments(nid);source=self.browser(nid=nid)
        for kind in ('comments','browser'):
            task=self.task(kind,nid);receipt=self.p.observed_review_plan(task['id'])
            receipt.update(reviewer='synthetic reader',all_current_documents_read=True)
            self.p.observed_review(task['id'],receipt,'All saved synthetic text read')
            self.p.review(task['id'],'reviewed','All synthetic text read')
        return source

    def test_relevant_only_queues_scope_and_preserves_previous_real_review(self):
        note=self.note();self.p.review(note['id'],'relevant','Relevant scope')
        self.assertEqual(self.task('note')['review_status'],'pending')
        self.assertEqual(self.task('comments')['status'],'pending')
        self.p.review(note['id'],'reviewed','No media; entire saved note read')
        self.p.review(note['id'],'relevant','Still relevant')
        self.assertEqual(self.task('note')['review_status'],'reviewed')

    def test_explicit_text_only_cannot_complete_even_media_free_note(self):
        note=self.note()
        for annotations in ({'scope':'full_saved_body_text_only'},{'media_and_comments_not_reviewed':True},{'all_body_media_actually_reviewed':False}):
            with self.assertRaises(xp.legacy.ResearchError):self.p.review(note['id'],'reviewed','Read text',annotations)
        self.assertEqual(self.p.db.execute('select count(*) from reviews').fetchone()[0],0)

    def test_pending_unreadable_and_partial_body_media_block_completion(self):
        note=self.note([{'url_default':'https://sns-webpic-qc.xhscdn.com/fixture'}])
        for status,review in [('pending','pending'),('blocked','pending'),('complete','pending')]:
            self.p.db.execute("update tasks set status=?,review_status=? where kind='media'",(status,review))
            with self.assertRaises(xp.legacy.ResearchError):self.p.review(note['id'],'reviewed','Full text only')
        image=self.p.path/'reviewed-fixture.image';image.write_bytes(b'synthetic image bytes')
        for row in self.p.db.execute("select id,payload from tasks where kind='media'").fetchall():
            item=json.loads(row['payload'])
            item.update(file=str(image),sha256=hashlib.sha256(image.read_bytes()).hexdigest())
            self.p.db.execute("update tasks set status='complete',review_status='reviewed',payload=? where id=?",(xp.dumps(item),row['id']))
        self.p.review(note['id'],'reviewed','Full image and text fixture reviewed')
        self.assertEqual(self.task('note')['review_status'],'reviewed')

    def test_missing_media_queue_is_detected_from_saved_body(self):
        note=self.note([{'url_default':'https://sns-webpic-qc.xhscdn.com/fixture','live_photo':True}])
        self.p.db.execute("delete from tasks where kind='media'")
        with self.assertRaisesRegex(xp.legacy.ResearchError,'missing_media_task'):self.p.review(note['id'],'reviewed','Claimed complete')

    def test_missing_live_photo_only_still_blocks_reviewed_static_image(self):
        note=self.note([{'url_default':'https://sns-webpic-qc.xhscdn.com/fixture','live_photo':True}])
        for row in self.p.db.execute("select id,payload from tasks where kind='media'").fetchall():
            if json.loads(row['payload'])['kind']=='video':self.p.db.execute('delete from tasks where id=?',(row['id'],))
            else:self.p.db.execute("update tasks set status='complete',review_status='reviewed' where id=?",(row['id'],))
        with self.assertRaisesRegex(xp.legacy.ResearchError,'missing_media_task'):self.p.review(note['id'],'reviewed','Static image read')

    def test_comment_media_does_not_gate_body_and_body_exclusion_is_valid(self):
        note=self.note([{'url_default':'https://sns-webpic-qc.xhscdn.com/body'}])
        self.p.db.execute("update tasks set status='excluded',review_status='excluded' where kind='media'")
        self.ingest('comments',{'comments':[{'id':CID,'content':'Image','pictures':[{'url_default':'https://sns-webpic-qc.xhscdn.com/comment'}]}],'has_more':False},page=1)
        self.p.review(note['id'],'reviewed','Body read; body image irrelevant by explicit scope')
        self.assertEqual(self.task('note')['review_status'],'reviewed')
        self.assertEqual(self.p.db.execute("select count(*) from tasks where kind='media' and review_status='pending'").fetchone()[0],1)

    def test_browser_text_change_reopens_only_same_note_pair_and_preserves_history(self):
        self.reviewed_pair();other='d'*24;self.reviewed_pair(other)
        history=[tuple(r) for r in self.p.db.execute('select * from reviews order by id')]
        self.browser('Changed wording')
        for kind in ('comments','browser'):
            self.assertEqual(self.task(kind)['review_status'],'pending')
            self.assertEqual(self.task(kind,other)['review_status'],'reviewed')
        self.assertEqual([tuple(r) for r in self.p.db.execute('select * from reviews order by id')],history)

    def test_exact_repeat_import_does_not_reopen_review(self):
        source=self.reviewed_pair();self.assertTrue(self.p.ingest(source)['cached'])
        self.assertEqual(self.task('comments')['review_status'],'reviewed')
        self.assertEqual(self.task('browser')['review_status'],'reviewed')

    def test_new_metadata_stales_exact_receipt_without_changed_text(self):
        self.reviewed_pair();self.browser(renderedImageLabels=['new visible picture'])
        self.assertEqual(self.task('comments')['review_status'],'pending')
        self.assertEqual(self.task('browser')['review_status'],'pending')
        self.assertEqual(self.task('comments')['status'],'complete')


if __name__=='__main__':unittest.main()
