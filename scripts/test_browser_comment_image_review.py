"""Synthetic ordinary browser image audits; no transport or real-image claims."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import xhs_media as media
import xhs_project as xp
import xhs_observed_review as review

NID, CID, UID, PARENT = "a" * 24, "b" * 24, "c" * 24, "d" * 24
URL = "https://sns-webpic.xhscdn.com/comment/synthetic-picture.webp"


class BrowserCommentImageReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.p = xp.Project(Path(self.temp.name) / "project", create=True); self.addCleanup(self.p.close)
        self.record = {"operation": "browser_comments", "ok": True, "note_id": NID,
            "captured_at": "2026-10-31T00:00:00Z", "coverage": {"rootPaginationState": "pending", "threads": []},
            "data": [{"commentId": CID, "parentCommentId": PARENT, "userId": UID,
                "authorHrefRaw": "/user/profile/" + UID, "is_reply": True, "text": "[图片评论]",
                "textSource": "media_placeholder", "renderedReplyPrefixOnly": True,
                "renderedText": "回复 synthetic target :", "replyToCommentId": PARENT,
                "replyToIdentity": "observed_id", "images": [URL, URL, URL]}]}
        self.ingest()
        self.task = self.p.db.execute("SELECT id FROM tasks WHERE kind='browser'").fetchone()[0]

    def ingest(self):
        path = self.p.path / "incoming.json"; path.write_text(json.dumps(self.record))
        result = self.p.ingest(path); self.p.db.commit()
        self.source = self.p.path / "snapshots" / (result["snapshot"] + ".json")

    def build(self):
        receipt = self.p.observed_review_plan(self.task)
        receipt.update(reviewer="Synthetic reader assertion", all_current_documents_read=True)
        document = next(d for d in receipt['documents'] if d['document_id'].endswith(':' + CID))
        manifest = {"format": "xiaohongshu-media-v2", "items": {}}
        self.manifest_path = self.p.path / "media-manifest.json"
        images = []
        for item in media.extract_media(self.record, str(self.source)):
            if item['kind'] != 'image':
                continue
            path = self.p.path / (item["media_id"] + ".image")
            path.write_bytes(b"synthetic identical picture bytes; no actual user media")
            item.update(acquisition_status="downloaded", review_status="reviewed", file=str(path),
                sha256=review.sha(path.read_bytes()), acquired_at="2026-10-31T01:00:00Z")
            manifest["items"][item["media_id"]] = item
            images.append({"media_id": item["media_id"], "locator": item["locator"],
                "image_ordinal": int(item['locator'].rsplit('/', 1)[1]), "observed_url": item["observed_url"],
                "resource_fingerprint": media.resource_identity(item), "resource_version": item["resource_version"],
                "manifest_path": str(self.manifest_path), "acquisition_sha256": review.sha(review.canonical(review.acquisition_binding(item)).encode()),
                "file": str(path), "sha256": item["sha256"], "actual_view_image": True, "review_status": "reviewed",
                "viewed_at": "2026-10-31T02:00:00Z", "observation": "Synthetic test assertion for this image slot"})
        self.manifest_path.write_text(json.dumps(manifest))
        audit = {"format": review.BROWSER_COMMENT_IMAGES_FORMAT, "reviewer": "Synthetic reader", "note_id": NID, "comment_id": CID,
            "raw_browser_source": {"document_id": document["document_id"], "user_id": UID,
                "parent_comment_id": self.record["data"][0].get("parentCommentId") or "", "snapshot_id": self.source.stem,
                "sha256": self.source.stem, "context_sha256": document["context_sha256"]},
            "observation": "All ordinary picture slots read; synthetic assertion, not a screenshot", "images": images}
        self.audit_path = self.p.path / "ordinary-audit.json"
        self.save(receipt, audit)
        return receipt, audit

    def save(self, receipt, audit):
        self.audit_path.write_text(json.dumps(audit))
        receipt["visual_observations"] = [{"document_id": "comment:" + NID + ":" + CID,
            "audit_path": str(self.audit_path), "audit_sha256": review.sha(self.audit_path.read_bytes())}]

    def validate(self, receipt):
        return review.validate_receipt(self.p.db, self.p.path, self.task, receipt)

    def test_three_same_url_slots_are_distinct_and_validation_is_read_only(self):
        receipt, audit = self.build()
        self.assertEqual(len({x['sha256'] for x in audit['images']}), 1)
        self.assertEqual(len({x['media_id'] for x in audit['images']}), 3)
        before = [tuple(r) for r in self.p.db.execute('SELECT * FROM tasks ORDER BY id')]
        manifest = self.manifest_path.read_bytes()
        result = self.validate(receipt)
        self.assertEqual((result['state'], result['visual_documents'], result['whole_task_reviewed']), ('current', 1, False))
        self.assertEqual(before, [tuple(r) for r in self.p.db.execute('SELECT * FROM tasks ORDER BY id')])
        self.assertEqual(manifest, self.manifest_path.read_bytes())
        self.assertNotIn('screenshot', audit)

    def test_root_empty_comment_with_original_image_can_be_reviewed(self):
        # A fresh fixture, not a reply-to-root identity rewrite in saved evidence.
        self.p = xp.Project(Path(self.temp.name) / "root-project", create=True); self.addCleanup(self.p.close)
        self.record['data'][0].update(is_reply=False, parentCommentId='', replyToCommentId='', text='', renderedReplyPrefixOnly=False)
        self.ingest(); self.task=self.p.db.execute("SELECT id FROM tasks WHERE kind='browser'").fetchone()[0]
        receipt, _ = self.build()
        self.assertEqual(self.validate(receipt)['state'], 'current')

    def test_missing_duplicate_unviewed_pending_partial_or_empty_observation_rejected(self):
        receipt, audit = self.build()
        changes = [lambda a:a['images'].pop(), lambda a:a['images'].__setitem__(1, deepcopy(a['images'][0])),
            lambda a:a['images'][0].update(actual_view_image=False), lambda a:a['images'][0].update(review_status='pending'),
            lambda a:a['images'][0].update(review_status='partial'), lambda a:a['images'][0].update(observation='')]
        for change in changes:
            candidate=deepcopy(audit);change(candidate);self.save(receipt,candidate)
            with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)

    def test_manifest_pending_or_partial_cannot_be_overridden_by_audit(self):
        receipt, audit=self.build();manifest=json.loads(self.manifest_path.read_text())
        for status in ['pending','partial','unreadable']:
            manifest['items'][audit['images'][0]['media_id']]['review_status']=status
            self.manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)

    def test_strict_ordinal_version_url_and_file_binding(self):
        receipt,audit=self.build()
        for key,value in [('image_ordinal',True),('image_ordinal',0),('image_ordinal',2),('resource_version',True),
                          ('resource_version',2),('observed_url',URL+'?unseen=1'),('resource_fingerprint','0'*64),
                          ('acquisition_sha256','0'*64),('sha256','0'*64),('locator','comment/'+CID+'/image/4')]:
            candidate=deepcopy(audit);candidate['images'][0][key]=value;self.save(receipt,candidate)
            with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)

    def test_wrong_comment_uid_parent_context_source_and_reviewer_rejected(self):
        receipt,audit=self.build()
        for key,value in [('user_id','e'*24),('parent_comment_id','e'*24),('context_sha256','f'*64),
                          ('snapshot_id','f'*64),('sha256','f'*64),('document_id','wrong')]:
            candidate=deepcopy(audit);candidate['raw_browser_source'][key]=value;self.save(receipt,candidate)
            with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)
        for key,value in [('reviewer',''),('comment_id','e'*24),('note_id','e'*24),('observation','')]:
            candidate=deepcopy(audit);candidate[key]=value;self.save(receipt,candidate)
            with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)

    def test_inline_and_api_formats_do_not_accept_ordinary_browser_images(self):
        receipt,audit=self.build()
        for fmt in [review.BROWSER_INLINE_IMAGES_FORMAT,review.API_IMAGES_FORMAT]:
            candidate=deepcopy(audit);candidate['format']=fmt
            if fmt==review.API_IMAGES_FORMAT:candidate['raw_api_source']=candidate.pop('raw_browser_source')
            self.save(receipt,candidate)
            with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)

    def test_ordinary_format_rejects_inline_only_and_mixed_content(self):
        for pictures in [[],[URL]]:
            self.record['data'][0].update(images=pictures,renderedInlineImages=[{'domOrdinal':1,'url':URL,'observedClass':'note-content-emoji'}])
            self.ingest();receipt,_=self.build()
            with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)

    def test_missing_sources_placeholder_and_paired_live_photo_remain_gaps(self):
        for pictures in [[],[''],['https://example.test/not-allowed'],[{'url':URL,'live_photo':True}]]:
            self.record['data'][0]['images']=pictures;self.ingest();receipt,_=self.build()
            with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)

    def test_source_or_image_corruption_invalidates_prior_reading(self):
        receipt,audit=self.build();path=Path(audit['images'][0]['file']);raw=path.read_bytes()
        path.write_bytes(b'changed')
        with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)
        path.write_bytes(raw);self.source.write_bytes(self.source.read_bytes()+b'\n')
        with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)

    def test_reading_before_source_or_acquisition_rejected(self):
        receipt,audit=self.build()
        for at in ['2026-10-30T23:59:00Z','2026-10-31T00:30:00Z','not-a-date']:
            audit['images'][0]['viewed_at']=at;self.save(receipt,audit)
            with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)

    def test_same_uid_parent_or_reply_target_changed_download_is_rejected(self):
        receipt,audit=self.build()
        for key in ['parentCommentId','replyToCommentId']:
            self.record['captured_at']='2026-10-31T00:30:00Z';self.record['data'][0][key]='e'*24;self.ingest()
            with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)
            fresh=self.p.observed_review_plan(self.task);fresh.update(reviewer='Synthetic',all_current_documents_read=True)
            candidate=deepcopy(audit);candidate['raw_browser_source'].update(snapshot_id=self.source.stem,sha256=self.source.stem,
                parent_comment_id=self.record['data'][0]['parentCommentId'],context_sha256=fresh['documents'][0]['context_sha256'])
            self.save(fresh,candidate)
            with self.assertRaises(xp.legacy.ResearchError):self.validate(fresh)

    def test_same_nickname_changed_uid_and_missing_real_comment_id_rejected(self):
        receipt,_=self.build();self.record['data'][0].update(userId='e'*24,authorHrefRaw='/user/profile/'+'e'*24)
        self.ingest()
        with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)
        self.record['data'][0].pop('commentId');self.ingest()
        with self.assertRaises(xp.legacy.ResearchError):self.p.observed_review_plan(self.task)

    def test_resource_url_or_manifest_version_change_requires_new_binding(self):
        receipt,audit=self.build();manifest=json.loads(self.manifest_path.read_text())
        manifest['items'][audit['images'][0]['media_id']]['resource_version']=2
        self.manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)
        self.record['captured_at']='2026-10-31T00:30:00Z';self.record['data'][0]['images'][0]=URL+'/replacement'
        self.ingest();fresh=self.p.observed_review_plan(self.task);fresh.update(reviewer='Synthetic',all_current_documents_read=True)
        fresh['visual_observations']=receipt['visual_observations']
        with self.assertRaises(xp.legacy.ResearchError):self.validate(fresh)

    def test_no_automatic_full_text_or_whole_task_completion(self):
        receipt,_=self.build();receipt['all_current_documents_read']=False
        with self.assertRaises(xp.legacy.ResearchError):self.validate(receipt)
        self.record['data'].append({'commentId':'f'*24,'userId':UID,'is_reply':False,'text':''})
        self.ingest();fresh,_=self.build()
        with self.assertRaises(xp.legacy.ResearchError):self.validate(fresh)

    def test_mixed_text_image_audit_does_not_fill_uncollected_expanded_text(self):
        self.record['data'][0].update(text='Visible excerpt … 展开',textSource='rendered_text',renderedReplyPrefixOnly=False)
        self.ingest();receipt,_=self.build();result=self.validate(receipt)
        self.assertFalse(result['whole_task_reviewed'])
        self.assertEqual(self.p.task(self.task)['review_status'],'pending')
        self.assertIn('uncollected_comments_or_replies',result['does_not_certify'])


if __name__=='__main__':unittest.main()
