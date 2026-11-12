"""Synthetic local fixtures; no real profile, image viewing, transport or DB."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import xhs_project as xp
import xhs_media as media
import xhs_observed_review as review

NID, CID, UID = "a" * 24, "b" * 24, "c" * 24


class ApiImageReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.p = xp.Project(Path(self.temp.name) / "project", create=True)
        self.addCleanup(self.p.close)
        self.record = {"operation": "comments", "note_id": NID, "captured_at": "2026-10-30T10:00:00Z", "page": 1,
                       "data": {"has_more": False, "comments": [{"id": CID, "content": "", "user_info": {"user_id": UID},
                           "pictures": [{"file_id": "image-1", "url_default": "https://sns-webpic.xhscdn.com/one"},
                                        {"file_id": "image-2", "url_default": "https://sns-webpic.xhscdn.com/two"}], "sub_comments": []}]}}
        self.ingest()
        self.task = self.p.db.execute("SELECT id FROM tasks WHERE kind='comments'").fetchone()[0]

    def ingest(self):
        path = self.p.path / "incoming.json"
        path.write_text(json.dumps(self.record))
        result = self.p.ingest(path); self.p.db.commit()
        self.source = self.p.path / "snapshots" / (result["snapshot"] + ".json")

    def build(self):
        receipt = self.p.observed_review_plan(self.task)
        receipt.update(reviewer="Synthetic test reader", all_current_documents_read=True)
        document = next(d for d in receipt["documents"] if d["document_id"].endswith(CID))
        manifest = {"format": "xiaohongshu-media-v2", "items": {}}
        self.manifest_path = self.p.path / "manifest.json"
        images = []
        for item in media.extract_media(self.record, str(self.source)):
            if item["comment_id"] != CID or item["kind"] != "image": continue
            path = self.p.path / (item["media_id"] + ".image")
            path.write_bytes(("synthetic binary " + item["media_id"]).encode())
            item.update(acquisition_status="downloaded", review_status="reviewed", file=str(path),
                        sha256=review.sha(path.read_bytes()), acquired_at="2026-10-30T11:00:00Z")
            manifest["items"][item["media_id"]] = item
            images.append({"media_id": item["media_id"], "locator": item["locator"],
                           "resource_fingerprint": media.resource_identity(item), "manifest_path": str(self.manifest_path),
                           "acquisition_sha256": review.sha(review.canonical(review.acquisition_binding(item)).encode()),
                           "file": str(path), "sha256": item["sha256"], "actual_view_image": True,
                           "review_status": "reviewed", "viewed_at": "2026-10-30T12:00:00Z",
                           "observation": "Synthetic fixture claim, not a real image reading"})
        self.manifest_path.write_text(json.dumps(manifest))
        audit = {"format": review.API_IMAGES_FORMAT, "reviewer": "Synthetic test reader", "note_id": NID, "comment_id": CID,
                 "raw_api_source": {"document_id": document["document_id"], "user_id": UID,
                                    "snapshot_id": self.source.stem, "sha256": self.source.stem,
                                    "context_sha256": document["context_sha256"]},
                 "observation": "Synthetic empty-text comment with all attached pictures checked", "images": images}
        self.audit_path = self.p.path / "audit.json"
        self.save_audit(receipt, audit)
        return receipt, audit

    def save_audit(self, receipt, audit):
        self.audit_path.write_text(json.dumps(audit))
        receipt["visual_observations"] = [{"document_id": "comment:" + NID + ":" + CID,
            "audit_path": str(self.audit_path), "audit_sha256": review.sha(self.audit_path.read_bytes())}]

    def validate(self, receipt):
        return review.validate_receipt(self.p.db, self.p.path, self.task, receipt)

    def test_all_images_resolve_observed_scope_without_changing_task_or_media(self):
        receipt, _ = self.build()
        before = [tuple(r) for r in self.p.db.execute("SELECT * FROM tasks ORDER BY id")]
        original = self.manifest_path.read_bytes()
        result = self.p.observed_review(self.task, receipt, "Read synthetic bounded image-only comment")
        self.assertEqual((result["state"], result["visual_documents"], result["whole_task_reviewed"]), ("current", 1, False))
        self.assertEqual([tuple(r) for r in self.p.db.execute("SELECT * FROM tasks ORDER BY id")], before)
        self.assertEqual(self.manifest_path.read_bytes(), original)

    def test_partial_missing_duplicate_or_unviewed_picture_refused(self):
        receipt, audit = self.build()
        for change in (lambda a: a["images"].pop(), lambda a: a["images"].__setitem__(1, deepcopy(a["images"][0])),
                       lambda a: a["images"][0].update(actual_view_image=False),
                       lambda a: a["images"][0].update(review_status="partial"),
                       lambda a: a["images"][0].update(observation="")):
            candidate = deepcopy(audit); change(candidate); self.save_audit(receipt, candidate)
            with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_wrong_comment_uid_snapshot_or_context_refused(self):
        receipt, audit = self.build()
        for field, value in (("user_id", "d" * 24), ("snapshot_id", "e" * 64), ("context_sha256", "e" * 64), ("document_id", "wrong")):
            candidate = deepcopy(audit); candidate["raw_api_source"][field] = value; self.save_audit(receipt, candidate)
            with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_wrong_slot_or_resource_or_download_binding_refused(self):
        receipt, audit = self.build()
        for key in ("media_id", "locator", "resource_fingerprint", "acquisition_sha256", "sha256"):
            candidate = deepcopy(audit); candidate["images"][0][key] = "wrong"; self.save_audit(receipt, candidate)
            with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_changed_actual_file_invalidates_existing_reading(self):
        receipt, audit = self.build()
        self.p.observed_review(self.task, receipt, "Initial synthetic reading")
        Path(audit["images"][0]["file"]).write_bytes(b"changed artifact")
        self.assertEqual(review.observed_review_status(self.p.db, self.p.path)[0]["state"], "stale")

    def test_unrelated_manifest_update_keeps_binding_but_changed_resource_does_not(self):
        receipt, audit = self.build()
        manifest = json.loads(self.manifest_path.read_text()); manifest["unrelated_updated_at"] = "new"
        manifest["items"]["other"] = {"observation": "unrelated"}; self.manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(self.validate(receipt)["state"], "current")
        item = manifest["items"][audit["images"][0]["media_id"]]; item["resource_id_hint"] = "other-image"
        self.manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_manifest_partial_review_remains_gap(self):
        receipt, audit = self.build()
        manifest = json.loads(self.manifest_path.read_text())
        manifest["items"][audit["images"][0]["media_id"]]["review_status"] = "partial"
        self.manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_current_metadata_change_rejects_old_and_rebound_receipt(self):
        receipt, audit = self.build()
        self.record["captured_at"] = "2026-10-30T13:00:00Z"
        self.record["data"]["comments"][0]["pictures"][0]["file_id"] = "replacement"
        self.ingest()
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)
        fresh = self.p.observed_review_plan(self.task); fresh.update(reviewer="Reader", all_current_documents_read=True)
        fresh["visual_observations"] = receipt["visual_observations"]
        with self.assertRaises(xp.legacy.ResearchError): self.validate(fresh)

    def test_no_images_or_reply_prefix_cannot_use_image_route(self):
        receipt, audit = self.build()
        for text, pictures in (("", []), ("回复 someone :", self.record["data"]["comments"][0]["pictures"])):
            self.record["captured_at"] = "2026-10-30T13:00:00Z"
            self.record["data"]["comments"][0].update(content=text, pictures=pictures)
            self.ingest()
            fresh = self.p.observed_review_plan(self.task); fresh.update(reviewer="Reader", all_current_documents_read=True)
            doc = fresh["documents"][0]
            candidate = deepcopy(audit)
            candidate["raw_api_source"].update(snapshot_id=self.source.stem, sha256=self.source.stem, context_sha256=doc["context_sha256"])
            self.save_audit(fresh, candidate)
            with self.assertRaises(xp.legacy.ResearchError): self.validate(fresh)

    def test_other_empty_comment_with_no_images_stays_gap(self):
        self.record["captured_at"] = "2026-10-30T10:30:00Z"
        self.record["data"]["comments"].append({"id": "e" * 24, "content": "", "user_info": {"user_id": UID}, "sub_comments": []})
        self.ingest()
        receipt, _ = self.build()
        with self.assertRaisesRegex(xp.legacy.ResearchError, "unread empty"): self.validate(receipt)

    def test_live_photo_and_missing_url_refused(self):
        for update in ({"live_photo": True}, {"url_default": "", "file_id": "same"}):
            with self.subTest(update=update):
                self.record["captured_at"] = "2026-10-30T10:30:00Z"
                self.record["data"]["comments"][0]["pictures"][0] = update
                self.ingest(); receipt, _ = self.build()
                with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_download_url_must_be_an_actually_observed_alternative(self):
        receipt, audit = self.build()
        manifest = json.loads(self.manifest_path.read_text())
        item = manifest["items"][audit["images"][0]["media_id"]]
        item["observed_url"] = "https://sns-webpic.xhscdn.com/unobserved"
        self.manifest_path.write_text(json.dumps(manifest))
        audit["images"][0]["acquisition_sha256"] = review.sha(review.canonical(review.acquisition_binding(item)).encode())
        self.save_audit(receipt, audit)
        with self.assertRaisesRegex(xp.legacy.ResearchError, "not observed"): self.validate(receipt)

    def test_unknown_comment_identity_or_author_stays_gap(self):
        self.p.db.execute("UPDATE documents SET identity_kind='snapshot_local'")
        with self.assertRaises(xp.legacy.ResearchError): self.p.observed_review_plan(self.task)
        self.p.db.execute("UPDATE documents SET identity_kind='platform_id',metadata=?", (json.dumps({"id": CID, "content": "", "pictures": [{}]}),))
        with self.assertRaises(xp.legacy.ResearchError): self.p.observed_review_plan(self.task)

    def test_symlink_artifact_and_pre_acquisition_read_time_refused(self):
        receipt, audit = self.build()
        candidate = deepcopy(audit); candidate["images"][0]["viewed_at"] = "2026-10-30T09:00:00Z"; self.save_audit(receipt, candidate)
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)
        target = Path(audit["images"][0]["file"]); held = target.with_suffix(".held"); target.rename(held); target.symlink_to(held)
        self.save_audit(receipt, audit)
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_reply_image_body_binds_its_real_parent_and_author(self):
        reply = self.record["data"]["comments"][0]
        self.record["captured_at"] = "2026-10-30T10:30:00Z"
        self.record["data"]["comments"] = [{"id": "d" * 24, "content": "Parent context", "user_info": {"user_id": "e" * 24}, "sub_comments": [reply]}]
        self.p = xp.Project(self.p.path / "reply-project", create=True)
        self.addCleanup(self.p.close)
        self.ingest()
        self.task = self.p.db.execute("SELECT id FROM tasks WHERE kind='comments'").fetchone()[0]
        receipt, _ = self.build()
        self.assertEqual(self.validate(receipt)["state"], "current")

    def test_corrupt_source_cannot_be_certified_by_valid_local_image(self):
        receipt, _ = self.build()
        self.source.write_text(self.source.read_text() + " ")
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)


if __name__ == "__main__": unittest.main()
