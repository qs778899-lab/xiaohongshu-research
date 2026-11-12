"""Offline inline-emoji capture/import/review regression fixtures; no transport."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import xhs_media as media
import xhs_project as xp
import xhs_browser as browser


CASES = json.loads((Path(__file__).parent / "fixtures/inline-emoji-dom.json").read_text())


class InlineEmojiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = xp.Project(Path(self.temp.name) / "project", create=True)
        self.addCleanup(self.project.close)
        self.counter = 0

    def record(self, sample=CASES[0], text="[图片评论]", assets=True):
        row = {"commentId": sample["cid"], "parentCommentId": sample["rootId"],
               "userId": sample["authorId"], "authorHrefRaw": "/user/profile/" + sample["authorId"],
               "is_reply": True, "text": text, "textSource": "media_placeholder" if text == "[图片评论]" else "rendered_text",
               "reply_to": sample["target"], "renderedReplyPrefixOnly": sample["wrapper"],
               "images": []}
        if assets:
            row["renderedInlineImages"] = [{"domOrdinal": i, "url": image["src"], "observedClass": "note-content-emoji"}
                                          for i, image in enumerate(sample["labels"], 1)]
        return {"operation": "browser_comments", "ok": True, "note_id": sample["note_id"], "data": [row],
                "coverage": {"rootPaginationState": "complete", "threads": [{"commentId": sample["rootId"],
                    "rootKey": "id:" + sample["rootId"], "state": "visible_replies_read", "expanders": 0,
                    "visibleReplies": 1, "expectedReplies": 1}]}}

    def ingest(self, record):
        self.counter += 1
        record["captured_at"] = f"2026-10-31T00:{self.counter:02d}:00Z"
        path = self.project.path / f"input-{self.counter}.json"
        path.write_text(json.dumps(record, ensure_ascii=False))
        result = self.project.ingest(path)
        self.project.db.commit()
        return path, result

    def browser(self, nid):
        row = self.project.db.execute("SELECT id FROM tasks WHERE kind='browser' AND entity_key=?", (nid,)).fetchone()
        return self.project.task(row[0])

    def media_tasks(self):
        return [self.project.task(row[0]) for row in self.project.db.execute("SELECT id FROM tasks WHERE kind='media' ORDER BY entity_key")]

    def mark_synthetic_media_reviewed(self):
        # A complete synthetic imported artifact, not only two completion flags.
        for task in self.media_tasks():
            item = task['payload']; path = self.project.path / (item['media_id'] + '.image')
            path.write_bytes(b'synthetic fixture image')
            item.update(file=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        acquisition_status='downloaded', observation='Synthetic complete image observation')
            self.project.db.execute("UPDATE tasks SET status='complete',review_status='reviewed',payload=? WHERE id=?",
                                    (json.dumps(item), task['id']))

    def test_dom_shapes_keep_identity_sources_occurrences_and_pending_visual_review(self):
        for sample in CASES:
            path, receipt = self.ingest(self.record(sample))
            self.assertEqual(receipt["snapshot"], hashlib.sha256(path.read_bytes()).hexdigest())
            doc = self.project.db.execute("SELECT * FROM documents WHERE id=?", ("comment:" + sample["note_id"] + ":" + sample["cid"],)).fetchone()
            metadata = json.loads(doc["metadata"])
            self.assertEqual(doc["text"], "[图片评论]")
            self.assertEqual(metadata["userId"], sample["authorId"])
            self.assertEqual(metadata["parentCommentId"], sample["rootId"])
            task = self.browser(sample["note_id"])
            self.assertEqual((task["status"], task["review_status"]), ("complete", "pending"))
            assets = [t for t in self.media_tasks() if t["payload"]["comment_id"] == sample["cid"]]
            self.assertEqual(len(assets), len(sample["labels"]))
            self.assertEqual(len({t["entity_key"] for t in assets}), len(assets))
            self.assertEqual(len({t["payload"]["resource_fingerprint"] for t in assets}), 1)
            for asset in assets:
                self.assertEqual((asset["status"], asset["review_status"]), ("pending", "pending"))
                self.assertIn(receipt["snapshot"], asset["payload"]["source_path"])
                self.assertEqual(asset["payload"]["media_role"], "inline_emoji")
            scope = self.project.observed_review_plan(task["id"])
            self.assertIn(doc["id"], scope["requires_visual"])
            scope.update(reviewer="synthetic test", all_current_documents_read=True)
            with self.assertRaises(xp.legacy.ResearchError):
                self.project.observed_review(task["id"], scope, "A placeholder alone is not a visual reading")

    def test_repeated_ingest_and_smaller_snapshots_keep_all_three_media_slots(self):
        record = self.record(CASES[1])
        path, receipt = self.ingest(record)
        self.assertTrue(self.project.ingest(path)["cached"])
        self.ingest(record)
        smaller = self.record(CASES[1]); smaller["data"] = []
        smaller["coverage"]["threads"][0]["state"] = "previously_read"
        self.ingest(smaller)
        self.assertEqual(len(self.media_tasks()), 3)
        self.assertEqual(self.project.db.execute("SELECT count(*) FROM documents").fetchone()[0], 1)
        self.assertEqual(self.browser(CASES[1]["note_id"])["status"], "complete")
        with self.assertRaisesRegex(xp.legacy.ResearchError, "media remain unresolved"):
            self.project.review(self.browser(CASES[1]["note_id"])["id"], "reviewed", "Cannot replace images with placeholder")

    def test_pending_failed_or_unread_media_cannot_be_promoted_by_generic_review(self):
        self.ingest(self.record())
        task = self.browser(CASES[0]["note_id"])
        for status, review in [("pending", "pending"), ("blocked", "pending"), ("complete", "pending")]:
            self.project.db.execute("UPDATE tasks SET status=?,review_status=? WHERE kind='media'", (status, review))
            for decision in ("reviewed", "relevant"):
                with self.assertRaisesRegex(xp.legacy.ResearchError, "media remain unresolved"):
                    self.project.review(task["id"], decision, "Synthetic missing visual work")
        self.assertEqual(self.browser(CASES[0]["note_id"])["review_status"], "pending")
        # Explicit mock of an independently validated media import; not a real reading.
        self.mark_synthetic_media_reviewed()
        self.project.review(task["id"], "reviewed", "Synthetic already validated media")
        self.assertEqual(self.browser(CASES[0]["note_id"])["review_status"], "reviewed")

    def test_new_inline_asset_reopens_old_generic_review_with_same_text(self):
        record = self.record(text="same visible words", assets=False)
        self.ingest(record); task = self.browser(CASES[0]["note_id"])
        self.project.review(task["id"], "reviewed", "Synthetic initial words")
        before = self.project.db.execute("SELECT count(*) FROM reviews").fetchone()[0]
        self.ingest(self.record(text="same visible words"))
        self.assertEqual(self.browser(CASES[0]["note_id"])["review_status"], "pending")
        self.assertEqual(self.project.db.execute("SELECT count(*) FROM reviews").fetchone()[0], before)

    def test_missing_media_queue_and_unsupported_observed_source_remain_gaps(self):
        record = self.record(); record["data"][0]["renderedInlineImages"].append({"domOrdinal": 2, "url": ""})
        self.ingest(record)
        unsupported = [t for t in self.media_tasks() if t["payload"]["acquisition_status"] == "unsupported"]
        self.assertEqual(len(unsupported), 1)
        self.assertEqual(unsupported[0]["payload"]["reason"], "no_supported_observed_url")
        self.project.db.execute("DELETE FROM tasks WHERE kind='media'")
        with self.assertRaisesRegex(xp.legacy.ResearchError, "missing_media_task"):
            self.project.review(self.browser(CASES[0]["note_id"])["id"], "reviewed", "Cannot skip absent queue")

    def test_inline_positions_do_not_collide_with_normal_comment_pictures(self):
        record = self.record(CASES[1]); record["data"][0]["images"] = ["https://sns-webpic-qc.xhscdn.com/ordinary"]
        found = media.extract_media(record, "snapshot-one")
        self.assertEqual(len(found), 4)
        self.assertEqual(len({item["media_id"] for item in found}), 4)
        self.assertEqual(len([item for item in found if item.get("media_role") == "inline_emoji"]), 3)
        record["data"][0].pop("commentId")
        one = {item["media_id"] for item in media.extract_media(record, "snapshot-one")}
        two = {item["media_id"] for item in media.extract_media(record, "snapshot-two")}
        self.assertFalse(one & two, "Unknown identities must remain source-bound")

    def test_visible_text_with_only_missing_inline_source_still_queues_media_gap(self):
        record = self.record(text="Visible words")
        record["data"][0]["renderedInlineImages"][0]["url"] = ""
        self.ingest(record)
        task = self.browser(CASES[0]["note_id"])
        self.assertEqual(task["status"], "complete", "Text was captured, image review remains separate")
        self.assertEqual(self.media_tasks()[0]["payload"]["acquisition_status"], "unsupported")
        with self.assertRaisesRegex(xp.legacy.ResearchError, "media remain unresolved"):
            self.project.review(task["id"], "reviewed", "Cannot silently omit a missing inline source")

    def test_new_resource_version_invalidates_visual_status(self):
        self.ingest(self.record())
        self.mark_synthetic_media_reviewed()
        task = self.browser(CASES[0]["note_id"])
        self.project.review(task["id"], "reviewed", "Synthetic already validated first image")
        record = self.record(); record["data"][0]["renderedInlineImages"][0]["url"] = CASES[1]["labels"][0]["src"]
        self.ingest(record)
        self.assertEqual(len(self.media_tasks()), 1)
        self.assertEqual(self.media_tasks()[0]["review_status"], "pending")
        self.assertEqual(self.browser(CASES[0]["note_id"])["review_status"], "pending")

    def test_failed_browser_snapshot_preserves_typed_block_without_iterating_error_data(self):
        record = self.record(); record.update(ok=False, data={"error": "synthetic access failure"})
        self.ingest(record)
        task = self.browser(CASES[0]["note_id"])
        self.assertEqual((task["status"], task["reason"]), ("blocked", "browser_snapshot_failed"))
        self.assertEqual(self.media_tasks(), [])

    def saved_progress(self, sample=CASES[1]):
        run = self.project.path / "run"
        (run / "raw").mkdir(parents=True)
        record = self.record(sample)
        path = run / "raw" / ("browser-deep-" + sample["note_id"] + "-test.json")
        path.write_text(json.dumps(record))
        progress = browser.merge_browser_progress({}, record, str(path))
        progress["browserSnapshotSha256"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()}
        return run, record, path, progress

    def test_cross_process_hydration_retains_all_inline_positions_and_source_binding(self):
        run, record, path, progress = self.saved_progress()
        sample = CASES[1]
        # Round-trip serialized progress, as happens on a fresh Python invocation.
        progress = json.loads(json.dumps(progress))
        hydrated = browser.hydrate_reply_body_captures(run, sample["note_id"], progress)
        thread = hydrated["threads"]["id:" + sample["rootId"]]
        self.assertEqual(thread["state"], "visible_replies_read")
        self.assertFalse(thread["replyBodyVerificationPending"])
        capture = thread["replyBodyCaptures"][sample["cid"]]
        self.assertEqual(capture["renderedInlineImages"], record["data"][0]["renderedInlineImages"])
        self.assertEqual(capture["authorId"], sample["authorId"])
        self.assertEqual(capture["parentCommentId"], sample["rootId"])
        self.assertEqual(capture["sourceSnapshot"], str(path))
        self.assertEqual(capture["sourceSha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(browser.hydrate_reply_body_captures(run, sample["note_id"], hydrated), hydrated)

    def test_cross_process_smaller_snapshot_cannot_erase_inline_capture(self):
        run, record, path, progress = self.saved_progress()
        sample = CASES[1]
        small = self.record(sample); small["data"] = []
        small["coverage"]["threads"][0]["state"] = "previously_read"
        smallpath = run / "raw" / ("browser-deep-" + sample["note_id"] + "-smaller.json")
        smallpath.write_text(json.dumps(small))
        merged = browser.merge_browser_progress(progress, small, str(smallpath))
        hydrated = browser.hydrate_reply_body_captures(run, sample["note_id"], merged)
        thread = hydrated["threads"]["id:" + sample["rootId"]]
        self.assertEqual(thread["state"], "visible_replies_read")
        self.assertEqual(len(thread["replyBodyCaptures"][sample["cid"]]["renderedInlineImages"]), 3)

    def test_cross_process_partial_inline_sources_preserve_capture_and_media_gap(self):
        run, record, path, progress = self.saved_progress()
        sample = CASES[1]
        record["data"][0]["renderedInlineImages"][2]["url"] = ""
        path.write_text(json.dumps(record))
        progress["browserSnapshotSha256"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        hydrated = browser.hydrate_reply_body_captures(run, sample["note_id"], progress)
        thread = hydrated["threads"]["id:" + sample["rootId"]]
        self.assertEqual(thread["state"], "visible_replies_read")
        self.assertEqual(thread["replyBodyCaptures"][sample["cid"]]["renderedInlineImages"][2]["url"], "")
        self.ingest(record)
        self.assertEqual(len(self.media_tasks()), 3)
        self.assertEqual(len([t for t in self.media_tasks() if t["payload"]["acquisition_status"] == "unsupported"]), 1)
        with self.assertRaisesRegex(xp.legacy.ResearchError, "media remain unresolved"):
            self.project.review(self.browser(sample["note_id"])["id"], "reviewed", "Partial sources are not complete reading")

    def test_cross_process_uid_conflict_does_not_reuse_media_capture(self):
        run, record, path, progress = self.saved_progress()
        sample = CASES[1]
        changed = self.record(sample)
        changed["data"][0].update(userId="d" * 24, authorHrefRaw="/user/profile/" + "d" * 24)
        other = run / "raw" / ("browser-deep-" + sample["note_id"] + "-other.json")
        other.write_text(json.dumps(changed))
        progress["snapshots"].append(str(other))
        hydrated = browser.hydrate_reply_body_captures(run, sample["note_id"], progress)
        thread = hydrated["threads"]["id:" + sample["rootId"]]
        self.assertEqual(thread["state"], "stalled")
        self.assertEqual(thread["replyBodyCaptures"], {})
        self.assertEqual(hydrated["replyBodyMigration"]["identityConflicts"], [sample["cid"]])

    def test_cross_process_changed_hash_or_wrong_run_and_note_refused(self):
        run, record, path, progress = self.saved_progress()
        sample = CASES[1]
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        bad_hash = browser.hydrate_reply_body_captures(run, sample["note_id"], progress)
        self.assertEqual(bad_hash["replyBodyMigration"]["skipped"][0]["reason"], "snapshot_sha256_changed")
        self.assertEqual(bad_hash["threads"]["id:" + sample["rootId"]]["replyBodyCaptures"], {})
        path.write_bytes(original)
        for wrong_run, wrong_note in [(run.parent / "another-run", sample["note_id"]), (run, "f" * 24)]:
            result = browser.hydrate_reply_body_captures(wrong_run, wrong_note, progress)
            self.assertEqual(result["replyBodyMigration"]["skipped"][0]["reason"], "not_same_run_note_raw_snapshot")
            self.assertEqual(result["threads"]["id:" + sample["rootId"]]["replyBodyCaptures"], {})

    def test_cross_process_old_progress_boolean_or_invalid_sources_not_capture(self):
        run, record, path, progress = self.saved_progress()
        sample = CASES[1]
        for assets in [None, [{"url": "", "domOrdinal": 1, "observedClass": "note-content-emoji"}],
                       [{"url": sample["labels"][0]["src"], "domOrdinal": 1, "observedClass": "avatar"}]]:
            row = record["data"][0]
            if assets is None: row.pop("renderedInlineImages", None)
            else: row["renderedInlineImages"] = assets
            path.write_text(json.dumps(record))
            progress["browserSnapshotSha256"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            result = browser.hydrate_reply_body_captures(run, sample["note_id"], progress)
            thread = result["threads"]["id:" + sample["rootId"]]
            self.assertEqual(thread["state"], "stalled")
            self.assertEqual(thread["replyBodyCaptures"], {})


class ReplyIntentPersistenceTests(unittest.TestCase):
    """A resolved click cannot become pending again after progress is merged."""
    root, first, second, author = "a" * 24, "b" * 24, "c" * 24, "d" * 24

    def fixture(self):
        body = {"commentId": self.first, "parentCommentId": self.root,
                "authorId": self.author, "text": "Original body", "textSource": "rendered_text"}
        pending = {"clickedAt": 100, "beforeReplyIds": [self.first],
                   "beforeReplyBodyCaptures": {self.first: body},
                   "expanderText": "expand one", "state": "outcome_unconfirmed"}
        thread = {"rootKey": "id:" + self.root, "commentId": self.root,
                  "state": "stalled", "reason": "reply_expansion_outcome_unconfirmed",
                  "visibleReplies": 1, "uniqueObservedReplyCount": 1,
                  "replyBodyCaptures": {self.first: body}, "pendingReplyExpansion": pending}
        previous = {"threads": {thread["rootKey"]: thread}, "observedRootIds": [self.root],
                    "observedCommentIds": [self.root, self.first], "rootPaginationState": "pending"}
        observed = json.loads(json.dumps(thread))
        observed.pop("pendingReplyExpansion")
        observed.update(state="visible_replies_read", reason="", visibleReplies=2,
                        uniqueObservedReplyCount=2, expanders=[])
        observed["replyBodyCaptures"][self.second] = {**body, "commentId": self.second, "text": "New body"}
        observed["lastReplyExpansion"] = {**pending, "state": "render_progress_observed",
            "observedAt": 101, "observedReplyIds": [self.first, self.second],
            "progressBasis": "new_rendered_reply_id"}
        return previous, observed

    def merge(self, previous, incoming, data=None):
        return browser.merge_browser_progress(previous, {"data": data or [], "coverage": {
            "rootPaginationState": "pending", "threads": [incoming]}}, "/synthetic/new.json")

    def test_matching_observed_new_id_retires_stale_pending_without_mutating_inputs(self):
        old, incoming = self.fixture(); saved = json.dumps([old, incoming], sort_keys=True)
        result = self.merge(old, incoming)
        thread = result["threads"][incoming["rootKey"]]
        self.assertNotIn("pendingReplyExpansion", thread)
        self.assertEqual(thread["lastReplyExpansion"], incoming["lastReplyExpansion"])
        self.assertEqual(thread["reconciledReplyExpansions"][0]["pendingReplyExpansion"],
                         old["threads"][incoming["rootKey"]]["pendingReplyExpansion"])
        self.assertEqual(json.dumps([old, incoming], sort_keys=True), saved)
        self.assertEqual(result["rootPaginationState"], "pending")

    def test_contaminated_cached_intent_normalizes_but_does_not_complete_scope(self):
        old, incoming = self.fixture(); key = incoming["rootKey"]
        incoming["pendingReplyExpansion"] = old["threads"][key]["pendingReplyExpansion"]
        incoming.update(state="stalled", reason="reply_expansion_outcome_unconfirmed", expanders=["expand one"])
        old["threads"][key] = json.loads(json.dumps(incoming))
        result = self.merge(old, incoming); thread = result["threads"][key]
        self.assertNotIn("pendingReplyExpansion", thread)
        self.assertEqual(thread["state"], "not_processed")
        self.assertNotEqual(result["status"], "visible_scope_complete")
        again = self.merge(result, thread)
        self.assertEqual(len(again["threads"][key]["reconciledReplyExpansions"]), 1)

    def test_mismatched_intent_or_unsupported_observation_keeps_pending(self):
        for change in ({"clickedAt": 99}, {"beforeReplyIds": []},
                       {"observedAt": 99}, {"observedReplyIds": [self.first]},
                       {"progressBasis": "count_increased"}, {"state": "outcome_unconfirmed"}):
            with self.subTest(change=change):
                old, incoming = self.fixture(); incoming["lastReplyExpansion"].update(change)
                thread = self.merge(old, incoming)["threads"][incoming["rootKey"]]
                self.assertIn("pendingReplyExpansion", thread)
        old, incoming = self.fixture()
        incoming["pendingReplyExpansion"] = {**old["threads"][incoming["rootKey"]]["pendingReplyExpansion"],
                                             "clickedAt": 102}
        self.assertEqual(self.merge(old, incoming)["threads"][incoming["rootKey"]]
                         ["pendingReplyExpansion"]["clickedAt"], 102)

    def test_counts_and_absent_controls_alone_cannot_release_pending(self):
        old, incoming = self.fixture(); incoming.pop("lastReplyExpansion")
        incoming["visibleReplies"] = 20
        thread = self.merge(old, incoming)["threads"][incoming["rootKey"]]
        self.assertIn("pendingReplyExpansion", thread)
        completed_roots = browser.merge_browser_progress(old, {"data": [], "coverage": {
            "rootPaginationState": "complete", "explicitEnd": True, "threads": [incoming]}}, "/synthetic/end.json")
        self.assertNotEqual(completed_roots["status"], "visible_scope_complete")

    def test_new_reply_id_accepts_media_or_bound_row_but_not_an_unobserved_id(self):
        old, incoming = self.fixture()
        incoming["replyBodyCaptures"][self.second].update(text="[图片评论]", textSource="media_placeholder",
                                                       images=["https://example.test/comment.png"])
        thread = self.merge(old, incoming)["threads"][incoming["rootKey"]]
        self.assertNotIn("pendingReplyExpansion", thread)
        self.assertNotIn("reviewed", thread["replyBodyCaptures"][self.second])
        old, incoming = self.fixture(); incoming["replyBodyCaptures"].pop(self.second)
        self.assertIn("pendingReplyExpansion", self.merge(old, incoming)["threads"][incoming["rootKey"]])
        row = {"commentId": self.second, "parentCommentId": self.root, "is_reply": True}
        self.assertNotIn("pendingReplyExpansion", self.merge(old, incoming, [row])["threads"][incoming["rootKey"]])
        row["parentCommentId"] = "f" * 24
        self.assertIn("pendingReplyExpansion", self.merge(old, incoming, [row])["threads"][incoming["rootKey"]])

    def test_missing_or_fingerprint_parent_cannot_release_an_unknown_intent(self):
        for parent, key in (("", "fp:legacy-root:0"), ("legacy-root", "id:legacy-root"),
                            (self.root, "fp:legacy-root:0")):
            with self.subTest(parent=parent, key=key):
                old, incoming = self.fixture(); original = incoming["rootKey"]
                old["threads"][key] = old["threads"].pop(original)
                for thread in (old["threads"][key], incoming):
                    thread.update(commentId=parent, rootKey=key)
                    for body in thread["replyBodyCaptures"].values():
                        body["parentCommentId"] = parent
                self.assertIn("pendingReplyExpansion", self.merge(old, incoming)["threads"][key])
                old, incoming = self.retired_fixture(); original = incoming["rootKey"]
                old["threads"][key] = old["threads"].pop(original)
                old["threads"][key].update(commentId=parent, rootKey=key)
                incoming.update(commentId=parent, rootKey=key)
                incoming["retiredReplyExpansions"][0]["replacementDocument"]["rootCommentId"] = parent
                self.assertIn("pendingReplyExpansion", self.retire_merge(old, incoming)["threads"][key])

    def test_changed_body_requires_same_real_identity_and_rendered_body(self):
        for changes, accepted in (({}, True), ({"authorId": "e" * 24}, False),
                                  ({"parentCommentId": "f" * 24}, False),
                                  ({"textSource": "rendered_reply_prefix_only"}, False),
                                  ({"text": ""}, False), ({"text": "Original body"}, False)):
            with self.subTest(changes=changes):
                old, incoming = self.fixture()
                incoming["replyBodyCaptures"] = {self.first: {
                    **old["threads"][incoming["rootKey"]]["replyBodyCaptures"][self.first],
                    "text": "Actually changed body", **changes}}
                incoming["lastReplyExpansion"].update(progressBasis="changed_verified_reply_body",
                                                       observedReplyIds=[self.first])
                thread = self.merge(old, incoming)["threads"][incoming["rootKey"]]
                self.assertEqual("pendingReplyExpansion" not in thread, accepted)

    def restoration_fixture(self):
        old, incoming = self.fixture(); key = incoming["rootKey"]
        cached = json.loads(json.dumps(incoming["replyBodyCaptures"][self.second]))
        old["threads"][key]["replyBodyCaptures"][self.second] = cached
        old["observedCommentIds"].append(self.second)
        pending = old["threads"][key]["pendingReplyExpansion"]
        pending.update(noteId="e" * 24, rootCommentId=self.root, documentTimeOrigin=50,
                       beforeKnownReplyIds=[self.first, self.second],
                       beforeKnownReplyBodyCaptures={self.second: cached})
        incoming["lastReplyExpansion"] = {
            **json.loads(json.dumps(pending)), "state": "render_progress_observed",
            "observedAt": 101, "observedReplyIds": [self.first, self.second],
            "restoredReplyIds": [self.second], "progressBasis": "known_reply_render_restored",
            "newResearchEvidence": False}
        row = {**cached, "is_reply": True, "authorHrefRaw": "/user/profile/" + self.author}
        return old, incoming, row

    def restoration_merge(self, old, incoming, rows):
        return browser.merge_browser_progress(old, {"note_id": "e" * 24, "data": rows,
            "coverage": {"rootPaginationState": "pending", "threads": [incoming]}}, "/synthetic/restored.json")

    def test_known_restoration_resolves_only_its_intent_without_new_content_or_completion(self):
        old, incoming, row = self.restoration_fixture(); key = incoming["rootKey"]
        incoming.update(state="stalled", reason="reply_expansion_outcome_unconfirmed")
        result = self.restoration_merge(old, incoming, [row]); thread = result["threads"][key]
        self.assertNotIn("pendingReplyExpansion", thread)
        self.assertEqual(thread["lastReplyExpansion"]["newResearchEvidence"], False)
        self.assertEqual(set(result["observedCommentIds"]), set(old["observedCommentIds"]))
        self.assertEqual(thread["state"], "not_processed")
        self.assertNotEqual(result["status"], "visible_scope_complete")
        self.assertEqual(set(thread["replyBodyCaptures"]), {self.first, self.second})
        self.assertNotIn("reviewed", thread["replyBodyCaptures"][self.second])

    def test_known_restoration_requires_exact_baseline_note_root_intent_and_old_body(self):
        cases = ["different_intent", "wrong_note", "wrong_root", "already_in_before_dom",
                 "not_previously_known", "missing_preclick_cache", "different_proof_cache",
                 "empty_cached_body", "wrong_cached_author", "changed_current_body",
                 "missing_trusted_old_cache", "flag_missing", "flag_true"]
        for case in cases:
            with self.subTest(case=case):
                old, incoming, row = self.restoration_fixture(); key = incoming["rootKey"]
                pending = old["threads"][key]["pendingReplyExpansion"]
                proof = incoming["lastReplyExpansion"]
                if case == "different_intent": proof["clickedAt"] = 102
                elif case == "wrong_note": pending["noteId"] = proof["noteId"] = "f" * 24
                elif case == "wrong_root": pending["rootCommentId"] = proof["rootCommentId"] = "f" * 24
                elif case == "already_in_before_dom":
                    pending["beforeReplyIds"] = proof["beforeReplyIds"] = [self.first, self.second]
                elif case == "not_previously_known":
                    pending["beforeKnownReplyIds"] = proof["beforeKnownReplyIds"] = [self.first]
                elif case == "missing_preclick_cache":
                    pending.pop("beforeKnownReplyBodyCaptures"); proof.pop("beforeKnownReplyBodyCaptures")
                elif case == "different_proof_cache":
                    proof["beforeKnownReplyBodyCaptures"][self.second]["text"] = "unsupported declaration"
                elif case == "empty_cached_body":
                    pending["beforeKnownReplyBodyCaptures"][self.second]["text"] = ""
                    proof["beforeKnownReplyBodyCaptures"][self.second]["text"] = ""
                elif case == "wrong_cached_author":
                    pending["beforeKnownReplyBodyCaptures"][self.second]["authorId"] = "f" * 24
                    proof["beforeKnownReplyBodyCaptures"][self.second]["authorId"] = "f" * 24
                elif case == "changed_current_body":
                    incoming["replyBodyCaptures"][self.second]["text"] = "new words need actual reading"
                    row["text"] = "new words need actual reading"
                elif case == "missing_trusted_old_cache":
                    old["threads"][key]["replyBodyCaptures"].pop(self.second)
                elif case == "flag_missing": proof.pop("newResearchEvidence")
                elif case == "flag_true": proof["newResearchEvidence"] = True
                self.assertIn("pendingReplyExpansion", self.restoration_merge(old, incoming, [row])["threads"][key])

    def test_known_restoration_requires_current_rendered_identity_and_body_not_cached_counts(self):
        for changes in (None, {"parentCommentId": "f" * 24}, {"authorHrefRaw": ""},
                        {"authorHrefRaw": "/user/profile/" + "f" * 24}, {"text": ""},
                        {"textSource": "media_placeholder", "text": "[图片评论]"},
                        {"textSource": "rendered_reply_prefix_only"}, {"is_reply": False}):
            with self.subTest(changes=changes):
                old, incoming, row = self.restoration_fixture()
                if changes is not None: row.update(changes)
                incoming["visibleReplies"] = 99
                incoming["lastReplyExpansion"]["observedAt"] = 999999
                rows = [] if changes is None else [row]
                self.assertIn("pendingReplyExpansion", self.restoration_merge(old, incoming, rows)
                              ["threads"][incoming["rootKey"]])

    def test_known_restoration_rejects_only_whole_deleted_placeholders_not_real_words(self):
        cases = [(text, False) for text in (
            "原评论已删除", "该评论已删除", "评论已被删除", "此评论已被删除。", " 原 评论 已 删除 ")]
        cases += [(text, True) for text in (
            "用户已注销但这是真实就诊正文", "原评论已删除，我补充真实治疗经过", "我不想删除这段评价")]
        for text, accepted in cases:
            with self.subTest(text=text):
                old, incoming, row = self.restoration_fixture(); key = incoming["rootKey"]
                old["threads"][key]["replyBodyCaptures"][self.second]["text"] = text
                old["threads"][key]["pendingReplyExpansion"]["beforeKnownReplyBodyCaptures"][self.second]["text"] = text
                incoming["lastReplyExpansion"]["beforeKnownReplyBodyCaptures"][self.second]["text"] = text
                incoming["replyBodyCaptures"][self.second]["text"] = text
                row.update(text=text, author="用户已注销")
                thread = self.restoration_merge(old, incoming, [row])["threads"][key]
                self.assertEqual("pendingReplyExpansion" not in thread, accepted)
                self.assertIn(self.second, thread["replyBodyCaptures"], "Rejecting proof must not delete old IDs")

    def test_smaller_snapshot_preserves_cumulative_bodies_ids_and_reply_minimum(self):
        old, incoming = self.fixture(); key = incoming["rootKey"]
        old["threads"][key] = json.loads(json.dumps(incoming))
        old["threads"][key].update(legacyReplyBodyIds=[self.first, self.second], replyBodyMinimumCount=2)
        old["observedCommentIds"].append(self.second)
        incoming["replyBodyCaptures"] = {self.first: incoming["replyBodyCaptures"][self.first]}
        incoming.update(legacyReplyBodyIds=[self.first], replyBodyMinimumCount=1,
                        uniqueObservedReplyCount=1, visibleReplies=1)
        thread = self.merge(old, incoming)["threads"][key]
        self.assertEqual(set(thread["replyBodyCaptures"]), {self.first, self.second})
        self.assertEqual(set(thread["legacyReplyBodyIds"]), {self.first, self.second})
        self.assertEqual(thread["replyBodyMinimumCount"], 2)
        self.assertEqual(thread["uniqueObservedReplyCount"], 2)

    def retired_fixture(self):
        old, incoming = self.fixture(); key = incoming["rootKey"]
        pending = old["threads"][key]["pendingReplyExpansion"]
        pending["documentTimeOrigin"] = 50
        incoming["lastReplyExpansion"].update(clickedAt=201, observedAt=203, documentTimeOrigin=200)
        incoming["retiredReplyExpansions"] = [{**pending, "state": "retired_by_replacement_document",
            "retiredAt": 202, "replacementDocument": {
                "basis": "current_document_created_after_original_click", "noteId": "e" * 24,
                "rootCommentId": self.root, "originalClickedAt": 100, "previousDocumentTimeOrigin": 50,
                "currentDocumentTimeOrigin": 200, "observedAt": 201, "strictExpanderText": "展开 1 条回复"}}]
        return old, incoming

    def retire_merge(self, old, incoming):
        return browser.merge_browser_progress(old, {"note_id": "e" * 24, "data": [], "coverage": {
            "rootPaginationState": "pending", "threads": [incoming]}}, "/synthetic/replacement.json")

    def test_typed_document_retirement_does_not_resurrect_the_old_pending(self):
        old, incoming = self.retired_fixture()
        thread = self.retire_merge(old, incoming)["threads"][incoming["rootKey"]]
        self.assertNotIn("pendingReplyExpansion", thread)
        self.assertEqual(thread["retiredReplyExpansions"], incoming["retiredReplyExpansions"])
        self.assertEqual(thread["lastReplyExpansion"], incoming["lastReplyExpansion"])

    def test_retirement_mismatch_never_clears_pending_and_new_pending_is_preserved(self):
        for changes in ({"noteId": "f" * 24}, {"rootCommentId": "f" * 24},
                        {"originalClickedAt": 99}, {"currentDocumentTimeOrigin": 100},
                        {"previousDocumentTimeOrigin": 101}, {"observedAt": 199},
                        {"strictExpanderText": "follow"}, {"basis": "generic_resolved"}):
            with self.subTest(changes=changes):
                old, incoming = self.retired_fixture()
                incoming["retiredReplyExpansions"][0]["replacementDocument"].update(changes)
                self.assertIn("pendingReplyExpansion", self.retire_merge(old, incoming)
                              ["threads"][incoming["rootKey"]])
        old, incoming = self.retired_fixture(); incoming["retiredReplyExpansions"][0]["retiredAt"] = 199
        self.assertIn("pendingReplyExpansion", self.retire_merge(old, incoming)["threads"][incoming["rootKey"]])
        old, incoming = self.retired_fixture()
        old["threads"][incoming["rootKey"]]["pendingReplyExpansion"].pop("beforeReplyIds")
        incoming["retiredReplyExpansions"][0].pop("beforeReplyIds")
        self.assertIn("pendingReplyExpansion", self.retire_merge(old, incoming)["threads"][incoming["rootKey"]])
        old, incoming = self.retired_fixture()
        incoming["pendingReplyExpansion"] = {"clickedAt": 203, "documentTimeOrigin": 200,
            "beforeReplyIds": [self.first, self.second], "state": "outcome_unconfirmed"}
        self.assertEqual(self.retire_merge(old, incoming)["threads"][incoming["rootKey"]]
                         ["pendingReplyExpansion"], incoming["pendingReplyExpansion"])


if __name__ == "__main__":
    unittest.main()
