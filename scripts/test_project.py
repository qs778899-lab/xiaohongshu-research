#!/usr/bin/env python3
"""Offline behavioral tests; never reads cookies, launches Chrome or contacts XHS."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_project as xp

NID = "0123456789abcdef01234567"
UID = "abcdef0123456789abcdef01"
URL = "https://www.xiaohongshu.com/explore/" + NID + "?xsec_token=observed&xsec_source=pc_search"


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.p = xp.Project(self.base / "project", create=True)
        self.p.plan({"target": "上海测试医生", "queries": ["测试医生 正畸"]})
        self.run = self.base / "session"
        self.run.mkdir()
        xp.legacy.write_json(self.run / "run.json", {"format": xp.legacy.FORMAT, "directory": str(self.run), "closed": False})
        self.counter = 0

    def tearDown(self):
        self.p.close()
        self.temp.cleanup()

    def snapshot(self, op, data, **extra):
        self.counter += 1
        if op == "note":
            for item in data.get("items", []):
                if isinstance(item.get("note_card"), dict):
                    item.setdefault("id", extra.get("note_id", NID))
                    item["note_card"].setdefault("note_id", extra.get("note_id", NID))
        obj = {"operation": op, "data": data, "captured_at": "2026-10-30T08:%02d:00+00:00" % self.counter, **extra}
        file = self.base / ("raw-%d.json" % self.counter)
        file.write_text(json.dumps(obj), encoding="utf-8")
        result = self.p.ingest(file)
        self.p.db.commit()
        return file, result

    def search(self, page=1, more=True, ids=None, sort="general"):
        ids = ids or [NID]
        return self.snapshot("search", {"items": [{"id": x, "model_type": "note", "xsec_token": "observed", "note_card": {"display_title": "求助"}} for x in ids], "has_more": more},
                             query="测试医生 正畸", sort=sort, api_sort=xp.SORTS[sort], page=page)

    def note(self):
        return self.snapshot("note", {"items": [{"note_card": {"title": "治疗记录", "desc": "原医生和后续医生不同", "user": {"user_id": UID}, "image_list": []}}]}, note_id=NID, url=URL)

    def comments(self, roots, more=False, cursor="", page=1):
        return self.snapshot("comments", {"comments": roots, "has_more": more, "cursor": cursor}, note_id=NID, url=URL, page=page, cursor_in="" if page == 1 else "c1")

    def task(self, kind, key=None):
        if key is None:
            row = self.p.db.execute("SELECT id FROM tasks WHERE kind=? ORDER BY id", (kind,)).fetchone()
        else: row = self.p.db.execute("SELECT id FROM tasks WHERE kind=? AND entity_key=?", (kind, key)).fetchone()
        return self.p.task(row[0])

    def test_plan_has_all_three_real_sort_mappings(self):
        tasks = self.p.next(100)
        self.assertEqual({t["payload"]["api_sort"] for t in tasks}, set(xp.SORTS.values()))

    def test_next_page_and_acquired_not_reviewed(self):
        self.search()
        self.assertEqual(self.task("search", "测试医生 正畸\0general")["next_page"], 2)
        self.note()
        task = self.task("note", NID)
        self.assertEqual((task["status"], task["review_status"]), ("complete", "pending"))
        self.assertEqual(self.p.status()["state"], "work_remaining")

    def test_idempotent_import(self):
        file, result = self.search()
        self.assertTrue(self.p.ingest(file)["cached"])
        self.assertEqual(self.p.status()["snapshots"], 1)
        self.assertEqual(len(self.p.next(100, kinds=["note"])), 1)

    def test_same_query_different_sorts_do_not_share_cursor(self):
        self.search(page=1, more=False, sort="latest")
        self.assertEqual(self.task("search", "测试医生 正畸\0latest")["status"], "complete")
        self.assertEqual(self.task("search", "测试医生 正畸\0general")["status"], "pending")

    def test_repeated_page_is_blocked_not_exhausted(self):
        self.search()
        self.search(page=2)
        task = self.task("search", "测试医生 正畸\0general")
        self.assertEqual(task["status"], "blocked")
        self.assertIn("not_proof", task["reason"])

    def test_empty_has_more_is_not_end(self):
        self.snapshot("search", {"items": [], "has_more": True}, query="测试医生 正畸", sort="general", page=1)
        self.assertEqual(self.task("search", "测试医生 正畸\0general")["status"], "blocked")

    def test_real_end_only_completes_pagination_not_review(self):
        self.comments([{"id": "r1", "content": "第一楼", "sub_comments": []}])
        task = self.task("comments", NID)
        self.assertEqual((task["status"], task["review_status"]), ("complete", "pending"))
        self.assertEqual(self.task("browser", NID)["status"], "pending")

    def test_author_second_page_and_triage(self):
        self.snapshot("author_notes", {"notes": [{"note_id": NID, "title": "生活", "xsec_token": "x"}], "cursor": "a1", "has_more": True}, user_id=UID, page=1)
        self.assertTrue(self.task("note", NID)["payload"]["needs_triage"])
        self.assertEqual(self.task("author", UID)["cursor"], "a1")
        self.snapshot("author_notes", {"notes": [{"note_id": "1123456789abcdef01234567", "title": "早期点名医生", "xsec_token": "x"}], "cursor": "", "has_more": False}, user_id=UID, page=2, cursor_in="a1")
        self.assertEqual(self.task("author", UID)["status"], "complete")
        self.assertEqual(len(self.p.next(100, kinds=["note"])), 2)

    def test_cursor_stall_is_blocked(self):
        self.comments([{"id": "r1", "content": "a"}], more=True, cursor="c1")
        self.comments([{"id": "r2", "content": "b"}], more=True, cursor="c1", page=2)
        self.assertEqual(self.task("comments", NID)["reason"], "cursor_missing_or_stalled")

    def test_more_than_200_threads_and_late_identity_preserved(self):
        roots = [{"id": "r%d" % i, "content": "是目标医生" if i == 220 else "普通楼层", "sub_comments": []} for i in range(250)]
        self.comments(roots)
        self.assertEqual(self.p.db.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 250)
        self.assertEqual(len(self.p.search_local("目标医生")), 1)

    def test_browser_smaller_snapshot_cannot_erase_prior_documents(self):
        rows = [{"rank": i, "commentId": "r%d" % i, "text": "正文" + str(i), "is_reply": False} for i in range(30)]
        self.snapshot("browser_comments", rows, note_id=NID, url=URL, ok=True, coverage={})
        self.snapshot("browser_comments", rows[:5], note_id=NID, url=URL, ok=True, coverage={})
        self.assertEqual(self.p.db.execute("SELECT COUNT(*) FROM documents WHERE kind='root_comment'").fetchone()[0], 30)

    def test_api_and_browser_merge_only_real_ids(self):
        self.comments([{"id": "r1", "content": "相同文本"}])
        self.snapshot("browser_comments", [{"rank": 1, "commentId": "r1", "text": "相同文本", "is_reply": False}, {"rank": 2, "text": "相同文本", "is_reply": False}], note_id=NID, url=URL, ok=True, coverage={})
        self.assertEqual(self.p.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)

    def test_partial_browser_empty_expanders_is_not_complete(self):
        self.comments([{"id": "r%d" % i, "content": "x"} for i in range(25)])
        threads = [{"rootKey": "id:r%d" % i, "commentId": "r%d" % i, "identityKind": "platform_id", "processed": True, "state": "visible_replies_read", "expanders": 0} for i in range(20)]
        self.snapshot("browser_comments", [], note_id=NID, url=URL, ok=True, coverage={"threads": threads, "rootPaginationState": "complete"})
        self.assertEqual(self.task("browser", NID)["status"], "pending")
        self.assertEqual(self.p.db.execute("SELECT COUNT(*) FROM threads WHERE status='pending'").fetchone()[0], 5)

    def test_browser_complete_requires_all_known_roots(self):
        self.comments([{"id": "r1", "content": "x"}])
        self.snapshot("browser_comments", [], note_id=NID, url=URL, ok=True, coverage={"threads": [{"commentId": "r1", "rootKey": "id:r1", "processed": True, "state": "visible_replies_read", "expanders": 0}], "rootPaginationState": "complete"})
        self.assertEqual(self.task("browser", NID)["status"], "complete")

    def test_semantic_review_explicit_and_exclusion_reason(self):
        self.note()
        task = self.task("note", NID)
        self.p.review(task["id"], "relevant", "有正畸记录，继续评论")
        self.assertEqual(self.task("note", NID)["review_status"], "pending")
        self.assertEqual(self.task("comments", NID)["status"], "pending")
        with self.assertRaises(xp.legacy.ResearchError): self.p.review(self.task("comments", NID)["id"], "reviewed", "尚未采集不能已读")

    def test_context_evidence_preserves_missing_identity_and_counter_context(self):
        _, result = self.note()
        ev = {"attribution": "context_supported", "summary": "同账号提到治疗不适", "sources": [{"snapshot_id": result["snapshot"]}], "missing_confirmation": "后文未再次写医生姓名", "counter_context": "另有改善记录"}
        self.p.evidence(ev)
        stored = json.loads(self.p.db.execute("SELECT payload FROM evidence").fetchone()[0])
        self.assertEqual(stored["counter_context"], "另有改善记录")
        del ev["missing_confirmation"]
        with self.assertRaises(xp.legacy.ResearchError): self.p.evidence(ev)

    def test_bad_snapshot_does_not_leave_partial_success(self):
        before = self.p.status()["snapshots"]
        with self.assertRaises(xp.legacy.ResearchError):
            self.snapshot("note", {"items": []}, note_id=NID)
        self.assertEqual(self.p.status()["snapshots"], before)

    def test_import_closed_run_does_not_require_credentials(self):
        file, _ = self.search()
        old = self.base / "old"
        (old / "raw").mkdir(parents=True)
        (old / "raw" / "search.json").write_bytes(file.read_bytes())
        (old / "run.json").write_text('{"closed":true}')
        self.assertEqual(self.p.import_run(old)["new_snapshots"], 0)

    def test_import_stop_propagates_to_new_session(self):
        old = self.base / "stopped"
        old.mkdir()
        (old / "STOP.json").write_text('{"reason":"verification"}')
        self.p.import_run(old)
        with self.assertRaises(xp.legacy.StopRun): self.p.resume(self.run)

    def test_resume_uses_next_page_and_unique_snapshot(self):
        self.search()
        task = self.task("search", "测试医生 正畸\0general")
        calls = []
        class API:
            def search_notes(inner, query, page, sort):
                calls.append((query, page, sort))
                from test_search_adapter import receipt
                inner.last_search_request = receipt(query, page, sort)
                return {"items": [{"id": "1123456789abcdef01234567", "model_type": "note", "xsec_token": "x", "note_card": {}}], "has_more": False}
        @contextmanager
        def client(run): yield API()
        with patch.object(xp.legacy, "client", client): self.p.resume(self.run, task_id=task["id"])
        self.assertEqual(calls[0][1], 2)
        self.assertEqual(self.task("search", "测试医生 正畸\0general")["status"], "complete")
        self.assertEqual(len(list((self.run / "raw").glob("project-*.json"))), 1)

    def test_stop_during_transport_preserved(self):
        @contextmanager
        def client(run): raise xp.legacy.StopRun("captcha"); yield
        with patch.object(xp.legacy, "client", client):
            with self.assertRaises(xp.legacy.StopRun): self.p.resume(self.run)
        self.assertIsNotNone(self.p.get_meta("stop"))

    def test_empty_live_note_is_not_saved_as_success(self):
        self.search()
        task = self.task("note", NID)
        class API:
            def get_note_by_id(inner, *args, **kwargs): return {"items": []}
        @contextmanager
        def client(run): yield API()
        with patch.object(xp.legacy, "client", client): result = self.p.resume(self.run, task_id=task["id"])
        self.assertEqual(self.task("note", NID)["status"], "blocked")
        files = list((self.run / "raw").glob("diagnostic-project-*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())["data"], {"items": []})
        self.assertIn("collection_error", json.loads(files[0].read_text()))
        self.assertEqual(self.p.db.execute("SELECT COUNT(*) FROM documents WHERE kind='note'").fetchone()[0], 0)

    def test_added_reply_reopens_previously_reviewed_comments(self):
        self.comments([{"id": "r1", "content": "医生是A", "sub_comments": []}])
        task = self.task("comments", NID)
        self.p.review(task["id"], "reviewed", "已读到当时的末尾")
        self.comments([{"id": "r1", "content": "医生是A", "sub_comments": [{"id": "reply1", "content": "后来换成了B"}]}])
        self.assertEqual(self.task("comments", NID)["review_status"], "pending")

    def test_media_failure_remains_visible_gap(self):
        media = self.base / "media"
        media.mkdir()
        (media / "manifest.json").write_text(json.dumps({"format": "xiaohongshu-media-v2", "items": {"media-1": {"media_id": "media-1", "kind": "video", "note_id": NID, "acquisition_status": "downloaded", "transcription_status": "blocked", "review_status": "pending"}}}))
        self.p.import_media(media)
        self.assertEqual(self.task("media", "media-1")["status"], "blocked")

    def test_literal_chinese_two_character_search(self):
        self.note()
        self.assertEqual(len(self.p.search_local("医生")), 1)

    def test_next_exposes_manual_reviews_but_auto_resume_skips_them(self):
        self.note()
        self.assertEqual(self.p.next(10, kinds=["note"])[0]["next_action"], "review")
        self.assertEqual(self.p.next(10, automatic=True, kinds=["note"]), [])

    def test_new_content_reopens_review(self):
        self.note()
        self.p.review(self.task("note", NID)["id"], "reviewed", "已核对正文")
        self.snapshot("note", {"items": [{"note_card": {"title": "更新", "desc": "后来更换医生"}}]}, note_id=NID, url=URL)
        self.assertEqual(self.task("note", NID)["review_status"], "pending")

    def test_media_transcript_does_not_imply_review(self):
        media = self.base / "media"
        media.mkdir()
        transcript = media / "transcript.json"
        transcript.write_text('{"segments":[{"text":"测试","start":0,"end":1}]}')
        (media / "manifest.json").write_text(json.dumps({"format": "xiaohongshu-media-v2", "items": {"media-1": {"media_id": "media-1", "kind": "video", "note_id": NID, "acquisition_status": "transcribed", "review_status": "pending", "transcript":str(transcript)}}}))
        self.p.import_media(media)
        task = self.task("media", "media-1")
        self.assertEqual((task["status"], task["review_status"]), ("complete", "pending"))
        with self.assertRaises(xp.legacy.ResearchError): self.p.review(task["id"], "reviewed", "不能绕过媒体听校")

    def test_page_three_without_prior_pages_cannot_finish(self):
        self.search(page=3, more=False, ids=["3123456789abcdef01234567"])
        task = self.task("search", "测试医生 正畸\0general")
        self.assertEqual((task["status"], task["next_page"]), ("blocked", 1))
        self.search(page=1)
        self.assertEqual(self.task("search", "测试医生 正畸\0general")["next_page"], 2)
        self.search(page=2, ids=["2123456789abcdef01234567"])
        self.assertEqual(self.task("search", "测试医生 正畸\0general")["status"], "complete")

    def test_cursor_chain_mismatch_cannot_finish(self):
        self.comments([{"id":"r1","content":"一"}], more=True, cursor="c1")
        self.snapshot("comments", {"comments":[{"id":"r2","content":"二"}],"has_more":False,"cursor":""},note_id=NID,url=URL,page=2,cursor_in="wrong")
        self.assertEqual(self.task("comments",NID)["status"],"blocked")

    def test_reviewed_note_enqueues_comment_scope(self):
        self.note()
        self.p.review(self.task("note",NID)["id"],"reviewed","正文已读，评论仍应检查")
        self.assertEqual(self.task("comments",NID)["status"],"pending")
        self.assertEqual(self.task("browser",NID)["status"],"pending")

    def test_identity_change_without_text_change_reopens_review(self):
        self.note()
        self.p.review(self.task("note",NID)["id"],"reviewed","已核对")
        self.snapshot("note", {"items":[{"note_card":{"title":"治疗记录","desc":"原医生和后续医生不同","user":{"user_id":"different-author"},"image_list":[]}}]},note_id=NID,url=URL)
        self.assertEqual(self.task("note",NID)["review_status"],"pending")

    def test_wrong_note_identity_is_rejected(self):
        with self.assertRaises(xp.legacy.ResearchError):
            self.snapshot("note", {"items":[{"id":"wrong-note","note_card":{"note_id":"wrong-note","desc":"别人的帖子"}}]},note_id=NID,url=URL)
        self.assertEqual(self.p.status()["snapshots"],0)

    def test_empty_card_is_not_a_body(self):
        with self.assertRaises(xp.legacy.ResearchError):
            self.snapshot("note", {"items":[{}]},note_id=NID,url=URL)

    def test_reply_count_growth_reopens_collection(self):
        self.comments([{"id":"r1","content":"内容","sub_comment_count":2,"sub_comments":[]}])
        self.snapshot("browser_comments", [],note_id=NID,url=URL,ok=True,coverage={"rootPaginationState":"complete","threads":[{"commentId":"r1","rootKey":"id:r1","state":"visible_replies_read","processed":True,"expanders":0,"visibleReplies":2}]})
        self.assertEqual(self.task("browser",NID)["status"],"complete")
        self.comments([{"id":"r1","content":"内容","sub_comment_count":10,"sub_comments":[]}])
        self.assertEqual(self.task("browser",NID)["status"],"pending")
        row=self.p.db.execute("SELECT payload,status FROM threads WHERE comment_id='r1'").fetchone()
        self.assertEqual((json.loads(row["payload"])["sub_comment_count"],row["status"]),(10,"pending"))

    def test_stalled_reply_is_deferred_not_infinite_auto_retry(self):
        self.comments([{"id":"r1","content":"内容"}])
        self.snapshot("browser_comments", [],note_id=NID,url=URL,ok=True,coverage={"rootPaginationState":"complete","threads":[{"commentId":"r1","rootKey":"id:r1","state":"stalled","processed":True,"expanders":1}]})
        self.assertEqual(self.task("browser",NID)["status"],"blocked")
        self.assertEqual(self.p.next(10,automatic=True,kinds=["browser"]),[])

    def test_catalog_import_keeps_author_only_notes_in_triage(self):
        file,_=self.snapshot("author_notes",{"notes":[{"note_id":NID,"title":"生活","xsec_token":"observed"}],"has_more":False,"cursor":""},user_id=UID,page=1)
        old=self.base/'author-old';(old/'raw').mkdir(parents=True)
        (old/'raw'/'author.json').write_bytes(file.read_bytes())
        (old/'catalog.json').write_text(json.dumps({NID:{"title":"生活","contexts":[{"token":"observed","source":"pc_user","url":URL}]}}))
        self.p.import_run(old)
        self.assertTrue(self.task('note',NID)['payload']['needs_triage'])
        self.assertEqual(self.p.next(10,automatic=True,kinds=['note']),[])

    def test_add_note_registers_observed_url(self):
        self.p.add('note',NID,{'note_id':NID,'url':URL,'provenance':['observed link']})
        context=self.p._register_run_context(self.run,NID)
        self.assertEqual(context['token'],'observed')

    def test_changed_media_resource_reopens_review(self):
        old={'media_id':'m1','kind':'image','observed_url':'https://ci.xhscdn.com/old.jpg'}
        task=self.p.add_task('media','m1',old)
        self.p.db.execute("UPDATE tasks SET status='complete',review_status='reviewed' WHERE id=?",(task,))
        self.p.add_task('media','m1',dict(old,observed_url='https://ci.xhscdn.com/new.jpg'))
        self.assertEqual((self.p.task(task)['status'],self.p.task(task)['review_status']),('pending','pending'))

    def test_stale_media_manifest_cannot_rollback_new_resource(self):
        old={'media_id':'m1','kind':'image','note_id':NID,'observed_url':'https://ci.xhscdn.com/old.jpg','acquisition_status':'downloaded','review_status':'reviewed','observation':'old inspected'}
        task=self.p.add_task('media','m1',old)
        self.p.add_task('media','m1',dict(old,observed_url='https://ci.xhscdn.com/new.jpg',acquisition_status='pending',review_status='pending'))
        media=self.base/'media-stale';media.mkdir()
        img=media/'old.jpg';img.write_bytes(b'old-image')
        old['file']=str(img)
        (media/'manifest.json').write_text(json.dumps({'format':'xiaohongshu-media-v2','items':{'m1':old}}))
        result=self.p.import_media(media)
        self.assertEqual(len(result['stale_manifest_items']),1)
        current=self.p.task(task)
        self.assertEqual(current['payload']['observed_url'],'https://ci.xhscdn.com/new.jpg')
        self.assertEqual((current['status'],current['review_status']),('pending','pending'))

    def legacy_media_manifest(self):
        import xhs_media
        self.snapshot("note", {"items": [{"note_card": {"desc": "治疗记录", "image_list": [
            {"file_id": "stable-file-id", "url_default": "https://ci.xhscdn.com/original.jpg"}],
            "video": {"media": {"video_id": "stable-video-id", "stream": {"h264": [
                {"master_url": "https://ci.xhscdn.com/original.mp4"}]}}}}}]}, note_id=NID, url=URL)
        directory = self.base / "legacy-media"
        directory.mkdir()
        items = {}
        for row in self.p.db.execute("SELECT id,payload FROM tasks WHERE kind='media'").fetchall():
            item = json.loads(row["payload"])
            items[item["media_id"]] = item
            old = {k: v for k, v in item.items() if k not in {"resource_id_hint", "resource_fingerprint", "resource_version"}}
            self.p.db.execute("UPDATE tasks SET payload=? WHERE id=?", (xp.dumps(old), row["id"]))
        xhs_media.write_json(directory / "manifest.json", {"format": xhs_media.FORMAT, "items": items})
        return directory, items

    def test_legacy_media_identity_enriched_only_from_registered_original(self):
        import xhs_media
        directory, items = self.legacy_media_manifest()
        for mid, item in items.items():
            artifact = directory / (mid + ".fixture")
            artifact.write_bytes(b"offline fixture")
            item.update(file=str(artifact), acquisition_status="downloaded")
        xhs_media.write_json(directory / "manifest.json", {"format": xhs_media.FORMAT, "items": items})
        result = self.p.import_media(directory)
        self.assertEqual(result["reconciled_legacy_media_items"], 2)
        self.assertEqual(result["stale_manifest_items"], [])
        for mid, item in items.items():
            task = self.task("media", mid)
            self.assertEqual((task["status"], task["review_status"]), ("complete", "pending"))
            self.assertEqual(task["payload"]["resource_id_hint"], item["resource_id_hint"])
            self.assertTrue(task["payload"]["identity_reconciled_from_snapshot"])
        self.assertEqual(self.p.import_media(directory)["reconciled_legacy_media_items"], 0)

    def test_legacy_media_identity_rejects_unproven_fields_and_tampered_snapshot(self):
        import xhs_media
        directory, items = self.legacy_media_manifest()
        mid, original = next(iter(items.items()))
        cases = [{"resource_id_hint": "different-file"}, {"observed_url": "https://ci.xhscdn.com/different.jpg"},
                 {"comment_id": "different-comment"}, {"locator": "note/image/20"},
                 {"captured_at": "2099-01-01T00:00:00Z"}, {"source_path": str(self.base / "not-registered.json")}]
        for fields in cases:
            with self.subTest(fields=fields):
                xhs_media.write_json(directory / "manifest.json", {"format": xhs_media.FORMAT, "items": {mid: dict(original, **fields)}})
                result = self.p.import_media(directory)
                self.assertEqual(len(result["stale_manifest_items"]), 1)
                self.assertEqual(result["reconciled_legacy_media_items"], 0)
                self.assertNotIn("resource_id_hint", self.task("media", mid)["payload"])
        source = Path(original["source_path"])
        source.write_bytes(source.read_bytes() + b" ")  # Same JSON semantics, wrong immutable snapshot hash.
        xhs_media.write_json(directory / "manifest.json", {"format": xhs_media.FORMAT, "items": {mid: original}})
        self.assertEqual(len(self.p.import_media(directory)["stale_manifest_items"]), 1)

    def test_scope_exclusion_survives_pending_import_and_recovers_prior_overwrite(self):
        directory, items = self.legacy_media_manifest()
        self.p.import_media(directory)
        mid = next(iter(items))
        task = self.task("media", mid)
        self.p.review(task["id"], "exclude", "Other doctor; media not read")
        self.p.import_media(directory)
        self.assertEqual((self.task("media", mid)["status"], self.task("media", mid)["review_status"]), ("excluded", "excluded"))
        # Recover an older import_media overwrite using the explicit latest decision.
        self.p.db.execute("UPDATE tasks SET status='pending',review_status='pending' WHERE id=?", (task["id"],))
        self.p.import_media(directory)
        self.assertEqual(self.task("media", mid)["reason"], "Other doctor; media not read")
        self.assertEqual(self.task("media", mid)["review_status"], "excluded")
        self.p.review(task["id"], "retry", "Reinclude in scope")
        self.p.import_media(directory)
        self.assertEqual((self.task("media", mid)["status"], self.task("media", mid)["review_status"]), ("pending", "pending"))

    def test_scope_exclusion_does_not_carry_review_or_artifact_across_replacement(self):
        directory, items = self.legacy_media_manifest()
        self.p.import_media(directory)
        mid, original = next(iter(items.items()))
        task = self.task("media", mid)
        self.p.review(task["id"], "exclude", "Outside research scope; not read")
        old = dict(original, file="old.image", sha256="old-hash", observation="Old observation", review_status="reviewed")
        self.p.db.execute("UPDATE tasks SET payload=? WHERE id=?", (xp.dumps(old), task["id"]))
        replacement = dict(original, observed_url="https://ci.xhscdn.com/replacement.jpg", resource_id_hint="new-file",
                           captured_at="2026-10-31T00:00:00Z")
        self.p.add_task("media", mid, replacement)
        current = self.task("media", mid)
        self.assertEqual((current["status"], current["review_status"]), ("excluded", "excluded"))
        self.assertEqual(current["payload"]["review_status"], "pending")
        self.assertNotIn("file", current["payload"])
        self.assertNotIn("observation", current["payload"])

    def test_same_resource_irrelevant_result_survives_pending_manifest(self):
        import xhs_media
        directory, items = self.legacy_media_manifest()
        self.p.import_media(directory)
        mid, original = next(iter(items.items()))
        irrelevant = dict(original, review_status="irrelevant", observation="Other subject")
        xhs_media.write_json(directory / "manifest.json", {"format": xhs_media.FORMAT, "items": {mid: irrelevant}})
        self.p.import_media(directory)
        xhs_media.write_json(directory / "manifest.json", {"format": xhs_media.FORMAT, "items": {mid: original}})
        self.p.import_media(directory)
        self.assertEqual((self.task("media", mid)["status"], self.task("media", mid)["review_status"]), ("excluded", "excluded"))
        replacement = dict(original, resource_id_hint="new-file", observed_url="https://ci.xhscdn.com/new.jpg",
                           captured_at="2026-10-31T00:00:00Z")
        self.p.add_task("media", mid, replacement)
        self.assertEqual((self.task("media", mid)["status"], self.task("media", mid)["review_status"]), ("pending", "pending"))

    def test_older_small_browser_cannot_finish_newer_api_reply_growth(self):
        self.comments([{"id":"r1","content":"内容","sub_comment_count":10,"sub_comments":[]}])
        self.snapshot("browser_comments",[],note_id=NID,url=URL,ok=True,captured_at="2020-01-01T00:00:00+00:00",coverage={"rootPaginationState":"complete","threads":[{"commentId":"r1","rootKey":"id:r1","state":"visible_replies_read","processed":True,"expanders":0,"visibleReplies":2}]})
        self.assertEqual(self.task('browser',NID)['status'],'pending')


if __name__ == "__main__": unittest.main()
