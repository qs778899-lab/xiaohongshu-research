#!/usr/bin/env python3
"""Offline scheduling tests; no credentials, browser, or network calls."""
from contextlib import contextmanager
import json
import hashlib
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

import xhs_batch as batch
import xhs_project as xp


def task(tid, kind, status="pending", reviewed="pending", priority=30, turn=0, payload=None):
    if kind == "search": payload = {"search_method_version": xp.xhs_search.SEARCH_METHOD, **(payload or {})}
    return {"id": tid, "kind": kind, "entity_key": tid, "payload": payload or {}, "status": status,
            "review_status": reviewed, "priority": priority, "turn": turn, "updated": "2026-01-01", "next_page": 1, "reason": ""}


def view(*tasks, stop=None, decisions=None, text_read=()):
    return {"tasks": list(tasks), "stop": stop, "decisions": decisions or {},
            "text_review_receipts": {tid: {"review_id": 1} for tid in text_read}}


class ChoiceTests(unittest.TestCase):
    def test_stop_precedes_explicit_priority(self):
        result = batch.choose(view(task("a", "author"), stop={"reason": "461"}), task_ids=["a"])
        self.assertEqual(result["action"], "stop")

    def test_accepted_body_precedes_search(self):
        result = batch.choose(view(task("n", "note", payload={"title_scope_review": {"reason": "checked"}}), task("s", "search")))
        self.assertEqual(result["task"]["id"], "n")

    def test_unscreened_search_card_is_handoff_not_body_or_more_search(self):
        result = batch.choose(view(task("n", "note", payload={"needs_triage": False}), task("s", "search")))
        self.assertEqual((result["action"], result["counts"]["triage"]), ("handoff", 1))

    def test_scope_include_without_full_review_allows_followup(self):
        included = task("n", "note", payload={"scope_included": True})
        result = batch.choose(view(included, task("s", "search")))
        self.assertEqual(result["task"]["id"], "n")
        reviews = [task(str(i), "note", "complete") for i in range(25)]
        result = batch.choose(view(*reviews, task("c", "comments")), task_ids=["c"])
        self.assertEqual(result["task"]["id"], "c")
        self.assertTrue(all(t["review_status"] == "pending" for t in reviews))

    def test_author_unscreened_notes_are_never_automated_even_explicit(self):
        result = batch.choose(view(task("n", "note", payload={"needs_triage": True})), task_ids=["n"])
        self.assertEqual(result["action"], "handoff")

    def test_explicit_selection_does_not_get_starved_by_unrelated_media(self):
        result = batch.choose(view(task("m", "media"), task("a", "author")), task_ids=["a"])
        self.assertEqual(result["task"]["id"], "a")

    def test_explicit_skips_complete_excluded_and_blocked(self):
        result = batch.choose(view(task("x", "author", "complete"), task("y", "author", "blocked"),
                                    task("z", "note", "excluded"), task("a", "author")), task_ids=["x", "y", "z", "a"])
        self.assertEqual(result["task"]["id"], "a")

    def test_unknown_explicit_id_is_error(self):
        with self.assertRaises(batch.legacy.ResearchError): batch.choose(view(), task_ids=["unknown"])

    def test_text_review_backpressure(self):
        tasks = [task(str(i), "note", "complete") for i in range(20)]
        result = batch.choose(view(*tasks, task("a", "author")))
        self.assertEqual(result["reason"], "acquired_text_review_checkpoint")

    def test_media_dependent_bodies_do_not_block_enqueued_followups(self):
        notes = [task("n"+str(i), "note", "complete") for i in range(20)]
        media = [task("m"+str(i), "media", payload={"note_id": "n"+str(i)}) for i in range(20)]
        candidate = task("new", "note", payload={"scope_included": True})
        result = batch.choose(view(*notes, *media, candidate, task("s", "search"), task("a", "author"), text_read=[t["id"] for t in notes]))
        self.assertEqual((result["task"]["id"], result["selection"]), ("a", "followup_while_media_pending"))
        self.assertTrue(all(t["review_status"] == "pending" for t in notes))
        result = batch.choose(view(*notes, *media, candidate, task("s", "search"), text_read=[t["id"] for t in notes]))
        self.assertEqual(result["action"], "handoff", "new bodies/search remain blocked by unread bodies")

    def test_unread_comments_still_force_review_despite_media_bodies(self):
        notes = [task("n"+str(i), "note", "complete") for i in range(20)]
        media = [task("m"+str(i), "media", payload={"note_id": "n"+str(i)}) for i in range(20)]
        replies = [task("c"+str(i), "comments", "complete") for i in range(20)]
        result = batch.choose(view(*notes, *media, *replies, task("a", "author"), text_read=[t["id"] for t in notes]))
        self.assertEqual(result["action"], "handoff")
        self.assertEqual({t["kind"] for t in result["tasks"]}, {"comments"})

    def test_media_dependent_followup_keeps_root_dependency_and_stop(self):
        notes = [task("n"+str(i), "note", "complete") for i in range(20)]
        media = [task("m"+str(i), "media", payload={"note_id": "n"+str(i)}) for i in range(20)]
        b, c = task("b", "browser"), task("c", "comments")
        b["entity_key"] = c["entity_key"] = "same"
        result = batch.choose(view(*notes, *media, b, c, text_read=[t["id"] for t in notes]))
        self.assertEqual(result["task"]["id"], "c")
        c["status"] = "blocked"
        self.assertEqual(batch.choose(view(*notes, *media, b, c, text_read=[t["id"] for t in notes]))["action"], "handoff")
        self.assertEqual(batch.choose(view(*notes, *media, b, c, stop={"reason":"461"}))["action"], "stop")

    def test_images_do_not_imply_body_text_was_read(self):
        notes = [task("n"+str(i), "note", "complete") for i in range(20)]
        media = [task("m"+str(i), "media", payload={"note_id": "n"+str(i)}) for i in range(20)]
        result = batch.choose(view(*notes, *media, task("a", "author")))
        self.assertEqual(result["action"], "handoff")

    def test_media_is_not_generic_collection_and_never_scope_complete(self):
        result = batch.choose(view(task("m", "media")))
        self.assertEqual(result["action"], "handoff")

    def test_browser_waits_for_root_enumeration(self):
        b, c = task("b", "browser", priority=5), task("c", "comments", priority=15)
        b["entity_key"] = c["entity_key"] = "same_note"
        result = batch.choose(view(b, c))
        self.assertEqual(result["task"]["id"], "c")

    def test_explicit_browser_enumerates_pending_roots_first(self):
        b, c = task("b", "browser"), task("c", "comments")
        b["entity_key"] = c["entity_key"] = "same_note"
        result = batch.choose(view(b, c), task_ids=["b"])
        self.assertEqual((result["task"]["id"], result["selection"]), ("c", "explicit_root_dependency"))
        c["status"] = "blocked"
        self.assertEqual(batch.choose(view(b, c), task_ids=["b"])["action"], "handoff")
        c["status"] = "complete"
        self.assertEqual(batch.choose(view(b, c), task_ids=["b"])["task"]["id"], "b")

    def test_search_adjacent_pages_survive_checkpoint_then_handoff(self):
        s, n = task("s", "search", turn=5), task("n", "note")
        state = {"search_task": "s", "search_units": 2}
        self.assertEqual(batch.choose(view(s, n), state)["task"]["id"], "s")
        state["search_units"] = 5
        self.assertEqual(batch.choose(view(s, n), state)["action"], "handoff")

    def test_next_group_rotates_without_fabricating_completion(self):
        s, old = task("s", "search", turn=5), task("old", "search", turn=0)
        result = batch.choose(view(s, old), {"search_task": "s", "search_units": 5})
        self.assertEqual(result["task"]["id"], "old")
        self.assertEqual(s["status"], "pending")

    def test_defined_scope_requires_no_gaps(self):
        self.assertEqual(batch.choose(view(task("s", "search", "blocked")))["action"], "handoff")
        self.assertEqual(batch.choose(view(task("s", "search", "complete", "not_required")))["action"], "scope_processed")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.project = self.base / "project"
        p = xp.Project(self.project, create=True)
        p.plan({"target": "test", "queries": ["test"]})
        self.ids = [r[0] for r in p.db.execute("SELECT id FROM tasks ORDER BY id")]
        p.close()
        self.run = self.base / "run"; self.run.mkdir()
        batch.legacy.write_json(self.run / "run.json", {"format": batch.legacy.FORMAT, "directory": str(self.run), "closed": False})
        self.guard = self.base / "guard"; self.guard.mkdir()
        self.now = 1000.0
        self.calls = []
        self.original_resume = xp.Project.resume
        self.patches = [patch.object(batch.legacy, "account_dir", return_value=self.guard),
                        patch.object(xp.Project, "resume", autospec=True, side_effect=self.collect)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def collect(self, project, run, steps, task_id):
        self.calls.append((task_id, self.now))
        old = project.task(task_id)
        project.db.execute("UPDATE tasks SET next_page=next_page+1,turn=turn+1 WHERE id=?", (task_id,)); project.db.commit()
        return {"processed_units": 1, "results": [{"task": project.task(task_id)}], "remaining": project.status()["outstanding_tasks"]}

    def clock(self): return self.now
    def sleep(self, duration): self.now += duration

    def run_batch(self, **kw):
        return batch.run_batch(self.project, self.run, clock=self.clock, sleep=self.sleep, **kw)

    def test_unit_budget_is_checkpoint_and_next_invocation_stays_same_query(self):
        self.assertEqual(self.run_batch(units=2)["reason"], "unit_budget")
        self.assertEqual(self.run_batch(units=2)["reason"], "unit_budget")
        self.assertEqual(len(set(tid for tid, _ in self.calls)), 1)
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(b[1] - a[1] >= 10 for a,b in zip(self.calls, self.calls[1:])))
        self.assertEqual(sum(t["status"] == "pending" for t in batch.read_view(self.project)["tasks"]), 3)

    def test_pacing_persists_across_runs_and_projects(self):
        self.run_batch(units=1)
        run2 = self.base / "run2"; run2.mkdir()
        batch.legacy.write_json(run2 / "run.json", {"format": batch.legacy.FORMAT, "directory": str(run2), "closed": False})
        project2 = self.base / "project2"
        p = xp.Project(project2, create=True); p.plan({"target": "test", "queries": ["test2"]}); p.close()
        batch.run_batch(project2, run2, units=1, clock=self.clock, sleep=self.sleep)
        self.assertGreaterEqual(self.calls[1][1] - self.calls[0][1], 10)

    def test_project_stop_is_unchanged_no_resume(self):
        p = xp.Project(self.project); p.set_meta("stop", {"reason": "461"}); p.db.commit(); p.close()
        before = (self.project / "project.sqlite3").read_bytes()
        result = self.run_batch(units=1)
        self.assertEqual(result["event"], "stop"); self.assertEqual(self.calls, [])
        self.assertEqual((self.project / "project.sqlite3").read_bytes(), before)

    def test_shared_account_stop_is_not_bypassed(self):
        batch.legacy.write_json(self.guard / "STOP.json", {"reason": "461"})
        with self.assertRaises(batch.legacy.StopRun): self.run_batch(units=1)
        self.assertEqual(self.calls, []); self.assertTrue((self.guard / "STOP.json").exists())

    def test_interrupt_preserves_reservation_and_inflight(self):
        with patch.object(xp.Project, "resume", side_effect=KeyboardInterrupt):
            result = self.run_batch(units=1)
        self.assertEqual(result["reason"], "interrupted")
        state = batch.legacy.read_json(self.project / "batch/state.json")
        self.assertIn("in_flight", state)
        self.assertEqual(batch.legacy.read_json(self.guard / "batch-pacing.json")["next_allowed_at"], 1010)
        self.assertFalse((self.run / ".collection.lock").exists())
        self.assertEqual(self.run_batch(units=1)["reason"], "requested")
        self.assertEqual(self.calls, [])
        # Simulate an explicit user continuation after inspecting the checkpoint.
        (self.project / "batch" / "PAUSE").unlink()
        self.run_batch(units=1)
        self.assertGreaterEqual(self.calls[0][1], 1010)

    def test_time_budget_prevents_waiting_from_triggering_extra_request(self):
        self.run_batch(units=1)
        result = self.run_batch(units=1, max_seconds=5)
        self.assertEqual(result["reason"], "time_budget"); self.assertEqual(len(self.calls), 1)

    def test_requested_checkpoint_issues_no_request(self):
        marker = self.base / "PAUSE"; marker.touch()
        result = self.run_batch(checkpoint_file=marker)
        self.assertEqual(result["reason"], "requested"); self.assertEqual(self.calls, [])

    def test_ordinary_blocked_unit_does_not_auto_retry(self):
        failed = []
        def fail(project, run, steps, task_id):
            failed.append(task_id)
            project.db.execute("UPDATE tasks SET status='blocked',reason='cursor_stall' WHERE id=?", (task_id,)); project.db.commit()
            return {"processed_units": 1, "results": [{"task_id": task_id, "blocked": "cursor_stall"}], "remaining": 3}
        with patch.object(xp.Project, "resume", new=fail):
            result = self.run_batch(units=10)
        self.assertEqual(result["reason"], "task_gap_requires_review"); self.assertEqual(len(failed), 1)

    def test_zero_progress_pending_task_does_not_spin(self):
        with patch.object(xp.Project, "resume", return_value={"processed_units": 0, "results": [], "remaining": 3}) as spy:
            result = self.run_batch(units=10)
        self.assertEqual(result["reason"], "no_progress_requires_review"); self.assertEqual(spy.call_count, 1)

    def test_lock_excludes_second_runner(self):
        with batch.batch_lock(self.project / "batch"):
            with self.assertRaises(batch.legacy.ResearchError): self.run_batch(units=1)
        self.assertEqual(self.calls, [])

    def test_real_resume_nested_account_lock_is_reentrant(self):
        calls = []
        class FakeAPI:
            def search_notes(self, query, page, sort):
                calls.append((query, page, sort))
                # This lock test uses a synthetic client. Importing another test
                # module needlessly required the optional live HTTP dependency.
                from xhs_search import request_receipt, adapt_request, SEARCH_METHOD
                self.last_search_request = request_receipt(adapt_request({
                    'keyword':query,'page':page,'sort':sort,'search_id':'synthetic-search-id',
                    'filters':[{'type':'sort_type','tags':['general']}]},SEARCH_METHOD),SEARCH_METHOD,'0.6.4')
                return {"has_more": False}
        @contextmanager
        def fake_client(run):
            with batch.legacy.account_lock(run):
                yield FakeAPI()
        with patch.object(xp.Project, "resume", new=self.original_resume), patch.object(batch.legacy, "client", side_effect=fake_client):
            result = self.run_batch(units=1)
        self.assertEqual(result["reason"], "unit_budget")
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(t["status"] == "complete" for t in batch.read_view(self.project)["tasks"]), 1)
        self.assertEqual(batch.legacy._ACCOUNT_LOCKS, {})

    def test_fresh_process_respects_held_account_and_project_batch_locks(self):
        code = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import xhs_batch as b
