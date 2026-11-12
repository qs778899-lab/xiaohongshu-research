#!/usr/bin/env python3
"""Offline diagnostics tests: no real browser, credentials, account or network."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_flow as flow
import xhs_project as xp


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.p = xp.Project(self.project, create=True)
        self.addCleanup(self.p.close)
        self.task = self.p.add_task("search", "synthetic-general", {"query": "synthetic", "sort": "general"})
        self.p.db.commit()
        self.run = self.root / "run"
        self.run.mkdir()
        self.write(self.run / "run.json", {"format": "xiaohongshu-research-v1", "directory": str(self.run), "closed": False})
        self.write(self.run / "account.json", {"key": "a" * 64, "credential_free": True})
        self.write(self.run / ".session/cookies.json", {"web_session": "SYNTHETIC_SECRET_NEVER_READ", "a1": "FAKE"})
        self.guard_root = self.root / "guards"
        self.guard = self.guard_root / ("a" * 64)
        self.guard.mkdir(parents=True, mode=0o700)
        self.guard_root.chmod(0o700)
        self.addCleanup(patch.stopall)
        patch.object(flow, "account_guard_root", return_value=self.guard_root).start()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def check(self, run=True, dependency=None):
        return flow.check(self.project, self.run if run else None,
                          probe_dependencies=dependency or (lambda browser: {"missing": [], "versions": {}}))

    def freeze(self):
        return {str(p.relative_to(self.root)): (p.stat().st_mode, p.stat().st_mtime_ns,
                    hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                for p in self.root.rglob("*")}

    def mark(self, kind="search", status="pending", review="not_required", reason=""):
        self.p.db.execute("UPDATE tasks SET kind=?,status=?,review_status=?,reason=? WHERE id=?", (kind, status, review, reason, self.task))
        self.p.db.commit()

    def test_active_is_locally_reusable_without_reading_cookies_or_network(self):
        original = Path.read_text
        def guarded(path, *args, **kwargs):
            if path.name == "cookies.json":
                raise AssertionError("credential read")
            return original(path, *args, **kwargs)
        before = self.freeze()
        with patch.object(Path, "read_text", guarded), patch("socket.socket", side_effect=AssertionError("network")):
            result = self.check()
        self.assertEqual(result["action"], "continue_serial_collection")
        self.assertEqual(result["session"]["state"], "locally_reusable")
        self.assertFalse(result["session"]["online_login_verified"])
        self.assertFalse(result["needs_user_action"])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
        self.assertEqual(before, self.freeze())

    def test_cleaned_session_uses_existing_authorization_not_human_relogin(self):
        self.write(self.run / "cleanup.json", {"temporary_browser_removed": True, "temporary_credentials_removed": True})
        (self.run / ".session/cookies.json").unlink()
        result = self.check()
        self.assertEqual(result["session"]["state"], "cleaned")
        self.assertEqual((result["action"], result["actor"]), ("prepare_authorized_session", "agent"))

    def test_closed_session_is_not_reopened_by_check(self):
        self.write(self.run / "run.json", {"format": "xiaohongshu-research-v1", "directory": str(self.run), "closed": True})
        before = self.freeze(); result = self.check()
        self.assertEqual(result["action"], "prepare_authorized_session")
        self.assertEqual(before, self.freeze())

    def test_missing_login_copy_is_agent_preparation_until_expiry_confirmed(self):
        (self.run / ".session/cookies.json").unlink()
        result = self.check()
        self.assertEqual((result["action"], result["actor"]), ("prepare_authorized_login_copy", "agent"))

    def test_account_stop_precedes_cleaned_run_and_dependencies(self):
        self.write(self.guard / "STOP.json", {"reason": "HTTP access/login/verification/rate restriction: 461", "secret": "PRIVATE_URL_TOKEN"})
        self.write(self.run / "cleanup.json", {"temporary_browser_removed": True, "temporary_credentials_removed": True})
        before = self.freeze()
        result = self.check(dependency=lambda b: (_ for _ in ()).throw(AssertionError("dependency probe unnecessary")))
        self.assertEqual((result["action"], result["reason"], result["actor"]), ("resolve_access_stop", "challenge_required", "user"))
        self.assertNotIn("PRIVATE_URL_TOKEN", json.dumps(result))
        self.assertEqual(before, self.freeze())

    def test_project_stop_survives_invalid_run_metadata(self):
        self.p.set_meta("stop", {"reason": "SessionExpiredError:300017"}); self.p.db.commit()
        self.write(self.run / "run.json", {"wrong": "PRIVATE_PROFILE_NAME"})
        result = self.check()
        self.assertEqual(result["reason"], "login_required")
        self.assertEqual(result["actor"], "user")

    def test_run_stop_survives_missing_manifest(self):
        (self.run / "run.json").unlink()
        self.write(self.run / "STOP.json", {"reason": "NeedVerifyError"})
        result = self.check()
        self.assertEqual((result["action"], result["reason"]), ("resolve_access_stop", "challenge_required"))

    def test_project_stop_survives_invalid_batch_state(self):
        self.p.set_meta("stop", {"reason": "SessionExpiredError"}); self.p.db.commit()
        self.write(self.project / "batch/state.json", ["invalid"])
        self.assertEqual(self.check()["reason"], "login_required")

    def test_pause_marker_is_not_read_or_removed(self):
        pause = self.project / "batch/PAUSE"
        self.write(pause, {"secret": "PRIVATE_PAUSE_CONTENT"})
        before = self.freeze()
        original = Path.read_text
        def guarded(path, *args, **kwargs):
            if path.name == "PAUSE":
                raise AssertionError("pause contents read")
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", guarded):
            result = self.check()
        self.assertEqual((result["action"], result["actor"]), ("preserve_pause", "agent"))
        self.assertFalse(result["needs_user_action"])
        self.assertNotIn("PRIVATE_PAUSE_CONTENT", json.dumps(result))
        self.assertEqual(before, self.freeze())

    def test_access_stop_precedes_pause(self):
        self.write(self.project / "batch/PAUSE", {})
        self.write(self.run / "STOP.json", {"reason": "NeedVerifyError"})
        self.assertEqual(self.check()["action"], "resolve_access_stop")

    def test_rate_or_unknown_stop_does_not_ask_for_login(self):
        for reason in ["HTTP access/login/verification/rate restriction: 429", "ACCESS_STATE_UNCONFIRMED", "some private token/name"]:
            self.write(self.run / "STOP.json", {"reason": reason})
            result = self.check()
            self.assertEqual(result["action"], "resolve_access_stop")
            self.assertEqual(result["actor"], "agent")
            self.assertNotIn("some private token/name", json.dumps(result))

    def test_handoff_goes_to_agent_even_if_run_cleaned(self):
        self.mark(kind="note", status="complete", review="pending")
        self.write(self.run / "cleanup.json", {"temporary_browser_removed": True, "temporary_credentials_removed": True})
        result = self.check()
        self.assertEqual((result["action"], result["actor"]), ("review_collected_material", "agent"))
        self.assertEqual(result["session"]["state"], "cleaned")

    def test_media_and_title_handoffs_never_become_user_approval(self):
        for kind in ("media", "note"):
            self.mark(kind=kind, status="pending", review="pending")
            result = self.check(run=False)
            self.assertEqual(result["actor"], "agent")
            self.assertEqual(result["reason"], "analyst_handoff_not_user_approval")

    def test_plain_legacy_timeout_is_diagnosis_not_automatic_retry(self):
        self.mark(status="blocked", reason="Transport failed: ReadTimeout")
        result = self.check()
        self.assertEqual((result["action"], result["actor"]), ("inspect_transport_checkpoint", "agent"))
        self.assertEqual(result["timeout_task_ids"], [self.task])
        self.assertEqual(self.p.task(self.task)["status"], "blocked")

    def test_missing_dependency_is_local_repair_without_login(self):
        result = self.check(dependency=lambda b: {"missing": ["xiaohongshu_cli_in_current_interpreter"], "versions": {}})
        self.assertEqual((result["action"], result["actor"]), ("repair_local_environment", "agent"))

    def test_dependency_probe_checks_paths_and_redacts_arbitrary_versions(self):
        package = self.root / "package"
        self.write(package / "package.json", {"name": "@jackwener/opencli", "version": "PRIVATE_URL_TOKEN"})
        with patch.object(flow.importlib.metadata, "version", return_value="PRIVATE_PROFILE"):
            result = flow.dependency_facts({"node": str(self.root / "missing-node"), "opencli_root": str(package)})
        self.assertEqual(result["missing"], ["recorded_node_executable", "recorded_opencli_layout"])
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_malformed_task_payload_is_a_local_agent_diagnosis(self):
        self.p.db.execute("UPDATE tasks SET payload='[]' WHERE id=?", (self.task,)); self.p.db.commit()
        self.assertEqual((self.check()["action"], self.check()["actor"]), ("inspect_local_metadata", "agent"))

    def test_original_browser_metadata_and_one_time_permission_are_not_reused(self):
        self.write(self.run / "browser.json", {"node": "PRIVATE_NODE_PATH", "opencli_root": "PRIVATE_ROOT", "profile_name": "PRIVATE_PROFILE", "target_id": "abc"})
        result = self.check()
        self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertEqual(result["authorization"]["broad_browser_state_permission"], "one_time_permission_is_not_reusable")

    def test_existing_run_lock_is_not_deleted_or_replaced(self):
        (self.run / ".collection.lock").write_text("12345")
        before = self.freeze(); result = self.check()
        self.assertEqual(result["action"], "inspect_existing_runner")
        self.assertEqual(before, self.freeze())

    def test_browser_missing_or_unbound_requires_only_owned_target_preparation(self):
        self.mark(kind="browser")
        self.assertEqual(self.check()["action"], "start_owned_browser")
        self.write(self.run / "browser.json", {"pid": 12345})
        self.assertEqual(self.check()["action"], "inspect_owned_target")

    def test_missing_guard_is_not_created(self):
        self.guard.rmdir(); self.guard_root.rmdir()
        self.check()
        self.assertFalse(self.guard_root.exists())

    def test_missing_project_still_cannot_hide_run_stop(self):
        self.write(self.run / "STOP.json", {"reason": "SessionExpiredError"})
        result = flow.check(self.root / "missing_project", self.run)
        self.assertEqual(result["action"], "resolve_access_stop")
        self.assertFalse((self.root / "missing_project").exists())

    def test_malformed_or_symlinked_metadata_never_echoes_content(self):
        (self.run / "run.json").write_text("SECRET_RAW_INVALID_JSON")
        result = self.check(); self.assertEqual(result["action"], "inspect_local_metadata")
        self.assertNotIn("SECRET_RAW", json.dumps(result))
        target = self.root / "private.json"; self.write(target, {"secret": "HIDDEN"})
        (self.run / "run.json").unlink(); (self.run / "run.json").symlink_to(target)
        self.assertEqual(self.check()["reason"], "unsafe_metadata_path")


if __name__ == "__main__":
    unittest.main()
