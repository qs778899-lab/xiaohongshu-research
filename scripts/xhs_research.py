#!/usr/bin/env python3
"""Bounded, read-only Xiaohongshu collection. No credentials on stdout."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse
import uuid
import fcntl
import tempfile
import xhs_search
from xhs_paths import external_path

_ACCOUNT_LOCKS = {}


def account_root():
    """Shared on this machine, outside short-lived research runs. No cookies."""
    root = Path(tempfile.gettempdir()) / ("xhs-research-accounts-" + str(os.getuid()))
    if root.is_symlink():
        raise ResearchError("Account guard directory must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ResearchError("Insecure account guard directory")
    return root


def account_dir(run):
    key = read_json(Path(run) / "account.json", {}).get("key")
    if key is None:
        return None  # Offline fixtures and legacy evidence have no account binding.
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ResearchError("Invalid account binding")
    guard = account_root() / key
    if guard.is_symlink():
        raise ResearchError("Account guard must not be a symlink")
    guard.mkdir(mode=0o700, exist_ok=True)
    return guard


def bind_account(run, chrome_root, profile_key):
    key = hashlib.sha256((str(Path(chrome_root).resolve()) + "\0" + profile_key).encode()).hexdigest()
    existing = read_json(Path(run) / "account.json", {})
    if existing and existing.get("key") != key:
        raise ResearchError("Do not change the account bound to an existing run")
    write_json(Path(run) / "account.json", {"key": key, "at": now(), "credential_free": True})
    return account_dir(run)


@contextmanager
def account_lock(run):
    """Nonblocking process lock; reentrant for nested adapters in this process."""
    guard = account_dir(run)
    if guard is None:
        # Legacy/offline callers remain compatible. All new login sessions bind.
        ensure_active(run)
        yield
        return
    lock = guard / "collection.lock"
    key = str(lock)
    if key in _ACCOUNT_LOCKS:
        ensure_active(run)
        yield
        return
    from xhs_account_identity import related_keys, account_lease
    profile_key = read_json(Path(run) / "account.json", {}).get("key")
    if len(related_keys(profile_key)) > 1:
        with account_lease(profile_key):
            ensure_active(run)
            yield
        return
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CollectionBusy("Another session is collecting this account") from None
        _ACCOUNT_LOCKS[key] = fd
        ensure_active(run)
        yield
    finally:
        _ACCOUNT_LOCKS.pop(key, None)
        os.close(fd)

FORMAT = "xiaohongshu-research-v1"
DOMAINS = {".xiaohongshu.com", "xiaohongshu.com", "www.xiaohongshu.com", "edith.xiaohongshu.com"}
GET_PATHS = {"/api/sns/web/v2/user/me", "/api/sns/web/v1/search/filter",
             "/api/sns/web/v1/search/recommend", "/api/sns/web/v2/comment/page",
             "/api/sns/web/v1/user_posted", "/api/sns/web/v1/user/otherinfo"}
POST_PATHS = {"/api/sns/web/v1/search/onebox", "/api/sns/web/v1/search/notes", "/api/sns/web/v1/feed"}


class ResearchError(RuntimeError):
    pass


class CollectionBusy(ResearchError):
    """Live or unverified collection ownership; safe to wait, never delete."""


class StopRun(ResearchError):
    """Deliberately not XhsApiError: upstream prewarm must not swallow it."""


class TransientTransportError(ResearchError):
    """One failed read-only send with no returned HTTP response, not a retry loop."""

    def __init__(self, timeout_kind, method, endpoint):
        allowed = GET_PATHS if method == "GET" else POST_PATHS if method == "POST" else set()
        if timeout_kind not in {"ConnectTimeout", "ReadTimeout"} or endpoint not in allowed:
            raise ValueError("Invalid transient transport classification")
        self.safe_details = {"schema": "xhs-transient-transport-v1", "retryable": True,
                             "timeout_kind": timeout_kind, "method": method, "endpoint": endpoint,
                             "response_received": False, "attempt_count": 1}
        super().__init__("Transport failed before HTTP response: " + timeout_kind)


REQUEST_DEADLINE_SECONDS = 45.0
REQUEST_TIMEOUTS = {"connect": 10.0, "read": 25.0, "write": 10.0, "pool": 5.0}


class TransportDeadlineExceeded(ResearchError):
    """Unknown transport phase; deliberately never eligible for automatic retry."""


@contextmanager
def transport_deadline(seconds=REQUEST_DEADLINE_SECONDS):
    """Bound a synchronous send/body read without leaving a background request.

    httpx's read timeout is an inactivity timeout, not a total response deadline.
    This macOS/POSIX adapter uses the main-thread timer and refuses to replace an
    existing timer. Other environments must supply an equally bounded adapter.
    """
    if (threading.current_thread() is not threading.main_thread() or
            not hasattr(signal, "setitimer") or not hasattr(signal, "ITIMER_REAL")):
        raise ResearchError("Bounded transport requires POSIX main-thread execution")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ResearchError("Another real-time deadline is active; preserve its timer")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(_signum, _frame):
        raise TransportDeadlineExceeded("Transport total deadline exceeded; phase unconfirmed, no automatic retry")

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def research_path(path):
    try:
        return external_path(path)
    except ValueError as exc:
        raise ResearchError(str(exc)) from None


def write_json(path, value):
    research_path(path)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def load_run(path):
    research_path(path)
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ResearchError("Run directory must not be a symlink")
    path = path.resolve()
    manifest = read_json(path / "run.json", {})
    if manifest.get("format") != FORMAT or manifest.get("directory") != str(path):
        raise ResearchError("Not a run created by this skill")
    for child in (".session", "browser", "raw"):
        if (path / child).is_symlink():
            raise ResearchError("Run contains a symlink at a managed directory")
    return path


@contextmanager
def run_lock(run):
    lock = run / ".collection.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise CollectionBusy("Another collection may be active; inspect the run lock before continuing") from None
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def stop(run, reason):
    write_json(run / "STOP.json", {"at": now(), "reason": reason})
    guard = account_dir(run)
    if guard is not None:
        write_json(guard / "STOP.json", {"at": now(), "reason": reason, "run": str(run)})
    raise StopRun(reason)


def ensure_active(run):
    if (run / "STOP.json").exists():
        raise StopRun("This run was stopped; preserve STOP.json and resolve the cause before a new run")
    if read_json(run / "run.json").get("closed"):
        raise ResearchError("Run is closed; create a new run for more collection")
    guard = account_dir(run)
    if guard is not None and (guard / "STOP.json").exists():
        raise StopRun("This account has an unresolved access restriction; a new run cannot bypass it")
    if guard is not None:
        from xhs_account_identity import related_keys, check_stops
        profile_key = read_json(Path(run) / "account.json", {}).get("key")
        if profile_key:
            check_stops(related_keys(profile_key))


def clean_data(value):
    if isinstance(value, list):
        return [clean_data(x) for x in value]
    if isinstance(value, dict):
        # Public author IDs are necessary for cross-post attribution. Keep them.
        return {k: clean_data(v) for k, v in value.items()
                if k.lower() not in {"cookie", "cookies", "web_session", "a1", "authorization", "set-cookie"}}
    return value


def client_class(run):
    import httpx
    import xhs_cli.cookies
    from xhs_cli.client import XhsClient
    from xhs_cli.exceptions import XhsApiError
    xhs_cli.cookies.CONFIG_DIR_NAME = str(run / ".session" / "cache")

    class ReadOnlyClient(XhsClient):
        def __init__(self, cookies):
            super().__init__(cookies, max_retries=1, request_delay=3, timeout=25)
            self._http.follow_redirects = False
            self._http.timeout = httpx.Timeout(**REQUEST_TIMEOUTS)
            guard = account_dir(run)
            self._last_request_time = max(read_json(run / "last-request.json", {}).get("time", 0),
                                          read_json(guard / "last-request.json", {}).get("time", 0) if guard else 0)
            self.search_method_version = xhs_search.SEARCH_METHOD
            self.last_search_request = None

        def _main_api_post(self, uri, data, header_overrides=None):
            if uri == xhs_search.SEARCH_ENDPOINT:
                from importlib.metadata import version
                try:
                    data = xhs_search.adapt_request(data, self.search_method_version)
                except ValueError as exc:
                    raise ResearchError(str(exc)) from None
                self.last_search_request = xhs_search.request_receipt(
                    data, self.search_method_version, version("xiaohongshu-cli"))
            return super()._main_api_post(uri, data, header_overrides=header_overrides)

        def _request_with_retry(self, method, url, **kwargs):
            parsed = urlparse(url)
            allowed = GET_PATHS if method == "GET" else POST_PATHS if method == "POST" else set()
            if (parsed.scheme != "https" or parsed.hostname != "edith.xiaohongshu.com" or
                    parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.path not in allowed):
                raise ResearchError("Endpoint is outside the read-only allowlist")
            ensure_active(run)
            self._rate_limit_delay()
            ensure_active(run)
            response = None
            attempted = False

            def reject_http_status():
                if response is None:
                    return
                if response.status_code in (401, 403, 429, 461, 471):
                    stop(run, "HTTP access/login/verification/rate restriction: " + str(response.status_code))
                if 300 <= response.status_code < 400:
                    stop(run, "HTTP redirect; access state unconfirmed: " + str(response.status_code))
                if response.status_code >= 400:
                    raise ResearchError("HTTP failed: " + str(response.status_code))

            try:
                # Inspect headers before reading the body: a 461 with a stalled
                # body must remain an access STOP, never a retryable ReadTimeout.
                kwargs.pop("timeout", None)
                kwargs.pop("follow_redirects", None)
                request = self._http.build_request(method, url, timeout=httpx.Timeout(**REQUEST_TIMEOUTS), **kwargs)
                with transport_deadline():
                    attempted = True
                    response = self._http.send(request, stream=True, follow_redirects=False)
                    if response.status_code < 300:
                        response.read()
                # Persist restrictions after disarming the timer, so deadline
                # expiry cannot interrupt recording a restriction already seen.
                reject_http_status()
            except (httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                ensure_active(run)
                if response is None and getattr(exc, "response", None) is None:
                    raise TransientTransportError(type(exc).__name__, method, parsed.path) from None
                raise ResearchError("Transport failed after HTTP response: " + type(exc).__name__ + "; no automatic retry") from None
            except TransportDeadlineExceeded:
                reject_http_status()
                raise
            except ResearchError:
                raise
            except Exception as exc:
                raise ResearchError("Transport failed: " + type(exc).__name__) from None
            finally:
                if response is not None:
                    response.close()
                if attempted:
                    self._mark_request()
                    write_json(run / "last-request.json", {"time": self._last_request_time})
                    guard = account_dir(run)
                    if guard is not None:
                        write_json(guard / "last-request.json", {"time": self._last_request_time})
            self._merge_response_cookies(response)
            return response

        def _handle_response(self, response):
            try:
                return super()._handle_response(response)
            except XhsApiError as exc:
                code = getattr(exc, "code", None)
                if type(exc).__name__ in {"NeedVerifyError", "IpBlockedError", "SessionExpiredError", "SignatureError"} or code in {300017, 300031}:
                    stop(run, type(exc).__name__ + ": " + str(code))
                payload = getattr(exc, "response", None) or {}
                optional = {"/api/sns/web/v1/search/onebox", "/api/sns/web/v1/search/filter", "/api/sns/web/v1/search/recommend"}
                # Observed 2026-10-30: optional onebox returns HTTP 200,
                # success=false, code=0, msg=成功. The upstream search method
                # tolerates this empty optional response. Keep that narrow case
                # catchable, but never verification/login/rate errors.
                empty_optional = ((code == 0 and payload.get("msg") == "成功") or
                                  (code == 3002 and payload.get("msg") == "方案执行结果为NULL"))
                if response.request.url.path in optional and empty_optional:
                    write_json(run / "diagnostics" / "optional-search-response.json", {
                        "at": now(), "endpoint": response.request.url.path, "code": code,
                        "message": payload["msg"], "handling": "upstream_optional_step_only"})
                    raise
                # Do not echo response text, headers or tokens into logs.
                raise ResearchError("API failed: " + type(exc).__name__ + ", code=" + str(code)) from None

    return ReadOnlyClient


@contextmanager
def client(run):
    ensure_active(run)
    path = run / ".session" / "cookies.json"
    if not path.exists() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise ResearchError("Missing or insecure temporary login; use the authorized login command")
    with account_lock(run):
        with client_class(run)(read_json(path)) as instance:
            yield instance


def login(run, chrome_root, profile_name):
    ensure_active(run)
    if (run / ".session" / "cookies.json").exists():
        raise ResearchError("This run already has a login copy; do not silently replace the account")
    import browser_cookie3
    chrome_root = Path(chrome_root).expanduser().resolve()
    state = read_json(chrome_root / "Local State", {})
    matches = [key for key, value in state.get("profile", {}).get("info_cache", {}).items()
               if value.get("name") == profile_name]
    if len(matches) != 1 or Path(matches[0]).name != matches[0] or matches[0] in {".", ".."}:
        raise ResearchError("Profile name must identify exactly one Chrome profile")
    profile = chrome_root / matches[0]
    bind_account(run, chrome_root, matches[0])
    ensure_active(run)
    paths = [profile / "Network" / "Cookies", profile / "Cookies"]
    cookie_path = next((p for p in paths if p.is_file()), None)
    if not cookie_path:
        raise ResearchError("The specified profile has no Chrome cookie database")
    jar = browser_cookie3.chrome(cookie_file=str(cookie_path), domain_name=".xiaohongshu.com")
    cookies = {c.name: c.value for c in jar if c.domain in DOMAINS}
    if not cookies.get("web_session") or not cookies.get("a1"):
        raise ResearchError("The specified profile has no usable Xiaohongshu session")
    (run / ".session").mkdir(mode=0o700, exist_ok=True)
    with account_lock(run):
        with client_class(run)(cookies) as api:
            info = api.get_self_info()
            if not isinstance(info, dict) or not info or info.get("guest") is True:
                raise ResearchError("Could not confirm login")
    write_json(run / ".session" / "cookies.json", cookies)
    write_json(run / ".session" / "origin.json", {"profile_name": profile_name, "at": now(), "original_modified": False})
    return {"authenticated": True, "original_profile_modified": False}


def note_url(note_id, token, source):
    return "https://www.xiaohongshu.com/explore/" + note_id + "?" + urlencode({"xsec_token": token, "xsec_source": source})


def register(run, note_id, token, source, title="", provenance=None):
    if not re.fullmatch(r"[a-zA-Z0-9]{16,40}", note_id):
        raise ResearchError("Invalid observed note ID")
    catalog = read_json(run / "catalog.json", {})
    row = catalog.setdefault(note_id, {"note_id": note_id, "contexts": []})
    if title:
        row["title"] = title
    if token and source:
        context = {"token": token, "source": source, "url": note_url(note_id, token, source), "provenance": provenance}
        if context not in row["contexts"]:
            row["contexts"].append(context)
    write_json(run / "catalog.json", catalog)


def resolve_note(run, value):
    if value.startswith("https://"):
        url = urlparse(value)
        match = re.fullmatch(r"/(?:explore|search_result)/([a-zA-Z0-9]{16,40})/?", url.path)
        if url.hostname != "www.xiaohongshu.com" or not match or url.username or url.password:
            raise ResearchError("Expected an observed Xiaohongshu note URL")
        nid = match[1]
        query = parse_qs(url.query)
        token, source = query.get("xsec_token", [""])[0], query.get("xsec_source", [""])[0]
        if token and source:
            register(run, nid, token, source, provenance="provided_url")
    else:
        nid = value
    row = read_json(run / "catalog.json", {}).get(nid, {})
    if not row.get("contexts"):
        raise ResearchError("No observed token/source for this note; search it or provide the complete source URL")
    return nid, row["contexts"][-1]


def capture(run, filename, metadata, action):
    path = run / "raw" / (filename + ".json")
    if path.exists():
        return read_json(path)["data"]
    data = clean_data(action())
    if not isinstance(data, dict):
        raise ResearchError("Unexpected API response shape; no success record saved")
    list_key = {"search": "items", "note": "items", "comments": "comments", "author_notes": "notes"}.get(metadata.get("operation"))
    if list_key and not isinstance(data.get(list_key), list):
        raise ResearchError("API response schema changed: expected " + list_key)
    if metadata.get("operation") == "note" and not data["items"]:
        raise ResearchError("Note content was not returned; this is not a successful read")
    write_json(path, {**metadata, "captured_at": now(), "data": data})
    return data


def collect_search(run, args):
    api_sort = {"general": "general", "latest": "time_descending", "popular": "popularity_descending"}.get(args.sort)
    if api_sort is None:
        raise ResearchError("Unsupported search sort")
    # Key on the actual API value so older incorrectly passed aliases are not reused.
    key_input = args.query + "\0" + api_sort
    if api_sort != "general": key_input += "\0" + xhs_search.SEARCH_METHOD
    key = hashlib.sha256(key_input.encode()).hexdigest()[:16]
    seen = set()
    pages = []
    reason = "page_budget"
    with client(run) as api:
        for page in range(1, args.pages + 1):
            metadata = {"operation": "search", "query": args.query, "sort": args.sort, "api_sort": api_sort, "page": page}
            def action():
                response = api.search_notes(args.query, page=page, sort=api_sort)
                try:
                    metadata.update(xhs_search.collection_metadata(api, args.query, page, api_sort))
                except ValueError as exc:
                    raise ResearchError(str(exc)) from None
                return response
            data = capture(run, f"search-{key}-{page}", metadata, action)
            if not isinstance(data.get("items"), list):
                raise ResearchError("Search schema changed: expected items")
            items = [x for x in data["items"] if x.get("model_type") == "note"]
            ids = {x["id"] for x in items}
            for x in items:
                n = x.get("note_card", {})
                register(run, x["id"], x.get("xsec_token", ""), "pc_search", n.get("display_title", n.get("title", "")),
                         {"query": args.query, "sort": args.sort, "page": page})
            new = len(ids - seen)
            pages.append({"page": page, "items": len(items), "new_ids": new, "has_more": data.get("has_more")})
            seen.update(ids)
            if data.get("has_more") is False:
                reason = "api_has_more_false"
                break
            if not items or (page > 1 and not new):
                reason = "empty_or_repeated_page_not_proof_of_exhaustion"
                break
    result = {"query": args.query, "sort": args.sort, "api_sort": api_sort,
              "search_method_version": xhs_search.SEARCH_METHOD, "legacy_general_compatible": api_sort == "general",
              "pages": pages, "unique_ids": len(seen), "stop_reason": reason}
    write_json(run / "progress" / f"search-{key}.json", result)
    return result


def collect_note(run, value):
    nid, ctx = resolve_note(run, value)
    with client(run) as api:
        data = capture(run, "note-" + nid, {"operation": "note", "note_id": nid, "url": ctx["url"]},
                       lambda: api.get_note_by_id(nid, xsec_token=ctx["token"], xsec_source=ctx["source"]))
    return {"note_id": nid, "file": str(run / "raw" / ("note-" + nid + ".json")),
            "notes": [{"title": x.get("note_card", {}).get("title"),
                       "body": x.get("note_card", {}).get("desc"),
                       "images": len(x.get("note_card", {}).get("image_list", []))} for x in data.get("items", [])]}


def collect_comments(run, value, max_pages):
    nid, ctx = resolve_note(run, value)
    cursor = ""
    cursors = {""}
    pages = []
    reason = "page_budget"
    with client(run) as api:
        for page in range(1, max_pages + 1):
            data = capture(run, f"comments-{nid}-{page}", {"operation": "comments", "note_id": nid, "url": ctx["url"], "page": page, "cursor_in": cursor},
                           lambda: api.get_comments(nid, cursor=cursor, xsec_token=ctx["token"], xsec_source=ctx["source"]))
            if not isinstance(data.get("comments"), list):
                raise ResearchError("Comment schema changed: expected comments")
            pages.append({"page": page, "root_rows": len(data["comments"]), "has_more": data.get("has_more")})
            if data.get("has_more") is False:
                reason = "root_pagination_finished_replies_not_guaranteed"
                break
            following = data.get("cursor")
            if not data["comments"] or not following or following in cursors:
                reason = "empty_or_stalled_cursor_not_proof_of_exhaustion"
                break
            cursor = following
            cursors.add(cursor)
    result = {"note_id": nid, "pages": pages, "stop_reason": reason, "all_replies_read": False}
    write_json(run / "progress" / ("comments-" + nid + ".json"), result)
    return result


def collect_author(run, user_id, max_pages=1):
    if not re.fullmatch(r"[a-zA-Z0-9]{16,40}", user_id):
        raise ResearchError("Use a public author ID observed in a relevant source")
    cursor, seen_cursors, seen_ids, pages = "", {""}, set(), []
    reason = "page_budget"
    with client(run) as api:
        for page in range(1, max_pages + 1):
            filename = "author-" + user_id + ("" if page == 1 else "-" + str(page))
            data = capture(run, filename, {"operation": "author_notes", "user_id": user_id, "page": page, "cursor_in": cursor},
                           lambda: api.get_user_notes(user_id, cursor=cursor))
            rows = data.get("notes", [])
            if not isinstance(rows, list):
                raise ResearchError("Author note schema changed")
            ids = set()
            for x in rows:
                nid = x.get("note_id", x.get("id", ""))
                if nid:
                    ids.add(nid)
                    register(run, nid, x.get("xsec_token", ""), "pc_user", x.get("display_title", x.get("title", "")),
                             {"author_id": user_id, "page": page})
            pages.append({"page": page, "rows": len(rows), "new_ids": len(ids-seen_ids), "has_more": data.get("has_more")})
            seen_ids.update(ids)
            if data.get("has_more") is False:
                reason = "api_has_more_false"
                break
            following = data.get("cursor")
            if not rows or not following or following in seen_cursors:
                reason = "empty_or_stalled_cursor_not_proof_of_exhaustion"
                break
            cursor = following
            seen_cursors.add(cursor)
    result = {"user_id": user_id, "notes": len(seen_ids), "has_more": data.get("has_more"), "pages": pages,
              "stop_reason": reason, "scope": "first_public_author_page_only" if max_pages == 1 else "public_author_pages_not_semantically_reviewed",
              "file": str(run / "raw" / ("author-" + user_id + ".json"))}
    write_json(run / "progress" / ("author-" + user_id + ".json"), result)
    return result


def summarize(run):
    candidates, notes, roots, replies, comment_notes = set(), set(), set(), set(), set()
    unknown = 0
    search_pages = 0
    browser = {}
    for path in sorted((run / "raw").glob("*.json")):
        o = read_json(path)
        data, op, nid = o.get("data", {}), o.get("operation"), o.get("note_id")
        if op == "search":
            search_pages += 1
            candidates.update(x["id"] for x in data.get("items", []) if x.get("model_type") == "note")
        elif op == "note":
            notes.add(nid)
        elif op == "comments":
            comment_notes.add(nid)
            for c in data.get("comments", []):
                if c.get("id"):
                    roots.add((nid, c["id"]))
                else:
                    unknown += 1
                for r in c.get("sub_comments", []):
                    if r.get("id"):
                        replies.add((nid, r["id"]))
                    else:
                        unknown += 1
        elif op == "browser_comments":
            if o.get("ok"):
                comment_notes.add(nid)
                if o.get("captured_at", "") >= browser.get(nid, {}).get("captured_at", ""):
                    browser[nid] = {"roots": sum(not x.get("is_reply") for x in data),
                                    "replies": sum(bool(x.get("is_reply")) for x in data),
                                    "captured_at": o.get("captured_at", ""), "file": path.name}
    result = {"captured_at": now(), "search_pages": search_pages, "unique_search_candidate_notes": len(candidates),
              "note_bodies_collected_not_semantically_verified": len(notes), "comment_threads_with_data": len(comment_notes),
              "api_unique_root_ids": len(roots), "api_unique_default_reply_ids": len(replies), "api_rows_without_ids": unknown,
              "browser_latest_snapshots_separate_from_api": browser, "do_not_add_browser_and_api_counts": True,
              "progress": [read_json(p) for p in sorted((run / "progress").glob("*.json"))],
              "stop": read_json(run / "STOP.json"), "semantic_conclusions": "Must be assessed by the agent with context"}
    write_json(run / "coverage.json", result)
    return result


def page_budget(value):
    number = int(value)
    if not 1 <= number <= 30:
        raise argparse.ArgumentTypeError("Choose 1..30 pages for this bounded pass")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path, help="A new or existing task-specific evidence directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    p = commands.add_parser("login", help="Only after scoped credential access is authorized")
    p.add_argument("--profile-name", required=True)
    p.add_argument("--chrome-root", default=str(Path.home() / "Library/Application Support/Google/Chrome"))
    p = commands.add_parser("search")
    p.add_argument("query")
    p.add_argument("--sort", choices=["general", "latest", "popular"], default="general")
    p.add_argument("--pages", type=page_budget, default=2)
    p = commands.add_parser("read")
    p.add_argument("note")
    p = commands.add_parser("comments")
    p.add_argument("note")
    p.add_argument("--pages", type=page_budget, default=3)
    p = commands.add_parser("author-notes", help="Public pages for a relevant observed author; legacy default is one")
    p.add_argument("user_id")
    p.add_argument("--pages", type=page_budget, default=1)
    commands.add_parser("summary")
    args = parser.parse_args()
    try:
        if args.command == "init":
            run = research_path(args.run)
            run.mkdir(parents=True, exist_ok=False, mode=0o700)
            write_json(run / "run.json", {"format": FORMAT, "id": uuid.uuid4().hex, "directory": str(run), "created_at": now(), "closed": False})
            result = {"run": str(run), "initialized": True}
        else:
            run = load_run(args.run)
            with run_lock(run):
                if args.command == "login": result = login(run, args.chrome_root, args.profile_name)
                elif args.command == "search": result = collect_search(run, args)
                elif args.command == "read": result = collect_note(run, args.note)
                elif args.command == "comments": result = collect_comments(run, args.note, args.pages)
                elif args.command == "author-notes": result = collect_author(run, args.user_id, args.pages)
                else: result = summarize(run)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        message = str(exc) if isinstance(exc, ResearchError) else "Inspect local environment: " + type(exc).__name__
        print(json.dumps({"ok": False, "type": type(exc).__name__, "message": message}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
