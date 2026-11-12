#!/usr/bin/env python3
"""Offline invariant tests. Synthetic data only; never access a real profile."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import xhs_research as r
import xhs_browser as b

N1, N2 = "a" * 24, "b" * 24


class OfflineResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run = self.root / "run"
        self.run.mkdir()
        r.write_json(self.run / "run.json", {"format": r.FORMAT, "directory": str(self.run), "closed": False})

    def record(self, name, **data):
        r.write_json(self.run / "raw" / (name + ".json"), data)

    def rendered_snapshot(self, suffix="first", **row_changes):
        row = {"commentId": "c" * 24, "parentCommentId": "d" * 24, "is_reply": True,
               "authorHrefRaw": "/user/profile/" + "e" * 24,
               "reply_to": "Target person", "text": "回复 Target person : 真实的旧正文"}
        row.update(row_changes)
        path = self.run / "raw" / ("browser-deep-" + N1 + "-" + suffix + ".json")
        r.write_json(path, {"operation": "browser_comments", "note_id": N1,
                            "ok": True, "data": [row]})
        return path

    def body_progress(self, *paths):
        return {"snapshots": list(map(str, paths)), "threads": {"id:" + "d" * 24: {
            "commentId": "d" * 24, "state": "visible_replies_read",
            "replyContentCaptured": True, "visibleReplies": 1}}}

    def test_rendered_body_migration_keeps_real_body_with_source_hash(self):
        source = self.rendered_snapshot()
        previous = self.body_progress(source)
        result = b.hydrate_reply_body_captures(self.run, N1, previous)
        receipt = result["threads"]["id:" + "d" * 24]["replyBodyCaptures"]["c" * 24]
        self.assertEqual(receipt["text"], "回复 Target person : 真实的旧正文")
        self.assertEqual(receipt["authorId"], "e" * 24)
        self.assertEqual(receipt["sourceSha256"], b.hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(previous, self.body_progress(source), "Read-only hydration must not mutate input")

    def test_rendered_body_migration_rejects_legacy_prefix_and_missing_identity(self):
        for changes in ({"text": "回复 Target person :"}, {"text": ""}, {"text": None}, {"text": "[图片评论]"},
                        {"authorHrefRaw": ""}, {"authorHrefRaw": "https://other.test/user/profile/" + "e" * 24},
                        {"commentId": "nickname-only"}, {"parentCommentId": ""}, {"textSource": "rendered_reply_prefix_only"}):
            source = self.rendered_snapshot(**changes)
            result = b.hydrate_reply_body_captures(self.run, N1, self.body_progress(source))
            self.assertEqual(result["threads"]["id:" + "d" * 24]["replyBodyCaptures"], {}, changes)
            self.assertEqual(result["threads"]["id:" + "d" * 24]["state"], "stalled", changes)

    def test_rendered_body_migration_does_not_inherit_identity_conflict(self):
        first = self.rendered_snapshot()
        other = self.rendered_snapshot("other", authorHrefRaw="/user/profile/" + "f" * 24)
        result = b.hydrate_reply_body_captures(self.run, N1, self.body_progress(first, other))
        self.assertEqual(result["threads"]["id:" + "d" * 24]["replyBodyCaptures"], {})
        self.assertEqual(result["replyBodyMigration"]["identityConflicts"], ["c" * 24])
        self.assertEqual(result["threads"]["id:" + "d" * 24]["state"], "stalled")

    def test_rendered_body_migration_rejects_changed_hash_and_unlinked_receipts(self):
        source = self.rendered_snapshot()
        result = b.hydrate_reply_body_captures(self.run, N1, self.body_progress(source))
        source.write_text(source.read_text().replace("真实", "改变"))
        changed = b.hydrate_reply_body_captures(self.run, N1, result)
        self.assertEqual(changed["threads"]["id:" + "d" * 24]["replyBodyCaptures"], {})
        self.assertEqual(changed["threads"]["id:" + "d" * 24]["state"], "stalled")
        self.assertEqual(changed["replyBodyMigration"]["skipped"][0]["reason"], "snapshot_sha256_changed")
        result["snapshots"] = []
        unlinked = b.hydrate_reply_body_captures(self.run, N1, result)
        self.assertEqual(unlinked["threads"]["id:" + "d" * 24]["replyBodyCaptures"], {})
        self.assertEqual(unlinked["threads"]["id:" + "d" * 24]["state"], "stalled")

    def test_rendered_body_migration_rejects_wrong_run_note_api_and_symlink(self):
        source = self.rendered_snapshot()
        other_run = self.root / source.name
        shutil.copyfile(source, other_run)
        linked = self.run / "raw" / ("browser-deep-" + N1 + "-linked.json")
        linked.symlink_to(source)
        api = self.run / "raw" / ("browser-deep-" + N1 + "-api.json")
        data = r.read_json(source)
        r.write_json(api, {**data, "operation": "comments"})
        wrong_note = self.run / "raw" / ("browser-deep-" + N1 + "-wrong.json")
        r.write_json(wrong_note, {**data, "note_id": N2})
        result = b.hydrate_reply_body_captures(self.run, N1, self.body_progress(other_run, linked, api, wrong_note))
        self.assertEqual(result["threads"]["id:" + "d" * 24]["replyBodyCaptures"], {})
        self.assertEqual(len(result["replyBodyMigration"]["skipped"]), 4)
        self.assertEqual(result["threads"]["id:" + "d" * 24]["state"], "stalled")

    def test_smaller_snapshot_keeps_old_rendered_body_and_migration_honors_stop(self):
        first = self.rendered_snapshot()
        small = self.rendered_snapshot("small", text="回复 Target person :")
        result = b.hydrate_reply_body_captures(self.run, N1, self.body_progress(first, small))
        self.assertEqual(result["threads"]["id:" + "d" * 24]["replyBodyCaptures"]["c" * 24]["sourceSnapshot"], str(first))
        self.assertEqual(result["threads"]["id:" + "d" * 24]["state"], "visible_replies_read")
        r.write_json(self.run / "STOP.json", {"reason": "synthetic restriction"})
        with patch.object(b, "hydrate_reply_body_captures") as hydrate, patch.object(b.subprocess, "run") as invoke:
            with self.assertRaises(r.ResearchError):
                b._deep_comments(self.run, N1, 1, 0, 1, resume=True)
            hydrate.assert_not_called()
            invoke.assert_not_called()
        self.assertTrue((self.run / "STOP.json").exists())

    def test_actual_prior_media_keeps_capture_but_not_review_claim(self):
        source = self.rendered_snapshot(text="[图片评论]", textSource="media_placeholder", images=["https://example.test/actual.jpg"])
        result = b.hydrate_reply_body_captures(self.run, N1, self.body_progress(source))
        thread = result["threads"]["id:" + "d" * 24]
        self.assertEqual(thread["state"], "visible_replies_read")
        self.assertEqual(thread["replyBodyCaptures"]["c" * 24]["textSource"], "media_placeholder")
        self.assertNotIn("reviewed", thread["replyBodyCaptures"]["c" * 24])

    def test_duplicate_rendered_rows_do_not_inflate_legacy_body_minimum(self):
        source = self.rendered_snapshot()
        previous = self.body_progress(source)
        previous["threads"]["id:" + "d" * 24].update(visibleReplies=3, uniqueObservedReplyCount=1)
        result = b.hydrate_reply_body_captures(self.run, N1, previous)
        thread = result["threads"]["id:" + "d" * 24]
        self.assertEqual(thread["replyBodyMinimumCount"], 1)
        self.assertEqual(thread["state"], "visible_replies_read")

    def test_note_context_retains_user_source(self):
        r.register(self.run, N1, "synthetic-token", "pc_user", provenance={"author_id": "public-1"})
        nid, context = r.resolve_note(self.run, N1)
        self.assertEqual(nid, N1)
        self.assertEqual(context["source"], "pc_user")
        self.assertIn("xsec_source=pc_user", context["url"])
        with self.assertRaises(r.ResearchError):
            r.resolve_note(self.run, N2)

    def test_search_sort_aliases_reach_api_and_separate_cache(self):
        calls = []
        class FakeApi:
            def search_notes(self, query, page, sort):
                calls.append(sort)
                from test_search_adapter import receipt
                self.last_search_request = receipt(query, page, sort)
                return {"items": [], "has_more": False}
        @contextmanager
        def fake_client(_): yield FakeApi()
        with patch.object(r, "client", fake_client):
            for sort in ("general", "latest", "popular", "latest"):
                r.collect_search(self.run, SimpleNamespace(query="synthetic", sort=sort, pages=1))
        self.assertEqual(calls, ["general", "time_descending", "popularity_descending"])

    def test_coverage_deduplicates_ids_and_separates_browser(self):
        row = {"id": "c1", "content": "synthetic", "sub_comments": [{"id": "reply-1"}]}
        for page in (1, 2):
            self.record("search" + str(page), operation="search", data={"items": [{"id": N1, "model_type": "note"}]})
            self.record("comments" + str(page), operation="comments", note_id=N1, data={"comments": [row]})
        self.record("another-note", operation="comments", note_id=N2, data={"comments": [row]})
        for name, at, rows in [("browser-50", "2026-10-29T00:00:00Z", [{"is_reply": False}, {"is_reply": True}]),
                               ("browser-20", "2026-10-29T01:00:00Z", [{"is_reply": False}])]:
            self.record(name, operation="browser_comments", note_id=N1, ok=True, captured_at=at, data=rows)
        result = r.summarize(self.run)
        self.assertEqual(result["unique_search_candidate_notes"], 1)
        self.assertEqual(result["api_unique_root_ids"], 2)
        self.assertEqual(result["api_unique_default_reply_ids"], 2)
        self.assertTrue(result["do_not_add_browser_and_api_counts"])
        self.assertEqual(result["browser_latest_snapshots_separate_from_api"][N1]["replies"], 0)

    def test_root_exhaustion_does_not_claim_all_replies(self):
        r.register(self.run, N1, "synthetic", "pc_search")
        self.record("comments-" + N1 + "-1", operation="comments", note_id=N1,
                    data={"comments": [{"id": "c", "sub_comment_count": "30", "sub_comment_has_more": True}], "has_more": False})
        @contextmanager
        def no_network(_):
            yield object()
        with patch.object(r, "client", no_network):
            result = r.collect_comments(self.run, N1, 3)
        self.assertFalse(result["all_replies_read"])
        self.assertIn("replies_not_guaranteed", result["stop_reason"])

    def test_stalled_cursor_is_not_exhaustion(self):
        r.register(self.run, N1, "synthetic", "pc_search")
        self.record("comments-" + N1 + "-1", operation="comments", note_id=N1,
                    data={"comments": [{"id": "c"}], "has_more": True, "cursor": ""})
        @contextmanager
        def no_network(_):
            yield object()
        with patch.object(r, "client", no_network):
            result = r.collect_comments(self.run, N1, 3)
        self.assertIn("stalled", result["stop_reason"])

    def test_cookie_filter_does_not_read_other_profile(self):
        chrome = self.root / "fake-chrome"
        r.write_json(chrome / "Local State", {"profile": {"info_cache": {"Default": {"name": "Research"}, "Profile 2": {"name": "Other"}}}})
        database = chrome / "Default" / "Cookies"
        database.parent.mkdir(exist_ok=True)
        database.write_bytes(b"synthetic-database-do-not-modify")
        jar = [SimpleNamespace(domain=".xiaohongshu.com", name="web_session", value="synthetic-session"),
               SimpleNamespace(domain="www.xiaohongshu.com", name="a1", value="synthetic-a1"),
               SimpleNamespace(domain=".not-xiaohongshu.com", name="outside", value="must-not-copy")]
        class FakeClient:
            def __init__(self, cookies): self.cookies = cookies
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get_self_info(self): return {"user_id": "synthetic-self"}
        with patch("browser_cookie3.chrome", return_value=jar) as read_cookies, patch.object(r, "client_class", return_value=FakeClient):
            result = r.login(self.run, chrome, "Research")
        read_cookies.assert_called_once_with(cookie_file=str(database), domain_name=".xiaohongshu.com")
        self.assertEqual(set(r.read_json(self.run / ".session/cookies.json")), {"web_session", "a1"})
        self.assertEqual(database.read_bytes(), b"synthetic-database-do-not-modify")
        self.assertFalse(result["original_profile_modified"])
        self.assertEqual((self.run / ".session/cookies.json").stat().st_mode & 0o077, 0)

    def test_verify_during_search_prewarm_stops_before_search(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(461, json={"success": False})
        api = r.client_class(self.run)({"a1": "synthetic", "web_session": "synthetic"})
        api._http.close()
        api._http = httpx.Client(transport=httpx.MockTransport(handler))
        api._rate_limit_delay = lambda: None
        with api, patch("xhs_cli.client.sign_main_api", return_value={}):
            with self.assertRaises(r.StopRun):
                api.search_notes("offline verification test")
        self.assertEqual(calls, ["/api/sns/web/v1/search/onebox"])
        self.assertTrue((self.run / "STOP.json").exists())

    def test_rate_limit_stops_without_retry(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(429, json={"success": False})
        api = r.client_class(self.run)({"a1": "synthetic", "web_session": "synthetic"})
        api._http.close()
        api._http = httpx.Client(transport=httpx.MockTransport(handler))
        api._rate_limit_delay = lambda: None
        with api, patch("xhs_cli.client.sign_main_api", return_value={}):
            with self.assertRaises(r.StopRun): api.get_self_info()
        self.assertEqual(len(calls), 1)

    def test_optional_empty_success_does_not_block_real_search(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path.endswith('/onebox'):
                return httpx.Response(200, json={"success": False, "code": 0, "msg": "成功"})
            if request.url.path.endswith('/recommend'):
                return httpx.Response(200, json={"success": False, "code": 3002, "msg": "方案执行结果为NULL"})
            return httpx.Response(200, json={"success": True, "data": {"items": [{"id": N1}], "has_more": False}})
        api = r.client_class(self.run)({"a1": "synthetic", "web_session": "synthetic"})
        api._http.close()
        api._http = httpx.Client(transport=httpx.MockTransport(handler))
        api._rate_limit_delay = lambda: None
        with api, patch("xhs_cli.client.sign_main_api", return_value={}):
            result = api.search_notes("offline optional empty response")
        self.assertEqual(result["items"][0]["id"], N1)
        self.assertIn("/api/sns/web/v1/search/notes", calls)
        self.assertFalse((self.run / "STOP.json").exists())

    def test_write_endpoint_rejected_before_transport(self):
        api = r.client_class(self.run)({"a1": "synthetic", "web_session": "synthetic"})
        with api, patch.object(api._http, "request") as transport:
            with self.assertRaises(r.ResearchError):
                api._request_with_retry("POST", "https://edith.xiaohongshu.com/api/sns/web/v1/note/like")
            transport.assert_not_called()

    def test_unavailable_note_not_saved_as_success(self):
        with self.assertRaises(r.ResearchError):
            r.capture(self.run, "note-" + N1, {"operation": "note", "note_id": N1}, lambda: {"items": []})
        self.assertFalse((self.run / "raw" / ("note-" + N1 + ".json")).exists())

    def test_public_author_identity_survives_redaction(self):
        data = r.clean_data({"user_id": "public-A", "nickname": "momo", "cookie": "fake-secret", "web_session": "fake-secret"})
        self.assertEqual(data, {"user_id": "public-A", "nickname": "momo"})

    def test_cleanup_retains_evidence_and_original_profile(self):
        original = self.root / "original-profile"
        original.mkdir()
        (original / "keep").write_text("original")
        r.write_json(self.run / ".session/cookies.json", {"web_session": "synthetic"})
        self.record("evidence", operation="note", note_id=N1, data={"items": []})
        result = b.cleanup(self.run)
        self.assertTrue(result["temporary_credentials_removed"])
        self.assertTrue((self.run / "raw/evidence.json").exists())
        self.assertEqual((original / "keep").read_text(), "original")
        with self.assertRaises(r.ResearchError): r.ensure_active(self.run)

    def test_cleanup_refuses_reused_pid(self):
        profile = self.run / "browser"
        profile.mkdir()
        r.write_json(self.run / "browser.json", {"pid": 12345, "profile": str(profile)})
        with patch.object(b, "process_command", return_value="Chrome --user-data-dir=/unrelated/profile"), patch.object(b.os, "kill") as kill:
            with self.assertRaises(r.ResearchError): b.cleanup(self.run)
            kill.assert_not_called()
        self.assertTrue(profile.exists())

    def test_managed_directory_symlink_refused(self):
        original = self.root / "original"
        original.mkdir()
        (self.run / ".session").symlink_to(original, target_is_directory=True)
        with self.assertRaises(r.ResearchError): r.load_run(self.run)

    def test_browser_bootstrap_with_offline_bridge(self):
        node = os.environ.get("XHS_TEST_NODE") or shutil.which("node")
        if not node:
            self.skipTest("Set XHS_TEST_NODE to exercise the JavaScript bridge stub")
        module = self.root / "fake-bridge.mjs"
        module.write_text('''
export class CDPBridge {
  async connect({cdpEndpoint}) {
    if (cdpEndpoint !== 'ws://127.0.0.1:54321/devtools/page/test_target') throw new Error('endpoint');
    return {evaluate: async () => 'about:blank', goto: async url => {if(url !== 'https://www.xiaohongshu.com/explore') throw new Error('url')}};
  }
  async send(method, {cookies}) {
    if (method !== 'Network.setCookies' || cookies.some(c => c.domain !== '.xiaohongshu.com')) throw new Error('scope');
  }
  async close() {}
}
''')
        cookies = self.root / "synthetic-cookies.json"
        r.write_json(cookies, {"web_session": "synthetic"})
        result = subprocess.run([node, str(Path(__file__).with_name("prepare_browser.mjs")), str(module),
                                 "http://127.0.0.1:54321", str(cookies), "ws://127.0.0.1:54321/devtools/page/test_target", "test_target"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["temporary_browser_prepared"])
        self.assertNotIn("synthetic", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
