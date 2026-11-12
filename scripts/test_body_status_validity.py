"""Old text-only states must not count as fully read notes. Offline fixtures."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import xhs_media as media
from test_media import Response
import xhs_project as xp

NID='a'*24

class BodyStatusValidityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.p=xp.Project(Path(self.tmp.name)/'project',create=True);self.addCleanup(self.p.close)

    def note(self, image=True):
        card={'note_id':NID,'title':'Fixture','desc':'Saved body text','image_list':[{'url_default':'https://sns-webpic-qc.xhscdn.com/fixture'}] if image else []}
        f=self.p.path/'raw.json';f.write_text(json.dumps({'operation':'note','note_id':NID,'captured_at':'2026-10-31T00:00:00Z','data':{'items':[{'id':NID,'note_card':card}]}}))
        self.p.ingest(f);self.p.db.commit()
        return self.p.db.execute("SELECT id FROM tasks WHERE kind='note'").fetchone()[0]

    def legacy_text_review(self, tid):
        self.p.db.execute("INSERT INTO reviews(task_id,decision,summary,annotations,at) VALUES(?,?,?,?,?)",(tid,'relevant','Only saved text read',xp.dumps({'review_scope':'saved_note_text_only','media_reviewed':False}),'2026-10-30'))
        self.p.db.execute("UPDATE tasks SET review_status='reviewed' WHERE id=?",(tid,));self.p.db.commit()

    def count(self, status, review):
        return sum(x['count'] for x in status['task_counts'] if x['kind']=='note' and x['review_status']==review)

    def test_legacy_text_only_is_effectively_pending_without_mutation(self):
        tid=self.note();self.legacy_text_review(tid)
        before=[tuple(r) for r in self.p.db.execute('SELECT * FROM reviews')]
        s=self.p.status()
        self.assertEqual(self.count(s,'reviewed'),0);self.assertEqual(self.count(s,'pending'),1)
        self.assertEqual(s['outstanding_tasks'],2) # body and its image
        self.assertEqual(s['invalid_review_bindings'][0]['reason'],'current_body_media_unresolved')
        self.assertEqual(self.p.task(tid)['review_status'],'reviewed')
        self.assertEqual(before,[tuple(r) for r in self.p.db.execute('SELECT * FROM reviews')])
        self.assertEqual(s['outstanding_tasks'],self.p.status()['outstanding_tasks'])

    def test_missing_media_task_cannot_make_old_body_complete(self):
        tid=self.note();self.legacy_text_review(tid);self.p.db.execute("DELETE FROM tasks WHERE kind='media'")
        s=self.p.status();self.assertEqual(s['outstanding_tasks'],1)
        self.assertIn('missing_media_task',s['invalid_review_bindings'][0]['media_gaps'][0])

    def test_already_pending_note_is_not_double_counted(self):
        self.note();s=self.p.status();self.assertEqual(s['outstanding_tasks'],2)
        self.assertEqual(s['invalid_review_bindings'],[])

    def test_excluded_body_does_not_reopen(self):
        tid=self.note();self.legacy_text_review(tid)
        self.p.db.execute("UPDATE tasks SET status='excluded',review_status='excluded'")
        s=self.p.status();self.assertEqual(s['outstanding_tasks'],0)
        self.assertEqual(s['invalid_review_bindings'],[])

    def test_body_without_media_stays_reviewed(self):
        tid=self.note(False);self.p.review(tid,'reviewed','Entire media-free body read')
        s=self.p.status();self.assertEqual(self.count(s,'reviewed'),1)
        self.assertEqual(s['invalid_review_bindings'],[])

    def test_resolving_media_removes_effective_gap(self):
        tid=self.note();self.legacy_text_review(tid)
        self.assertEqual(self.count(self.p.status(),'pending'),1)
        # Explicitly out-of-scope fixture image is an allowed resolution.
        self.p.db.execute("UPDATE tasks SET status='excluded',review_status='excluded' WHERE kind='media'")
        s=self.p.status();self.assertEqual(self.count(s,'reviewed'),1)
        self.assertEqual(s['outstanding_tasks'],0)

    def test_status_does_not_scan_all_media_for_each_note(self):
        tid=self.note();self.legacy_text_review(tid)
        for i in range(1,30):
            nid=f'{i:024x}'
            raw={'operation':'note','note_id':nid,'data':{'items':[{'id':nid,'note_card':{
                'note_id':nid,'title':'Fixture','desc':'Actual saved body',
                'image_list':[{'url_default':'https://sns-webpic-qc.xhscdn.com/fixture'+str(i)}]}}]}}
            f=self.p.path/f'body-{i}.json';f.write_text(json.dumps(raw));self.p.ingest(f)
            self.p.db.execute("UPDATE tasks SET review_status='reviewed' WHERE kind='note' AND entity_key=?",(nid,))
        statements=[];self.p.db.set_trace_callback(statements.append)
        try:s=self.p.status()
        finally:self.p.db.set_trace_callback(None)
        full_media_scans=[q for q in statements if "FROM tasks WHERE kind='media'" in q and 'SELECT' in q]
        self.assertLessEqual(len(full_media_scans),2)
        self.assertEqual(self.count(s,'pending'),30)

    def acquired_image(self):
        directory=self.p.path/'media'
        items=media.inspect_sources([self.p.path/'raw.json'],directory)['items']
        mid=next(iter(items));url=items[mid]['observed_url']
        media.download_media(directory,mid,opener=lambda *a,**k:Response(b'synthetic-image',url,'image/png'))
        media.review_media(directory,mid,'Synthetic fixture content reviewed','reviewed')
        self.p.import_media(directory)
        return Path(media.get_item(directory,mid)[2]['file'])

    def test_missing_or_changed_reviewed_image_is_effectively_pending(self):
        tid=self.note();self.legacy_text_review(tid);file=self.acquired_image();original=file.read_bytes()
        self.assertEqual(self.count(self.p.status(),'reviewed'),1)
        for replacement in (b'changed-image',None):
            if replacement is None:file.unlink()
            else:file.write_bytes(replacement)
            status=self.p.status()
            self.assertEqual(self.count(status,'pending'),1)
            self.assertEqual(status['outstanding_tasks'],2) # one media and one body
            self.assertEqual(self.p.task(tid)['review_status'],'reviewed')
            file.write_bytes(original)
            self.assertEqual(self.count(self.p.status(),'reviewed'),1)

    def test_image_hash_cached_only_within_each_status(self):
        tid=self.note();self.legacy_text_review(tid);file=self.acquired_image()
        opened=[];original=Path.open
        def tracked(path,*args,**kwargs):
            if path==file and args and args[0]=='rb':opened.append(path)
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,'open',tracked):
            self.assertEqual(self.count(self.p.status(),'reviewed'),1)
            self.assertEqual(len(opened),1)
            self.assertEqual(self.count(self.p.status(),'reviewed'),1)
            self.assertEqual(len(opened),2)

if __name__=='__main__':unittest.main()
