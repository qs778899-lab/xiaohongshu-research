"""Offline tests: synthetic profiles, local fixture DBs, mocked transport only."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

import xhs_recovery as r
import xhs_research as legacy
from xhs_project import Project


def at(offset=0):
    return datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.patcher = patch.object(r.tempfile, "gettempdir", return_value=str(self.root))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.project = self.root / "project"
        self.run = self.project / "session-old"
        self.run.mkdir(parents=True)
        self.chrome = self.root / "fake-chrome"
        self.chrome.mkdir()
        legacy.write_json(self.chrome / "Local State", {"profile": {"info_cache": {"Profile 2": {"name": "profile_alpha"}}}})
        legacy.write_json(self.run / "run.json", {"format": legacy.FORMAT, "directory": str(self.run), "closed": True})
        self.guard = legacy.bind_account(self.run, self.chrome, "Profile 2")
        self.uid = "a" * 24
        self.account_uid = "e" * 24
        stop = {"at": at(-120), "reason": "HTTP access/login/verification/rate restriction: 461"}
        legacy.write_json(self.run / "STOP.json", stop)
        legacy.write_json(self.guard / "STOP.json", {**stop, "run": str(self.run)})
        p = Project(self.project, create=True)
        self.task = p.add_task("author", self.uid, {"user_id": self.uid})
        p.db.execute("UPDATE tasks SET status='blocked',reason='access_stop' WHERE id=?", (self.task,))
        p.set_meta("stop", {**stop, "run": str(self.run)})
        p.db.commit()
        p.close()
        self.evidence = self.root / "evidence.json"
        legacy.write_json(self.evidence, {
            "user_request": {"at": at(-60), "text": "进一步完善skill和进一步搜索"},
            "identity_baseline": {"account_user_id": self.account_uid, "source": "prior authorized UI observation"},
            "ui": {"observed_at": at(-30), "profile_name": "profile_alpha", "original_session": True,
                   "logged_in": True, "no_access_warning": True, "normal_content_visible": True,
                   "url": "https://www.xiaohongshu.com/explore", "observation": "我 links to the same public UID; normal public cards and no challenge",
                   "account_user_id": self.account_uid}})
        self.cookies = {"a1": "synthetic-secret", "web_session": "synthetic-session"}
        self.data = {"notes": [{"note_id": "b" * 24, "display_title": "synthetic orthodontic diary"}], "has_more": False, "cursor": ""}
        self.calls = []

    def prepare(self):
        return Path(r.prepare(self.project, self.run, self.evidence, "profile_alpha", self.chrome)["attempt"])

    def request(self, cookies, spec, guard):
        self.calls.append((dict(cookies), spec, guard))
        return self.data

    def probe(self, attempt):
        return r.probe(attempt, cookie_reader=lambda _: self.cookies, requester=self.request)

    def stop_bytes(self):
        with r._connect(self.project) as db:
            project_stop = r._project_meta(db, "stop")
        return (self.run / "STOP.json").read_bytes(), (self.guard / "STOP.json").read_bytes(), project_stop

    def test_status_and_plan_read_only(self):
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        r.inspect(self.project, self.run)
        result = r.plan(self.project, self.run)
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result["probe"]["params"]["user_id"], self.uid)
        self.assertFalse(result["network_performed"])

    def test_prepare_preserves_all_stops(self):
        before = self.stop_bytes()
        attempt = self.prepare()
        self.assertTrue((attempt / "prepared.json").exists())
        self.assertEqual(before, self.stop_bytes())
        self.assertFalse(self.calls)

    def test_success_probe_does_not_unlock_and_is_one_shot(self):
        before = self.stop_bytes()
        attempt = self.prepare()
        receipt = self.probe(attempt)
        self.assertEqual(receipt["status"], "verified_api_success")
        self.assertEqual(before, self.stop_bytes())
        self.assertEqual(self.cookies, {})
        with self.assertRaises(r.RecoveryError): self.probe(attempt)
        with self.assertRaises(r.RecoveryError): self.prepare()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("synthetic-secret", "".join(p.read_text() for p in attempt.iterdir() if p.is_file()))

    def test_failure_preserves_stops_and_cannot_reprepare(self):
        before = self.stop_bytes()
        attempt = self.prepare()
        def fail(*_):
            raise r.RecoveryError("Probe HTTP status: 461")
        with self.assertRaises(r.RecoveryError):
            r.probe(attempt, cookie_reader=lambda _: self.cookies, requester=fail)
        self.assertEqual(before, self.stop_bytes())
        self.assertEqual(self.cookies, {})
        self.assertTrue((attempt / "failure.json").exists())
        with self.assertRaises(r.RecoveryError): self.prepare()
        with self.assertRaises(r.RecoveryError): r.commit(attempt)

    def test_keyboard_interrupt_consumes_capability(self):
        attempt = self.prepare()
        def interrupted(*_): raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            r.probe(attempt, cookie_reader=lambda _: self.cookies, requester=interrupted)
        self.assertEqual(self.cookies, {})
        with self.assertRaises(r.RecoveryError): self.probe(attempt)

    def test_cookie_error_also_consumes_capability(self):
        attempt = self.prepare()
        def fail(_): raise RuntimeError("never echo a sensitive provider error")
        with self.assertRaises(RuntimeError): r.probe(attempt, cookie_reader=fail, requester=self.request)
        self.assertNotIn("sensitive provider", (attempt / "failure.json").read_text())
        self.assertFalse(self.calls)
        with self.assertRaises(r.RecoveryError): self.probe(attempt)

    def test_success_commit_ingests_exact_page_and_keeps_old_run(self):
        old_stop = (self.run / "STOP.json").read_bytes()
        old_run = (self.run / "run.json").read_bytes()
        attempt = self.prepare(); receipt = self.probe(attempt)
        result = r.commit(attempt)
        self.assertEqual(result["status"], "committed")
        self.assertFalse((self.guard / "STOP.json").exists())
        self.assertEqual(old_stop, (self.run / "STOP.json").read_bytes())
        self.assertEqual(old_run, (self.run / "run.json").read_bytes())
        with r._connect(self.project) as db:
            self.assertIsNone(r._project_meta(db, "stop"))
            task = db.execute("SELECT status,next_page FROM tasks WHERE id=?", (self.task,)).fetchone()
            self.assertEqual(task, ("complete", 2))
            self.assertTrue(db.execute("SELECT 1 FROM snapshots WHERE id=?", (receipt["response_sha256"],)).fetchone())
            self.assertTrue(r.resolved_run_stop(db, self.run))
        self.assertEqual(r.commit(attempt), result)

    def test_interrupted_commit_stays_account_blocked_then_finishes_without_probe(self):
        attempt = self.prepare(); self.probe(attempt)
        def crash(stage): raise RuntimeError(stage)
        with self.assertRaises(RuntimeError): r.commit(attempt, fault=crash)
        self.assertTrue((self.guard / "STOP.json").exists())
        with r._connect(self.project) as db: self.assertFalse(r.resolved_run_stop(db, self.run))
        r.commit(attempt)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.guard / "STOP.json").exists())

    def test_interrupted_commit_rechecks_media_stop(self):
        attempt = self.prepare(); self.probe(attempt)
        with self.assertRaises(RuntimeError):
            r.commit(attempt, fault=lambda _: (_ for _ in ()).throw(RuntimeError("crash")))
        legacy.write_json(self.project / "media" / "STOP.json", {"at": at(), "reason": "new media stop"})
        with self.assertRaises(r.RecoveryError): r.commit(attempt)
        self.assertTrue((self.guard / "STOP.json").exists())

    def test_interrupted_commit_rechecks_target_task(self):
        attempt = self.prepare(); self.probe(attempt)
        with self.assertRaises(RuntimeError):
            r.commit(attempt, fault=lambda _: (_ for _ in ()).throw(RuntimeError("crash")))
        with sqlite3.connect(self.project / "project.sqlite3") as db:
            db.execute("UPDATE tasks SET cursor='unexpected' WHERE id=?", (self.task,))
        with self.assertRaises(r.RecoveryError): r.commit(attempt)
        self.assertTrue((self.guard / "STOP.json").exists())

    def test_interrupted_commit_rechecks_snapshot(self):
        attempt = self.prepare(); receipt = self.probe(attempt)
        with self.assertRaises(RuntimeError):
            r.commit(attempt, fault=lambda _: (_ for _ in ()).throw(RuntimeError("crash")))
        (self.project / "snapshots" / (receipt["response_sha256"] + ".json")).write_text('{}')
        with self.assertRaises(r.RecoveryError): r.commit(attempt)
        self.assertTrue((self.guard / "STOP.json").exists())

    def test_finished_commit_allows_subsequent_task_changes(self):
        attempt = self.prepare(); self.probe(attempt)
        result = r.commit(attempt)
        with sqlite3.connect(self.project / "project.sqlite3") as db:
            db.execute("UPDATE tasks SET reason='later semantic annotation' WHERE id=?", (self.task,))
        self.assertEqual(r.commit(attempt), result)

    def test_new_stop_prevents_commit_and_historical_resolution(self):
        attempt = self.prepare(); self.probe(attempt)
        newer = {"at": at(), "reason": "a newer restriction", "run": str(self.run)}
        legacy.write_json(self.guard / "STOP.json", newer)
        with self.assertRaises(r.RecoveryError): r.commit(attempt)
        self.assertEqual(json.loads((self.guard / "STOP.json").read_text()), newer)

    def test_new_stop_after_commit_is_not_hidden(self):
        attempt = self.prepare(); self.probe(attempt); r.commit(attempt)
        legacy.write_json(self.guard / "STOP.json", {"at": at(), "reason": "new restriction", "run": str(self.run)})
        with r._connect(self.project) as db: self.assertFalse(r.resolved_run_stop(db, self.run))
        with self.assertRaises(r.RecoveryError): r.commit(attempt)

    def test_state_change_before_probe_sends_nothing(self):
        attempt = self.prepare()
        with sqlite3.connect(self.project / "project.sqlite3") as db:
            db.execute("UPDATE tasks SET cursor='changed' WHERE id=?", (self.task,))
        with self.assertRaises(r.RecoveryError): self.probe(attempt)
        self.assertFalse(self.calls)

    def test_response_tampering_prevents_commit(self):
        attempt = self.prepare(); self.probe(attempt)
        (attempt / "response.json").write_text('{}')
        with self.assertRaises(r.RecoveryError): r.commit(attempt)
        self.assertTrue((self.guard / "STOP.json").exists())

    def test_ui_challenge_or_changed_account_refused(self):
        evidence = json.loads(self.evidence.read_text())
        evidence["ui"]["no_access_warning"] = False
        legacy.write_json(self.evidence, evidence)
        with self.assertRaises(r.RecoveryError): self.prepare()
        evidence["ui"]["no_access_warning"] = True
        evidence["ui"]["account_user_id"] = "f" * 24
        legacy.write_json(self.evidence, evidence)
        with self.assertRaises(r.RecoveryError): self.prepare()

    def test_no_reusing_old_user_or_ui_confirmation(self):
        evidence = json.loads(self.evidence.read_text())
        evidence["user_request"]["at"] = at(-300)
        legacy.write_json(self.evidence, evidence)
        with self.assertRaises(r.RecoveryError): self.prepare()

    def test_same_name_different_profile_binding_refused(self):
        other = self.root / "other-chrome"
        other.mkdir()
        legacy.write_json(other / "Local State", {"profile": {"info_cache": {"Profile 2": {"name": "profile_alpha"}}}})
        with self.assertRaises(r.RecoveryError):
            r.prepare(self.project, self.run, self.evidence, "profile_alpha", other)

    def test_closed_run_required_and_other_routes_unsupported(self):
        manifest = json.loads((self.run / "run.json").read_text())
        manifest["closed"] = False
        legacy.write_json(self.run / "run.json", manifest)
        with self.assertRaises(r.RecoveryError): self.prepare()
        manifest["closed"] = True
        legacy.write_json(self.run / "run.json", manifest)
        with sqlite3.connect(self.project / "project.sqlite3") as db:
            db.execute("UPDATE tasks SET kind='search' WHERE id=?", (self.task,))
        with self.assertRaises(r.RecoveryError): self.prepare()

    def test_empty_terminal_valid_but_unknown_pagination_rejected(self):
        spec = r.plan(self.project, self.run)["probe"]
        r.validate_author_data({"has_more": False}, spec)
        for bad in ({"notes": []}, {"has_more": 0}, {"has_more": "false"}, {"has_more": True, "notes": []}, {"has_more": False, "notes": {}}, {"has_more": True, "notes": self.data["notes"], "cursor": ""}):
            with self.assertRaises(r.RecoveryError): r.validate_author_data(bad, spec)

    def test_exact_http_route_and_no_redirect_or_retry(self):
        try:
            import httpx
            import xhs_cli.client
        except ImportError:
            self.skipTest("Pinned optional dependency unavailable")
        spec = r.plan(self.project, self.run)["probe"]
        calls = []
        def send(_client, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return httpx.Response(461, json={"success": False}, request=httpx.Request(method, url))
        with patch.object(httpx.Client, "request", send), patch.object(xhs_cli.client, "sign_main_api", return_value={}):
            with self.assertRaises(r.RecoveryError): r.request_once(dict(self.cookies), spec, self.guard)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][2]["follow_redirects"])
        self.assertIn(self.uid, calls[0][1])
        self.assertEqual(calls[0][0], "GET")


if __name__ == "__main__":
    unittest.main(verbosity=2)