b.legacy.account_dir = lambda run: Path(sys.argv[3])
try:
    with (b.legacy.account_lock(Path(sys.argv[2])) if sys.argv[4]=='account' else b.batch_lock(Path(sys.argv[5]))):
        print('acquired')
except b.legacy.ResearchError:
    print('locked')
"""
        def child(which):
            return subprocess.run([sys.executable, "-c", code, str(Path(batch.__file__).parent), str(self.run),
                str(self.guard), which, str(self.project / "batch")], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        with batch.legacy.account_lock(self.run):
            self.assertEqual(child("account"), "locked")
        self.assertEqual(child("account"), "acquired")
        with batch.batch_lock(self.project / "batch"):
            self.assertEqual(child("batch"), "locked")
        self.assertEqual(child("batch"), "acquired")

    def test_text_receipt_is_tied_to_actual_document_and_current_text(self):
        p = xp.Project(self.project)
        nid, text = "a"*24, "已实读文字，图片仍待核"
        tid = p.add_task("note", nid, {"note_id": nid}, 8)
        p.doc("synthetic_snapshot", nid, "note", text, {}, "note", nid)
        p.db.execute("UPDATE tasks SET status='complete',review_status='pending' WHERE id=?", (tid,))
        marker = {"text_review": {"document_id": "note:"+nid, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}}
        p.review(tid, "include", "Only text was read; images unresolved", marker)
        self.assertIn(tid, batch.read_view(self.project)["text_review_receipts"])
        self.assertEqual(p.task(tid)["review_status"], "pending")
        p.db.execute("UPDATE documents SET text='新增未读文字' WHERE id=?", ("note:"+nid,)); p.db.commit()
        self.assertNotIn(tid, batch.read_view(self.project)["text_review_receipts"])
        wrong = {"text_review": {"document_id": "note:"+"b"*24, "text_sha256": hashlib.sha256('新增未读文字'.encode()).hexdigest()}}
        p.review(tid, "include", "Invalid source does not count", wrong)
        self.assertNotIn(tid, batch.read_view(self.project)["text_review_receipts"])
        p.close()

    def test_read_only_plan_does_not_write_project_or_batch_files(self):
        before = (self.project / "project.sqlite3").read_bytes()
        batch.choose(batch.read_view(self.project))
        self.assertEqual((self.project / "project.sqlite3").read_bytes(), before)
        self.assertFalse((self.project / "batch").exists())


if __name__ == "__main__": unittest.main()
