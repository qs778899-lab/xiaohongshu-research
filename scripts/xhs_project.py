#!/usr/bin/env python3
"""Persistent read-only research queue. Collection and semantic review are separate."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import parse_qs, urlparse
import uuid

import xhs_research as legacy
import xhs_search

VERSION = "xiaohongshu-project-v2"
SORTS = {"general": "general", "latest": "time_descending", "popular": "popularity_descending"}
AUTO_KINDS = ("search", "note", "comments", "author", "browser")
ATTRIBUTIONS = {"explicit", "context_supported", "insufficient", "other_target"}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else dumps(value).encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def collection_rows(operation, data):
    """An explicit terminal page may omit its list; other missing lists are gaps."""
    key = {"search": "items", "note": "items", "comments": "comments", "author_notes": "notes"}[operation]
    if not isinstance(data, dict):
        raise legacy.ResearchError("Unexpected collection schema: " + operation)
    rows = data.get(key)
    if isinstance(rows, list):
        return rows
    if operation != "note" and rows is None and data.get("has_more") is False:
        return []
    raise legacy.ResearchError("Unexpected collection schema: " + operation)


def identity_context(metadata):
    user = metadata.get("user") or metadata.get("user_info") or {}
    target = metadata.get("target_comment") or {}
    return {"author_id": metadata.get("userId") or user.get("user_id"),
            "parent_id": metadata.get("parentCommentId") or metadata.get("root_comment_id"),
            "reply_id": metadata.get("replyToCommentId") or target.get("id"),
            "reply_user_id": (target.get("user_info") or {}).get("user_id"),
            "reply_to": metadata.get("reply_to"), "root_key": metadata.get("rootKey"),
            "is_reply": metadata.get("is_reply"),
            "published": metadata.get("create_time") or metadata.get("time")}


class Project:
    def __init__(self, path, create=False):
        self.path = legacy.research_path(path)
        if create:
            self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.is_dir():
            raise legacy.ResearchError("Project does not exist; use plan first")
        database = self.path / "project.sqlite3"
        if database.is_symlink():
            raise legacy.ResearchError("Project database must not be a symlink")
        if not create and not database.exists():
            raise legacy.ResearchError("Not a research project")
        self.db = sqlite3.connect(database, timeout=10)
        os.chmod(database, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY,kind TEXT NOT NULL,entity_key TEXT NOT NULL,payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',review_status TEXT NOT NULL DEFAULT 'pending',
          next_page INTEGER NOT NULL DEFAULT 1,cursor TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',
          priority INTEGER NOT NULL DEFAULT 50,turn INTEGER NOT NULL DEFAULT 0,updated TEXT NOT NULL,
          UNIQUE(kind,entity_key));
        CREATE TABLE IF NOT EXISTS snapshots(
          id TEXT PRIMARY KEY,path TEXT NOT NULL,source_path TEXT NOT NULL,operation TEXT NOT NULL,captured_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS documents(
          id TEXT PRIMARY KEY,note_id TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,
          metadata TEXT NOT NULL,identity_kind TEXT NOT NULL,captured_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS document_sources(
          document_id TEXT NOT NULL,snapshot_id TEXT NOT NULL,locator TEXT NOT NULL,
          PRIMARY KEY(document_id,snapshot_id,locator));
        CREATE TABLE IF NOT EXISTS notes(note_id TEXT PRIMARY KEY,title TEXT NOT NULL,contexts TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS threads(
          note_id TEXT NOT NULL,root_key TEXT NOT NULL,comment_id TEXT NOT NULL,identity_kind TEXT NOT NULL,
          status TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(note_id,root_key));
        CREATE TABLE IF NOT EXISTS pages(
          task_id TEXT NOT NULL,page INTEGER NOT NULL,cursor_in TEXT NOT NULL,snapshot_id TEXT NOT NULL,
          ids TEXT NOT NULL,has_more TEXT NOT NULL,cursor_out TEXT NOT NULL,
          PRIMARY KEY(task_id,snapshot_id));
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY,task_id TEXT NOT NULL,
          decision TEXT NOT NULL,summary TEXT NOT NULL,annotations TEXT NOT NULL,at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,payload TEXT NOT NULL,at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS evidence_links(
          id TEXT PRIMARY KEY,earlier TEXT NOT NULL REFERENCES evidence(id),
          later TEXT NOT NULL REFERENCES evidence(id),relation TEXT NOT NULL,
          summary TEXT NOT NULL,earlier_hash TEXT NOT NULL,later_hash TEXT NOT NULL,at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS imports(path TEXT PRIMARY KEY,at TEXT NOT NULL);
        """)
        fmt = self.get_meta("format")
        if fmt and fmt != VERSION:
            raise legacy.ResearchError("Unsupported project schema")
        self.set_meta("format", VERSION)
        self.db.commit()

    def close(self):
        self.db.close()

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, dumps(value)))

    def add_task(self, kind, key, payload, priority=50, review_status=None):
        existing = self.db.execute("SELECT id FROM tasks WHERE kind=? AND entity_key=?", (kind, key)).fetchone()
        task_id = existing[0] if existing else kind + "-" + digest([kind, key])[:20]
        semantic = review_status or ("not_required" if kind in {"search", "author"} else "pending")
        self.db.execute("INSERT OR IGNORE INTO tasks(id,kind,entity_key,payload,priority,review_status,updated) VALUES(?,?,?,?,?,?,?)",
                        (task_id, kind, key, dumps(payload), priority, semantic, legacy.now()))
        old = self.task(task_id)
        merged = dict(old["payload"])
        resource_changed = False
        if kind == "media" and any(k in old["payload"] for k in ("observed_url", "inline_caption", "resource_id_hint")):
            try:
                from xhs_media import resource_identity
                resource_changed = resource_identity(old["payload"]) != resource_identity(payload)
            except ImportError:
                before, after = urlparse(old["payload"].get("observed_url", "")), urlparse(payload.get("observed_url", ""))
                resource_changed = (before.hostname, before.path) != (after.hostname, after.path)
            if resource_changed:
                if (old["payload"].get("captured_at") or "") > (payload.get("captured_at") or ""):
                    return task_id
                merged = {k: v for k, v in merged.items() if k not in {"file", "sha256", "transcript", "transcript_sha256", "transcript_method", "reviewed_at", "observation", "checked_segments", "transcription_status", "review_scope", "review_binding", "reading_limits"}}
        for k, v in payload.items():
            if k == "provenance":
                sources = merged.get(k, [])
                if not isinstance(sources, list): sources = [sources]
                for item in v if isinstance(v, list) else [v]:
                    if item not in sources: sources.append(item)
                merged[k] = sources
            elif v is not None and v != "":
                merged[k] = v
        self.db.execute("UPDATE tasks SET payload=?,priority=MIN(priority,?) WHERE id=?", (dumps(merged), priority, task_id))
        if resource_changed:
            exclusion = self._media_scope_exclusion(task_id)
            if exclusion:
                self.db.execute("UPDATE tasks SET status='excluded',review_status='excluded',reason=? WHERE id=?", (exclusion["summary"], task_id))
            else:
                self.db.execute("UPDATE tasks SET status='pending',review_status='pending',reason='media_resource_changed' WHERE id=?", (task_id,))
        return task_id

    def task(self, task_id, *, native_dispatch=None):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row: raise legacy.ResearchError("Unknown task")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["historical"] = xhs_search.historical(result)
        result["automatable"] = (result["kind"] in AUTO_KINDS and not result["payload"].get("needs_triage", False)
                                 and xhs_search.collection_allowed(result))
        result["next_action"] = ("review" if result["status"] == "complete" and result["review_status"] == "pending" else
                                 "triage" if result["payload"].get("needs_triage") else
                                 "media" if result["kind"] == "media" else "collect")
        if result["historical"]: result["next_action"] = "historical_search_method"
        elif result["kind"] == "search" and not xhs_search.collection_allowed(result):
            result["next_action"] = "explicitly_select_new_search_method"
        elif result["kind"] == "native":
            from xhs_native import hint
            result["next_action"] = hint(self, result) if native_dispatch is None else hint(self, result, dispatch=native_dispatch)
        return result

    def _supersede_search(self, query, sort, replacement):
        """Only an explicit plan/add declares replacement; preserve old state/pages."""
        if sort == "general": return
        key = xhs_search.task_key(query, sort, xhs_search.LEGACY_SEARCH_METHOD)
        row = self.db.execute("SELECT id,payload FROM tasks WHERE kind='search' AND entity_key=?", (key,)).fetchone()
        if row and row["id"] != replacement:
            payload = json.loads(row["payload"])
            payload.update(superseded_by=replacement, superseded_reason="search_sort_filter_method_changed",
                           search_method_version=xhs_search.LEGACY_SEARCH_METHOD)
            self.db.execute("UPDATE tasks SET payload=? WHERE id=?", (dumps(payload), row["id"]))

    def plan(self, config):
        if not isinstance(config.get("target"), str) or not config["target"].strip():
            raise legacy.ResearchError("Config requires a target and explicit query list")
        old = self.get_meta("config")
        if old and old["target"] != config["target"]:
            raise legacy.ResearchError("Use a separate project for a different target")
        queries = config.get("queries", [])
        if not queries: raise legacy.ResearchError("Config requires explicit queries")
        self.set_meta("config", config)
        ids = []
        for item in queries:
            query = item if isinstance(item, str) else item["query"]
            provenance = "plan" if isinstance(item, str) else item.get("provenance", "plan")
            for sort in SORTS:
                key, method = xhs_search.task_key(query, sort), xhs_search.SEARCH_METHOD
                # Replanning an existing matrix does not create new method work.
                # Select a particular query/sort via add to request a fresh chain.
                old_key = xhs_search.task_key(query, sort, xhs_search.LEGACY_SEARCH_METHOD)
                if sort != "general" and not self.db.execute("SELECT 1 FROM tasks WHERE kind='search' AND entity_key=?", (key,)).fetchone():
                    if self.db.execute("SELECT 1 FROM tasks WHERE kind='search' AND entity_key=?", (old_key,)).fetchone():
                        key, method = old_key, xhs_search.LEGACY_SEARCH_METHOD
                task = self.add_task("search", key,
                                     {"query": query, "sort": sort, "api_sort": SORTS[sort],
                                      "search_method_version": method, "provenance": [provenance]}, 40)
                ids.append(task)
        self.db.commit()
        return {"project": str(self.path), "target": config["target"], "search_tasks": len(ids)}

    def register_note(self, nid, title="", token="", source="", url="", provenance=None, needs_triage=False):
        if not nid: return
        row = self.db.execute("SELECT * FROM notes WHERE note_id=?", (nid,)).fetchone()
        contexts = json.loads(row["contexts"]) if row else []
        if url:
            parts = parse_qs(urlparse(url).query)
            token = token or parts.get("xsec_token", [""])[0]
            source = source or parts.get("xsec_source", [""])[0]
        if token and source:
            ctx = {"token": token, "source": source, "url": url or legacy.note_url(nid, token, source)}
            if ctx not in contexts: contexts.append(ctx)
        self.db.execute("INSERT OR REPLACE INTO notes VALUES(?,?,?)", (nid, title or (row["title"] if row else ""), dumps(contexts)))
        existing = self.db.execute("SELECT payload FROM tasks WHERE kind='note' AND entity_key=?", (nid,)).fetchone()
        if existing and not json.loads(existing[0]).get("needs_triage", False): needs_triage = False
        task = self.add_task("note", nid, {"note_id": nid, "title": title, "provenance": [provenance] if provenance else [], "needs_triage": needs_triage}, 30)
        return task

    def doc(self, sid, nid, kind, text, metadata, locator, real_id=None, captured_at=""):
        # Only platform IDs merge across transports. Fingerprints remain snapshot-local.
        identity = "platform_id" if real_id else "snapshot_local"
        key = ("note:" + nid if kind == "note" else "comment:" + nid + ":" + str(real_id)) if real_id else "observation:" + sid + ":" + str(locator)
        old = self.db.execute("SELECT captured_at,text,metadata FROM documents WHERE id=?", (key,)).fetchone()
        if old is None or captured_at >= old["captured_at"]:
            if old is None or old["text"] != (text or "") or identity_context(json.loads(old["metadata"])) != identity_context(metadata):
                self._review_changed = True
            self.db.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?)",
                            (key, nid, kind, text or "", dumps(metadata), identity, captured_at))
        self.db.execute("INSERT OR IGNORE INTO document_sources VALUES(?,?,?)", (key, sid, str(locator)))
        return key

    def _page(self, task_id, record, sid, ids, list_empty):
        task = self.task(task_id)
        data = record["data"]
        page = int(record.get("page", 1))
        incoming = str(record.get("cursor_in", ""))
        outgoing = str(data.get("cursor", ""))
        more = data.get("has_more")
        self.db.execute("INSERT OR IGNORE INTO pages VALUES(?,?,?,?,?,?,?)", (task_id, page, incoming, sid, dumps(ids), dumps(more), outgoing))
        # Rebuild a contiguous chain, including snapshots recovered/imported out of order.
        by_page = {}
        for row in self.db.execute("SELECT p.*,s.captured_at FROM pages p JOIN snapshots s ON s.id=p.snapshot_id WHERE p.task_id=? ORDER BY p.page,s.captured_at,s.id", (task_id,)):
            by_page[row["page"]] = row
        wanted, cursor, seen_ids, seen_cursors = 1, "", set(), {""}
        status, reason = "pending", "more_pages_pending"
        while True:
            current = by_page.get(wanted)
            if current is None:
                if any(p > wanted for p in by_page):
                    status, reason = "blocked", "missing_prior_page:" + str(wanted)
                break
            cursor_based = task["kind"] in {"comments", "author"}
            if cursor_based and current["cursor_in"] != cursor:
                status, reason = "blocked", "cursor_chain_mismatch_at_page:" + str(wanted)
                break
            current_ids = set(json.loads(current["ids"]))
            more = json.loads(current["has_more"])
            following = current["cursor_out"]
            if more is False:
                status, reason = "complete", "platform_returned_end"
                wanted += 1
                cursor = following
                break
            if not current_ids:
                status, reason = "blocked", "empty_page_not_proof_of_exhaustion"
                break
            if wanted > 1 and not current_ids - seen_ids:
                status, reason = "blocked", "repeated_page_not_proof_of_exhaustion"
                break
            if cursor_based and (not following or following in seen_cursors):
                status, reason = "blocked", "cursor_missing_or_stalled"
                break
            if more is not True:
                status, reason = "blocked", "has_more_unknown"
                break
            seen_ids.update(current_ids)
            seen_cursors.add(following)
            cursor = following
            wanted += 1
        self.db.execute("UPDATE tasks SET status=?,reason=?,next_page=?,cursor=?,updated=? WHERE id=?",
                        (status, reason, wanted, cursor, legacy.now(), task_id))

    def ingest(self, file, origin=None):
        self.db.execute("SAVEPOINT ingest_snapshot")
        try:
            result = self._ingest(file, origin)
            self.db.execute("RELEASE ingest_snapshot")
            return result
        except Exception:
            self.db.execute("ROLLBACK TO ingest_snapshot")
            self.db.execute("RELEASE ingest_snapshot")
            raise

    def _ingest(self, file, origin=None):
        self._review_changed = False
        file = Path(file).resolve()
        record_bytes = file.read_bytes()
        record = json.loads(record_bytes)
        if "collection_error" in record:
            return {"skipped": str(file), "reason": "collection_error"}
        op = record.get("operation", "")
        if op not in {"search", "note", "comments", "author_notes", "browser_comments"}:
            return {"skipped": str(file), "reason": "not_collection_snapshot"}
        sid = digest(record_bytes)
        if self.db.execute("SELECT 1 FROM snapshots WHERE id=?", (sid,)).fetchone():
            return {"snapshot": sid, "cached": True}
        collected_rows = collection_rows(op, record.get("data")) if op != "browser_comments" else None
        target = self.path / "snapshots" / (sid + ".json")
        target.parent.mkdir(mode=0o700, exist_ok=True)
        if not target.exists():
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream: stream.write(record_bytes)
        captured = record.get("captured_at", "")
        self.db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?)", (sid, str(target.relative_to(self.path)), str(origin or file), op, captured))
        data = record.get("data", {})
        nid = record.get("note_id", "")
        provenance = {"snapshot": sid, "operation": op}
        if nid: self.register_note(nid, url=record.get("url", ""), provenance=provenance)
        if op == "search":
            query, sort = record["query"], record.get("sort", "general")
            if sort not in SORTS or record.get("api_sort", SORTS[sort]) != SORTS[sort]:
                raise legacy.ResearchError("Unverified legacy sort mapping cannot seed project pagination")
            try:
                method = xhs_search.method_of(record)
                if method == xhs_search.SEARCH_METHOD:
                    xhs_search.validate_receipt(record.get("search_request"), query, record.get("page", 1), SORTS[sort])
            except ValueError as exc:
                raise legacy.ResearchError(str(exc)) from None
            task = self.add_task("search", xhs_search.task_key(query, sort, method),
                                 {"query": query, "sort": sort, "api_sort": SORTS[sort], "search_method_version": method}, 40)
            if record.get("project_task") and record["project_task"] != task:
                raise legacy.ResearchError("Search method version does not match the recorded project task")
            if method == xhs_search.SEARCH_METHOD:
                self._supersede_search(query, sort, task)
            if method == xhs_search.LEGACY_SEARCH_METHOD and sort != "general":
                replacement = self.db.execute("SELECT id FROM tasks WHERE kind='search' AND entity_key=?",
                                              (xhs_search.task_key(query, sort),)).fetchone()
                if replacement: self._supersede_search(query, sort, replacement["id"])
            rows = [x for x in collected_rows if x.get("model_type") == "note"]
            for n in rows:
                card = n.get("note_card", {})
                self.register_note(n["id"], card.get("display_title", card.get("title", "")), n.get("xsec_token", ""), "pc_search", provenance=provenance)
            self._page(task, record, sid, [x["id"] for x in rows], not rows)
        elif op == "author_notes":
            uid = record["user_id"]
            task = self.add_task("author", uid, {"user_id": uid}, 35)
            rows = collected_rows
            for n in rows:
                self.register_note(n.get("note_id", n.get("id", "")), n.get("display_title", n.get("title", "")), n.get("xsec_token", ""), "pc_user", provenance=provenance, needs_triage=True)
            self._page(task, record, sid, [x.get("note_id", x.get("id", "")) for x in rows], not rows)
        elif op == "note":
            rows = collected_rows
            if not rows: raise legacy.ResearchError("Empty note is not a completed read")
            matches = []
            for item in rows:
                n = item.get("note_card")
                if not isinstance(n, dict): continue
                identifiers = {str(x) for x in (item.get("id"), n.get("note_id")) if x}
                if identifiers == {nid} and isinstance(n.get("desc"), str): matches.append(n)
            if len(matches) != 1 or len(rows) != 1:
                raise legacy.ResearchError("Expected exactly one note body matching the requested note ID")
            for n in matches:
                self.doc(sid, nid, "note", n.get("title", "") + "\n" + n.get("desc", ""), n, "note", nid, captured)
                self.register_note(nid, n.get("title", ""))
            task = self.add_task("note", nid, {"note_id": nid}, 30)
            self.db.execute("UPDATE tasks SET status='complete',reason='body_collected_not_reviewed' WHERE id=?", (task,))
        elif op == "comments":
            task = self.add_task("comments", nid, {"note_id": nid}, 15)
            rows = collected_rows
            for i, c in enumerate(rows):
                cid = c.get("id")
                rootkey = "id:" + cid if cid else "local:" + sid + ":" + str(i)
                self.doc(sid, nid, "root_comment", c.get("content", ""), c, "root:" + str(i), cid, captured)
                previous = self.db.execute("SELECT status,payload FROM threads WHERE note_id=? AND root_key=?", (nid, rootkey)).fetchone()
                prior = json.loads(previous["payload"]) if previous else {}
                if captured >= prior.get("_api_captured_at", ""):
                    before_ids = {x.get("id") for x in prior.get("sub_comments", []) if x.get("id")}
                    after_ids = {x.get("id") for x in c.get("sub_comments", []) if x.get("id")}
                    grew = int(c.get("sub_comment_count") or 0) > int(prior.get("sub_comment_count") or 0) or bool(after_ids - before_ids)
                    state = "pending" if not previous or grew else previous["status"]
                    self.db.execute("INSERT OR REPLACE INTO threads VALUES(?,?,?,?,?,?)", (nid, rootkey, cid or "", "platform_id" if cid else "snapshot_local", state, dumps({**prior, **c, "_api_captured_at": captured})))
                for j, reply in enumerate(c.get("sub_comments", [])):
                    meta = dict(reply, root_comment_id=cid)
                    self.doc(sid, nid, "reply", reply.get("content", ""), meta, "root:" + str(i) + ":reply:" + str(j), reply.get("id"), captured)
            self._page(task, record, sid, [x.get("id") or "local:" + sid + ":" + str(i) for i, x in enumerate(rows)], not rows)
            browser_task = self.add_task("browser", nid, {"note_id": nid}, 20)
            if self.db.execute("SELECT 1 FROM threads WHERE note_id=? AND status!='complete' LIMIT 1", (nid,)).fetchone():
                self.db.execute("UPDATE tasks SET status='pending',reason='known_roots_need_browser_review' WHERE id=? AND status='complete'", (browser_task,))
        elif op == "browser_comments":
            task = self.add_task("browser", nid, {"note_id": nid}, 20)
            if not record.get("ok"):
                self.db.execute("UPDATE tasks SET status='blocked',reason='browser_snapshot_failed' WHERE id=?", (task,))
            else:
                for i, row in enumerate(data):
                    rid = row.get("commentId") or row.get("comment_id")
                    self.doc(sid, nid, "reply" if row.get("is_reply") else "root_comment", row.get("text", ""), row, row.get("rank", i), rid, captured)
                cov = record.get("coverage", {})
                for thread in cov.get("threads", []):
                    cid = thread.get("commentId") or ""
                    key = "id:" + cid if cid else "browser:" + str(thread.get("rootKey", digest(thread)))
                    old = self.db.execute("SELECT status,payload FROM threads WHERE note_id=? AND root_key=?", (nid, key)).fetchone()
                    prior = json.loads(old["payload"]) if old else {}
                    state = thread.get("state", "pending")
                    done = state == "visible_replies_read" and not thread.get("identityAmbiguous", False) and not thread.get("expanders", 0)
                    if int(thread.get("visibleReplies") or 0) < int(prior.get("sub_comment_count") or 0): done = False
                    if prior.get("_api_captured_at", "") > captured and old and old["status"] != "complete": done = False
                    state = "complete" if done else "stalled" if state == "stalled" else "pending"
                    if old and old["status"] == "complete" and thread.get("state") in {"previously_read", "not_processed"}: state = "complete"
                    self.db.execute("INSERT OR REPLACE INTO threads VALUES(?,?,?,?,?,?)", (nid, key, cid, thread.get("identityKind", "uncertain"), state, dumps({**prior, **thread})))
                pending = self.db.execute("SELECT COUNT(*) FROM threads WHERE note_id=? AND status!='complete'", (nid,)).fetchone()[0]
                unprocessed = self.db.execute("SELECT COUNT(*) FROM threads WHERE note_id=? AND status='pending'", (nid,)).fetchone()[0]
                ended = cov.get("rootPaginationState") == "complete"
                status = "complete" if ended and not pending else "pending"
                reason = "visible_threads_processed" if status == "complete" else "remaining_roots_or_replies_not_established"
                if cov.get("rootPaginationState") == "stalled": status, reason = "blocked", "browser_scroll_stalled"
                elif ended and pending and not unprocessed: status, reason = "blocked", "browser_reply_threads_stalled"
                self.db.execute("UPDATE tasks SET status=?,reason=? WHERE id=?", (status, reason, task))
        try:
            from xhs_media import extract_media
        except ImportError:
            extract_media = None
        if extract_media and op in {"note", "comments", "browser_comments"}:
            for media in extract_media(record, str(target)):
                self.add_task("media", media["media_id"], media, 25)
        if self._review_changed and nid:
            kinds = {"note": ("note", "comments", "browser"), "comments": ("comments", "browser"), "browser_comments": ("comments", "browser")}.get(op, ())
            for kind in kinds:
                self.db.execute("UPDATE tasks SET review_status='pending' WHERE kind=? AND entity_key=? AND review_status='reviewed'", (kind, nid))
        if nid and op in {"comments", "browser_comments"}:
            self._invalidate_stale_comment_reviews(nid)
            if self._comment_media_gaps(nid):
                self.db.execute("UPDATE tasks SET review_status='pending' WHERE entity_key=? AND kind IN ('comments','browser') AND review_status='reviewed'", (nid,))
        return {"snapshot": sid, "operation": op}

    def _inline_comment_media_gaps(self, nid):
        """Compatibility alias: every comment media type now participates."""
        return self._comment_media_gaps(nid)

    def _comment_media_gaps(self, nid, *, image_file_cache=None):
        """Do not complete comments while any current/retained media is unread."""
        from xhs_media import extract_media, resource_identity, review_binding_gaps, image_review_valid
        from xhs_observed_review import acquisition_binding, canonical
        expected, uncertain = {}, []
        for doc in self.db.execute("SELECT id,metadata FROM documents WHERE note_id=? AND kind IN ('root_comment','reply')", (nid,)):
            row = json.loads(doc["metadata"])
            if not (row.get("commentId") or row.get("comment_id") or row.get("id")):
                if row.get("images") or row.get("pictures") or row.get("renderedInlineImages"):
                    # Do not invent a new media ID by extracting an anonymous
                    # row without its original source path/array position.
                    uncertain.append("source_bound_comment_media:" + doc["id"] + ":unknown_comment_identity")
                continue
            if "commentId" in row or "comment_id" in row or "renderedInlineImages" in row or "is_reply" in row:
                record = {"operation": "browser_comments", "note_id": nid, "data": [row]}
            else:
                # Replies already have their own current documents; an older
                # embedded reply must not replace a newer current media slot.
                record = {"operation": "comments", "note_id": nid,
                          "data": {"comments": [dict(row, sub_comments=[])]}}
            expected.update((item["media_id"], item) for item in extract_media(record))
        existing = {}
        for row in self.db.execute("SELECT entity_key,payload,status,review_status FROM tasks WHERE kind='media'"):
            item = json.loads(row["payload"])
            if item.get("note_id") == nid and str(item.get("locator", "")).startswith("comment/"):
                existing[row["entity_key"]] = (row, item)
        gaps = list(uncertain)
        manifests = {}
        for mid in sorted(expected.keys() | existing.keys()):
            if mid not in existing:
                gaps.append(mid + ":missing_media_task")
                continue
            row, item = existing[mid]
            if mid in expected and resource_identity(item) != resource_identity(expected[mid]):
                gaps.append(mid + ":current_comment_resource_differs")
                continue
            if row["status"] == "excluded" and row["review_status"] == "excluded":
                continue
            if row["status"] != "complete" or row["review_status"] != "reviewed":
                gaps.append(mid + ":" + row["status"] + "/" + row["review_status"])
                continue
            try:
                manifest_path = item.get("manifest")
                current = None
                if manifest_path:
                    if manifest_path not in manifests:
                        manifests[manifest_path] = load(Path(manifest_path))
                    manifest = manifests[manifest_path]
                    if not isinstance(manifest, dict) or not isinstance(manifest.get("items"), dict):
                        raise ValueError("invalid media manifest")
                    current = manifest.get("items", {}).get(mid, {})
                    if (not isinstance(current, dict) or manifest.get("format") != "xiaohongshu-media-v2" or current.get("review_status") != "reviewed" or
                            canonical(acquisition_binding(current)) != canonical(acquisition_binding(item))):
                        raise ValueError("current manifest binding differs")
                if (item.get("kind") not in {"image", "video", "subtitle"} or not item.get("observation") or
                        item.get("kind") == "image" and item.get("acquisition_status") != "downloaded" or
                        item.get("resource_fingerprint") != resource_identity(item) or
                        type(item.get("resource_version")) is not int or item["resource_version"] < 1):
                    raise ValueError("missing current resource review")
                file = Path(item.get("file") or "")
                if not file.is_absolute():
                    file = (Path(manifest_path).parent if manifest_path else self.path) / file
                if file.stat().st_size == 0:
                    raise ValueError("empty media file")
                if item.get("kind") == "image":
                    valid_file = image_review_valid(dict(item, file=str(file)), file_cache=image_file_cache)
                else:
                    hasher = hashlib.sha256()
                    with file.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            hasher.update(chunk)
                    valid_file = bool(item.get("sha256")) and hasher.hexdigest() == item["sha256"]
                if not valid_file:
                    raise ValueError("current file binding differs")
                if review_binding_gaps([dict(item, file=str(file), review_status="reviewed")], image_file_cache=image_file_cache):
                    raise ValueError("incomplete video or caption reading")
                if current is not None and (not current.get("observation") or
                        review_binding_gaps([dict(current, file=str(file), review_status="reviewed")], image_file_cache=image_file_cache)):
                    raise ValueError("current manifest reading is no longer complete")
            except (OSError, ValueError, TypeError, KeyError, legacy.ResearchError):
                gaps.append(mid + ":current_comment_media_review_binding_invalid")
        return gaps

    def _invalidate_stale_comment_reviews(self, nid):
        """Only reopen reviewed tasks whose exact saved comment receipt changed."""
        from xhs_observed_review import validate_receipt
        for task in self.db.execute("SELECT id FROM tasks WHERE entity_key=? AND kind IN ('comments','browser') AND review_status='reviewed'", (nid,)).fetchall():
            row = self.db.execute("SELECT annotations FROM reviews WHERE task_id=? AND decision='observed_scope' ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
            if row is None:
                continue
            try:
                validate_receipt(self.db, self.path, task["id"], json.loads(row["annotations"])["observed_scope_review"])
            except (legacy.ResearchError, ValueError, TypeError, KeyError):
                # Keep acquisition status and all historical review rows intact.
                self.db.execute("UPDATE tasks SET review_status='pending' WHERE id=?", (task["id"],))

    def _body_media_gaps(self, nid, *, existing_media=None, image_file_cache=None):
        """Require current body assets, including assets absent from old queues."""
        from xhs_media import extract_media, resource_identity, review_binding_gaps
        body = self.db.execute("SELECT metadata FROM documents WHERE id=? AND kind='note'", ("note:" + nid,)).fetchone()
        if body is None:
            return ["missing_saved_body"]
        note = json.loads(body["metadata"])
        expected = {x["media_id"]: x for x in extract_media(
            {"operation": "note", "note_id": nid, "data": {"items": [{"id": nid, "note_card": note}]}})}
        existing = existing_media
        if existing is None:
            existing = {}
            for row in self.db.execute("SELECT entity_key,payload,status,review_status FROM tasks WHERE kind='media'"):
                item = json.loads(row["payload"])
                if item.get("note_id") == nid and str(item.get("locator", "")).startswith("note/"):
                    existing[row["entity_key"]] = (row, item)
        gaps = []
        for mid in expected.keys() | existing.keys():
            if mid not in existing:
                gaps.append(mid + ":missing_media_task")
                continue
            row, item = existing[mid]
            if mid in expected and resource_identity(item) != resource_identity(expected[mid]):
                gaps.append(mid + ":current_body_resource_differs")
            elif not ((row["status"] == "complete" and row["review_status"] == "reviewed") or
                      (row["status"] == "excluded" and row["review_status"] == "excluded")):
                gaps.append(mid + ":body_media_not_reviewed")
            elif row["review_status"] == "reviewed" and review_binding_gaps([dict(item, review_status="reviewed")], image_file_cache=image_file_cache):
                gaps.append(mid + ":current_body_media_review_binding_invalid")
        return gaps

    def import_run(self, directory):
        directory = Path(directory).resolve()
        if (directory / "STOP.json").exists():
            try:
                from xhs_recovery import resolved_run_stop
                resolved = resolved_run_stop(self.db, directory)
            except ImportError:
                resolved = False
            # An archived, verified historical STOP must not re-block recovery;
            # unresolved imports must also never overwrite a newer active STOP.
            if not resolved and not self.get_meta("stop"):
                self.set_meta("stop", {"source": str(directory / "STOP.json"), "reason": "Imported run has unresolved STOP; do not resume through another session"})
        results = []
        files = list((directory / "raw").glob("*.json"))
        files.sort(key=lambda p: (load(p).get("captured_at", ""), load(p).get("page", 0), p.name))
        for file in files: results.append(self.ingest(file))
        # The catalog can hold an access context missing from an otherwise useful snapshot.
        for nid, n in legacy.read_json(directory / "catalog.json", {}).items():
            for c in n.get("contexts", []):
                self.register_note(nid, n.get("title", ""), c.get("token", ""), c.get("source", ""), c.get("url", ""), needs_triage=True)
        # Also repair an earlier import performed before catalog-only enrichment stopped
        # overwriting author triage. Explicit semantic decisions always take precedence.
        for row in self.db.execute("SELECT id,payload FROM tasks WHERE kind='note' AND status='pending'").fetchall():
            payload = json.loads(row["payload"])
            provenance = payload.get("provenance", [])
            if provenance and all(isinstance(x, dict) and x.get("operation") == "author_notes" for x in provenance):
                if not self.db.execute("SELECT 1 FROM reviews WHERE task_id=? LIMIT 1", (row["id"],)).fetchone():
                    payload["needs_triage"] = True
                    self.db.execute("UPDATE tasks SET payload=? WHERE id=?", (dumps(payload), row["id"]))
        self.db.execute("INSERT OR REPLACE INTO imports VALUES(?,?)", (str(directory), legacy.now()))
        self.db.commit()
        return {"imported_run": str(directory), "new_snapshots": sum(not r.get("cached") and "snapshot" in r for r in results), "stop": self.get_meta("stop")}

    def next(self, limit=10, automatic=False, kinds=None):
        if not 1 <= limit <= 1000: raise legacy.ResearchError("Choose a next-task limit between 1 and 1000")
        rows = self.db.execute("SELECT id FROM tasks WHERE status='pending' OR (status='complete' AND (review_status='pending' OR kind='native')) ORDER BY turn,priority,updated,id").fetchall()
        from xhs_native import coverage as native_coverage
        observation_audits = {g['task_id']: g for g in native_coverage(self)['invalid_review_bindings']
                              if g['reason'] in {'native_observation_correction_source_missing_or_changed', 'native_scope_migration_source_missing_or_changed'}}
        result = []
        for row in rows:
            task = self.task(row["id"])
            if task["historical"]: continue
            if kinds and task["kind"] not in kinds: continue
            if task["kind"] == "native" and task["status"] == "complete" and task["review_status"] == "reviewed":
                from xhs_native import valid_completion
                if valid_completion(self, task):
                    if task['id'] not in observation_audits: continue
                else:
                    task.update(stored_review_status="reviewed", review_status="pending",
                                reason="native_completion_source_missing_or_changed",
                                next_action="codex_offline_review", effective_report_only=True)
            if task['id'] in observation_audits:
                task.update(observation_audit_required=True,
                            reason=observation_audits[task['id']]['reason'],
                            next_action='codex_offline_review', effective_report_only=True)
            if automatic and (not task["automatable"] or task["status"] != "pending"): continue
            result.append(task)
            if len(result) >= limit: break
        return result

    def add(self, kind, key, payload, priority=50):
        if kind == "search":
            if not payload.get("query") or payload.get("sort") not in SORTS:
                raise legacy.ResearchError("Search payload requires query and supported sort")
            if payload.get("search_method_version", xhs_search.SEARCH_METHOD) != xhs_search.SEARCH_METHOD:
                raise legacy.ResearchError("New search tasks must use the current method; import old evidence separately")
            payload["search_method_version"] = xhs_search.SEARCH_METHOD
            key = xhs_search.task_key(payload["query"], payload["sort"])
            payload["api_sort"] = SORTS[payload["sort"]]
        elif kind in {"note", "comments", "browser"}:
            nid = payload.get("note_id")
            if not nid or nid != key:
                raise legacy.ResearchError("Task key must equal the observed note_id")
            self.register_note(nid, payload.get("title", ""), payload.get("token", ""), payload.get("source", ""),
                               payload.get("url", ""), payload.get("provenance"))
        elif kind == "author" and payload.get("user_id") != key:
            raise legacy.ResearchError("Author key must equal the observed user_id")
        elif kind == "media" and payload.get("media_id") != key:
            raise legacy.ResearchError("Media key must equal media_id")
        task = self.add_task(kind, key, payload, priority)
        if kind == "search": self._supersede_search(payload["query"], payload["sort"], task)
        self.db.commit()
        return {"task_id": task}

    def review(self, task_id, decision, summary, annotations=None):
        task = self.task(task_id)
        if task["kind"] == "native":
            raise legacy.ResearchError("Use xhs_native record with source-bound stage receipts")
        if not summary.strip(): raise legacy.ResearchError("Review requires an explicit rationale")
        annotations = annotations or {}
        if annotations.get("attribution") and annotations["attribution"] not in ATTRIBUTIONS:
            raise legacy.ResearchError("Unknown attribution class")
        if decision not in {"include", "relevant", "exclude", "reviewed", "blocked", "retry"}:
            raise legacy.ResearchError("Unknown review decision")
        if decision == "include" and (task["kind"] != "note" or task["status"] == "excluded"):
            raise legacy.ResearchError("Include is a note scope decision; explicitly reconsider excluded tasks before including")
        if task["kind"] == "media" and decision in {"reviewed", "relevant"}:
            raise legacy.ResearchError("Media review must use xhs_media review and import-media; generic review cannot bypass original-media checks")
        if decision == "reviewed" and task["status"] not in {"complete", "excluded"}:
            raise legacy.ResearchError("Cannot mark uncollected content reviewed; use relevant, blocked or explicit media results")
        if task["kind"] in {"comments", "browser"} and decision in {"reviewed", "relevant"} and task["status"] == "complete":
            gaps = self._comment_media_gaps(task["entity_key"])
            if gaps:
                raise legacy.ResearchError("Comment media remain unresolved: " + ", ".join(gaps))
        if task["kind"] == "note" and decision == "reviewed":
            text_only = annotations.get("scope") in {"full_saved_body_text_only", "body_text_only", "text_only"}
            if (text_only or annotations.get("media_and_comments_not_reviewed") is True
                    or annotations.get("all_body_media_actually_reviewed") is False
                    or annotations.get("media_reviewed") is False):
                raise legacy.ResearchError("Text-only reading cannot complete a note; use include/relevant and retain media gaps")
            gaps = self._body_media_gaps(task["entity_key"])
            if gaps:
                raise legacy.ResearchError("Note body media remain unresolved: " + ", ".join(gaps))
        if decision == "retry" and self.get_meta("stop"):
            raise legacy.ResearchError("STOP must not be cleared by retry")
        self.db.execute("INSERT INTO reviews(task_id,decision,summary,annotations,at) VALUES(?,?,?,?,?)", (task_id, decision, summary, dumps(annotations), legacy.now()))
        if decision == "include" or (decision == "relevant" and task["kind"] == "note"):
            # Research relevance can be known before images or the full body are
            # read. Queue discussion without falsely completing semantic review.
            payload = dict(task["payload"], needs_triage=False, scope_included=True)
            self.db.execute("UPDATE tasks SET payload=? WHERE id=?", (dumps(payload), task_id))
            nid = payload["note_id"]
            self.add_task("comments", nid, {"note_id": nid, "provenance": [{"scope_included_note": task_id}]}, 15)
            self.add_task("browser", nid, {"note_id": nid, "provenance": [{"scope_included_note": task_id}]}, 20)
        elif decision == "exclude":
            self.db.execute("UPDATE tasks SET status='excluded',review_status='excluded',reason=? WHERE id=?", (summary, task_id))
        elif decision == "blocked":
            self.db.execute("UPDATE tasks SET status='blocked',reason=? WHERE id=?", (summary, task_id))
        elif decision == "retry":
            self.db.execute("UPDATE tasks SET status='pending',reason=? WHERE id=?", (summary, task_id))
            if task["kind"] == "browser":
                payload = task["payload"]
                payload["retry_stalled_once"] = True
                self.db.execute("UPDATE tasks SET payload=? WHERE id=?", (dumps(payload), task_id))
        else:
            payload = task["payload"]
            payload["needs_triage"] = False
            review_state = "reviewed" if task["status"] == "complete" else "pending"
            self.db.execute("UPDATE tasks SET payload=?,review_status=? WHERE id=?", (dumps(payload), review_state, task_id))
            if task["kind"] == "note":
                nid = payload["note_id"]
                self.add_task("comments", nid, {"note_id": nid, "provenance": [{"reviewed_note": task_id}]}, 15)
                self.add_task("browser", nid, {"note_id": nid}, 20)
        self.db.commit()
        return self.task(task_id)

    def observed_review_plan(self, task_id):
        from xhs_observed_review import current_scope
        return current_scope(self.db, self.path, task_id)

    def observed_review(self, task_id, receipt, summary):
        from xhs_observed_review import validate_receipt
        if not isinstance(summary, str) or not summary.strip():
            raise legacy.ResearchError("Observed review requires an explicit rationale")
        result = validate_receipt(self.db, self.path, task_id, receipt)
        annotations = dumps({"observed_scope_review": receipt})
        prior = self.db.execute("SELECT id,summary,annotations FROM reviews WHERE task_id=? AND decision='observed_scope' ORDER BY id DESC LIMIT 1",
                                (task_id,)).fetchone()
        if prior and prior["summary"] == summary and prior["annotations"] == annotations:
            return {"task_id": task_id, "review_id": prior[0], "idempotent": True, **result}
        cursor = self.db.execute("INSERT INTO reviews(task_id,decision,summary,annotations,at) VALUES(?,?,?,?,?)",
                                 (task_id, "observed_scope", summary, annotations, legacy.now()))
        self.db.commit()
        return {"task_id": task_id, "review_id": cursor.lastrowid, "idempotent": False, **result}

    def evidence(self, value):
        if value.get("attribution") not in ATTRIBUTIONS or not value.get("summary") or not value.get("sources"):
            raise legacy.ResearchError("Evidence needs attribution, summary and non-empty source references")
        for source in value["sources"]:
            if not isinstance(source, dict) or not (source.get("snapshot_id") or source.get("document_id")):
                raise legacy.ResearchError("Sources must reference ingested snapshots or documents")
            for key, table in (("snapshot_id", "snapshots"), ("document_id", "documents")):
                if source.get(key) and not self.db.execute("SELECT 1 FROM " + table + " WHERE id=?", (source[key],)).fetchone():
                    raise legacy.ResearchError("Evidence references unknown source")
        if value["attribution"] == "context_supported" and not value.get("missing_confirmation"):
            raise legacy.ResearchError("Context-supported attribution must state the missing confirmation")
        eid = value.get("id") or digest(value)[:24]
        self.db.execute("INSERT OR REPLACE INTO evidence VALUES(?,?,?)", (eid, dumps(value), legacy.now()))
        self.db.commit()
        return {"evidence_id": eid}

    def link_evidence(self, earlier, later, relation, summary):
        """Append a qualified update without overwriting either observation."""
        if relation not in {"resolves_gap", "updates", "contradicts"} or not summary.strip() or earlier == later:
            raise legacy.ResearchError("Evidence links need two distinct records, a supported relation and a rationale")
        payloads = []
        for eid in (earlier, later):
            row = self.db.execute("SELECT payload FROM evidence WHERE id=?", (eid,)).fetchone()
            if not row:
                raise legacy.ResearchError("Evidence link references unknown evidence")
            payloads.append(row[0])
        hashes = [digest(x.encode()) for x in payloads]
        lid = digest([earlier, later, relation, summary, hashes])[:24]
        self.db.execute("INSERT OR IGNORE INTO evidence_links VALUES(?,?,?,?,?,?,?,?)",
                        (lid, earlier, later, relation, summary, *hashes, legacy.now()))
        self.db.commit()
        return {"evidence_link_id": lid}

    def search_local(self, query, limit=50):
        # Literal substring works for Chinese names and two-character abbreviations without a tokenizer.
        rows = self.db.execute("SELECT * FROM documents WHERE instr(lower(text),lower(?))>0 ORDER BY captured_at DESC LIMIT ?", (query, limit))
        return [dict(r, metadata=json.loads(r["metadata"])) for r in rows]

    def _media_scope_exclusion(self, task_id):
        # A later retry/relevant decision supersedes the earlier scope exclusion.
        latest = self.db.execute("SELECT decision,summary FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        return latest if latest and latest["decision"] == "exclude" else None

    def _legacy_media_identity(self, expected, incoming):
        """Recover omitted identity fields only from the exact immutable snapshot."""
        if (expected.get("resource_id_hint") or expected.get("resource_fingerprint")
                or not incoming.get("resource_id_hint")
                or expected.get("acquisition_status") != "pending" or expected.get("review_status") != "pending"
                or any(expected.get(k) for k in ("file", "transcript", "download_attempted_at", "reason", "observation"))):
            return None
        try:
            source = Path(expected["source_path"]).resolve()
            if source.parent != self.path / "snapshots" or source != Path(incoming["source_path"]).resolve():
                return None
            snapshot = self.db.execute("SELECT id,captured_at FROM snapshots WHERE path=?",
                                       (str(source.relative_to(self.path)),)).fetchone()
            if not snapshot:
                return None
            raw = source.read_bytes()
            if digest(raw) != snapshot["id"]:
                return None
            from xhs_media import extract_media, resource_identity
            matches = [x for x in extract_media(json.loads(raw), str(source)) if x["media_id"] == expected.get("media_id")]
            if len(matches) != 1:
                return None
            observed = matches[0]
            fields = ("media_id", "kind", "note_id", "comment_id", "locator", "captured_at", "observed_url", "inline_caption")
            if any(value.get(k) != observed.get(k) for value in (expected, incoming) for k in fields):
                return None
            if not observed.get("resource_id_hint") or resource_identity(observed) != resource_identity(incoming):
                return None
            enriched = dict(expected)
            for key in ("resource_id_hint", "resource_fingerprint", "caption_resource_fingerprints"):
                if key in observed:
                    enriched[key] = observed[key]
            enriched.setdefault("resource_version", observed["resource_version"])
            enriched["identity_reconciled_from_snapshot"] = snapshot["id"]
            return enriched
        except (KeyError, OSError, ValueError, TypeError):
            return None

    def import_media(self, directory):
        manifest_path = Path(directory).resolve() / "manifest.json"
        manifest = load(manifest_path)
        if manifest.get("format") != "xiaohongshu-media-v2":
            raise legacy.ResearchError("Expected a v2 media manifest")
        count, stale, reconciled = 0, [], 0
        comment_notes = set()
        for mid, item in manifest.get("items", {}).items():
            if item.get("note_id") and str(item.get("locator", "")).startswith("comment/"):
                comment_notes.add(item["note_id"])
            existing = self.db.execute("SELECT id,payload,status,review_status,reason FROM tasks WHERE kind='media' AND entity_key=?", (mid,)).fetchone()
            if existing:
                from xhs_media import resource_identity
                expected = json.loads(existing["payload"])
                if resource_identity(expected) != resource_identity(item):
                    may_enrich = existing["status"] == existing["review_status"] and existing["status"] in {"pending", "excluded"}
                    enriched = self._legacy_media_identity(expected, item) if may_enrich else None
                    if enriched is None:
                        stale.append({"media_id": mid, "reason": "manifest_resource_differs_from_current_source", "manifest": str(manifest_path)})
                        continue
                    self.db.execute("UPDATE tasks SET payload=? WHERE id=?", (dumps(enriched), existing["id"]))
                    reconciled += 1
            task_id = self.add_task("media", mid, dict(item, manifest=str(manifest_path)), 25)
            acquired = item.get("acquisition_status")
            reviewed = item.get("review_status", "pending")
            state = "complete" if acquired in {"downloaded", "caption_available", "transcribed"} else "pending"
            semantic = "reviewed" if reviewed == "reviewed" else "pending"
            reason = "media_" + str(acquired)
            artifact_paths = []
            for field in ("file", "transcript"):
                if item.get(field):
                    artifact = Path(item[field])
                    if not artifact.is_absolute(): artifact = Path(directory) / artifact
                    artifact_paths.append(artifact)
            has_artifact = any(p.is_file() for p in artifact_paths)
            if state == "complete" and not has_artifact:
                state, semantic, reason = "blocked", "pending", "media_artifact_missing"
            if reviewed == "reviewed":
                if item.get("kind") == "subtitle":
                    from xhs_media import caption_text_review_valid
                    complete_media_check = caption_text_review_valid(item)
                else:
                    from xhs_media import video_full_review_valid
                    check_item = dict(item)
                    if check_item.get("file") and not Path(check_item["file"]).is_absolute():
                        check_item["file"] = str(Path(directory) / check_item["file"])
                    complete_media_check = item.get("kind") != "video" or video_full_review_valid(check_item)
                if not has_artifact or not item.get("observation") or not complete_media_check:
                    state, semantic, reason = "blocked", "pending", "media_review_requirements_missing"
            if reviewed == "irrelevant": state, semantic = "excluded", "excluded"
            elif reviewed == "unreadable" or acquired in {"unsupported", "blocked", "unavailable"}:
                state, reason = "blocked", "media_unreadable_or_unavailable"
            elif item.get("transcription_status") == "blocked":
                state, reason = "blocked", "media_transcription_unavailable"
            exclusion = self._media_scope_exclusion(task_id)
            if exclusion:
                state, semantic, reason = "excluded", "excluded", exclusion["summary"]
            elif reviewed == "pending" and existing and existing["status"] == existing["review_status"] == "excluded":
                # Same-resource pending manifests do not erase a prior irrelevant
                # media result. A resource change was already rejected above.
                latest = self.db.execute("SELECT decision FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
                if latest is None:
                    state, semantic, reason = "excluded", "excluded", existing["reason"]
            self.db.execute("UPDATE tasks SET status=?,review_status=?,reason=? WHERE id=?", (state, semantic, reason, task_id))
            text = item.get("observation", "")
            transcript = item.get("transcript")
            if transcript:
                transcript_path = Path(transcript)
                if not transcript_path.is_absolute(): transcript_path = Path(directory) / transcript_path
                if transcript_path.is_file():
                    parsed = load(transcript_path)
                    segments = parsed.get("segments", []) if isinstance(parsed, dict) else parsed
                    text += "\n" + "\n".join(str(s.get("text", "")) for s in segments)
            if text:
                try:
                    from xhs_media import resource_identity
                    version = resource_identity(item)
                except ImportError:
                    version = digest([item.get("observed_url"), item.get("sha256")])
                key = "media:" + mid + ":" + version
                self.db.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?)", (key, item.get("note_id", ""), "media", text,
                                dumps(dict(item, manifest=str(manifest_path))), "media_locator", legacy.now()))
            count += 1
        for nid in comment_notes:
            if self._comment_media_gaps(nid):
                self.db.execute("UPDATE tasks SET review_status='pending' WHERE entity_key=? AND kind IN ('comments','browser') AND review_status='reviewed'", (nid,))
        self.db.commit()
        return {"media_items": count, "manifest": str(manifest_path), "stale_manifest_items": stale,
                "reconciled_legacy_media_items": reconciled}

    def status(self):
        from xhs_observed_review import observed_review_status
        active = ("NOT (kind='search' AND json_extract(payload,'$.sort') IN ('latest','popular') "
                  "AND COALESCE(json_extract(payload,'$.search_method_version'),'" + xhs_search.LEGACY_SEARCH_METHOD + "')='" + xhs_search.LEGACY_SEARCH_METHOD + "' "
                  "AND COALESCE(json_extract(payload,'$.superseded_by'),'')!='')")
        grouped = [dict(r) for r in self.db.execute("SELECT kind,status,review_status,COUNT(*) AS count FROM tasks WHERE " + active + " GROUP BY kind,status,review_status ORDER BY kind,status")]
        outstanding = self.db.execute("SELECT COUNT(*) FROM tasks WHERE (status IN ('pending','running','blocked') OR (review_status='pending' AND status!='excluded')) AND " + active).fetchone()[0]
        historical = [self.task(r[0]) for r in self.db.execute("SELECT id FROM tasks WHERE NOT (" + active + ") ORDER BY id")]
        logical_searches = {}
        for row in self.db.execute("SELECT id FROM tasks WHERE kind='search' ORDER BY id"):
            task = self.task(row[0]); payload = task["payload"]
            key = (payload["query"], payload["sort"])
            group = logical_searches.setdefault(key, {"query": key[0], "sort": key[1], "methods": []})
            group["methods"].append({"task_id": task["id"], "task_method_version": xhs_search.method_of(payload),
                                     "status": task["status"], "next_page": task["next_page"], "historical": task["historical"],
                                     "superseded_by": payload.get("superseded_by"),
                                     "collected_page_snapshots": self.db.execute("SELECT COUNT(*) FROM pages WHERE task_id=?", (task["id"],)).fetchone()[0]})
        search_coverage = {"logical_combinations": len(logical_searches), "grouping": "query+sort",
                           "method_task_count": sum(len(v["methods"]) for v in logical_searches.values()),
                           "combinations": list(logical_searches.values()), "no_platform_recall_estimate": True}
        from xhs_media import review_scope_counts, review_binding_gaps
        media_rows = self.db.execute("SELECT id,entity_key,payload,status,review_status FROM tasks WHERE kind='media'").fetchall()
        media_items = [dict(json.loads(row["payload"]), review_status=row["review_status"])
                       for row in media_rows if row["status"] != "excluded"]
        image_file_cache = {}
        invalid = {gap["media_id"]: gap for gap in review_binding_gaps(media_items, image_file_cache=image_file_cache)}
        invalid_review_bindings = []
        effective_counts = {(row["kind"], row["status"], row["review_status"]): row["count"] for row in grouped}
        comment_checks, comment_cache = [], {}
        media_by_id = {row["entity_key"]: row for row in media_rows}
        reviewed_comments = self.db.execute("SELECT id,kind,entity_key,status,review_status FROM tasks WHERE kind IN ('comments','browser') AND review_status='reviewed' AND status!='excluded'").fetchall()
        comment_notes = {row["entity_key"] for row in reviewed_comments}
        body_media_by_note = {}
        for row in media_rows:
            item = json.loads(row["payload"])
            if item.get('note_id') and str(item.get('locator', '')).startswith('note/'):
                body_media_by_note.setdefault(item['note_id'], {})[row['entity_key']] = (row, item)
            if row["status"] != "excluded" and item.get("note_id") and str(item.get("locator", "")).startswith("comment/"):
                comment_notes.add(item["note_id"])
        for nid in comment_notes:
            gaps = comment_cache[nid] = self._comment_media_gaps(nid, image_file_cache=image_file_cache)
            for gap in gaps:
                mid = gap.split(":", 1)[0]
                media_row = media_by_id.get(mid)
                if media_row and media_row["review_status"] == "reviewed" and media_row["status"] != "excluded":
                    invalid.setdefault(mid, {"media_id": mid, "kind": json.loads(media_row["payload"]).get("kind"),
                                            "reason": "current_comment_media_review_binding_invalid"})
        for row in reviewed_comments:
            if comment_cache[row["entity_key"]]:
                comment_checks.append((row, comment_cache[row["entity_key"]]))
        for row in media_rows:
            if row["entity_key"] not in invalid or row["status"] == "excluded": continue
            invalid_review_bindings.append(dict(invalid[row["entity_key"]], task_id=row["id"],
                stored_status=row["status"], stored_review_status=row["review_status"]))
            if row["status"] not in {"pending", "running", "blocked"}: outstanding += 1
            before, after = ("media", row["status"], row["review_status"]), ("media", row["status"], "pending")
            effective_counts[before] -= 1
            effective_counts[after] = effective_counts.get(after, 0) + 1
        for row, gaps in comment_checks:
            invalid_review_bindings.append({"task_id": row["id"], "kind": row["kind"],
                "stored_status": row["status"], "stored_review_status": row["review_status"],
                "reason": "current_comment_media_unresolved", "media_gaps": gaps})
            if row["status"] not in {"pending", "running", "blocked"}:
                outstanding += 1
            before, after = (row["kind"], row["status"], row["review_status"]), (row["kind"], row["status"], "pending")
            effective_counts[before] -= 1
            effective_counts[after] = effective_counts.get(after, 0) + 1
        # Older imports could mark saved text reviewed while body images were
        # still pending. Report effective scope without rewriting that history.
        for row in self.db.execute("SELECT id,kind,entity_key,status,review_status FROM tasks WHERE kind='note' AND review_status='reviewed' AND status!='excluded'"):
            gaps = self._body_media_gaps(row['entity_key'], existing_media=body_media_by_note.get(row['entity_key'], {}), image_file_cache=image_file_cache)
            if not gaps:
                continue
            invalid_review_bindings.append({'task_id': row['id'], 'kind': 'note',
                'stored_status': row['status'], 'stored_review_status': row['review_status'],
                'reason': 'current_body_media_unresolved', 'media_gaps': gaps})
            if row['status'] not in {'pending', 'running', 'blocked'}:
                outstanding += 1
            before, after = ('note', row['status'], row['review_status']), ('note', row['status'], 'pending')
            effective_counts[before] -= 1
            effective_counts[after] = effective_counts.get(after, 0) + 1
        from xhs_native import coverage as native_coverage
        native = native_coverage(self)
        adjusted_native = set()
        for gap in native["invalid_review_bindings"]:
            invalid_review_bindings.append(gap)
            if gap['reason'] != 'native_completion_source_missing_or_changed' or gap['task_id'] in adjusted_native:
                continue
            row = self.db.execute('SELECT status,review_status FROM tasks WHERE id=?', (gap['task_id'],)).fetchone()
            if not row or row['status'] != 'complete' or row['review_status'] != 'reviewed':
                continue
            adjusted_native.add(gap['task_id'])
            outstanding += 1
            before, after = ("native", "complete", "reviewed"), ("native", "complete", "pending")
            effective_counts[before] -= 1
            effective_counts[after] = effective_counts.get(after, 0) + 1
        grouped = [dict(kind=k[0], status=k[1], review_status=k[2], count=count)
                   for k, count in sorted(effective_counts.items()) if count]
        # Exclusions are still reported separately, never as unfinished reading.
        media_items += [dict(json.loads(row["payload"]), review_status="excluded")
                        for row in media_rows if row["status"] == "excluded"]
        observation_audits = {g['task_id'] for g in native['invalid_review_bindings']
                              if g['reason'] in {'native_observation_correction_source_missing_or_changed', 'native_scope_migration_source_missing_or_changed'}}
        from xhs_bridge import summary as bridge_summary
        return {"project": str(self.path), "target": self.get_meta("config", {}).get("target"), "state": "work_remaining" if outstanding or observation_audits else "defined_visible_scope_processed",
                "task_counts": grouped, "outstanding_tasks": outstanding, "historical_search_tasks": historical,
                "outstanding_observation_audits": len(observation_audits),
                "search_coverage": search_coverage,
                "native_coverage": native,
                "bridge": bridge_summary(self),
                "media_review_scopes": review_scope_counts(media_items, set(invalid)),
                "invalid_review_bindings": invalid_review_bindings, "counts_use_current_media_review_validity": True,
                "snapshots": self.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
                "documents": [dict(r) for r in self.db.execute("SELECT kind,identity_kind,COUNT(*) AS count FROM documents GROUP BY kind,identity_kind")],
                "threads": [dict(r) for r in self.db.execute("SELECT status,identity_kind,COUNT(*) AS count FROM threads GROUP BY status,identity_kind")],
                "stop": self.get_meta("stop"), "no_platform_recall_estimate": True,
                "counts_are_known_tasks_not_all_platform_content": True,
                "observed_scope_reviews": observed_review_status(self.db, self.path)}

    def report(self):
        report = self.status()
        report["remaining"] = [self.task(r[0]) for r in self.db.execute("SELECT id FROM tasks WHERE status IN ('pending','running','blocked') OR (review_status='pending' AND status!='excluded') ORDER BY kind,id")]
        report["remaining"] = [t for t in report["remaining"] if not t["historical"]]
        remaining = {task["id"]: task for task in report["remaining"]}
        for gap in report["invalid_review_bindings"]:
            task = remaining.get(gap["task_id"]) or self.task(gap["task_id"])
            if gap['reason'] in {'native_observation_correction_source_missing_or_changed', 'native_scope_migration_source_missing_or_changed'}:
                task.update(observation_audit_required=True, reason=gap['reason'],
                            next_action='codex_offline_review', effective_report_only=True)
            else:
                task.update(stored_review_status=task["review_status"], review_status="pending",
                            reason=gap["reason"], next_action="review", effective_report_only=True)
            remaining[task["id"]] = task
        report["remaining"] = sorted(remaining.values(), key=lambda task: (task["kind"], task["id"]))
        report["evidence"] = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM evidence ORDER BY at,id")]
        report["evidence_links"] = []
        for row in self.db.execute("SELECT * FROM evidence_links ORDER BY at,id"):
            link = dict(row)
            current = [self.db.execute("SELECT payload FROM evidence WHERE id=?", (link[key],)).fetchone() for key in ("earlier", "later")]
            link["current_sources_match"] = all(value and digest(value[0].encode()) == link[key + "_hash"] for value, key in zip(current, ("earlier", "later")))
            report["evidence_links"].append(link)
        report["imports"] = [dict(r) for r in self.db.execute("SELECT * FROM imports")]
        legacy.write_json(self.path / "coverage-report.json", report)
        lines = ["小红书项目覆盖报告", "目标：" + str(report["target"]), "状态：" + report["state"],
                 "本报告统计已发现任务，不代表全站召回率；采集与语义核查分别计算。", "", "已知任务："]
        lines.append("搜索逻辑组合(query+sort)：" + str(report["search_coverage"]["logical_combinations"]) + "；方法任务数(含历史)：" + str(report["search_coverage"]["method_task_count"]))
        lines.append("观察纠正或范围迁移来源待复核范围：" + str(report['outstanding_observation_audits']) + "（单列审计，不扣减已完成内容）")
        for row in report["task_counts"]: lines.append(f"{row['kind']}: {row['status']} / review={row['review_status']} : {row['count']}")
        for stage, counts in report["native_coverage"]["stages"].items():
            lines.append(f"原生UI范围 {stage}: {dumps(counts)}（范围数，不是评论/患者数）")
        lines.append("原生网页恢复范围：" + dumps(report["native_coverage"].get("native_web_recovery", {})))
        lines.append("当前原生操作通道：" + dumps(report["native_coverage"].get("channel_health", {})) + "；网页恢复不解除API或媒体STOP。")
        lines.append("固定配置连接与采集/审读状态：" + dumps(report["bridge"]))
        scopes = report["media_review_scopes"]
        lines.append(f"字幕全文已读：{scopes['caption_text_reviewed']}；字幕文字待阅：{scopes['caption_text_not_reviewed']}；已排除：{scopes['caption_excluded']}。字幕阅读不证明原声已核验（{scopes['caption_reading_does_not_verify_original_audio']} 项文字阅读）。")
        lines.append(f"完整视频已核验：{scopes['video_fully_reviewed']}；视频未完整核验：{scopes['video_not_fully_reviewed']}；已排除：{scopes['video_excluded']}。字幕已读不改变视频或正文的完成状态。")
        lines.extend(["", "剩余任务（详见 coverage-report.json）："])
        for task in report["remaining"]: lines.append(f"{task['id']} | {task['entity_key']} | {task['status']} | review={task['review_status']} | next_page={task['next_page']} | {task['reason']}")
        lines.extend(["", "已被新搜索方法替代的历史任务（保留原状态和已采页，不计入待办）："])
        for task in report["historical_search_tasks"]: lines.append(f"{task['id']} | original_status={task['status']} | next_page={task['next_page']} | superseded_by={task['payload']['superseded_by']}")
        lines.extend(["", "已采集范围人工审读（不改变任务完成状态或剩余数；current仅对已冻结原文范围有效）："])
        for review in report["observed_scope_reviews"]: lines.append(dumps(review))
        lines.extend(["", "经人工记录的结论："])
        for ev in report["evidence"]: lines.append(dumps(ev))
        lines.extend(["", "证据后续关联（仅限定说明中的缺口；不删除旧观察，也不自动改变任务完成状态）："])
        for link in report["evidence_links"]: lines.append(dumps(link))
        (self.path / "coverage-report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"json": str(self.path / "coverage-report.json"), "text": str(self.path / "coverage-report.txt"), "outstanding_tasks": report["outstanding_tasks"]}

    def _register_run_context(self, run, nid):
        row = self.db.execute("SELECT * FROM notes WHERE note_id=?", (nid,)).fetchone()
        if not row: raise legacy.ResearchError("No observed note access context")
        contexts = json.loads(row["contexts"])
        if not contexts: raise legacy.ResearchError("No observed note token/source; discover a public source first")
        c = contexts[-1]
        legacy.register(run, nid, c["token"], c["source"], row["title"], {"project": str(self.path)})
        return c

    def collect_one(self, task_id, run):
        if self.get_meta("stop"):
            raise legacy.StopRun("Project has a preserved STOP; do not switch sessions to continue")
        legacy.ensure_active(run)
        task = self.task(task_id)
        if task["status"] != "pending" or not task["automatable"]:
            raise legacy.ResearchError("Task is not pending automatic collection")
        payload, kind = task["payload"], task["kind"]
        if kind == "search" and not xhs_search.collection_allowed(task):
            raise legacy.ResearchError("Search method changed; explicitly add this query/sort to start a separate latest/popular page chain")
        if kind == "browser":
            import xhs_browser
            nid = payload["note_id"]
            self._register_run_context(run, nid)
            progress_path = self.path / "browser-progress" / (nid + ".json")
            progress = legacy.read_json(progress_path, {})
            progress["expectedRootIds"] = [r[0] for r in self.db.execute("SELECT comment_id FROM threads WHERE note_id=? AND comment_id!=''", (nid,))]
            comment_task = self.db.execute("SELECT status FROM tasks WHERE kind='comments' AND entity_key=?", (nid,)).fetchone()
            progress["apiRootsComplete"] = bool(comment_task and comment_task[0] == "complete")
            progress["retryStalled"] = payload.pop("retry_stalled_once", False)
            self.db.execute("UPDATE tasks SET payload=? WHERE id=?", (dumps(payload), task_id))
            progress["expectedReplyCounts"] = {}
            for root in self.db.execute("SELECT comment_id,payload,status FROM threads WHERE note_id=? AND comment_id!=''", (nid,)):
                entry = json.loads(root["payload"])
                if entry.get("sub_comment_count") is not None:
                    progress["expectedReplyCounts"][root["comment_id"]] = int(entry["sub_comment_count"])
                previous_thread = progress.get("threads", {}).get("id:" + root["comment_id"])
                if root["status"] == "pending" and previous_thread and previous_thread.get("state") == "visible_replies_read":
                    previous_thread["state"] = "not_processed"
            legacy.write_json(progress_path, progress)
            result = xhs_browser.deep_comments(run, nid, 100, 60, 90, resume=True, progress_path=progress_path)
            file = Path(result["file"])
        else:
            record = {"captured_at": legacy.now(), "project_task": task_id, "page": task["next_page"]}
            with legacy.client(run) as api:
                if kind == "search":
                    record.update(operation="search", query=payload["query"], sort=payload["sort"], api_sort=SORTS[payload["sort"]])
                    data = api.search_notes(payload["query"], page=task["next_page"], sort=record["api_sort"])
                    try:
                        record.update(xhs_search.collection_metadata(api, payload["query"], task["next_page"], record["api_sort"]))
                    except ValueError as exc:
                        raise legacy.ResearchError(str(exc)) from None
                elif kind == "author":
                    record.update(operation="author_notes", user_id=payload["user_id"], cursor_in=task["cursor"])
                    data = api.get_user_notes(payload["user_id"], cursor=task["cursor"])
                else:
                    nid = payload["note_id"]
                    context = self._register_run_context(run, nid)
                    record.update(note_id=nid, url=context["url"])
                    if kind == "note":
                        record["operation"] = "note"
                        data = api.get_note_by_id(nid, xsec_token=context["token"], xsec_source=context["source"])
                    else:
                        record.update(operation="comments", cursor_in=task["cursor"])
                        data = api.get_comments(nid, cursor=task["cursor"], xsec_token=context["token"], xsec_source=context["source"])
            record["data"] = legacy.clean_data(data)
            try:
                rows = collection_rows(record["operation"], data)
                if kind == "note" and not rows:
                    raise legacy.ResearchError("Empty note response is not a successful body read")
            except legacy.ResearchError as exc:
                # Keep the original response as evidence without replaying a
                # rejected collection attempt over a later successful retry.
                record["collection_error"] = {"code": type(exc).__name__, "message": str(exc)}
                diagnostic = run / "raw" / ("diagnostic-project-" + task_id + "-" + uuid.uuid4().hex + ".json")
                legacy.write_json(diagnostic, record)
                raise
            file = run / "raw" / ("project-" + task_id + "-" + uuid.uuid4().hex + ".json")
            legacy.write_json(file, record)
        result = self.ingest(file)
        turn = int(self.get_meta("turn", 0)) + 1
        self.set_meta("turn", turn)
        self.db.execute("UPDATE tasks SET turn=?,updated=? WHERE id=?", (turn, legacy.now(), task_id))
        self.db.commit()
        return {"task": self.task(task_id), "snapshot": result, "file": str(file)}

    def resume(self, run, steps=1, task_id=None, kinds=None):
        if not 1 <= steps <= 100: raise legacy.ResearchError("Choose 1..100 bounded work units; resume again for more")
        if self.get_meta("stop"):
            raise legacy.StopRun("Project has a preserved STOP; do not switch sessions to continue")
        legacy.ensure_active(run)
        # Browser progress can commit before the project DB. Recover its immutable sources
        # before a resumed browser skips threads it has already expanded.
        for progress_file in sorted((self.path / "browser-progress").glob("*.json")):
            progress = load(progress_file)
            for source in progress.get("snapshots", []):
                source_path = Path(source)
                if source_path.is_file():
                    record = load(source_path)
                    if record.get("operation") == "browser_comments" and record.get("note_id") == progress_file.stem:
                        self.ingest(source_path)
        # Recover immutable responses written before an interruption committed queue state.
        for file in sorted((run / "raw").glob("project-*.json")):
            record = load(file)
            if record.get("project_task") and self.db.execute("SELECT 1 FROM tasks WHERE id=?", (record["project_task"],)).fetchone():
                try:
                    self.ingest(file)
                except legacy.ResearchError as exc:
                    self.db.execute("UPDATE tasks SET status='blocked',reason=? WHERE id=?", ("interrupted_snapshot_invalid: " + str(exc), record["project_task"]))
        self.db.commit()
        results = []
        for _ in range(steps):
            choices = [self.task(task_id)] if task_id else self.next(1, automatic=True, kinds=kinds)
            if not choices or choices[0]["status"] != "pending": break
            chosen = choices[0]
            try:
                results.append(self.collect_one(chosen["id"], run))
            except legacy.StopRun as exc:
                self.set_meta("stop", {"at": legacy.now(), "run": str(run), "reason": str(exc)})
                self.db.execute("UPDATE tasks SET status='blocked',reason='access_stop' WHERE id=?", (chosen["id"],))
                self.db.commit()
                raise
            except legacy.TransientTransportError as exc:
                # Preserve typed evidence. Old free-form errors are deliberately
                # not promoted into permission for an automatic retry.
                failure = {**exc.safe_details, "task_id": chosen["id"],
                           "failure_id": uuid.uuid4().hex, "run": str(Path(run).resolve()),
                           "at": legacy.now(), "reason": str(exc),
                           "request_key": digest([chosen[k] for k in
                               ("id", "kind", "entity_key", "next_page", "cursor", "payload")])}
                self.set_meta("transient_failure:" + chosen["id"], failure)
                self.db.execute("UPDATE tasks SET status='blocked',reason=? WHERE id=?", (str(exc), chosen["id"]))
                self.db.commit()
                results.append({"task_id": chosen["id"], "blocked": str(exc),
                                "transient_transport": failure})
                break
            except (legacy.ResearchError, OSError, ValueError) as exc:
                self.db.execute("UPDATE tasks SET status='blocked',reason=? WHERE id=?", (str(exc), chosen["id"]))
                self.db.commit()
                results.append({"task_id": chosen["id"], "blocked": str(exc)})
                break
        return {"processed_units": len(results), "results": results, "remaining": self.status()["outstanding_tasks"]}


@contextmanager
def project_lock(path):
    import fcntl
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (path / ".project.lock").open("a") as stream:
        try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise legacy.CollectionBusy("Another process owns this project") from None
        try: yield
        finally: fcntl.flock(stream, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan"); p.add_argument("--config", required=True, type=Path)
    p = sub.add_parser("import"); p.add_argument("run", type=Path)
    p = sub.add_parser("ingest"); p.add_argument("file", type=Path)
    p = sub.add_parser("import-media"); p.add_argument("directory", type=Path)
    p = sub.add_parser("next"); p.add_argument("--limit", type=int, default=10); p.add_argument("--kind", action="append"); p.add_argument("--automatic", action="store_true")
    p = sub.add_parser("resume"); p.add_argument("--run", required=True, type=Path); p.add_argument("--steps", type=int, default=1); p.add_argument("--task"); p.add_argument("--kind", action="append")
    sub.add_parser("status"); sub.add_parser("report")
    p = sub.add_parser("add"); p.add_argument("kind", choices=["search", "note", "comments", "browser", "author", "media"]); p.add_argument("--key", required=True); p.add_argument("--payload", required=True, type=Path); p.add_argument("--priority", type=int, default=50)
    p = sub.add_parser("review"); p.add_argument("--task", required=True); p.add_argument("--decision", required=True, choices=["include", "relevant", "exclude", "reviewed", "blocked", "retry"]); p.add_argument("--summary", required=True); p.add_argument("--annotations", type=Path)
    p = sub.add_parser("observed-review-plan"); p.add_argument("--task", required=True)
    p = sub.add_parser("observed-review"); p.add_argument("--task", required=True); p.add_argument("--receipt", required=True, type=Path); p.add_argument("--summary", required=True)
    p = sub.add_parser("search-local"); p.add_argument("query"); p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("evidence"); p.add_argument("--file", required=True, type=Path)
    p = sub.add_parser("link-evidence"); p.add_argument("--earlier", required=True); p.add_argument("--later", required=True); p.add_argument("--relation", required=True, choices=["resolves_gap", "updates", "contradicts"]); p.add_argument("--summary", required=True)
    import xhs_rounds
    xhs_rounds.add_cli(sub)
    args = parser.parse_args()
    project = None
    try:
        with project_lock(args.project):
            project = Project(args.project, create=args.command == "plan")
            if args.command in {'round','audit','review-next','import-evidence','import-audit','export'}: result = xhs_rounds.dispatch(project,args)
            elif args.command == "plan": result = project.plan(load(args.config))
            elif args.command == "import": result = project.import_run(args.run)
            elif args.command == "ingest": result = project.ingest(args.file); project.db.commit()
            elif args.command == "import-media": result = project.import_media(args.directory)
            elif args.command == "next": result = project.next(args.limit, args.automatic, args.kind)
            elif args.command == "resume":
                run = legacy.load_run(args.run)
                with legacy.run_lock(run): result = project.resume(run, args.steps, args.task, args.kind)
            elif args.command == "status": result = project.status()
            elif args.command == "report": result = project.report()
            elif args.command == "add":
                result = project.add(args.kind, args.key, load(args.payload), args.priority)
            elif args.command == "review": result = project.review(args.task, args.decision, args.summary, load(args.annotations) if args.annotations else None)
            elif args.command == "observed-review-plan": result = project.observed_review_plan(args.task)
            elif args.command == "observed-review": result = project.observed_review(args.task, load(args.receipt), args.summary)
            elif args.command == "search-local": result = project.search_local(args.query, args.limit)
            elif args.command == "link-evidence": result = project.link_evidence(args.earlier, args.later, args.relation, args.summary)
            else: result = project.evidence(load(args.file))
            print(dumps(result))
    except Exception as exc:
        print(dumps({"ok": False, "type": type(exc).__name__, "message": str(exc) if isinstance(exc, legacy.ResearchError) else "Inspect local environment"}))
        raise SystemExit(1)
    finally:
        if project: project.close()


if __name__ == "__main__":
    main()
