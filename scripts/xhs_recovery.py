#!/usr/bin/env python3
"""Audited, one-shot verification of a stopped author-page API; never auto-resume.

status/plan are read-only. prepare records fresh user/UI evidence. probe consumes
a per-STOP one-shot capability BEFORE reading scoped credentials or requesting.
commit is explicit and keeps the old run closed with its STOP unchanged.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import parse_qsl, urlparse
import uuid

import xhs_research as legacy

VERSION = "xhs-access-recovery-v1"
ENDPOINT = "/api/sns/web/v1/user_posted"


class RecoveryError(legacy.ResearchError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def regular(path, required=True):
    path = Path(path)
    if path.is_symlink():
        raise RecoveryError("Managed recovery paths must not be symlinks")
    if not path.exists() and not required:
        return None
    if not path.is_file():
        raise RecoveryError("Required recovery file is missing")
    return path.read_bytes()


def immutable(path, value, raw=False):
    path = Path(path)
    data = value if raw else encoded(value)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return sha(data)


def file_record(path, required=True):
    data = regular(path, required)
    return {"path": str(path), "sha256": sha(data) if data is not None else None,
            "value": json.loads(data) if data is not None else None}


def _connect(project):
    return sqlite3.connect((Path(project) / "project.sqlite3").as_uri() + "?mode=ro", uri=True)


def _project_meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _guard_from_run(run):
    binding = file_record(run / "account.json")
    key = binding["value"].get("key", "")
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise RecoveryError("Stopped run has no valid account binding")
    root = Path(tempfile.gettempdir()) / ("xhs-research-accounts-" + str(os.getuid()))
    guard = root / key
    for path in (root, guard):
        if path.is_symlink() or not path.is_dir():
            raise RecoveryError("Existing account guard required")
        if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise RecoveryError("Insecure account guard")
    return guard, binding


def inspect(project, run, task_id=None):
    """No mkdir, chmod, SQLite writes, Cookie reads, or network."""
    project, run = Path(project).absolute(), Path(run).absolute()
    for path in (project, run, project / "project.sqlite3"):
        if path.is_symlink():
            raise RecoveryError("Managed recovery paths must not be symlinks")
    project, run = project.resolve(), run.resolve()
    manifest = file_record(run / "run.json")
    if manifest["value"].get("directory") != str(run) or manifest["value"].get("format") != legacy.FORMAT:
        raise RecoveryError("Stopped run identity mismatch")
    guard, binding = _guard_from_run(run)
    with _connect(project) as db:
        db.row_factory = sqlite3.Row
        stop_text = _project_meta(db, "stop")
        if task_id:
            rows = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM tasks WHERE status='blocked' AND reason='access_stop'").fetchall()
        tasks = [dict(row) for row in rows]
    return {"format": VERSION, "project": str(project), "run": str(run), "guard": str(guard),
            "account_binding": binding, "run_manifest": manifest,
            "run_stop": file_record(run / "STOP.json", False),
            "account_stop": file_record(guard / "STOP.json", False),
            "media_stop": file_record(project / "media" / "STOP.json", False),
            "project_stop": {"sha256": sha(stop_text.encode()) if stop_text else None,
                             "raw": stop_text, "value": json.loads(stop_text) if stop_text else None},
            "tasks": tasks}


def exact_spec(state):
    for layer in ("run_stop", "account_stop", "project_stop"):
        if not state[layer]["value"]:
            raise RecoveryError("All three stopped-state layers must be present")
    if state["media_stop"]["value"]:
        raise RecoveryError("A separate media restriction needs separate review")
    if state["run_manifest"]["value"].get("closed") is not True:
        raise RecoveryError("Clean up the stopped run first; it must remain closed")
    for layer in ("account_stop", "project_stop"):
        if state[layer]["value"].get("run") != state["run"]:
            raise RecoveryError("STOP records do not identify the same stopped run")
    if not re.fullmatch(r"HTTP access/login/verification/rate restriction: (401|403|429|461|471)",
                        state["run_stop"]["value"].get("reason", "")):
        raise RecoveryError("Only an observed HTTP API restriction is supported")
    if len(state["tasks"]) != 1:
        raise RecoveryError("Identify exactly one access-stopped task")
    task = state["tasks"][0]
    payload = json.loads(task["payload"])
    if task["kind"] != "author" or task["status"] != "blocked" or task["reason"] != "access_stop":
        raise RecoveryError("First recovery version supports only the failed author-page task")
    uid = payload.get("user_id", "")
    if not re.fullmatch(r"[a-fA-F0-9]{24}", uid) or uid != task["entity_key"]:
        raise RecoveryError("Invalid author identity in failed task")
    return {"method": "GET", "host": "edith.xiaohongshu.com", "endpoint": ENDPOINT,
            "params": {"num": 30, "cursor": task["cursor"], "user_id": uid, "image_scenes": "FD_WM_WEBP"},
            "task_id": task["id"], "page": task["next_page"]}


def plan(project, run, task_id=None):
    state = inspect(project, run, task_id)
    spec = exact_spec(state)
    return {"state": state, "state_sha256": sha(encoded(state)), "probe": spec,
            "network_performed": False, "stop_cleared": False,
            "needs": "fresh original-profile UI evidence plus renewed user research request; API recovery remains unproved"}


def _time(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise RecoveryError("Evidence timestamps require ISO 8601 with timezone") from None


def validate_evidence(evidence, state, profile_name):
    ui = evidence.get("ui", {})
    request = evidence.get("user_request", {})
    if not isinstance(request.get("text"), str) or not request["text"].strip():
        raise RecoveryError("A renewed user request must be recorded verbatim")
    stop_at = max(_time(state[x]["value"]["at"]) for x in ("run_stop", "account_stop", "project_stop"))
    for at in (request.get("at"), ui.get("observed_at")):
        if not stop_at < _time(at) <= time.time() + 60:
            raise RecoveryError("User request and observed UI must follow this STOP")
    if ui.get("profile_name") != profile_name or not profile_name:
        raise RecoveryError("Evidence must identify the already-authorized Chrome profile")
    if any(ui.get(k) is not True for k in ("original_session", "logged_in", "no_access_warning", "normal_content_visible")):
        raise RecoveryError("Original-session UI evidence is incomplete or still restricted")
    url = urlparse(ui.get("url", ""))
    if url.scheme != "https" or url.hostname != "www.xiaohongshu.com" or url.path.startswith(("/message", "/notification")):
        raise RecoveryError("Evidence must concern an authorized public Xiaohongshu page")
    if not isinstance(ui.get("observation"), str) or not ui["observation"].strip():
        raise RecoveryError("Describe what was actually observed; do not infer API recovery")
    baseline = evidence.get("identity_baseline", {})
    uid = ui.get("account_user_id", "")
    if (not re.fullmatch(r"[a-fA-F0-9]{24}", uid) or baseline.get("account_user_id") != uid
            or not isinstance(baseline.get("source"), str) or not baseline["source"].strip()):
        raise RecoveryError("Current public account ID must match a previously observed original-account ID")


def _profile(chrome_root, profile_name, binding_key):
    chrome_root = Path(chrome_root).expanduser().resolve()
    state = json.loads(regular(chrome_root / "Local State"))
    matches = [k for k, v in state.get("profile", {}).get("info_cache", {}).items() if v.get("name") == profile_name]
    if len(matches) != 1 or Path(matches[0]).name != matches[0] or matches[0] in {".", ".."}:
        raise RecoveryError("Profile name must match exactly one existing Chrome profile")
    key = sha((str(chrome_root) + "\0" + matches[0]).encode())
    if key != binding_key:
        raise RecoveryError("Recovery must use the same original profile binding")
    return chrome_root / matches[0]


@contextmanager
def locked(project, guard):
    """Same lock paths/order as ordinary collection, without pretending STOP vanished."""
    handles = []
    try:
        for path in (Path(project) / ".project.lock", Path(guard) / "collection.lock"):
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            handles.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RecoveryError("Another process owns the project or account") from None
        yield
    finally:
        for fd in reversed(handles):
            os.close(fd)


def prepare(project, run, evidence_file, profile_name, chrome_root, task_id=None):
    initial = plan(project, run, task_id)
    state = initial["state"]
    with locked(state["project"], state["guard"]):
        if inspect(project, run, initial["probe"]["task_id"]) != state:
            raise RecoveryError("STOP or task changed during preparation")
        evidence_bytes = regular(evidence_file)
        evidence = json.loads(evidence_bytes)
        validate_evidence(evidence, state, profile_name)
        _profile(chrome_root, profile_name, state["account_binding"]["value"]["key"])
        shared = Path(state["guard"]) / "recovery" / state["account_stop"]["sha256"]
        if (shared / "spent.json").exists():
            raise RecoveryError("This STOP already had its single probe; do not retry automatically")
        directory = Path(state["project"]) / "recovery" / uuid.uuid4().hex
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        immutable(directory / "evidence.json", evidence_bytes, raw=True)
        record = {**initial, "prepared_at": legacy.now(), "profile_name": profile_name,
                  "chrome_root": str(Path(chrome_root).expanduser().resolve()), "attempt": str(directory),
                  "evidence_sha256": sha(evidence_bytes)}
        immutable(directory / "prepared.json", record)
        return {"attempt": str(directory), "prepared_sha256": sha(encoded(record)),
                "network_performed": False, "stop_cleared": False}


def _load_attempt(directory):
    directory = Path(directory).absolute()
    if directory.is_symlink():
        raise RecoveryError("Recovery attempt must not be a symlink")
    raw = regular(directory / "prepared.json")
    record = json.loads(raw)
    if record.get("attempt") != str(directory.resolve()) or directory.resolve().parent != Path(record["state"]["project"]) / "recovery":
        raise RecoveryError("Recovery attempt does not belong to this project")
    if sha(encoded(record["state"])) != record["state_sha256"] or exact_spec(record["state"]) != record["probe"]:
        raise RecoveryError("Recovery preparation is internally inconsistent")
    if sha(regular(directory / "evidence.json")) != record["evidence_sha256"]:
        raise RecoveryError("Recorded UI/user evidence changed")
    validate_evidence(json.loads(regular(directory / "evidence.json")), record["state"], record["profile_name"])
    return directory.resolve(), record, sha(raw)


def read_scoped_cookies(record):
    """No cookie file copies; only the already-authorized profile and XHS domains."""
    import browser_cookie3
    profile = _profile(record["chrome_root"], record["profile_name"], record["state"]["account_binding"]["value"]["key"])
    cookie_path = next((p for p in (profile / "Network" / "Cookies", profile / "Cookies") if p.is_file()), None)
    if cookie_path is None or cookie_path.is_symlink():
        raise RecoveryError("Original profile has no regular Cookie database")
    jar = browser_cookie3.chrome(cookie_file=str(cookie_path), domain_name=".xiaohongshu.com")
    cookies = {c.name: c.value for c in jar if c.domain in legacy.DOMAINS}
    if not cookies.get("web_session") or not cookies.get("a1"):
        cookies.clear()
        raise RecoveryError("Original session is unavailable; no recovery request was sent")
    return cookies


def validate_author_data(data, spec):
    if not isinstance(data, dict) or type(data.get("has_more")) is not bool:
        raise RecoveryError("Probe response lacks explicit pagination state")
    rows = data.get("notes")
    if rows is None and data["has_more"] is False:
        rows = []
    if not isinstance(rows, list) or any(not isinstance(n, dict) or not re.fullmatch(r"[a-fA-F0-9]{24}", n.get("note_id", n.get("id", ""))) for n in rows):
        raise RecoveryError("Probe response is not a valid author page")
    if data["has_more"] and (not rows or not isinstance(data.get("cursor"), str) or not data["cursor"] or data["cursor"] == spec["params"]["cursor"]):
        raise RecoveryError("Probe response cannot advance the exact failed cursor")
    return data


def request_once(cookies, spec, guard):
    """The ONLY network entrypoint. No prewarm, login check, redirect, or retry."""
    from xhs_cli.client import XhsClient

    class SingleRequest(XhsClient):
        def __init__(self):
            super().__init__(cookies, timeout=25, request_delay=0, max_retries=1)
            self.calls = 0

        def _request_with_retry(self, method, url, **kwargs):
            u = urlparse(url)
            pairs = parse_qsl(u.query, keep_blank_values=True)
            expected = {k: str(v) for k, v in spec["params"].items()}
            if (self.calls or method != "GET" or u.scheme != "https" or u.hostname != spec["host"]
                    or u.port is not None or u.username or u.password or u.fragment or u.path != spec["endpoint"]
                    or len(pairs) != len(expected) or dict(pairs) != expected):
                raise RecoveryError("Probe attempted a second request or changed the failed endpoint/parameters")
            self.calls += 1
            previous = legacy.read_json(Path(guard) / "last-request.json", {}).get("time", 0)
            if previous > time.time() + 60:
                raise RecoveryError("Account request clock is inconsistent")
            remaining = 6 - (time.time() - previous)
            if remaining > 0:
                time.sleep(remaining)
            try:
                return self._http.request(method, url, follow_redirects=False, **kwargs)
            finally:
                legacy.write_json(Path(guard) / "last-request.json", {"time": time.time()})

        def _handle_response(self, response):
            if response.status_code != 200:
                raise RecoveryError("Probe HTTP status: " + str(response.status_code))
            try:
                value = response.json()
            except ValueError:
                raise RecoveryError("Probe returned non-JSON content") from None
            if not isinstance(value, dict) or value.get("success") is not True:
                code = value.get("code") if isinstance(value, dict) else None
                suffix = str(code) if isinstance(code, int) else "unknown"
                raise RecoveryError("Probe API failure code: " + suffix)
            return validate_author_data(value.get("data"), spec)

    with SingleRequest() as api:
        result = api.get_user_notes(spec["params"]["user_id"], cursor=spec["params"]["cursor"])
        if api.calls != 1:
            raise RecoveryError("Expected exactly one failed-endpoint verification request")
        return result


def probe(directory, *, cookie_reader=None, requester=None):
    directory, record, prepared_hash = _load_attempt(directory)
    state, spec = record["state"], record["probe"]
    cookie_reader, requester = cookie_reader or read_scoped_cookies, requester or request_once
    with locked(state["project"], state["guard"]):
        if inspect(state["project"], state["run"], spec["task_id"]) != state:
            raise RecoveryError("STOP, account binding, old run, or failed task changed; probe refused")
        shared = Path(state["guard"]) / "recovery" / state["account_stop"]["sha256"]
        spent = {"at": legacy.now(), "attempt": str(directory), "prepared_sha256": prepared_hash,
                 "request_limit": 1, "scope": spec}
        # O_EXCL means a crash, timeout, failure, or another prepared attempt cannot repeat.
        try:
            immutable(shared / "spent.json", spent)
        except FileExistsError:
            raise RecoveryError("This STOP's probe capability was already consumed") from None
        immutable(directory / "spent.json", spent)
        cookies = {}
        try:
            cookies = cookie_reader(record)
            data = validate_author_data(requester(cookies, spec, state["guard"]), spec)
            result = {"operation": "author_notes", "captured_at": legacy.now(), "project_task": spec["task_id"],
                      "page": spec["page"], "user_id": spec["params"]["user_id"],
                      "cursor_in": spec["params"]["cursor"], "data": legacy.clean_data(data)}
            response_hash = immutable(directory / "response.json", result)
            receipt = {"status": "verified_api_success", "at": legacy.now(), "attempt": str(directory),
                       "prepared_sha256": prepared_hash, "state_sha256": record["state_sha256"],
                       "response_sha256": response_hash, "scope": spec,
                       "stop_cleared": False, "original_browser_modified_or_logged_out": False,
                       "credentials_persisted": False}
            immutable(directory / "receipt.json", receipt)
            return {**receipt, "response_file": str(directory / "response.json")}
        except BaseException as exc:
            failure = {"status": "failed_or_interrupted", "at": legacy.now(), "attempt": str(directory),
                       "prepared_sha256": prepared_hash, "error_type": type(exc).__name__,
                       "reason": str(exc) if isinstance(exc, RecoveryError) else "Probe did not complete; no automatic retry",
                       "stop_preserved": True, "capability_consumed": True}
            immutable(directory / "failure.json", failure)
            immutable(shared / "failure.json", failure)
            raise
        finally:
            cookies.clear()


def _verified(directory, record, prepared_hash):
    receipt_raw = regular(directory / "receipt.json")
    receipt = json.loads(receipt_raw)
    if (receipt.get("status") != "verified_api_success" or receipt.get("prepared_sha256") != prepared_hash
            or receipt.get("state_sha256") != record["state_sha256"] or receipt.get("scope") != record["probe"]
            or receipt.get("response_sha256") != sha(regular(directory / "response.json"))
            or (directory / "failure.json").exists()):
        raise RecoveryError("No matching immutable successful probe receipt")
    response = json.loads(regular(directory / "response.json"))
    spec = record["probe"]
    if (response.get("operation") != "author_notes" or response.get("project_task") != spec["task_id"]
            or response.get("user_id") != spec["params"]["user_id"] or response.get("cursor_in") != spec["params"]["cursor"]
            or response.get("page") != spec["page"]):
        raise RecoveryError("Successful response no longer matches the stopped task")
    validate_author_data(response.get("data"), spec)
    shared = Path(record["state"]["guard"]) / "recovery" / record["state"]["account_stop"]["sha256"]
    spent = json.loads(regular(shared / "spent.json"))
    if spent.get("attempt") != str(directory) or spent.get("prepared_sha256") != prepared_hash:
        raise RecoveryError("Probe receipt is not the account's consumed attempt")
    return receipt, sha(receipt_raw), shared


def commit(directory, *, fault=None):
    """Fail-closed two-store commit: DB first, account gate last. Explicit only.

    A crash after the SQLite commit leaves the account STOP in place. Repeating
    commit completes that journaled transition; it never repeats the probe.
    """
    directory, record, prepared_hash = _load_attempt(directory)
    state, spec = record["state"], record["probe"]
    with locked(state["project"], state["guard"]):
        receipt, receipt_hash, shared = _verified(directory, record, prepared_hash)
        history_key = "recovery_" + directory.name
        with _connect(state["project"]) as db:
            history_text = _project_meta(db, history_key)
            active_stop = _project_meta(db, "stop")
        history = json.loads(history_text) if history_text else None
        archive = shared / "resolved-account-stop.json"
        if history is None:
            if inspect(state["project"], state["run"], spec["task_id"]) != state:
                raise RecoveryError("A STOP or failed task changed since successful probe; commit refused")
            intent = {"at": legacy.now(), "prepared_sha256": prepared_hash, "receipt_sha256": receipt_hash,
                      "state_sha256": record["state_sha256"], "ordering": "SQLite commit before account STOP archival"}
            if not (directory / "commit-intent.json").exists():
                immutable(directory / "commit-intent.json", intent)
            else:
                old = json.loads(regular(directory / "commit-intent.json"))
                if any(old.get(k) != intent[k] for k in ("prepared_sha256", "receipt_sha256", "state_sha256")):
                    raise RecoveryError("Recovery commit journal mismatch")
            from xhs_project import Project
            project = Project(state["project"])
            try:
                project.db.execute("BEGIN IMMEDIATE")
                result = project.ingest(directory / "response.json")
                if result.get("snapshot") != receipt["response_sha256"]:
                    raise RecoveryError("Successful probe snapshot could not be imported")
                task_after = project.task(spec["task_id"])
                history = {"format": VERSION, "status": "verified_api_success", "at": legacy.now(),
                           "attempt": str(directory), "run": state["run"], "guard": state["guard"],
                           "run_stop_sha256": state["run_stop"]["sha256"],
                           "account_stop_sha256": state["account_stop"]["sha256"],
                           "prepared_sha256": prepared_hash, "receipt_sha256": receipt_hash,
                           "response_sha256": receipt["response_sha256"], "task_after": task_after,
                           "scope": spec, "account_archive": str(archive)}
                project.set_meta(history_key, history)
                project.db.execute("DELETE FROM meta WHERE key='stop'")
                project.db.commit()
            except BaseException:
                project.db.rollback()
                raise
            finally:
                project.close()
            if fault:
                fault("after_sqlite_commit")
        else:
            if (history.get("receipt_sha256") != receipt_hash or history.get("prepared_sha256") != prepared_hash
                    or history.get("attempt") != str(directory) or active_stop not in (None, "null")):
                raise RecoveryError("A different stop or recovery changed the commit journal")
            current_state = inspect(state["project"], state["run"], spec["task_id"])
            for field in ("run_stop", "run_manifest", "account_binding", "media_stop"):
                if current_state[field] != state[field]:
                    raise RecoveryError("Historical run/account binding or a separate STOP changed")
            with _connect(state["project"]) as db:
                saved = db.execute("SELECT path FROM snapshots WHERE id=?", (receipt["response_sha256"],)).fetchone()
                if not saved or sha(regular(Path(state["project"]) / saved[0])) != receipt["response_sha256"]:
                    raise RecoveryError("Committed probe snapshot is missing or changed")
            if not (directory / "committed.json").exists():
                if len(current_state["tasks"]) != 1:
                    raise RecoveryError("Committed target task disappeared")
                task_now = dict(current_state["tasks"][0])
                task_now["payload"] = json.loads(task_now["payload"])
                if any(history.get("task_after", {}).get(k) != v for k, v in task_now.items()):
                    raise RecoveryError("Committed target task changed before account-gate release")
        current = regular(Path(state["guard"]) / "STOP.json", False)
        if current is not None:
            if sha(current) != state["account_stop"]["sha256"]:
                raise RecoveryError("A newer account STOP must not be cleared")
            if not archive.exists():
                immutable(archive, current, raw=True)
            elif sha(regular(archive)) != sha(current):
                raise RecoveryError("Archived account STOP hash mismatch")
            (Path(state["guard"]) / "STOP.json").unlink()
        elif sha(regular(archive)) != state["account_stop"]["sha256"]:
            raise RecoveryError("Account STOP disappeared without this recovery archive")
        complete = {"at": legacy.now(), "receipt_sha256": receipt_hash, "snapshot": receipt["response_sha256"],
                    "historical_run_unchanged": True, "new_collection_session_required": True,
                    "status": "committed", "task_id": spec["task_id"]}
        if not (directory / "committed.json").exists():
            immutable(directory / "committed.json", complete)
        return json.loads(regular(directory / "committed.json"))


def resolved_run_stop(db, directory):
    """True only for this old STOP with a committed verified receipt and no new account STOP.

    Caller passes the existing project SQLite connection. Never clears metadata.
    Failure, missing evidence, tampering or a new account STOP all return False.
    """
    try:
        directory = Path(directory).resolve()
        old_hash = sha(regular(directory / "STOP.json"))
        guard, _ = _guard_from_run(directory)
        if (guard / "STOP.json").exists():
            return False
        for row in db.execute("SELECT value FROM meta WHERE key LIKE 'recovery_%'"):
            h = json.loads(row[0])
            if h.get("run") != str(directory) or h.get("run_stop_sha256") != old_hash or h.get("guard") != str(guard):
                continue
            attempt, record, prepared_hash = _load_attempt(h["attempt"])
            receipt, receipt_hash, _ = _verified(attempt, record, prepared_hash)
            if (h.get("status") != "verified_api_success" or h.get("prepared_sha256") != prepared_hash
                    or h.get("receipt_sha256") != receipt_hash or h.get("response_sha256") != receipt["response_sha256"]
                    or sha(regular(h["account_archive"])) != h["account_stop_sha256"]
                    or not (attempt / "committed.json").exists()):
                continue
            completed = json.loads(regular(attempt / "committed.json"))
            if (completed.get("status") != "committed" or completed.get("receipt_sha256") != receipt_hash
                    or completed.get("snapshot") != receipt["response_sha256"]):
                continue
            saved = db.execute("SELECT path FROM snapshots WHERE id=?", (receipt["response_sha256"],)).fetchone()
            if saved and sha(regular(Path(record["state"]["project"]) / saved[0])) == receipt["response_sha256"]:
                return True
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RecoveryError):
        return False
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "plan", "prepare"):
        p = sub.add_parser(name)
        p.add_argument("--project", type=Path, required=True)
        p.add_argument("--run", type=Path, required=True)
        p.add_argument("--task")
        if name == "prepare":
            p.add_argument("--evidence", type=Path, required=True)
            p.add_argument("--profile-name", required=True)
            p.add_argument("--chrome-root", type=Path, required=True)
    for name in ("probe", "commit"):
        p = sub.add_parser(name)
        p.add_argument("--attempt", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "status": result = inspect(args.project, args.run, args.task)
        elif args.command == "plan": result = plan(args.project, args.run, args.task)
        elif args.command == "prepare": result = prepare(args.project, args.run, args.evidence, args.profile_name, args.chrome_root, args.task)
        elif args.command == "probe": result = probe(args.attempt)
        else: result = commit(args.attempt)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"ok": False, "type": type(exc).__name__, "message": str(exc) if isinstance(exc, RecoveryError) else "Recovery failed; inspect local audit files without exposing credentials"}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
