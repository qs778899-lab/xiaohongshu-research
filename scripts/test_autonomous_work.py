#!/usr/bin/env python3
"""Offline end-to-end scheduling and typed retry tests; no browser or network."""
import json
import unittest
from unittest.mock import patch

import xhs_batch as batch
import xhs_project as xp
import test_batch as fixtures


class AutonomousWorkTests(unittest.TestCase):
    setUp = fixtures.RuntimeTests.setUp
    tearDown = fixtures.RuntimeTests.tearDown
    collect = fixtures.RuntimeTests.collect
    clock = fixtures.RuntimeTests.clock
    sleep = fixtures.RuntimeTests.sleep

    def author(self):
        p = xp.Project(self.project)
        tid = p.add_task("author", "test-author", {"user_id": "test-author"})
        p.db.commit(); p.close()
        return tid

    def work(self, **kwargs):
        return batch.work(self.project, self.run, clock=self.clock, sleep=self.sleep, **kwargs)

    def actual_resume(self, collect):
        # Test real Project.resume with only the network collection replaced.
        self.patches[-1].stop()
        p = patch.object(xp.Project, "collect_one", autospec=True, side_effect=collect)
        p.start(); self.patches.append(p)

    def test_ordinary_batches_continue_and_budget_is_not_completion(self):
        tid = self.author()
        result = self.work(units=2, max_batches=3, task_ids=[tid])
        self.assertEqual(len(self.calls), 6)
        self.assertEqual((result["reason"], result["batches"]), ("work_budget", 3))
        self.assertEqual(result["actor"], "codex")
        self.assertFalse(result["requires_user_input"])
        self.assertTrue(result["session_retained"])
        self.assertTrue(all(b[1] - a[1] >= 10 for a,b in zip(self.calls,self.calls[1:])))
        self.assertFalse((self.run / "cleanup.json").exists())
        self.assertEqual(batch.read_view(self.project)["tasks"][0]["status"], "pending")

    def test_explicit_search_is_bounded_before_codex_card_review(self):
        result = self.work(units=50, max_batches=10, task_ids=[self.ids[0]], search_pages=3)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result["reason"], "search_card_review_checkpoint")
        self.assertEqual(result["actor"], "codex")

    def test_pause_survives_and_never_calls_collection(self):
        (self.project / "batch").mkdir()
        pause = self.project / "batch" / "PAUSE"; pause.write_text("user paused")
        result = self.work()
        self.assertEqual((result["reason"], self.calls), ("requested", []))
        self.assertTrue(pause.exists())

    def test_stop_writes_handoff_but_raises_and_never_clears_guard(self):
        batch.legacy.write_json(self.guard / "STOP.json", {"reason": "461"})
        with self.assertRaises(batch.legacy.StopRun): self.work()
        self.assertEqual(self.calls, [])
        self.assertEqual(json.loads((self.project / "batch" / "handoff.json").read_text())["event"], "stop")
        self.assertTrue((self.guard / "STOP.json").exists())

    def test_only_typed_timeout_retries_same_page_after_cooldown(self):
        tid = self.author(); attempts = []
        def collect(p, task_id, run):
            t = p.task(task_id)
            attempts.append((str(run), t["id"], t["cursor"], t["next_page"], self.now))
            if len(attempts) == 1:
                raise batch.legacy.TransientTransportError("ReadTimeout", "GET", "/api/sns/web/v1/user_posted")
            p.db.execute("UPDATE tasks SET status='complete' WHERE id=?", (task_id,)); p.db.commit()
            return {"task": p.task(task_id)}
        self.actual_resume(collect)
        result = self.work(units=1, task_ids=[tid])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0][:4], attempts[1][:4])
        self.assertGreaterEqual(attempts[1][4] - attempts[0][4], 15)
        state = json.loads((self.project / "batch" / "state.json").read_text())
        self.assertEqual(len(state["transport_retries"]), 1)
        self.assertFalse(result["requires_user_input"])

    def test_second_timeout_and_reinvocation_do_not_replenish_retry(self):
        tid = self.author(); attempts = []
        def collect(p, task_id, run):
            attempts.append(task_id)
            raise batch.legacy.TransientTransportError("ConnectTimeout", "GET", "/api/sns/web/v1/user_posted")
        self.actual_resume(collect)
        self.assertEqual(self.work(task_ids=[tid])["reason"], "task_gap_requires_review")
        self.assertEqual(len(attempts), 2)
        self.work(task_ids=[tid])
        self.assertEqual(len(attempts), 2)
        # Even an explicit analyst retry does not renew the automatic allowance.
        p = xp.Project(self.project); p.review(tid, "retry", "Offline test after diagnosis"); p.close()
        self.work(task_ids=[tid])
        self.assertEqual(len(attempts), 3)

    def test_old_string_deadline_and_optional_prewarm_are_not_auto_retry(self):
        cases = [batch.legacy.ResearchError("Transport failed: ReadTimeout"),
                 batch.legacy.TransportDeadlineExceeded("deadline"),
                 batch.legacy.TransientTransportError("ReadTimeout", "POST", "/api/sns/web/v1/search/onebox")]
        self.actual_resume(lambda *a: None)
        for index, error in enumerate(cases):
            tid = self.ids[index]
            with patch.object(xp.Project, "collect_one", side_effect=error) as collect:
                self.work(task_ids=[tid])
                self.assertEqual(collect.call_count, 1)
        state = json.loads((self.project / "batch" / "state.json").read_text())
        self.assertFalse(state.get("transport_retries"))

    def test_stop_arriving_during_cooldown_prevents_retry(self):
        tid = self.author(); calls = []
        def collect(*args):
            calls.append(True)
            raise batch.legacy.TransientTransportError("ReadTimeout", "GET", "/api/sns/web/v1/user_posted")
        self.actual_resume(collect)
        def sleep(duration):
            self.now += duration
            batch.legacy.write_json(self.guard / "STOP.json", {"reason": "verification"})
        with self.assertRaises(batch.legacy.StopRun):
            batch.work(self.project, self.run, task_ids=[tid], clock=self.clock, sleep=sleep)
        self.assertEqual(len(calls), 1)

    def test_response_observed_or_request_changed_cannot_retry(self):
        tid = self.author()
        self.actual_resume(lambda *args: (_ for _ in ()).throw(
            batch.legacy.TransientTransportError("ReadTimeout", "GET", "/api/sns/web/v1/user_posted")))
        p = xp.Project(self.project)
        result = p.resume(self.run, task_id=tid); chosen = p.task(tid)
        root = self.project / "batch"; root.mkdir()
        result["results"][-1]["transient_transport"]["response_received"] = True
        self.assertFalse(batch.schedule_transport_retry(self.project, self.run, root, {}, result, chosen, self.clock))
        result["results"][-1]["transient_transport"]["response_received"] = False
        p.db.execute("UPDATE tasks SET cursor='changed' WHERE id=?", (tid,)); p.db.commit(); p.close()
        self.assertFalse(batch.schedule_transport_retry(self.project, self.run, root, {}, result, chosen, self.clock))

    def test_interrupt_leaves_inflight_and_handoff_for_codex(self):
        with patch.object(xp.Project, "resume", side_effect=KeyboardInterrupt):
            result = self.work()
        self.assertEqual(result["reason"], "interrupted")
        self.assertTrue(json.loads((self.project / "batch" / "state.json").read_text())["in_flight"])
        self.assertFalse(result["requires_user_input"])
        self.assertTrue((self.project / "batch" / "PAUSE").exists())
        self.assertEqual(self.work()["reason"], "requested")
        self.assertEqual(self.calls, [])

    def test_legacy_run_also_preserves_default_pause(self):
        (self.project / "batch").mkdir()
        pause = self.project / "batch" / "PAUSE"; pause.write_text("user paused")
        result = batch.run_batch(self.project, self.run, clock=self.clock, sleep=self.sleep)
        self.assertEqual((result["reason"], self.calls), ("requested", []))
        self.assertEqual(pause.read_text(), "user paused")


if __name__ == "__main__": unittest.main()
