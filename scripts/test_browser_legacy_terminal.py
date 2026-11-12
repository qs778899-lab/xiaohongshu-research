"""Legacy completion claims cannot replace observed root-end evidence."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import xhs_browser as browser
import xhs_bridge as bridge
import xhs_native as native
import xhs_project as project


NOTE = "a" * 24
OTHER = "b" * 24
URL = "https://www.xiaohongshu.com/explore/" + NOTE


class LegacyTerminalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = self.root / "bridge" / "raw"
        self.raw.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, coverage, note=NOTE, **extra):
        value = dict(url=URL.replace(NOTE, note), results=[], commentTotal=0,
                     deepCoverage=coverage, **extra)
        raw = json.dumps(value, sort_keys=True).encode()
        file = self.raw / (hashlib.sha256(raw).hexdigest() + ".json")
        file.write_bytes(raw)
        return file

    def complete(self):
        return {"rootPaginationState": "complete", "explicitEnd": False,
                "threads": [], "observedRootIds": []}

    def empty(self):
        return dict(self.complete(), rootEndBasis="rendered_comment_empty_state",
                    emptyCommentState={"scope": "rendered_comments_container_empty_state",
                        "visible": True, "acceptedAsEnd": True,
                        "loadingVisible": False, "text": "暂无评论"})

    def pending(self, attempts=1):
        coverage = {"rootPaginationState": "pending", "explicitEnd": False,
                    "threads": [], "observedRootIds": [],
                    "rootScrollProgress": {"noteId": NOTE, "attempts": attempts,
                        "stalledRounds": 0, "state": "pending"}}
        file = self.source(coverage)
        return {"note_id": NOTE, "data": [], "coverage": coverage}, file

    def prior(self, source):
        return {"rootPaginationState": "complete", "status": "visible_scope_complete",
                "last_snapshot": str(source), "snapshots": [str(source)],
                "threads": {}, "observedRootIds": [], "observedCommentIds": []}

    def test_old_zero_claim_becomes_pending_and_preserves_original_claim(self):
        old_file = self.source(self.complete()); original = old_file.read_bytes()
        old = self.prior(old_file); saved = copy.deepcopy(old)
        snap, new_file = self.pending()
        result = browser.merge_browser_progress(old, snap, str(new_file))
        self.assertEqual(result["rootPaginationState"], "pending")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["legacyRootCompletionClaims"][0]["last_snapshot"], str(old_file))
        self.assertEqual(old, saved)
        self.assertEqual(old_file.read_bytes(), original)

    def test_real_explicit_end_and_empty_state_remain_complete(self):
        for coverage in (dict(self.complete(), explicitEnd=True), self.empty()):
            with self.subTest(coverage=coverage):
                old_file = self.source(coverage); snap, new_file = self.pending()
                result = browser.merge_browser_progress(self.prior(old_file), snap, str(new_file))
                self.assertEqual(result["rootPaginationState"], "complete")
                self.assertEqual(result["rootEndEvidence"][0]["source_sha256"], old_file.stem)

    def test_earlier_actual_end_survives_later_partial_capture(self):
        end = self.source(dict(self.complete(), explicitEnd=True))
        partial = self.source(self.complete())
        old = self.prior(partial); old["snapshots"].insert(0, str(end))
        snap, new_file = self.pending()
        result = browser.merge_browser_progress(old, snap, str(new_file))
        self.assertEqual(result["rootPaginationState"], "complete")
        self.assertEqual(result["rootEndEvidence"][0]["source_file"], str(end))

    def test_wrong_note_corrupt_or_failed_end_does_not_supply_evidence(self):
        cases = [(self.source(dict(self.complete(), explicitEnd=True), note=OTHER), False)]
        corrupt = self.source(dict(self.complete(), explicitEnd=True))
        corrupt.write_text(corrupt.read_text() + " ")
        cases.append((corrupt, False))
        for flag in ("loginWall", "securityBlock", "notFound", "partial", "partial_prior_note_only"):
            cases.append((self.source(dict(self.complete(), explicitEnd=True), **{flag: True}), False))
        cases.append((self.source(dict(self.complete(), explicitEnd=True), ok=False), False))
        for file, _ in cases:
            with self.subTest(file=file):
                snap, new_file = self.pending()
                result = browser.merge_browser_progress(self.prior(file), snap, str(new_file))
                self.assertEqual(result["rootPaginationState"], "pending")

    def test_loading_or_positive_total_cannot_validate_empty(self):
        for change in ("loading", "positive", "not_visible", "not_accepted"):
            coverage = self.empty()
            if change == "loading": coverage["emptyCommentState"]["loadingVisible"] = True
            if change == "positive": coverage["commentCountObservation"] = {"latest": {"positiveTotalObserved": True}}
            if change == "not_visible": coverage["emptyCommentState"]["visible"] = False
            if change == "not_accepted": coverage["emptyCommentState"]["acceptedAsEnd"] = False
            snap, file = self.pending()
            result = browser.merge_browser_progress(self.prior(self.source(coverage)), snap, str(file))
            self.assertEqual(result["rootPaginationState"], "pending")

    def test_older_scroll_diagnostic_does_not_resurrect_unsupported_complete(self):
        old = self.prior(self.source(self.complete()))
        old["rootScrollProgress"] = {"noteId": NOTE, "attempts": 5, "stalledRounds": 2}
        old["observedRootIds"] = [OTHER]; old["observedCommentIds"] = [OTHER]
        snap, file = self.pending()
        result = browser.merge_browser_progress(old, snap, str(file))
        self.assertEqual(result["rootPaginationState"], "pending")
        self.assertEqual(result["rootScrollProgress"], old["rootScrollProgress"])
        self.assertEqual(result["observedRootIds"], [OTHER])
        self.assertEqual(result["observedCommentIds"], [OTHER])

    def stalled_empty_history(self):
        return {"rootPaginationState": "pending", "status": "pending", "threads": {},
                "observedRootIds": [], "observedCommentIds": [], "expectedRootIds": [],
                "uncertainRowsBySnapshot": {"older": 0},
                "rootScrollProgress": {"noteId": NOTE, "attempts": 3, "stalledRounds": 2, "state": "waiting"}}

    def new_empty_after_stall(self):
        coverage = dict(self.empty(), rootsInDomAtEnd=0,
                        commentCountObservation={"latest": {"positiveTotalObserved": False}},
                        rootScrollProgress={"noteId": NOTE, "attempts": 3, "stalledRounds": 0, "state": "end_observed"})
        coverage["emptyCommentState"].update(variant="no_comments_component", commentsContainerPresent=False,
                                           directParentChainVerified=True, text="这是一片荒地点击评论")
        return {"note_id": NOTE, "coverage": coverage, "data": []}

    def test_actual_empty_end_can_settle_same_attempt_stall(self):
        previous = self.stalled_empty_history(); saved = copy.deepcopy(previous)
        snap = self.new_empty_after_stall()
        result = browser.merge_browser_progress(previous, snap, "current-empty.json")
        self.assertEqual(result["rootPaginationState"], "complete")
        self.assertEqual(result["status"], "visible_scope_complete")
        self.assertEqual(result["rootScrollProgress"]["stalledRounds"], 0)
        self.assertEqual(previous, saved)

    def test_counter_reset_without_real_empty_evidence_does_not_complete(self):
        for change in ("basis", "accepted", "hidden", "loading", "positive", "dom_roots", "missing_dom_count", "not_complete"):
            with self.subTest(change=change):
                previous = self.stalled_empty_history(); snap = self.new_empty_after_stall(); cov = snap["coverage"]
                if change == "basis": cov.pop("rootEndBasis")
                if change == "accepted": cov["emptyCommentState"]["acceptedAsEnd"] = False
                if change == "hidden": cov["emptyCommentState"]["visible"] = False
                if change == "loading": cov["emptyCommentState"]["loadingVisible"] = True
                if change == "positive": cov["commentCountObservation"]["latest"]["positiveTotalObserved"] = True
                if change == "dom_roots": cov["rootsInDomAtEnd"] = 1
                if change == "missing_dom_count": cov.pop("rootsInDomAtEnd")
                if change == "not_complete": cov["rootPaginationState"] = "pending"
                result = browser.merge_browser_progress(previous, snap, "current-empty.json")
                self.assertEqual(result["rootPaginationState"], "pending")
                self.assertEqual(result["rootScrollProgress"], previous["rootScrollProgress"])

    def test_empty_reset_cannot_erase_cumulative_roots_threads_or_comments(self):
        for change in ("root", "expected", "incoming_expected", "comment", "uncertain", "thread", "current_comment", "current_parent"):
            with self.subTest(change=change):
                previous = self.stalled_empty_history(); snap = self.new_empty_after_stall()
                if change == "root": previous["observedRootIds"] = [OTHER]
                if change == "expected": previous["expectedRootIds"] = [OTHER]
                if change == "incoming_expected": snap["coverage"]["expectedRootIds"] = [OTHER]
                if change == "comment": previous["observedCommentIds"] = [OTHER]
                if change == "uncertain": previous["uncertainRowsBySnapshot"]["older"] = 1
                if change == "thread": previous["threads"] = {"old": {"state": "visible_replies_read"}}
                if change == "current_comment": snap["data"] = [{"commentId": OTHER, "is_reply": True}]
                if change == "current_parent": snap["data"] = [{"commentId": OTHER, "is_reply": False}]
                result = browser.merge_browser_progress(previous, snap, "current-empty.json")
                self.assertNotEqual(result["rootPaginationState"], "complete")
                self.assertNotEqual(result["status"], "visible_scope_complete")
                self.assertEqual(result["rootScrollProgress"], previous["rootScrollProgress"])
                if change == "comment": self.assertEqual(result["observedCommentIds"], [OTHER])
                if change == "uncertain": self.assertEqual(result["uncertainRowsBySnapshot"]["older"], 1)

    def test_invalid_empty_claim_cannot_use_new_parent_as_reset_fallback(self):
        for change in ("loading", "positive", "hidden", "accepted", "empty_text", "scope", "dom_roots", "missing_dom_count"):
            with self.subTest(change=change):
                previous = self.stalled_empty_history(); snap = self.new_empty_after_stall(); cov = snap["coverage"]
                snap["data"] = [{"commentId": OTHER, "is_reply": False}]
                if change == "loading": cov["emptyCommentState"]["loadingVisible"] = True
                if change == "positive": cov["commentCountObservation"]["latest"]["positiveTotalObserved"] = True
                if change == "hidden": cov["emptyCommentState"]["visible"] = False
                if change == "accepted": cov["emptyCommentState"]["acceptedAsEnd"] = False
                if change == "empty_text": cov["emptyCommentState"]["text"] = ""
                if change == "scope": cov["emptyCommentState"]["scope"] = "untrusted_scope"
                if change == "dom_roots": cov["rootsInDomAtEnd"] = 1
                if change == "missing_dom_count": cov.pop("rootsInDomAtEnd")
                result = browser.merge_browser_progress(previous, snap, "contradictory-empty.json")
                self.assertEqual(result["rootPaginationState"], "pending")
                self.assertEqual(result["status"], "pending")
                self.assertEqual(result["rootScrollProgress"], previous["rootScrollProgress"])
                self.assertEqual(result["observedRootIds"], [])
                self.assertEqual(result["observedCommentIds"], [OTHER])

    def test_smaller_scroll_view_does_not_replace_newer_scroll_diagnostics(self):
        previous = self.stalled_empty_history(); snap = self.new_empty_after_stall()
        snap["coverage"]["rootScrollProgress"]["attempts"] = 2
        result = browser.merge_browser_progress(previous, snap, "older-scroll.json")
        self.assertEqual(result["rootPaginationState"], "pending")
        self.assertEqual(result["rootScrollProgress"], previous["rootScrollProgress"])

    def test_store_capture_persists_actual_empty_end_but_waits_for_review(self):
        p = project.Project(self.root / "empty-project", create=True)
        try:
            source = self.root / "source.json"; source.write_text(json.dumps({"url": URL}))
            manifest = self.root / "empty-manifest.json"
            manifest.write_text(json.dumps({"units": [{"stage": "roots", "target": NOTE,
                "sources": [str(source)], "next": "read"}]}))
            tid = native.import_manifest(p, manifest)["task_ids"][0]
            first = bridge.store_capture(p, p.task(tid), {"url": URL, "results": [],
                "deepCoverage": {"rootPaginationState": "pending", "threads": [], "observedRootIds": [],
                    "rootScrollProgress": self.stalled_empty_history()["rootScrollProgress"]}})
            bridge.store_capture(p, p.task(tid), {"url": URL, "results": [],
                "deepCoverage": self.new_empty_after_stall()["coverage"]})
            task = p.task(tid); state = task["payload"]["bridge"]
            self.assertTrue(state["collection_end"])
            self.assertEqual(state["comments"]["rootPaginationState"], "complete")
            self.assertEqual(state["comments"]["rootScrollProgress"]["stalledRounds"], 0)
            self.assertIn(first["snapshot"], state["comments"]["snapshots"])
            self.assertTrue(state["awaiting_review"])
            self.assertEqual(task["status"], "pending")
        finally:
            p.close()

    def test_store_capture_reopens_collection_end_without_changing_native_history(self):
        p = project.Project(self.root / "project", create=True)
        try:
            source = self.root / "observed.json"; source.write_text(json.dumps({"url": URL}))
            manifest = self.root / "manifest.json"
            manifest.write_text(json.dumps({"units": [{"stage": "roots", "target": NOTE,
                "sources": [str(source)], "next": "read"}]}))
            tid = native.import_manifest(p, manifest)["task_ids"][0]
            old = bridge.store_capture(p, p.task(tid), {"url": URL, "results": [],
                "deepCoverage": self.complete()})
            before = p.task(tid)["payload"]["bridge"]
            self.assertTrue(before["collection_end"])
            events = [r[0] for r in p.db.execute("SELECT id FROM native_events WHERE task_id=?", (tid,))]
            snap, _ = self.pending()
            bridge.store_capture(p, p.task(tid), {"url": URL, "results": [],
                "deepCoverage": snap["coverage"]})
            after = p.task(tid)["payload"]["bridge"]
            self.assertFalse(after["collection_end"])
            self.assertEqual(after["comments"]["rootPaginationState"], "pending")
            self.assertIn(old["snapshot"], after["comments"]["snapshots"])
            self.assertTrue(all(p.db.execute("SELECT 1 FROM native_events WHERE id=?", (event,)).fetchone()
                                for event in events))
            self.assertEqual(p.task(tid)["status"], "pending")
        finally:
            p.close()


if __name__ == "__main__":
    unittest.main()
