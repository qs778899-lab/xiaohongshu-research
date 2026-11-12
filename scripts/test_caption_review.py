"""Offline caption-text scope, immutable binding and video/body boundary tests."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import xhs_media as media
import xhs_project as xp


class CaptionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = xp.Project(self.root / "project", create=True); self.addCleanup(self.project.close)
        self.directory = self.project.path / "media"
        self.nid = "a" * 24
        self.raw = {"operation": "note", "note_id": self.nid, "captured_at": "2026-10-31T00:00:00Z", "data": {"items": [{"id": self.nid,
            "note_card": {"note_id": self.nid, "title": "Synthetic treatment account", "desc": "Synthetic body text", "type": "video",
                "video": {"url": "https://sns-video-v2.xhscdn.com/synthetic/video.mp4",
                          "subtitles": [{"start": 0, "end": 2, "text": "不是这位医生。转写可能漏字，原音未核。"}]}}}]}}
        self.source = self.project.path / "input.json"; self.source.write_text(json.dumps(self.raw))
        self.project.ingest(self.source)
        manifest = media.inspect_sources([self.source], self.directory)
        self.caption = next(i for i in manifest["items"].values() if i["kind"] == "subtitle")
        self.video = next(i for i in manifest["items"].values() if i["kind"] == "video")
        self.caption = media.download_media(self.directory, self.caption["media_id"])

    def declaration(self, **changes):
        item = media.get_item(self.directory, self.caption["media_id"])[2]
        binding = media.caption_review_binding(item)
        return {"full_caption_text": True, "actual_text_read": True, "reviewer": "synthetic test reader",
                "file_sha256": binding["file_sha256"], "resource_fingerprint": binding["resource_fingerprint"],
                "resource_version": binding["resource_version"], "original_audio_checked": False,
                "observation": "Entire saved synthetic caption read; possible missing negation remains unverified in original audio", **changes}

    def review(self, declaration=None):
        return media.review_media(self.directory, self.caption["media_id"],
            "Full caption text read; original audio not checked; preserve possible missing negation", "reviewed", [declaration or self.declaration()])

    def task(self, kind):
        return self.project.task(self.project.db.execute("SELECT id FROM tasks WHERE kind=? AND entity_key=?", (kind, self.nid)).fetchone()[0])

    def test_caption_full_text_is_own_scope_and_keeps_uncertainty(self):
        item = self.review()
        self.assertEqual(item["review_status"], "reviewed")
        self.assertEqual(item["review_scope"], "full_caption_text")
        self.assertTrue(media.caption_text_review_valid(item))
        self.assertEqual(item["review_binding"]["file_sha256"], hashlib.sha256(Path(item["file"]).read_bytes()).hexdigest())
        self.assertIn("original_audio_not_verified_by_caption_reading", item["reading_limits"])
        self.assertFalse(item["checked_segments"][0]["original_audio_checked"])
        self.assertIn("possible missing negation", item["observation"])
        video = media.get_item(self.directory, self.video["media_id"])[2]
        self.assertEqual(video["review_status"], "pending")

    def test_actual_reader_hash_version_and_audio_boundary_are_required(self):
        for key, value in [("actual_text_read", False), ("reviewer", ""), ("file_sha256", "0" * 64),
                           ("resource_fingerprint", "0" * 64), ("resource_version", 2), ("resource_version", True),
                           ("original_audio_checked", True), ("original_audio_checked", None), ("observation", None)]:
            with self.subTest(key=key, value=value), self.assertRaises(media.MediaError):
                self.review(self.declaration(**{key: value}))
        with self.assertRaises(media.MediaError):
            media.review_media(self.directory, self.caption["media_id"], "Read text", "reviewed", [{"full_caption_text": True, "observation": "Bare claim"}])

    def test_video_full_flag_cannot_complete_caption_and_caption_flag_cannot_complete_video(self):
        with self.assertRaises(media.MediaError):
            media.review_media(self.directory, self.caption["media_id"], "Wrong scope", "reviewed", [{"full_video": True, "observation": "Watched video"}])
        media.transcribe_media(self.directory, self.video["media_id"])
        with self.assertRaises(media.MediaError):
            media.review_media(self.directory, self.video["media_id"], "Only captions read", "reviewed", [self.declaration()])
        with self.assertRaises(media.MediaError):
            media.review_media(self.directory, self.video["media_id"], "Only transcript available", "reviewed", [{"full_video": True, "observation": "Unsupported whole-video claim"}])
        fake = dict(self.video, review_status="reviewed", transcript=self.caption["file"],
                    checked_segments=[{"full_video": True, "observation": "No original file"}])
        self.assertEqual(media.review_scope_counts([fake])["video_fully_reviewed"], 0)

    def test_excerpt_is_partial_and_no_full_scope_is_persisted(self):
        ranges = [{"start": 0, "end": 1, "observation": "Read only this caption line; audio not checked"}]
        with self.assertRaises(media.MediaError):
            media.review_media(self.directory, self.caption["media_id"], "Excerpt only", "reviewed", ranges)
        item = media.review_media(self.directory, self.caption["media_id"], "Other text remains unread", "partial", ranges)
        self.assertEqual(item["review_status"], "partial")
        self.assertNotIn("review_scope", item)
        self.assertEqual(media.status(self.directory)["review_scopes"]["caption_text_reviewed"], 0)

    def test_import_counts_caption_only_and_parent_video_body_stay_pending(self):
        self.review(); self.project.import_media(self.directory)
        caption_task = self.project.task(self.project.db.execute("SELECT id FROM tasks WHERE entity_key=?", (self.caption["media_id"],)).fetchone()[0])
        self.assertEqual((caption_task["status"], caption_task["review_status"]), ("complete", "reviewed"))
        video_task = self.project.task(self.project.db.execute("SELECT id FROM tasks WHERE entity_key=?", (self.video["media_id"],)).fetchone()[0])
        self.assertEqual(video_task["review_status"], "pending")
        self.assertEqual(self.task("note")["review_status"], "pending")
        with self.assertRaises(xp.legacy.ResearchError):
            self.project.review(self.task("note")["id"], "reviewed", "Caption alone cannot complete body")
        report = self.project.status()
        self.assertEqual(report["media_review_scopes"], {"caption_text_reviewed": 1, "caption_text_not_reviewed": 0,
            "caption_reading_does_not_verify_original_audio": 1, "video_fully_reviewed": 0, "video_not_fully_reviewed": 1,
            "caption_excluded": 0, "video_excluded": 0})
        self.assertGreater(report["outstanding_tasks"], 0)
        output = self.project.report()
        self.assertIn("字幕全文已读：1", Path(output["text"]).read_text())
        self.assertIn("完整视频已核验：0", Path(output["text"]).read_text())

    def test_current_file_changed_or_deleted_rejects_read_and_import(self):
        item = self.review(); path = Path(item["file"]); original = path.read_bytes()
        path.write_bytes(original + b"\n")
        self.assertFalse(media.caption_text_review_valid(item))
        self.project.import_media(self.directory)
        task = self.project.task(self.project.db.execute("SELECT id FROM tasks WHERE entity_key=?", (item["media_id"],)).fetchone()[0])
        self.assertEqual(task["review_status"], "pending")
        with self.assertRaises(media.MediaError): self.review()
        path.unlink()
        with self.assertRaises(media.MediaError): self.review()

    def test_stored_caption_review_cannot_complete_body_after_file_change_before_import(self):
        item = self.review(); self.project.import_media(self.directory)
        self.project.db.execute("UPDATE tasks SET status='excluded',review_status='excluded' WHERE kind='media' AND entity_key=?", (self.video["media_id"],))
        Path(item["file"]).write_bytes(b"changed caption after its reading")
        with self.assertRaisesRegex(xp.legacy.ResearchError, "current_body_media_review_binding_invalid"):
            self.project.review(self.task("note")["id"], "reviewed", "Old media task state is insufficient")

    def test_resource_change_preserves_old_version_but_resets_caption_and_video(self):
        original = self.review(); old_path = Path(original["file"])
        self.project.import_media(self.directory)
        changed = copy.deepcopy(self.raw); changed["captured_at"] = "2026-10-31T01:00:00Z"
        changed["data"]["items"][0]["note_card"]["video"]["subtitles"][0]["text"] = "Changed synthetic wording"
        source = self.project.path / "later.json"; source.write_text(json.dumps(changed))
        self.project.ingest(source)
        manifest = media.inspect_sources([source], self.directory)
        caption = manifest["items"][self.caption["media_id"]]
        self.assertEqual(caption["resource_version"], 2)
        self.assertEqual(caption["review_status"], "pending")
        self.assertNotIn("review_scope", caption)
        self.assertNotIn("review_binding", caption)
        self.assertEqual(caption["versions"][0]["review_scope"], "full_caption_text")
        self.assertTrue(old_path.exists())
        self.project.import_media(self.directory)
        self.assertEqual(self.project.status()["media_review_scopes"]["caption_text_reviewed"], 0)
        current = self.project.task(self.project.db.execute("SELECT id FROM tasks WHERE entity_key=?", (self.caption["media_id"],)).fetchone()[0])
        self.assertNotIn("review_binding", current["payload"])

    def test_forged_import_cross_type_flags_or_stale_binding_never_raise_review_counts(self):
        self.review()
        directory, manifest = media.load_manifest(self.directory)
        original = copy.deepcopy(manifest)
        for mutation in ("wrong_version", "caption_full_video", "video_from_caption"):
            manifest = copy.deepcopy(original)
            caption = manifest["items"][self.caption["media_id"]]
            if mutation == "wrong_version": caption["review_binding"]["resource_version"] = 2
            elif mutation == "caption_full_video": caption["checked_segments"] = [{"full_video": True, "observation": "Wrong scope"}]
            else:
                video = manifest["items"][self.video["media_id"]]
                video.update(acquisition_status="transcribed", review_status="reviewed", observation="Caption only",
                             transcript=caption["file"], checked_segments=caption["checked_segments"])
            media.write_json(directory / "manifest.json", manifest)
            self.project.import_media(directory)
            report = self.project.status()["media_review_scopes"]
            self.assertEqual(report["video_fully_reviewed"], 0)
            if mutation != "video_from_caption": self.assertEqual(report["caption_text_reviewed"], 0)
            self.assertEqual(self.task("note")["review_status"], "pending")

    def test_explicit_exclusions_are_neither_read_nor_outstanding_text_video_counts(self):
        counts = media.review_scope_counts([dict(self.caption, review_status="irrelevant"), dict(self.video, review_status="excluded")])
        self.assertEqual(counts["caption_excluded"], 1)
        self.assertEqual(counts["video_excluded"], 1)
        for key in ("caption_text_reviewed", "caption_text_not_reviewed", "video_fully_reviewed", "video_not_fully_reviewed"):
            self.assertEqual(counts[key], 0)

    def test_report_only_reopens_invalid_caption_binding_without_rewriting_saved_review(self):
        item = self.review(); self.project.import_media(self.directory)
        self.project.db.execute("UPDATE tasks SET status='excluded',review_status='excluded' WHERE entity_key!=?", (item["media_id"],))
        self.project.db.commit()
        before = [tuple(row) for row in self.project.db.execute("SELECT * FROM tasks ORDER BY id")]
        self.assertEqual(self.project.status()["outstanding_tasks"], 0)
        manifest_bytes = (self.directory / "manifest.json").read_bytes()
        Path(item["file"]).write_bytes(b"changed after the recorded reading")
        media_status = media.status(self.directory)
        self.assertIn(item["media_id"], media_status["unresolved"])
        self.assertEqual(media_status["counts"]["subtitle:pending"], 1)
        report = self.project.status()
        self.assertEqual(report["outstanding_tasks"], 1)
        self.assertEqual(report["state"], "work_remaining")
        self.assertEqual(report["media_review_scopes"]["caption_text_not_reviewed"], 1)
        self.assertEqual(report["invalid_review_bindings"][0]["stored_review_status"], "reviewed")
        output = self.project.report(); saved = json.loads(Path(output["json"]).read_text())
        self.assertEqual(len(saved["remaining"]), 1)
        self.assertTrue(saved["remaining"][0]["effective_report_only"])
        self.assertEqual(saved["remaining"][0]["review_status"], "pending")
        self.assertEqual(before, [tuple(row) for row in self.project.db.execute("SELECT * FROM tasks ORDER BY id")])
        self.assertEqual(manifest_bytes, (self.directory / "manifest.json").read_bytes())

    def test_current_video_bytes_control_video_report_and_import_not_only_path_existence(self):
        directory, manifest = media.load_manifest(self.directory)
        video = manifest["items"][self.video["media_id"]]
        path = self.root / "synthetic-original.video"; path.write_bytes(b"synthetic full original video")
        video.update(acquisition_status="downloaded", file=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        media.write_json(directory / "manifest.json", manifest)
        video = media.review_media(directory, video["media_id"], "Synthetic complete video reading", "reviewed",
            [{"full_video": True, "observation": "All synthetic video checked"}])
        self.project.import_media(directory)
        self.project.db.execute("UPDATE tasks SET status='excluded',review_status='excluded' WHERE entity_key!=?", (video["media_id"],))
        self.project.db.commit()
        self.assertEqual(self.project.status()["media_review_scopes"]["video_fully_reviewed"], 1)
        path.write_bytes(b"changed source bytes at the same path")
        self.assertEqual(media.status(directory)["review_scopes"]["video_fully_reviewed"], 0)
        self.assertIn(video["media_id"], media.status(directory)["unresolved"])
        report = self.project.status()
        self.assertEqual(report["media_review_scopes"]["video_fully_reviewed"], 0)
        self.assertEqual(report["outstanding_tasks"], 1)
        self.project.import_media(directory)
        task = self.project.task(self.project.db.execute("SELECT id FROM tasks WHERE entity_key=?", (video["media_id"],)).fetchone()[0])
        self.assertEqual(task["review_status"], "pending")


if __name__ == "__main__":
    unittest.main()
