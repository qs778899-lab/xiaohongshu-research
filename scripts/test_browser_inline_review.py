"""Synthetic source-bound inline-image audits; no transport or real image reading."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import xhs_media as media
import xhs_project as xp
import xhs_observed_review as review

NID, CID, UID, PARENT = "a" * 24, "b" * 24, "c" * 24, "d" * 24
URL = "https://picasso-static.xiaohongshu.com/fe-platform/synthetic-emoji.png"


class BrowserInlineReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.p = xp.Project(Path(self.temp.name) / "project", create=True); self.addCleanup(self.p.close)
        self.record = {"operation": "browser_comments", "ok": True, "note_id": NID,
            "captured_at": "2026-10-31T00:00:00Z", "coverage": {"rootPaginationState": "pending", "threads": []},
            "data": [{"commentId": CID, "parentCommentId": PARENT, "userId": UID,
                "authorHrefRaw": "/user/profile/" + UID, "is_reply": True, "text": "[图片评论]",
                "textSource": "media_placeholder", "renderedReplyPrefixOnly": True,
                "renderedText": "回复 synthetic target :", "replyToCommentId": PARENT,
                "replyToIdentity": "observed_id", "images": [], "renderedInlineImages": [
                    {"domOrdinal": i, "url": URL, "observedClass": "note-content-emoji"} for i in range(1, 4)]}]}
        self.ingest()
        self.task = self.p.db.execute("SELECT id FROM tasks WHERE kind='browser'").fetchone()[0]

    def ingest(self):
        path = self.p.path / "incoming.json"; path.write_text(json.dumps(self.record))
        result = self.p.ingest(path); self.p.db.commit()
        self.source = self.p.path / "snapshots" / (result["snapshot"] + ".json")

    def build(self):
        receipt = self.p.observed_review_plan(self.task)
        receipt.update(reviewer="Synthetic actual reader assertion", all_current_documents_read=True)
        document = receipt["documents"][0]
        manifest = {"format": "xiaohongshu-media-v2", "items": {}}
        self.manifest_path = self.p.path / "media-manifest.json"
        images = []
        for item in media.extract_media(self.record, str(self.source)):
            path = self.p.path / (item["media_id"] + ".image")
            path.write_bytes(b"same synthetic image bytes for each observed DOM occurrence")
            item.update(acquisition_status="downloaded", review_status="reviewed", file=str(path),
                sha256=review.sha(path.read_bytes()), acquired_at="2026-10-31T01:00:00Z")
            manifest["items"][item["media_id"]] = item
            images.append({"media_id": item["media_id"], "locator": item["locator"], "dom_ordinal": item.get("dom_ordinal"),
                "resource_fingerprint": media.resource_identity(item), "resource_version": item["resource_version"],
                "manifest_path": str(self.manifest_path), "acquisition_sha256": review.sha(review.canonical(review.acquisition_binding(item)).encode()),
                "file": str(path), "sha256": item["sha256"], "actual_view_image": True, "review_status": "reviewed",
                "viewed_at": "2026-10-31T02:00:00Z", "observation": "Synthetic individual image-reading claim; no emotion/identity inference"})
        self.manifest_path.write_text(json.dumps(manifest))
        audit = {"format": review.BROWSER_INLINE_IMAGES_FORMAT, "reviewer": "Synthetic reader", "note_id": NID, "comment_id": CID,
            "raw_browser_source": {"document_id": document["document_id"], "user_id": UID,
                "parent_comment_id": self.record["data"][0].get("parentCommentId") or "", "snapshot_id": self.source.stem,
                "sha256": self.source.stem, "context_sha256": document["context_sha256"]},
            "observation": "All synthetic inline slots actually inspected, not a screenshot assertion", "images": images}
        self.audit_path = self.p.path / "inline-audit.json"
        self.save(receipt, audit)
        return receipt, audit

    def save(self, receipt, audit):
        self.audit_path.write_text(json.dumps(audit))
        receipt["visual_observations"] = [{"document_id": "comment:" + NID + ":" + CID,
            "audit_path": str(self.audit_path), "audit_sha256": review.sha(self.audit_path.read_bytes())}]

    def validate(self, receipt):
        return review.validate_receipt(self.p.db, self.p.path, self.task, receipt)

    def test_three_same_url_slots_need_no_fabricated_screenshot_and_change_no_tasks(self):
        receipt, audit = self.build()
        self.assertEqual(len({x["sha256"] for x in audit["images"]}), 1)
        self.assertEqual(len({x["media_id"] for x in audit["images"]}), 3)
        before = [tuple(r) for r in self.p.db.execute("SELECT * FROM tasks ORDER BY id")]
        manifest = self.manifest_path.read_bytes()
        result = self.validate(receipt)
        self.assertEqual((result["state"], result["visual_documents"], result["whole_task_reviewed"]), ("current", 1, False))
        self.assertEqual(before, [tuple(r) for r in self.p.db.execute("SELECT * FROM tasks ORDER BY id")])
        self.assertEqual(manifest, self.manifest_path.read_bytes())
        self.assertNotIn("screenshot", audit)

    def test_missing_duplicate_unviewed_partial_or_empty_observation_slot_rejected(self):
        receipt, audit = self.build()
        for change in (lambda a: a["images"].pop(), lambda a: a["images"].__setitem__(1, deepcopy(a["images"][0])),
                       lambda a: a["images"][0].update(actual_view_image=False), lambda a: a["images"][0].update(review_status="partial"),
                       lambda a: a["images"][0].update(observation="")):
            candidate = deepcopy(audit); change(candidate); self.save(receipt, candidate)
            with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_comment_uid_parent_context_or_source_binding_mismatch_rejected(self):
        receipt, audit = self.build()
        for key, value in [("user_id", "e" * 24), ("parent_comment_id", "e" * 24), ("context_sha256", "f" * 64),
                           ("snapshot_id", "f" * 64), ("sha256", "f" * 64), ("document_id", "wrong")]:
            candidate = deepcopy(audit); candidate["raw_browser_source"][key] = value; self.save(receipt, candidate)
            with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_wrong_dom_ordinal_version_resource_url_or_file_hash_rejected(self):
        receipt, audit = self.build()
        for key, value in [("dom_ordinal", 2), ("dom_ordinal", True), ("resource_version", 2), ("resource_version", True),
                           ("resource_fingerprint", "0" * 64), ("acquisition_sha256", "0" * 64), ("sha256", "0" * 64)]:
            candidate = deepcopy(audit); candidate["images"][0][key] = value; self.save(receipt, candidate)
            with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)
        self.save(receipt, audit)
        manifest = json.loads(self.manifest_path.read_text())
        item = manifest["items"][audit["images"][0]["media_id"]]; item["observed_url"] = URL + "?unobserved=1"
        audit["images"][0]["acquisition_sha256"] = review.sha(review.canonical(review.acquisition_binding(item)).encode())
        self.manifest_path.write_text(json.dumps(manifest)); self.save(receipt, audit)
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_missing_unusable_or_duplicate_dom_sources_remain_gaps(self):
        for changes in (lambda row: row["renderedInlineImages"][1].update(url=""),
                        lambda row: [image.update(url="") for image in row["renderedInlineImages"]],
                        lambda row: row["renderedInlineImages"][1].update(url="https://example.test/untrusted"),
                        lambda row: row["renderedInlineImages"][1].update(domOrdinal=1),
                        lambda row: row["renderedInlineImages"][1].update(observedClass="avatar")):
            original = deepcopy(self.record); changes(self.record["data"][0]); self.ingest()
            receipt, audit = self.build()
            with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)
            self.record = original; self.ingest()

    def test_ordinary_images_and_api_format_cannot_slip_into_inline_only_scope(self):
        receipt, audit = self.build(); candidate = deepcopy(audit); candidate["format"] = review.API_IMAGES_FORMAT
        candidate["raw_api_source"] = candidate.pop("raw_browser_source"); self.save(receipt, candidate)
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)
        self.record["data"][0]["images"] = ["https://sns-webpic.xhscdn.com/ordinary"]
        self.ingest(); receipt, audit = self.build()
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_source_or_downloaded_file_corruption_invalidates_audit(self):
        receipt, audit = self.build(); path = Path(audit["images"][0]["file"]); original = path.read_bytes()
        path.write_bytes(b"changed")
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)
        path.write_bytes(original); self.source.write_bytes(self.source.read_bytes() + b"\n")
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_reading_must_not_predate_source_or_acquisition(self):
        receipt, audit = self.build()
        audit["images"][0]["viewed_at"] = "2026-10-30T23:59:00Z"; self.save(receipt, audit)
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_same_uid_parent_or_reply_target_change_stales_old_and_rebound_audit(self):
        receipt, audit = self.build()
        for key in ("parentCommentId", "replyToCommentId"):
            self.record["captured_at"] = "2026-10-31T00:30:00Z"
            self.record["data"][0][key] = "e" * 24; self.ingest()
            with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)
            fresh = self.p.observed_review_plan(self.task); fresh.update(reviewer="Synthetic", all_current_documents_read=True)
            candidate = deepcopy(audit)
            candidate["raw_browser_source"].update(snapshot_id=self.source.stem, sha256=self.source.stem,
                parent_comment_id=self.record["data"][0]["parentCommentId"], context_sha256=fresh["documents"][0]["context_sha256"])
            self.save(fresh, candidate)
            with self.assertRaises(xp.legacy.ResearchError): self.validate(fresh)

    def test_changed_uid_and_unknown_id_are_never_inferred_from_nickname(self):
        receipt, audit = self.build()
        self.record["data"][0].update(userId="e" * 24, authorHrefRaw="/user/profile/" + "e" * 24)
        self.record["captured_at"] = "2026-10-31T00:30:00Z"; self.ingest()
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)
        self.record["data"][0].pop("commentId"); self.ingest()
        with self.assertRaises(xp.legacy.ResearchError): self.p.observed_review_plan(self.task)

    def test_new_resource_and_version_cannot_reuse_prior_actual_view_assertion(self):
        receipt, audit = self.build()
        self.record["captured_at"] = "2026-10-31T00:30:00Z"
        self.record["data"][0]["renderedInlineImages"][0]["url"] = URL.replace("emoji", "replacement")
        self.ingest(); fresh = self.p.observed_review_plan(self.task); fresh.update(reviewer="Synthetic", all_current_documents_read=True)
        fresh["visual_observations"] = receipt["visual_observations"]
        with self.assertRaises(xp.legacy.ResearchError): self.validate(fresh)
        # Even changing only the current manifest version invalidates the audit's frozen acquisition binding.
        manifest = json.loads(self.manifest_path.read_text()); manifest["items"][audit["images"][0]["media_id"]]["resource_version"] = 2
        self.manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)

    def test_other_empty_body_and_unread_full_text_scope_are_not_filled_by_images(self):
        receipt, audit = self.build()
        receipt["all_current_documents_read"] = False
        with self.assertRaises(xp.legacy.ResearchError): self.validate(receipt)
        self.record["data"].append({"commentId": "f" * 24, "userId": UID, "text": "", "is_reply": False})
        self.ingest(); fresh = self.p.observed_review_plan(self.task); fresh.update(reviewer="Synthetic", all_current_documents_read=True)
        fresh["visual_observations"] = receipt["visual_observations"]
        with self.assertRaises(xp.legacy.ResearchError): self.validate(fresh)

    def test_mixed_text_media_audit_does_not_certify_any_uncollected_expanded_text(self):
        self.record["data"][0].update(text="Synthetic visible excerpt … 展开", textSource="rendered_text", renderedReplyPrefixOnly=False)
        self.ingest(); receipt, audit = self.build()
        result = self.validate(receipt)
        self.assertFalse(result["whole_task_reviewed"])
        self.assertIn("uncollected_comments_or_replies", result["does_not_certify"])
        self.assertEqual(self.p.task(self.task)["review_status"], "pending")


if __name__ == "__main__":
    unittest.main()
