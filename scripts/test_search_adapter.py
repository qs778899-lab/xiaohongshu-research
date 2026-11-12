"""Offline request-body, pagination-version and protected A/B behavioral checks."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import xhs_research as r
import xhs_project as xp
import xhs_search as s
import xhs_search_ab as ab


def receipt(query, page, sort, method=s.SEARCH_METHOD):
    return s.request_receipt(s.adapt_request({"keyword": query, "page": page, "sort": sort,
        "search_id": "synthetic-search-id", "filters": [{"type": "sort_type", "tags": ["general"]}]}, method), method, "0.6.4")


class SearchAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.run = self.base / "run"
        self.run.mkdir()
        r.write_json(self.run / "run.json", {"format": r.FORMAT, "directory": str(self.run), "closed": False})

    def test_actual_dependency_serialized_request_all_sorts_pages(self):
        from xhs_cli.client_mixins import _SEARCH_DEFAULT_FILTERS
        original = deepcopy(_SEARCH_DEFAULT_FILTERS)
        captured = []
        def transport(method, url, **kwargs):
            captured.append(kwargs["content"])
            return httpx.Response(200, request=httpx.Request(method, url),
                                  json={"success": True, "data": {"items": [], "has_more": False}})
        with patch("xhs_cli.client.sign_main_api", return_value={}), \
             patch("xhs_cli.client_mixins._acquire_search_session", return_value=("synthetic-id", False)):
            with r.client_class(self.run)({}) as api:
                api._request_with_retry = transport
                for method in (s.LEGACY_SEARCH_METHOD, s.SEARCH_METHOD):
                    api.search_method_version = method
                    for sort in xp.SORTS.values():
                        for page in (1, 2, 51):
                            api.search_notes("示例城市 测试甲 主题", page=page, sort=sort)
                            body = json.loads(captured[-1])
                            self.assertEqual((body["keyword"], body["page"], body["sort"], body["search_id"]),
                                             ("示例城市 测试甲 主题", page, sort, "synthetic-id"))
                            selected = next(x for x in body["filters"] if x["type"] == "sort_type")
                            self.assertEqual(selected["tags"], [sort if method == s.SEARCH_METHOD else "general"])
                            self.assertEqual(api.last_search_request["body_json"], captured[-1])
                            self.assertEqual(api.last_search_request["payload"], body)
                            if method == s.SEARCH_METHOD:
                                s.collection_metadata(api, "示例城市 测试甲 主题", page, sort)
        self.assertEqual(_SEARCH_DEFAULT_FILTERS, original)
        self.assertEqual(len(captured), 18)
        for offset in (0, 1, 2): self.assertEqual(captured[offset], captured[9 + offset])

    def test_unknown_or_missing_sort_filter_fails_closed(self):
        for value in ({"sort": "unknown", "filters": []}, {"sort": "general"},
                      {"sort": "general", "filters": []},
                      {"sort": "general", "filters": [{"type": "sort_type"}, {"type": "sort_type"}]}):
            with self.assertRaises(ValueError): s.adapt_request(value)

    def test_receipt_rejects_changed_query_page_sort_and_body(self):
        value = receipt("q", 2, "time_descending")
        for query, page, sort in (("other", 2, "time_descending"), ("q", 3, "time_descending"), ("q", 2, "general")):
            with self.assertRaises(ValueError): s.validate_receipt(value, query, page, sort)
        value["body_json"] += " "
        with self.assertRaises(ValueError): s.validate_receipt(value, "q", 2, "time_descending")

    def test_ab_checks_project_stop_before_client(self):
        p = xp.Project(self.base / "project", create=True)
        p.set_meta("stop", {"reason": "verification"}); p.db.commit(); p.close()
        with patch.object(r, "client") as client:
            with self.assertRaises(r.StopRun): ab.run_ab(self.base / "project", self.run, self.base / "ab", "q", ["latest"], 2)
            client.assert_not_called()

    def test_ab_checks_run_stop_before_client(self):
        p = xp.Project(self.base / "project", create=True); p.close()
        r.write_json(self.run / "STOP.json", {"reason": "verification"})
        with patch.object(r, "client") as client:
            with self.assertRaises(r.StopRun): ab.run_ab(self.base / "project", self.run, self.base / "ab", "q", ["latest"], 2)
            client.assert_not_called()

    def test_ab_account_stop_does_not_request_or_create_output(self):
        p = xp.Project(self.base / "project", create=True); p.close()
        guard = self.base / "guard"; guard.mkdir()
        r.write_json(guard / "STOP.json", {"reason": "verification"})
        with patch.object(r, "account_dir", return_value=guard), patch.object(r, "client") as client:
            with self.assertRaises(r.StopRun): ab.run_ab(self.base / "project", self.run, self.base / "ab", "q", ["latest"], 2)
            client.assert_not_called()
        self.assertFalse((self.base / "ab").exists())

    def test_ab_partial_failure_preserves_first_response_and_stops(self):
        p = xp.Project(self.base / "project", create=True); p.close()
        r.write_json(self.run / "account.json", {"key": "a" * 64})
        calls = []
        class API:
            def search_notes(api, query, page, sort):
                calls.append(sort)
                if len(calls) == 2: raise r.StopRun("verification")
                api.last_search_request = receipt(query, page, sort, api.search_method_version)
                return {"items": [], "has_more": True}
        @contextmanager
        def client(run): yield API()
        guard = self.base / "guard"; guard.mkdir()
        with patch.object(r, "account_dir", return_value=guard), patch.object(r, "client", client):
            with self.assertRaises(r.StopRun): ab.run_ab(self.base / "project", self.run, self.base / "ab", "q", ["latest"], 2)
        summary = r.read_json(self.base / "ab" / "summary.json")
        self.assertEqual((summary["state"], len(summary["records"]), len(calls)), ("stopped", 1, 2))
        self.assertTrue((self.base / "ab" / summary["records"][0]).is_file())

    def test_ab_four_serial_requests_preserve_control_order_and_separate_raw(self):
        p = xp.Project(self.base / "project", create=True); p.close()
        r.write_json(self.run / "account.json", {"key": "a" * 64})
        calls = []
        class API:
            def search_notes(api, query, page, sort):
                calls.append((api.search_method_version, query, page, sort))
                api.last_search_request = receipt(query, page, sort, api.search_method_version)
                return {"items": [], "has_more": False}
        @contextmanager
        def client(run):
            # The production client owns the account lock. Assert it is not replaced.
            with r.account_lock(run): yield API()
        guard = self.base / "guard"; guard.mkdir()
        with patch.object(r, "account_dir", return_value=guard), patch.object(r, "client", client):
            result = ab.run_ab(self.base / "project", self.run, self.base / "ab", "q", ["latest"], 2)
        self.assertEqual(result["records"], 4)
        self.assertEqual([c[0] for c in calls], [s.LEGACY_SEARCH_METHOD, s.SEARCH_METHOD, s.SEARCH_METHOD, s.LEGACY_SEARCH_METHOD])
        raws = [json.loads(f.read_text()) for f in (self.base / "ab").glob("*.json") if f.name != "summary.json"]
        self.assertTrue(all(row["operation"] == "search_ab" for row in raws))
        self.assertFalse((self.run / "raw").exists())
        self.assertEqual(r._ACCOUNT_LOCKS, {})


class SearchVersionProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.p = xp.Project(self.base / "project", create=True)
        self.addCleanup(self.p.close)
        self.count = 0
        self.config = {"target": "test target", "queries": ["doctor orthodontics"]}
        self.query = self.config["queries"][0]
        self.run = self.base / "run"; self.run.mkdir()
        r.write_json(self.run / "run.json", {"format": r.FORMAT, "directory": str(self.run), "closed": False})

    def snapshot(self, sort="latest", page=1, more=True, current=False, **extra):
        self.count += 1
        record = {"operation": "search", "query": self.query, "sort": sort, "api_sort": xp.SORTS[sort],
                  "page": page, "captured_at": str(self.count),
                  "data": {"items": [{"model_type": "note", "id": "%024d" % self.count, "note_card": {}}], "has_more": more}}
        if current:
            record.update(search_method_version=s.SEARCH_METHOD,
                          search_request=receipt(self.query, page, xp.SORTS[sort]))
        record.update(extra)
        file = self.base / ("snapshot-%s.json" % self.count)
        r.write_json(file, record)
        return self.p.ingest(file)

    def task(self, sort="latest", current=True):
        key = s.task_key(self.query, sort, s.SEARCH_METHOD if current else s.LEGACY_SEARCH_METHOD)
        row = self.p.db.execute("SELECT id FROM tasks WHERE kind='search' AND entity_key=?", (key,)).fetchone()
        return self.p.task(row[0])

    def select(self, sort):
        return self.p.add("search", "ignored", {"query": self.query, "sort": sort})

    def test_general_continues_old_page_without_research_or_new_task(self):
        self.snapshot(sort="general")
        before = self.task(sort="general")
        self.p.plan(self.config)
        after = self.task(sort="general")
        self.assertEqual((after["id"], after["next_page"], after["historical"]), (before["id"], 2, False))
        calls = []
        class API:
            def search_notes(api, query, page, sort):
                calls.append(page); api.last_search_request = receipt(query, page, sort)
                return {"items": [], "has_more": False}
        @contextmanager
        def client(run): yield API()
        with patch.object(r, "client", client): self.p.collect_one(after["id"], self.run)
        self.assertEqual(calls, [2])
        self.assertEqual(self.task(sort="general")["status"], "complete")
        self.assertEqual(self.p.db.execute("SELECT COUNT(*) FROM tasks WHERE kind='search'").fetchone()[0], 3)

    def test_terminal_general_stays_complete_after_replan(self):
        self.snapshot(sort="general", more=False)
        before = self.task(sort="general")
        self.p.plan(self.config)
        after = self.task(sort="general")
        self.assertEqual((after["id"], after["status"], after["next_page"]), (before["id"], "complete", before["next_page"]))

    def test_latest_popular_new_chains_keep_old_status_and_coverage(self):
        self.snapshot(sort="latest", more=False)
        self.snapshot(sort="popular", more=True)
        before = {sort: self.task(sort, current=False) for sort in ("latest", "popular")}
        page_count = self.p.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        self.p.plan(self.config)
        self.assertEqual(self.p.db.execute("SELECT COUNT(*) FROM tasks WHERE kind='search'").fetchone()[0], 3)
        self.assertFalse(self.task("latest", current=False)["historical"])
        for sort in before: self.select(sort)
        for sort in before:
            old, new = self.task(sort, current=False), self.task(sort)
            self.assertEqual((old["status"], old["next_page"]), (before[sort]["status"], before[sort]["next_page"]))
            self.assertTrue(old["historical"]); self.assertFalse(old["automatable"])
            self.assertEqual(old["payload"]["superseded_by"], new["id"])
            self.assertEqual((new["status"], new["next_page"]), ("pending", 1))
        self.assertEqual(self.p.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0], page_count)
        self.p.plan(self.config)
        self.assertEqual(self.p.db.execute("SELECT COUNT(*) FROM tasks WHERE kind='search'").fetchone()[0], 5)
        self.assertEqual(len(self.p.status()["historical_search_tasks"]), 2)
        self.assertEqual(self.p.status()["search_coverage"]["logical_combinations"], 3)
        self.assertEqual(self.p.status()["search_coverage"]["method_task_count"], 5)
        active = [t for t in self.p.next(100) if t["kind"] == "search"]
        self.assertEqual(len(active), 3)

    def test_old_non_general_cannot_issue_a_new_request_before_migration(self):
        self.snapshot()
        old = self.task(current=False)
        with patch.object(r, "client") as client:
            with self.assertRaises(r.ResearchError): self.p.collect_one(old["id"], self.run)
            client.assert_not_called()
        self.assertEqual(self.p.next(100, automatic=True, kinds=["search"]), [])

    def test_late_legacy_import_never_advances_new_chain(self):
        self.p.plan(self.config)
        self.snapshot(page=1)
        self.snapshot(page=2, more=False)
        self.assertTrue(self.task(current=False)["historical"])
        self.assertEqual(self.task()["next_page"], 1)
        self.assertEqual(self.task()["status"], "pending")
        self.snapshot(page=1, current=True)
        self.assertEqual(self.task()["next_page"], 2)
        self.assertEqual(self.task()["status"], "pending")
        self.snapshot(page=2, current=True, more=False)
        self.assertEqual(self.task()["status"], "complete")
        self.assertEqual(self.p.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 4)

    def test_version_label_without_matching_request_evidence_rejected(self):
        for extra in ({"search_method_version": s.SEARCH_METHOD}, {"search_method_version": "future-unknown"}):
            with self.assertRaises(r.ResearchError): self.snapshot(**extra)
        self.assertEqual(self.p.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 0)

    def test_new_page_cannot_claim_old_task_even_when_version_is_present(self):
        self.snapshot()
        old = self.task(current=False)
        with self.assertRaises(r.ResearchError): self.snapshot(current=True, project_task=old["id"])
        self.assertEqual(self.task(current=False)["next_page"], 2)

    def test_superseded_gaps_do_not_block_status_or_batch_completion(self):
        import xhs_batch as batch
        self.snapshot(sort="latest")
        self.snapshot(sort="popular")
        self.p.plan(self.config)
        for sort in ("latest", "popular"): self.select(sort)
        for sort in xp.SORTS: self.snapshot(sort=sort, current=True, more=False)
        self.p.db.execute("UPDATE tasks SET status='excluded',review_status='excluded' WHERE kind='note'")
        self.p.db.commit()
        status = self.p.status()
        self.assertEqual(status["outstanding_tasks"], 0)
        self.assertEqual(status["state"], "defined_visible_scope_processed")
        self.assertEqual(len(status["historical_search_tasks"]), 2)
        view = batch.read_view(self.p.path)
        self.assertEqual(batch.counts(view)["pending"], 0)
        self.assertEqual(batch.choose(view)["action"], "scope_processed")
        for task in view["tasks"]:
            if s.historical(task): self.assertFalse(batch.eligible(task, {}, explicit=True))
        report = self.p.report()
        self.assertEqual(json.loads(Path(report["json"]).read_text())["remaining"], [])


if __name__ == "__main__": unittest.main()
