"""Offline hash-bound human-review tests; no real account, DB or image is used."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import xhs_project as xp

NID, CID, UID = "a" * 24, "b" * 24, "c" * 24


class ObservedReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.p = xp.Project(Path(self.temp.name) / "project", create=True)
        self.addCleanup(self.p.close)
        self.source = self.p.path / "synthetic.json"
        self.add_snapshot()
        self.task = self.p.db.execute("SELECT id FROM tasks WHERE kind='browser' AND entity_key=?", (NID,)).fetchone()[0]
        self.p.db.execute("UPDATE tasks SET status='blocked',review_status='pending',reason='synthetic_parser_gap' WHERE id=?", (self.task,))
        self.p.db.commit()

    def add_snapshot(self, text="Captured actual text", cid=CID, uid=UID, captured="2026-10-30T00:00:00Z"):
        self.record = {"operation": "browser_comments", "note_id": NID, "ok": True, "captured_at": captured,
                       "data": [{"commentId": cid, "is_reply": True, "parentCommentId": "d" * 24,
                                 "userId": uid, "authorHrefRaw": "/user/profile/" + uid,
                                 "text": text, "textSource": "rendered_text", "reply_to": "Target"}],
                       "coverage": {"rootPaginationState": "complete", "threads": []}}
        self.source.write_text(json.dumps(self.record))
        result = self.p.ingest(self.source)
        self.p.db.commit()
        return result

    def receipt(self):
        receipt = self.p.observed_review_plan(self.task)
        receipt.update(reviewer="synthetic analyst", all_current_documents_read=True)
        return receipt

    def freeze(self):
        return {table: [tuple(row) for row in self.p.db.execute("SELECT * FROM " + table + " ORDER BY 1")]
                for table in ["tasks", "threads", "meta", "documents", "snapshots", "document_sources"]}

    def visual_audit(self, receipt):
        folder = self.p.path / "manual"
        folder.mkdir(exist_ok=True)
        image = folder / "synthetic.png"
        image.write_bytes(b"synthetic fixture image, no actual account image")
        binding = folder / "binding.json"
        binding.write_text(json.dumps({"structure": [{"commentId": CID, "matchCount": 1}],
                                       "screenshot": {"path": str(image), "noteId": NID, "commentId": CID,
                                                      "capturedAt": "2026-10-30T02:00:00Z"}}))
        sid = receipt["documents"][0]["sources"][0]["snapshot_id"]
        audit = {"reviewer": "synthetic independent reader",
                 "screenshot": {"file": str(image), "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                                "actual_view_image": True, "note_id": NID, "comment_id": CID},
                 "raw_automation_source": {"document_id": "comment:" + NID + ":" + CID, "user_id": UID, "snapshot_id": sid},
                 "capture_source_record": {"file": str(binding), "sha256": hashlib.sha256(binding.read_bytes()).hexdigest()},
                 "visual_observation": {"body": "Synthetic visible emoji only; no doctor-name inference"}}
        audit["screenshot"]["captured_at"] = "2026-10-30T02:00:00Z"
        path = folder / "audit.json"
        path.write_text(json.dumps(audit))
        receipt["visual_observations"] = [{"document_id": "comment:" + NID + ":" + CID,
                                            "audit_path": str(path), "audit_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
        return path, audit

    def test_keeps_stop_pending_blocked_thread_and_remaining_unchanged(self):
        self.p.set_meta("stop", {"reason": "synthetic access restriction"})
        self.p.db.commit()
        before, outstanding = self.freeze(), self.p.status()["outstanding_tasks"]
        result = self.p.observed_review(self.task, self.receipt(), "Read only frozen observed documents")
        self.assertEqual(result["state"], "current")
        self.assertFalse(result["whole_task_reviewed"])
        self.assertEqual(self.freeze(), before)
        self.assertEqual(self.p.status()["outstanding_tasks"], outstanding)
        self.assertEqual(self.p.task(self.task)["review_status"], "pending")

    def test_idempotence_and_report_do_not_certify_full_completion(self):
        receipt = self.receipt()
        first = self.p.observed_review(self.task, receipt, "Frozen scope read")
        again = self.p.observed_review(self.task, receipt, "Frozen scope read")
        self.assertEqual(first["review_id"], again["review_id"])
        self.assertEqual(self.p.db.execute("SELECT count(*) FROM reviews").fetchone()[0], 1)
        self.p.report()
        report = xp.load(self.p.path / "coverage-report.json")
        self.assertEqual(report["state"], "work_remaining")
        self.assertEqual(report["observed_scope_reviews"][0]["state"], "current")
        self.assertIn(self.task, {task["id"] for task in report["remaining"]})

    def test_idempotence_only_reuses_latest_observed_review(self):
        receipt = self.receipt()
        old = self.p.observed_review(self.task, receipt, "Earlier rationale")
        middle = self.p.observed_review(self.task, receipt, "Later rationale")
        restored = self.p.observed_review(self.task, receipt, "Earlier rationale")
        self.assertNotEqual(restored["review_id"], old["review_id"])
        self.assertGreater(restored["review_id"], middle["review_id"])
        self.assertEqual(self.p.status()["observed_scope_reviews"][0]["review_id"], restored["review_id"])

    def test_plan_is_not_a_reading_assertion(self):
        with self.assertRaises(xp.legacy.ResearchError):
            self.p.observed_review(self.task, self.p.observed_review_plan(self.task), "A machine plan is not reading")
        self.assertEqual(self.p.db.execute("SELECT count(*) FROM reviews").fetchone()[0], 0)

    def test_real_project_api_schema_has_no_ok_flag(self):
        record = {"operation": "comments", "note_id": NID, "captured_at": "2026-10-29T00:00:00Z",
                  "page": 1, "cursor_in": "", "data": {"has_more": False, "comments": [
                      {"id": "d" * 24, "content": "Parent", "user_info": {"user_id": "f" * 24},
                       "sub_comment_count": 1, "sub_comments": [
                           {"id": CID, "content": "Earlier API text variant", "user_info": {"user_id": UID}}]}]}}
        source = self.p.path / "api.json"; source.write_text(json.dumps(record))
        self.p.ingest(source); self.p.db.commit()
        receipt = self.receipt()
        self.assertEqual(len(receipt["documents"]), 2)
        self.p.observed_review(self.task, receipt, "All current rows and source identities reviewed")

    def test_uid_text_source_context_and_full_set_cannot_be_substituted(self):
        for field in ["user_id", "text_sha256", "context_sha256", "sources", "document_id"]:
            receipt = self.receipt()
            receipt["documents"][0][field] = [] if field == "sources" else "forged"
            with self.assertRaises(xp.legacy.ResearchError):
                self.p.observed_review(self.task, receipt, "Wrong proof")
        for docs in [[], self.receipt()["documents"] * 2]:
            receipt = self.receipt(); receipt["documents"] = docs
            with self.assertRaises(xp.legacy.ResearchError): self.p.observed_review(self.task, receipt, "Partial/duplicate")
        self.assertEqual(self.p.db.execute("SELECT count(*) FROM reviews").fetchone()[0], 0)

    def test_prefix_requires_visual_and_valid_visual_is_narrow(self):
        self.add_snapshot("回复 Target :", captured="2026-10-30T01:00:00Z")
        receipt = self.receipt()
        with self.assertRaises(xp.legacy.ResearchError): self.p.observed_review(self.task, receipt, "Prefix is not body")
        self.visual_audit(receipt)
        before = self.freeze()
        result = self.p.observed_review(self.task, receipt, "Read captured body plus one bound image")
        self.assertEqual(result["visual_documents"], 1)
        self.assertEqual(self.freeze(), before)
        self.assertIn("other_media_content", result["does_not_certify"])

    def test_visual_claim_requires_actual_read_exact_note_comment_uid_and_source(self):
        self.add_snapshot("回复 Target :", captured="2026-10-30T01:00:00Z")
        for section, field, wrong in [("screenshot", "actual_view_image", False), ("screenshot", "note_id", "e" * 24),
                                      ("screenshot", "comment_id", "e" * 24), ("raw_automation_source", "user_id", "e" * 24),
                                      ("raw_automation_source", "snapshot_id", "e" * 64), ("screenshot", "sha256", "e" * 64)]:
            receipt = self.receipt(); path, audit = self.visual_audit(receipt)
            audit[section][field] = wrong; path.write_text(json.dumps(audit))
            receipt["visual_observations"][0]["audit_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(xp.legacy.ResearchError): self.p.observed_review(self.task, receipt, "Bad visual binding")

    def test_new_document_and_new_source_make_previous_review_stale(self):
        self.p.observed_review(self.task, self.receipt(), "Frozen scope")
        self.add_snapshot(cid="e" * 24, captured="2026-10-30T01:00:00Z")
        self.assertEqual(self.p.status()["observed_scope_reviews"][0]["state"], "stale")
        self.p.observed_review(self.task, self.receipt(), "Read enlarged scope")
        self.add_snapshot(captured="2026-10-30T02:00:00Z")
        self.assertEqual(self.p.status()["observed_scope_reviews"][0]["state"], "stale")

    def test_changed_text_or_parent_context_makes_review_stale(self):
        self.p.observed_review(self.task, self.receipt(), "Frozen scope")
        self.p.db.execute("UPDATE documents SET text='changed' WHERE kind='reply'")
        self.p.db.commit()
        self.assertEqual(self.p.status()["observed_scope_reviews"][0]["state"], "stale")
        self.p.db.execute("UPDATE documents SET text='Captured actual text'")
        metadata = json.loads(self.p.db.execute("SELECT metadata FROM documents LIMIT 1").fetchone()[0])
        metadata["parentCommentId"] = "f" * 24
        self.p.db.execute("UPDATE documents SET metadata=?", (json.dumps(metadata),))
        self.p.db.commit()
        self.assertEqual(self.p.status()["observed_scope_reviews"][0]["state"], "stale")
        with self.assertRaises(xp.legacy.ResearchError): self.receipt()

    def test_snapshot_corruption_and_artifact_change_stale(self):
        self.p.observed_review(self.task, self.receipt(), "Frozen scope")
        relative = self.p.db.execute("SELECT path FROM snapshots LIMIT 1").fetchone()[0]
        (self.p.path / relative).write_text("changed raw snapshot")
        self.assertEqual(self.p.status()["observed_scope_reviews"][0]["state"], "stale")

    def test_changed_screenshot_stales_visual_reading(self):
        self.add_snapshot("回复 Target :", captured="2026-10-30T01:00:00Z")
        receipt = self.receipt(); _, audit = self.visual_audit(receipt)
        self.p.observed_review(self.task, receipt, "Read bound screenshot")
        Path(audit["screenshot"]["file"]).write_bytes(b"changed")
        self.assertEqual(self.p.status()["observed_scope_reviews"][0]["state"], "stale")

    def test_old_screenshot_cannot_support_new_same_id_prefix_version(self):
        self.add_snapshot("回复 Target :", captured="2026-10-30T01:00:00Z")
        receipt = self.receipt(); self.visual_audit(receipt)
        self.p.observed_review(self.task, receipt, "Initial bound version")
        self.add_snapshot("回复 Target :", captured="2026-10-30T03:00:00Z")
        fresh = self.receipt()
        fresh["visual_observations"] = receipt["visual_observations"]
        with self.assertRaisesRegex(xp.legacy.ResearchError, "predates"):
            self.p.observed_review(self.task, fresh, "Cannot reuse an older image for a later version")
        self.assertEqual(self.p.status()["observed_scope_reviews"][0]["state"], "stale")

    def test_visual_time_must_be_valid_and_match_capture_binding(self):
        self.add_snapshot("回复 Target :", captured="2026-10-30T01:00:00Z")
        for value in [None, "invalid", "2026-10-30T02:00:00", "2026-10-30T04:00:00Z"]:
            receipt = self.receipt(); path, audit = self.visual_audit(receipt)
            audit["screenshot"]["captured_at"] = value; path.write_text(json.dumps(audit))
            receipt["visual_observations"][0]["audit_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(xp.legacy.ResearchError): self.p.observed_review(self.task, receipt, "Invalid timing")

    def test_identity_conflict_and_unknown_uid_refused(self):
        self.add_snapshot(uid="e" * 24, captured="2026-10-30T01:00:00Z")
        with self.assertRaises(xp.legacy.ResearchError): self.receipt()

    def test_snapshot_local_identity_and_symlink_refused(self):
        self.p.db.execute("UPDATE documents SET identity_kind='snapshot_local'")
        with self.assertRaises(xp.legacy.ResearchError): self.receipt()
        self.p.db.execute("UPDATE documents SET identity_kind='platform_id'")
        relative = self.p.db.execute("SELECT path FROM snapshots LIMIT 1").fetchone()[0]
        path = self.p.path / relative
        moved = path.with_suffix(".real"); path.rename(moved); path.symlink_to(moved)
        with self.assertRaises(xp.legacy.ResearchError): self.receipt()

    def test_generic_review_cannot_bypass_new_interface(self):
        with self.assertRaises(xp.legacy.ResearchError): self.p.review(self.task, "observed_scope", "No generic shortcut")
        with self.assertRaises(xp.legacy.ResearchError): self.p.review(self.task, "reviewed", "Blocked is not complete")
        self.assertEqual(self.p.status()["observed_scope_reviews"], [])


if __name__ == "__main__":
    unittest.main()
