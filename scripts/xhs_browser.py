#!/usr/bin/env python3
"""Own one temporary browser; read comments; clean only this run's session."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import uuid
from urllib.request import urlopen
from urllib.parse import urlparse

from xhs_research import (ResearchError, account_lock, clean_data, ensure_active, load_run, now,
                          read_json, resolve_note, run_lock, stop, write_json)


class NoteScopeChanged(ResearchError):
    code = "NOTE_SCOPE_CHANGED"



class TargetBindingRequired(ResearchError):
    code = "BROWSER_TARGET_BINDING_REQUIRED"


class TargetAccessUnconfirmed(ResearchError):
    code = "ACCESS_STATE_UNCONFIRMED"


def owned_endpoint(run, state):
    """Validate ownership before reading URL-only metadata from this Chrome."""
    profile = Path(run) / "browser"
    if (not state or state.get("profile") != str(profile) or profile.is_symlink() or
            "--user-data-dir=" + str(profile) not in process_command(state.get("pid", 0))):
        raise ResearchError("Owned temporary browser is unavailable")
    try:
        active = profile / "DevToolsActivePort"
        if active.is_symlink(): raise ValueError()
        port = int(active.read_text().splitlines()[0])
        if not 1 <= port <= 65535: raise ValueError()
    except (OSError, ValueError, IndexError):
        raise ResearchError("Owned browser endpoint cannot be verified") from None
    endpoint = "http://127.0.0.1:" + str(port)
    if state.get("endpoint") != endpoint:
        raise ResearchError("Owned browser endpoint no longer matches its profile")
    return endpoint, port


def target_metadata(run, state):
    endpoint, port = owned_endpoint(run, state)
    # Direct loopback HTTP: do not use proxies or follow a redirect elsewhere.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/json/list")
        response = connection.getresponse()
        if response.status != 200:
            raise ResearchError("Owned browser target listing failed")
        value = json.loads(response.read())
    except (OSError, ValueError, http.client.HTTPException):
        raise ResearchError("Owned browser target listing is unavailable") from None
    finally:
        connection.close()
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ResearchError("Unexpected owned browser target listing")
    # Never retain titles, favicons, previews, or another tab's contents.
    return [{key: row.get(key) for key in ("id", "type", "url", "webSocketDebuggerUrl")}
            for row in value if row.get("type") == "page"]


def checked_target(target, endpoint):
    target_id, address = target.get("id", ""), target.get("webSocketDebuggerUrl", "")
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", endpoint):
        raise ResearchError("Expected the owned loopback endpoint")
    if not isinstance(target_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target_id):
        raise ResearchError("Invalid owned target identity")
    try:
        ws, origin = urlparse(address), urlparse(endpoint)
        valid = (ws.scheme == "ws" and ws.hostname == "127.0.0.1" and ws.port == origin.port and
                 ws.username is None and ws.password is None and not ws.query and not ws.fragment and
                 ws.path == "/devtools/page/" + target_id)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ResearchError("Target websocket does not match the owned endpoint and target ID")
    url = target.get("url", "")
    try:
        parsed = urlparse(url)
        public = (url == "about:blank" or (parsed.scheme == "https" and parsed.hostname == "www.xiaohongshu.com" and
                  parsed.username is None and parsed.password is None and parsed.port is None and
                  re.fullmatch(r"/(?:explore|search_result)(?:/[a-zA-Z0-9]{16,40})?/?", parsed.path)))
    except (ValueError, TypeError):
        public = False
    if not public:
        if (isinstance(url, str) and re.match(r"^https://www\.xiaohongshu\.com/(?:chat|user/profile)(?:[/?#]|$)", url)
                and not re.search(r"error_code=(?:300017|300031)", url)):
            raise NoteScopeChanged("NOTE_SCOPE_CHANGED: bound target is not a public research note surface; no page body was read")
        raise TargetAccessUnconfirmed("ACCESS_STATE_UNCONFIRMED: target URL is outside the known public research surface")
    if isinstance(url, str) and re.search(r"error_code=(?:300017|300031)", url):
        raise TargetAccessUnconfirmed("ACCESS_STATE_UNCONFIRMED: restriction code in bound target URL")
    return {"target_id": target_id, "target_websocket": address, "url": url}


def save_target_binding(run, state, target, mode):
    value = checked_target(target, state["endpoint"])
    record = {"at": now(), "mode": mode, "endpoint": state["endpoint"], **value,
              "prior_target_id": state.get("target_id"), "url_only_checked": True, "other_tabs_modified": False}
    file = run / "diagnostics" / ("browser-target-binding-" + uuid.uuid4().hex + ".json")
    write_json(file, record)
    state.update(target_id=value["target_id"], target_websocket=value["target_websocket"],
                 target_binding_record=str(file))
    write_json(run / "browser.json", state)
    return value


def bound_target(run, state, explicit_target_id=None, initial=False):
    pages = target_metadata(run, state)
    bound_id = state.get("target_id")
    if explicit_target_id is not None:
        if bound_id and explicit_target_id != bound_id:
            raise TargetBindingRequired("An existing target binding cannot be replaced by another tab")
        chosen_id = explicit_target_id
    elif bound_id:
        chosen_id = bound_id
    else:
        if len(pages) != 1:
            raise TargetBindingRequired("Multiple or missing page targets: use bind-target with the explicitly observed research target ID")
        chosen_id = pages[0]["id"]
    matches = [row for row in pages if row.get("id") == chosen_id]
    if len(matches) != 1:
        if bound_id:
            stop(run, "ACCESS_STATE_UNCONFIRMED: bound research target is missing or ambiguous; no other tab was selected")
        raise TargetBindingRequired("The explicit target ID is not a unique page in this owned browser")
    try:
        value = checked_target(matches[0], state["endpoint"])
    except NoteScopeChanged:
        raise
    except TargetAccessUnconfirmed:
        stop(run, "ACCESS_STATE_UNCONFIRMED: bound research target URL is restricted or outside the known scope; no body was read")
    except ResearchError:
        if bound_id:
            stop(run, "ACCESS_STATE_UNCONFIRMED: bound target websocket changed or cannot be verified")
        raise
    if not bound_id:
        value = save_target_binding(run, state, matches[0], "explicit" if explicit_target_id else "initial" if initial else "legacy_single_page")
    elif state.get("target_websocket") != value["target_websocket"]:
        stop(run, "ACCESS_STATE_UNCONFIRMED: bound research websocket changed; preserve binding")
    return value


def bind_target(run, target_id):
    with account_lock(run):
        ensure_active(run)
        state = read_json(run / "browser.json", {})
        value = bound_target(run, state, explicit_target_id=target_id)
        return {"bound_target_id": value["target_id"], "url_only_checked": True,
                "binding_record": state.get("target_binding_record"), "other_tabs_modified": False}


def process_command(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise ResearchError("Could not inspect owned browser process; preserve its files")
    return result.stdout.strip() if result.returncode == 0 else ""


def stop_browser(run):
    state = read_json(run / "browser.json")
    profile = run / "browser"
    if not state:
        if profile.exists():
            raise ResearchError("Untracked browser directory; preserve it for inspection")
        return
    if state.get("profile") != str(profile) or profile.is_symlink():
        raise ResearchError("Browser ownership mismatch; no process or directory was changed")
    pid = state.get("pid")
    if not isinstance(pid, int) or pid < 2:
        raise ResearchError("Missing owned browser PID; preserve browser directory")
    command = process_command(pid)
    if command:
        if "--user-data-dir=" + str(profile) not in command:
            raise ResearchError("PID no longer identifies this run's browser; no process was terminated")
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and process_command(pid):
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            time.sleep(0.2)
        if process_command(pid):
            raise ResearchError("Temporary browser is still running; preserve its profile")
    if profile.exists():
        shutil.rmtree(profile)
    (run / "browser.json").unlink(missing_ok=True)


def cleanup(run):
    # Remove the standalone cookie copy even if browser shutdown needs attention.
    cookie_copy = run / ".session" / "cookies.json"
    cookie_copy.unlink(missing_ok=True)
    stop_browser(run)
    session = run / ".session"
    if session.exists():
        shutil.rmtree(session)
    manifest = read_json(run / "run.json")
    manifest.update(closed=True, closed_at=now())
    write_json(run / "run.json", manifest)
    result = {"at": now(), "temporary_browser_removed": not (run / "browser").exists(),
              "temporary_credentials_removed": not session.exists(), "original_browser_modified_or_logged_out": False}
    write_json(run / "cleanup.json", result)
    return result


def start_browser(run, args):
    with account_lock(run):
        return _start_browser(run, args)


def _start_browser(run, args):
    ensure_active(run)
    if (run / "browser.json").exists() or (run / "browser").exists():
        raise ResearchError("An owned browser already exists; inspect it instead of replacing it")
    cookies = run / ".session" / "cookies.json"
    if not cookies.is_file() or cookies.is_symlink() or cookies.stat().st_mode & 0o077:
        raise ResearchError("An authorized, private temporary login copy is required")
    package = Path(args.opencli_root).expanduser().resolve()
    metadata = read_json(package / "package.json", {})
    if metadata.get("name") != "@jackwener/opencli":
        raise ResearchError("opencli-root must identify the installed @jackwener/opencli package")
    cli = package / "dist/src/main.js"
    bridge = package / "dist/src/browser/cdp.js"
    if not cli.is_file() or not bridge.is_file():
        raise ResearchError("Installed OpenCLI layout changed; inspect before proceeding")
    chrome = Path(args.chrome).expanduser().resolve()
    if not chrome.is_file():
        raise ResearchError("Chrome executable not found")
    node = shutil.which(args.node) or (str(Path(args.node).expanduser().resolve()) if Path(args.node).expanduser().is_file() else None)
    if not node:
        raise ResearchError("Node executable not found")
    profile = run / "browser"
    profile.mkdir(mode=0o700)
    command = [str(chrome), "--user-data-dir=" + str(profile), "--remote-debugging-address=127.0.0.1",
               "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check", "--new-window", "about:blank"]
    try:
        with (run / ".session" / "chrome.log").open("w") as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except Exception:
        profile.rmdir()
        raise
    state = {"pid": process.pid, "profile": str(profile), "node": node,
             "opencli_root": str(package), "version": metadata.get("version"), "at": now()}
    write_json(run / "browser.json", state)
    try:
        ready = False
        for _ in range(30):
            if process.poll() is not None:
                raise ResearchError("Temporary Chrome exited during startup")
            try:
                # This file is created by our new profile, avoiding a port-reuse race.
                port = int((profile / "DevToolsActivePort").read_text().splitlines()[0])
                endpoint = f"http://127.0.0.1:{port}"
                with urlopen(endpoint + "/json/version", timeout=1) as response:
                    ready = bool(json.load(response).get("webSocketDebuggerUrl"))
            except (OSError, ValueError, IndexError):
                pass
            if ready:
                break
            time.sleep(0.3)
        if not ready:
            raise ResearchError("Temporary browser did not expose its local debugging endpoint")
        state["endpoint"] = endpoint
        write_json(run / "browser.json", state)
        target = bound_target(run, state, initial=True)
        bootstrap = Path(__file__).with_name("prepare_browser.mjs")
        result = subprocess.run([node, str(bootstrap), str(bridge), endpoint, str(cookies),
                                 target["target_websocket"], target["target_id"]],
                                capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise ResearchError("Temporary browser preparation failed; inspect the dependency version")
    except Exception:
        stop_browser(run)
        raise
    return {"temporary_browser_ready": True, "opencli_version": metadata.get("version"), "original_browser_modified": False}


def browser_comments(run, value, limit):
    # The upstream CLI retries access restrictions. Both entry points instead
    # use our one-navigation reader so a restriction stops immediately.
    return deep_comments(run, value, limit, 60, 90)


def hydrate_reply_body_captures(run, note_id, previous):
    """Rebuild rendered-body receipts only from this run's linked raw snapshots.

    Legacy completion booleans cannot distinguish a real body from a nickname
    prefix. Preserve real raw evidence, but never promote API rows or nicknames.
    """
    progress = json.loads(json.dumps(previous or {}))
    threads = progress.get("threads", {})
    for thread in threads.values():
        thread["replyBodyCaptures"] = {}
    raw_dir = Path(run) / "raw"
    hashes = dict(progress.get("browserSnapshotSha256", {}))
    skipped, observed, conflicts, known_by_parent = [], {}, set(), {}
    id_pattern = r"[a-fA-F0-9]{16,40}"
    for source in progress.get("snapshots", []):
        if not isinstance(source, str):
            skipped.append({"reason": "invalid_snapshot_path"})
            continue
        path = Path(source)
        if (raw_dir.is_symlink() or path.is_symlink() or not path.is_absolute() or
                path.parent != raw_dir or not path.name.startswith("browser-deep-" + note_id + "-") or
                path.suffix != ".json"):
            skipped.append({"source": str(path), "reason": "not_same_run_note_raw_snapshot"})
            continue
        try:
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if hashes.get(str(path)) and hashes[str(path)] != digest:
                skipped.append({"source": str(path), "reason": "snapshot_sha256_changed"})
                continue
            snapshot = json.loads(content)
        except (OSError, ValueError):
            skipped.append({"source": str(path), "reason": "snapshot_unreadable"})
            continue
        if (not isinstance(snapshot, dict) or snapshot.get("operation") != "browser_comments" or snapshot.get("note_id") != note_id or
                snapshot.get("ok") is not True or not isinstance(snapshot.get("data"), list)):
            skipped.append({"source": str(path), "reason": "snapshot_scope_or_transport_mismatch"})
            continue
        hashes[str(path)] = digest
        for row in snapshot["data"]:
            if not isinstance(row, dict) or row.get("is_reply") is not True:
                continue
            cid, parent = row.get("commentId", ""), row.get("parentCommentId", "")
            if not (isinstance(cid, str) and isinstance(parent, str) and
                    re.fullmatch(id_pattern, cid) and re.fullmatch(id_pattern, parent)):
                continue
            known_by_parent.setdefault(parent, set()).add(cid)
            href = str(row.get("authorHrefRaw", "")).split("?", 1)[0].split("#", 1)[0]
            author = re.fullmatch(r"(?:https://www\.xiaohongshu\.com)?/user/profile/(" + id_pattern + r")/?", href)
            if not author:
                continue
            identity = (parent, author[1])
            if cid in observed and observed[cid][0] != identity:
                conflicts.add(cid)
            observed.setdefault(cid, (identity, None))
            text = row.get("text", "")
            text = text.strip() if isinstance(text, str) else ""
            source_kind = row.get("textSource", "rendered_text")
            ordinary_images = (isinstance(row.get("images"), list) and bool(row["images"]) and
                               all(isinstance(image, str) and image for image in row["images"]))
            inline = row.get("renderedInlineImages")
            inline_images = (isinstance(inline, list) and bool(inline) and
                             all(isinstance(image, dict) and image.get("observedClass") == "note-content-emoji" and
                                 isinstance(image.get("url"), str) and
                                 type(image.get("domOrdinal")) is int and image["domOrdinal"] > 0
                                 for image in inline))
            if inline_images:
                inline_images = len({image["domOrdinal"] for image in inline}) == len(inline)
            actual_media = (source_kind == "media_placeholder" and text == "[图片评论]" and
                            (ordinary_images or inline_images and any(image["url"] for image in inline)))
            # Conservative migration filter only; never modify historical text.
            # An ambiguous prefix-shaped sentence is insufficient body evidence.
            if (not actual_media and (not text or text == "[图片评论]" or re.fullmatch(r"回复\s*.+?\s*[:：]", text) or
                    source_kind not in {"rendered_text", "rendered_emoji_alt", "rendered_emoji_accessible_label"})):
                continue
            if source_kind.startswith("rendered_emoji_") and not row.get("renderedImageLabels"):
                continue
            receipt = {"commentId": cid, "parentCommentId": parent, "authorId": author[1],
                       "text": text, "textSource": source_kind, "sourceSnapshot": str(path), "sourceSha256": digest}
            if ordinary_images:
                receipt["images"] = row["images"]
            if inline_images:
                # Source/author binding above remains mandatory. This receipt
                # proves capture only. Blank adjacent sources stay media gaps,
                # not a reason to discard a complete source-bound observation.
                receipt["renderedInlineImages"] = inline
            observed[cid] = (identity, receipt)
    for cid, (_, receipt) in observed.items():
        if cid not in conflicts and receipt and "id:" + receipt["parentCommentId"] in threads:
            threads["id:" + receipt["parentCommentId"]]["replyBodyCaptures"][cid] = receipt
    for key, thread in threads.items():
        parent = thread.get("commentId") or key.removeprefix("id:")
        known_ids = set(thread.get("legacyReplyBodyIds", [])) | known_by_parent.get(parent, set())
        observed_count = thread.get("uniqueObservedReplyCount")
        if not isinstance(observed_count, int) or observed_count < 0:
            observed_count = thread.get("visibleReplies", 0)
        counts = [observed_count, thread.get("replyBodyMinimumCount", 0)]
        minimum = max([len(known_ids)] + [count for count in counts if isinstance(count, int) and count >= 0])
        receipts = thread["replyBodyCaptures"]
        thread["legacyReplyBodyIds"] = sorted(known_ids)
        thread["replyBodyMinimumCount"] = minimum
        pending = len(receipts) < minimum or bool(known_ids - receipts.keys())
        thread["replyBodyVerificationPending"] = pending
        if pending:
            thread["replyContentCaptured"] = False
            if thread.get("state") in {"visible_replies_read", "previously_read"}:
                thread.update(state="stalled", reason="legacy_reply_body_unverified",
                              reopenedBy="linked_raw_body_verification")
    progress["browserSnapshotSha256"] = hashes
    progress["replyBodyMigration"] = {"scope": "same_run_linked_rendered_snapshots_only",
                                      "skipped": skipped, "identityConflicts": sorted(conflicts)}
    return progress


def _observed_reply_intent_progress(thread, rows, note_id=None, previous=None):
    """Accept the same submitted intent's identity-bound rendered evidence only."""
    pending, proof = thread.get("pendingReplyExpansion"), thread.get("lastReplyExpansion")
    if not isinstance(pending, dict) or not isinstance(proof, dict):
        return False
    clicked = pending.get("clickedAt")
    observed = proof.get("observedAt")
    if (type(clicked) not in {int, float} or not 0 < clicked < float("inf") or proof.get("clickedAt") != clicked or
            type(observed) not in {int, float} or not clicked <= observed < float("inf") or
            proof.get("state") != "render_progress_observed"):
        return False
    for field in ("documentTimeOrigin", "expanderText"):
        if field in pending or field in proof:
            if pending.get(field) != proof.get(field):
                return False
    before, proof_before, after = (pending.get("beforeReplyIds"), proof.get("beforeReplyIds"),
                                  proof.get("observedReplyIds"))
    if not all(isinstance(ids, list) and all(isinstance(cid, str) and
            re.fullmatch(r"[a-fA-F0-9]{16,40}", cid) for cid in ids) for ids in (before, proof_before, after)):
        return False
    if set(before) != set(proof_before):
        return False
    parent = thread.get("commentId")
    if (not isinstance(parent, str) or not re.fullmatch(r"[a-fA-F0-9]{16,40}", parent) or
            thread.get("rootKey") != "id:" + parent):
        return False
    captures = thread.get("replyBodyCaptures", {})

    def reliable(body, cid):
        if not isinstance(body, dict):
            return False
        text, author = body.get("text"), body.get("authorId")
        return (body.get("commentId") == cid and body.get("parentCommentId") == parent and
                isinstance(author, str) and re.fullmatch(r"[a-fA-F0-9]{16,40}", author) and
                isinstance(text, str) and bool(text.strip()) and text != "[图片评论]" and
                not re.fullmatch(r"回复\s*.+?\s*[:：]", text.strip()) and
                body.get("textSource", "rendered_text") in {
                    "rendered_text", "rendered_emoji_alt", "rendered_emoji_accessible_label"})

    if proof.get("progressBasis") == "new_rendered_reply_id":
        # A real new ID proves expansion even when its body is an unread image.
        # Unlike changed text, it need not supply an author's identity or prose.
        return any((isinstance(captures.get(cid), dict) and captures[cid].get("commentId") == cid and
                    captures[cid].get("parentCommentId") == parent) or
                   any(row.get("commentId") == cid and row.get("parentCommentId") == parent and
                       row.get("is_reply") is True for row in rows)
                   for cid in set(after) - set(before))
    if proof.get("progressBasis") == "known_reply_render_restored":
        # Restoring a known DOM row resolves only this click; it is no new
        # research evidence and cannot borrow an unverified current cache.
        known = pending.get("beforeKnownReplyIds")
        baseline = pending.get("beforeKnownReplyBodyCaptures")
        restored = proof.get("restoredReplyIds")
        if (proof.get("newResearchEvidence") is not False or not isinstance(note_id, str) or
                not re.fullmatch(r"[a-fA-F0-9]{24}", note_id) or
                pending.get("noteId") != note_id or proof.get("noteId") != note_id or
                pending.get("rootCommentId") != parent or proof.get("rootCommentId") != parent or
                not isinstance(baseline, dict) or baseline != proof.get("beforeKnownReplyBodyCaptures") or
                known != proof.get("beforeKnownReplyIds") or
                not all(isinstance(ids, list) and all(isinstance(cid, str) and
                    re.fullmatch(r"[a-fA-F0-9]{16,40}", cid) for cid in ids) for ids in (known, restored)) or
                not restored or not set(after) <= set(known) or not isinstance(previous, dict) or
                previous.get("commentId") != parent or previous.get("rootKey") != "id:" + parent):
            return False
        trusted = previous.get("replyBodyCaptures", {})

        def same_body(first, second):
            fields = ("commentId", "parentCommentId", "authorId", "text", "textSource")
            return all(first.get(k, "rendered_text" if k == "textSource" else None) ==
                       second.get(k, "rendered_text" if k == "textSource" else None) for k in fields)

        for cid in set(restored) & set(after) & set(known) - set(before):
            old_body, current, cached = baseline.get(cid), captures.get(cid), trusted.get(cid)
            if isinstance(old_body, dict) and isinstance(old_body.get("text"), str) and re.fullmatch(
                    r"(?:原|该|此)?评论已(?:被)?删除[。.!！]?", re.sub(r"\s+", "", old_body["text"])):
                continue
            if not (reliable(old_body, cid) and reliable(current, cid) and reliable(cached, cid) and
                    same_body(old_body, current) and same_body(old_body, cached)):
                continue
            for row in rows:
                href = str(row.get("authorHrefRaw", "")).split("?", 1)[0].split("#", 1)[0]
                author = re.fullmatch(r"(?:https://www\.xiaohongshu\.com)?/user/profile/([a-fA-F0-9]{16,40})/?", href)
                observed_body = dict(row, authorId=author[1]) if author else {}
                if row.get("is_reply") is True and reliable(observed_body, cid) and same_body(old_body, observed_body):
                    return True
        return False
    if proof.get("progressBasis") != "changed_verified_reply_body":
        return False
    baseline = pending.get("beforeReplyBodyCaptures")
    if not isinstance(baseline, dict) or baseline != proof.get("beforeReplyBodyCaptures"):
        return False
    return any(reliable(baseline.get(cid), cid) and reliable(captures.get(cid), cid) and
               baseline[cid]["authorId"] == captures[cid]["authorId"] and
               baseline[cid]["text"] != captures[cid]["text"] for cid in set(after) & set(before))


def _typed_reply_intent_retirement(thread, note_id):
    """A replacement document can retire one exact old intent, never a new one."""
    pending = thread.get("pendingReplyExpansion")
    if not isinstance(pending, dict) or not isinstance(note_id, str) or not re.fullmatch(r"[a-fA-F0-9]{24}", note_id):
        return None
    before = pending.get("beforeReplyIds")
    if (not isinstance(before, list) or not all(isinstance(cid, str) and
            re.fullmatch(r"[a-fA-F0-9]{16,40}", cid) for cid in before) or
            not isinstance(pending.get("expanderText"), str) or not pending["expanderText"] or
            pending.get("state") not in {"click_intent_recorded", "click_submitted", "outcome_unconfirmed"}):
        return None
    parent = thread.get("commentId")
    if (not isinstance(parent, str) or not re.fullmatch(r"[a-fA-F0-9]{16,40}", parent) or
            thread.get("rootKey") != "id:" + parent):
        return None
    for retired in thread.get("retiredReplyExpansions", []):
        if not isinstance(retired, dict) or retired.get("state") != "retired_by_replacement_document":
            continue
        if any(pending.get(k) != retired.get(k) for k in
               ("clickedAt", "documentTimeOrigin", "beforeReplyIds", "beforeReplyBodyCaptures", "expanderText")):
            continue
        proof = retired.get("replacementDocument")
        if not isinstance(proof, dict) or (proof.get("basis"), proof.get("noteId"), proof.get("rootCommentId")) != (
                "current_document_created_after_original_click", note_id, parent):
            continue
        clicked, epoch, observed, at = (pending.get("clickedAt"), proof.get("currentDocumentTimeOrigin"),
                                        proof.get("observedAt"), retired.get("retiredAt"))
        if not all(type(t) in {int, float} and 0 < t < float("inf") for t in (clicked, epoch, observed, at)):
            continue
        prior = pending.get("documentTimeOrigin")
        if (proof.get("originalClickedAt") != clicked or proof.get("previousDocumentTimeOrigin") != prior or
                not clicked < epoch <= observed <= at or (prior is not None and
                (type(prior) not in {int, float} or not 0 < prior <= clicked))):
            continue
        control = proof.get("strictExpanderText")
        if (not isinstance(control, str) or len(control) > 28 or not re.fullmatch(
                r"(?:展开(?:\s*更多)?(?:\s*\d+\s*条)?\s*回复|更多回复|全部回复|查看(?:全部|更多|\d+条)?回复|共\s*\d+\s*条回复)", control)):
            continue
        return retired
    return None


def _root_end_basis(coverage):
    if not isinstance(coverage, dict):
        return None
    if coverage.get("explicitEnd") is True:
        return "rendered_end_signal"
    empty = coverage.get("emptyCommentState")
    counts = coverage.get("commentCountObservation") or {}
    if not isinstance(counts, dict):
        counts = {}
    if (coverage.get("rootEndBasis") == "rendered_comment_empty_state" and
            isinstance(empty, dict) and empty.get("scope") == "rendered_comments_container_empty_state" and
            empty.get("acceptedAsEnd") is True and empty.get("visible") is True and
            empty.get("loadingVisible") is False and isinstance(empty.get("text"), str) and
            empty["text"].strip() and not counts.get("latest", {}).get("positiveTotalObserved") and
            not coverage.get("observedRootIds") and not coverage.get("threads")):
        return "rendered_comment_empty_state"
    if (coverage.get("apiRootsComplete") is True and coverage.get("expectedRootIds") and
            set(coverage["expectedRootIds"]) <= set(coverage.get("observedRootIds", []))):
        return "complete_api_root_list"
    return None


def _prior_root_end_evidence(progress, snapshot, snapshot_file):
    """Recover a real legacy end from linked, same-note, hash-bound raw only."""
    if basis := _root_end_basis(progress):
        return {"basis": basis, "source_file": progress.get("last_snapshot"),
                "source_kind": "previous_explicit_end_observation"}
    current = Path(snapshot_file)
    coverage = snapshot.get("coverage", {})
    note = snapshot.get("note_id") or coverage.get("rootScrollProgress", {}).get("noteId")
    if not note and current.is_file() and not current.is_symlink():
        try:
            raw = json.loads(current.read_text())
            if not isinstance(raw, dict):
                return None
            note = raw.get("note_id") or re.search(r"/([a-fA-F0-9]{24})(?:/|$)",
                urlparse(raw.get("url") or raw.get("pageUrl") or "").path)[1]
        except (OSError, ValueError, TypeError):
            return None
    if not isinstance(note, str) or not re.fullmatch(r"[a-fA-F0-9]{24}", note):
        return None
    candidates = [e.get("source_file") for e in progress.get("rootEndEvidence", []) if isinstance(e, dict)]
    candidates += [progress.get("last_snapshot")] + list(reversed(progress.get("snapshots", [])))
    for value in dict.fromkeys(value for value in candidates if isinstance(value, str)):
        source = Path(value)
        if (source.is_symlink() or not source.is_file() or
                source.parent.resolve() != current.parent.resolve()):
            continue
        try:
            raw_bytes = source.read_bytes()
            digest = hashlib.sha256(raw_bytes).hexdigest()
            expected = source.stem if re.fullmatch(r"[a-f0-9]{64}", source.stem) else progress.get(
                "browserSnapshotSha256", {}).get(str(source))
            if digest != expected:
                continue
            raw = json.loads(raw_bytes)
            if (not isinstance(raw, dict) or raw.get("ok") is False or raw.get("error") or
                    any(raw.get(flag) is True for flag in ("loginWall", "securityBlock", "notFound",
                        "partial", "partial_prior_note_only"))):
                continue
            raw_note = raw.get("note_id") or re.search(r"/([a-fA-F0-9]{24})(?:/|$)",
                urlparse(raw.get("url") or raw.get("pageUrl") or "").path)[1]
            old_coverage = raw.get("deepCoverage", raw.get("coverage", {}))
            basis = _root_end_basis(old_coverage)
            if raw_note == note and basis:
                return {"basis": basis, "source_file": str(source), "source_sha256": digest,
                        "source_kind": "linked_original_snapshot"}
        except (OSError, ValueError, TypeError):
            continue
    return None


def merge_browser_progress(previous, snapshot, snapshot_file):
    """Keep monotonic observations; a smaller snapshot cannot erase older roots."""
    progress = dict(previous or {})
    coverage = snapshot.get("coverage", {})
    threads = dict(progress.get("threads", {}))
    for incoming in coverage.get("threads", []):
        key = incoming["rootKey"]
        old = threads.get(key, {})
        value = dict(incoming)
        if value.get("state") == "previously_read":
            value["state"] = old.get("state", "not_processed")
        if old.get("state") == "visible_replies_read" and value.get("state") == "not_processed":
            value["state"] = old["state"]
        value["visibleReplies"] = max(value.get("visibleReplies", 0), old.get("visibleReplies", 0))
        value["last_snapshot"] = snapshot_file
        merged = {**old, **value}
        # Per-thread maps and minima are cumulative, just like the global ID sets.
        if "replyBodyCaptures" in old or "replyBodyCaptures" in value:
            merged["replyBodyCaptures"] = {**old.get("replyBodyCaptures", {}),
                                           **value.get("replyBodyCaptures", {})}
        for field in ("replyBodyMinimumCount", "uniqueObservedReplyCount"):
            counts = [t[field] for t in (old, value) if type(t.get(field)) is int and t[field] >= 0]
            if counts:
                merged[field] = max(counts)
        if "legacyReplyBodyIds" in old or "legacyReplyBodyIds" in value:
            merged["legacyReplyBodyIds"] = sorted(set(old.get("legacyReplyBodyIds", [])) |
                                                   set(value.get("legacyReplyBodyIds", [])))
        retired = list(old.get("retiredReplyExpansions", []))
        for item in value.get("retiredReplyExpansions", []):
            if item not in retired:
                retired.append(item)
        if retired:
            merged["retiredReplyExpansions"] = retired
        history = list(old.get("reconciledReplyExpansions", []))
        for item in value.get("reconciledReplyExpansions", []):
            if item not in history:
                history.append(item)
        note_id = snapshot.get("note_id") or coverage.get("rootScrollProgress", {}).get("noteId")
        progressed = _observed_reply_intent_progress(merged, snapshot.get("data", []), note_id, old)
        retired_proof = _typed_reply_intent_retirement(merged, note_id)
        if progressed or retired_proof:
            record = {"pendingReplyExpansion": merged.pop("pendingReplyExpansion")}
            record["lastReplyExpansion" if progressed else "retiredReplyExpansion"] = (
                merged["lastReplyExpansion"] if progressed else retired_proof)
            if record not in history:
                history.append(record)
            if merged.get("reason") == "reply_expansion_outcome_unconfirmed":
                merged.update(state="not_processed", reason="", processed=False)
        if history:
            merged["reconciledReplyExpansions"] = history
        threads[key] = merged
    ids = set(progress.get("observedRootIds", [])) | set(coverage.get("observedRootIds", []))
    known_rows = set(progress.get("observedCommentIds", []))
    uncertain_rows = dict(progress.get("uncertainRowsBySnapshot", {}))
    uncertain_rows[snapshot_file] = sum(not row.get("commentId") for row in snapshot.get("data", []))
    known_rows.update(row["commentId"] for row in snapshot.get("data", []) if row.get("commentId"))
    missing_ids = sorted(set(progress.get("expectedRootIds", [])) - ids)
    unresolved = [key for key, thread in threads.items() if thread.get("state") != "visible_replies_read" or
                  thread.get("identityAmbiguous") or thread.get("pendingReplyExpansion")]
    root_state = coverage.get("rootPaginationState", "pending")
    old_complete = progress.get("rootPaginationState") == "complete"
    prior_end = _prior_root_end_evidence(progress, snapshot, snapshot_file) if old_complete else None
    if prior_end:
        evidence = list(progress.get("rootEndEvidence", []))
        if prior_end not in evidence:
            evidence.append(prior_end)
        progress["rootEndEvidence"] = evidence
    # This records a bounded UI scroll intention/observation, independently of
    # cumulative root evidence. A smaller/legacy snapshot must not erase it.
    incoming_scroll = coverage.get("rootScrollProgress")
    if isinstance(incoming_scroll, dict):
        old_scroll = progress.get("rootScrollProgress", {})
        new_roots = {row.get("commentId") for row in snapshot.get("data", [])
                     if row.get("commentId") and not row.get("is_reply")} - set(progress.get("observedRootIds", []))
        known_root_end = (progress.get("apiRootsComplete") is True and bool(progress.get("expectedRootIds")) and
                          set(progress["expectedRootIds"]) <= set(coverage.get("observedRootIds", [])) and
                          coverage.get("rootPaginationState") == "complete")
        # An actually rendered empty component is also a terminal observation,
        # but cannot erase any cumulative identified or unidentified comment.
        claimed_empty_end = coverage.get("rootEndBasis") == "rendered_comment_empty_state"
        rendered_empty_end = _root_end_basis(coverage) == "rendered_comment_empty_state"
        known_empty_end = (root_state == "complete" and rendered_empty_end and
                           type(coverage.get("rootsInDomAtEnd")) is int and coverage["rootsInDomAtEnd"] == 0 and
                           not ids and not known_rows and not threads and not any(uncertain_rows.values()) and
                           not progress.get("expectedRootIds") and not coverage.get("expectedRootIds") and
                           not snapshot.get("data"))
        older = incoming_scroll.get("attempts", 0) < old_scroll.get("attempts", 0)
        unsupported_reset = (incoming_scroll.get("attempts", 0) == old_scroll.get("attempts", 0) and
                             incoming_scroll.get("stalledRounds", 0) < old_scroll.get("stalledRounds", 0) and
                             not ((new_roots and not claimed_empty_end) or coverage.get("explicitEnd") is True or known_root_end or known_empty_end))
        if older or unsupported_reset:
            if not (old_complete and not prior_end):
                root_state = progress.get("rootPaginationState", root_state)
        else:
            progress["rootScrollProgress"] = incoming_scroll
    if old_complete and root_state != "complete":
        if prior_end:
            root_state = "complete"  # A smaller view cannot erase an actual end.
        else:
            claims = list(progress.get("legacyRootCompletionClaims", []))
            claim = {"rootPaginationState": "complete", "status": progress.get("status"),
                     "last_snapshot": progress.get("last_snapshot"),
                     "snapshots": list(progress.get("snapshots", [])),
                     "reason": "no_verified_end_or_empty_state_in_linked_evidence"}
            if claim not in claims:
                claims.append(claim)
            progress["legacyRootCompletionClaims"] = claims
    if missing_ids:
        root_state = "stalled" if coverage.get("explicitEnd") or coverage.get("stalledScrollRounds", 0) >= 3 else "pending"
    complete = root_state == "complete" and not unresolved and not missing_ids
    progress.update(format="xiaohongshu-browser-progress-v2", threads=threads,
                    observedRootIds=sorted(ids), observedCommentIds=sorted(known_rows),
                    uncertainRowsBySnapshot=uncertain_rows, missingExpectedRootIds=missing_ids,
                    unresolvedRootKeys=unresolved, rootPaginationState=root_state,
                    status="visible_scope_complete" if complete else "stalled" if root_state == "stalled" else "pending",
                    completion_scope="visible_snapshot_not_platform_exhaustion",
                    updated_at=now(), last_snapshot=snapshot_file,
                    snapshots=list(dict.fromkeys(progress.get("snapshots", []) + [snapshot_file])))
    return progress


def deep_comments(run, value, roots, reply_clicks, budget_seconds, resume=False, progress_path=None):
    with account_lock(run):
        return _deep_comments(run, value, roots, reply_clicks, budget_seconds, resume, progress_path)


def _deep_comments(run, value, roots, reply_clicks, budget_seconds, resume=False, progress_path=None):
    ensure_active(run)
    state = read_json(run / "browser.json", {})
    profile = run / "browser"
    if not state or state.get("profile") != str(profile) or "--user-data-dir=" + str(profile) not in process_command(state["pid"]):
        raise ResearchError("Owned temporary browser is unavailable")
    nid, context = resolve_note(run, value)
    reader = Path(__file__).with_name("deep_comments.mjs")
    revision = hashlib.sha256(reader.read_bytes()).hexdigest()[:12]
    legacy_path = run / "raw" / f"browser-deep-{nid}-{roots:03d}-{reply_clicks:03d}-{budget_seconds}-{revision}.json"
    progress_path = Path(progress_path) if progress_path else run / "progress" / f"browser-{nid}.json"
    if progress_path.is_symlink():
        raise ResearchError("Browser progress must not be a symlink")
    progress = read_json(progress_path, {}) if resume else {}
    if resume:
        progress = hydrate_reply_body_captures(run, nid, progress)
    if not resume and legacy_path.exists() and read_json(legacy_path).get("ok"):
        return {"cached": True, "file": str(legacy_path)}
    # Resume always creates a fresh immutable snapshot, even with identical budgets.
    path = legacy_path.with_name(legacy_path.stem + "-" + uuid.uuid4().hex[:12] + ".json") if resume else legacy_path
    target = bound_target(run, state)
    config = {"endpoint": state["endpoint"], "targetId": target["target_id"], "targetWebSocketUrl": target["target_websocket"],
              "opencliRoot": state["opencli_root"], "url": context["url"],
              "roots": roots, "maxReplyClicks": reply_clicks, "budgetMs": budget_seconds * 1000, "progress": progress}
    command = [state["node"], str(reader), json.dumps(config)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=budget_seconds + 50)
    except subprocess.TimeoutExpired:
        # Old successful progress remains intact; absence of output is not completion.
        write_json(path, {"operation": "browser_comments", "note_id": nid, "ok": False,
                          "captured_at": now(), "error": {"code": "BROWSER_TIMEOUT"}})
        stop(run, "ACCESS_STATE_UNCONFIRMED: browser batch timed out; preserve progress and inspect access state")
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        raise ResearchError("Deep comment reader returned an unexpected response") from None
    if result.returncode or not payload.get("ok"):
        error = payload.get("error", {})
        partial = payload.get("partial", {})
        write_json(path, {"operation": "browser_comments", "mode": "deep", "note_id": nid,
                          "ok": False, "captured_at": now(), "error": error,
                          "data": clean_data(partial.get("data", [])), "coverage": partial.get("coverage", {}),
                          "partial_prior_note_only": bool(partial)})
        if error.get("code") == "NOTE_SCOPE_CHANGED":
            raise NoteScopeChanged("NOTE_SCOPE_CHANGED: left the target note; preserve partial evidence and explicitly inspect scope before retry")
        if error.get("code") in {"SECURITY_BLOCK", "ACCESS_STATE_UNCONFIRMED"}:
            stop(run, "Browser access restricted or interrupted during deep read: " + error["code"])
        raise ResearchError("Deep comment read failed: " + str(error.get("message", "unknown")))
    rows = payload["data"]
    snapshot = {"operation": "browser_comments", "mode": "deep-resume" if resume else "deep", "note_id": nid,
                "url": context["url"], "ok": True, "captured_at": now(), "data": clean_data(rows),
                "coverage": payload["coverage"], "extractor_revision": revision, "all_replies_read": False}
    write_json(path, snapshot)
    progress.setdefault("browserSnapshotSha256", {})[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    progress = merge_browser_progress(progress, snapshot, str(path))
    write_json(progress_path, progress)
    return {"file": str(path), "root_rows": sum(not x["is_reply"] for x in rows),
            "reply_rows": sum(x["is_reply"] for x in rows), "coverage": payload["coverage"],
            "progress_file": str(progress_path), "status": progress["status"],
            "remaining_roots": len(progress["unresolvedRootKeys"]), "missing_expected_roots": progress["missingExpectedRootIds"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("start")
    p.add_argument("--opencli-root", required=True, help="Installed node_modules/@jackwener/opencli directory")
    p.add_argument("--node", default="node")
    p.add_argument("--chrome", default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    p = commands.add_parser("bind-target", help="Bind an explicitly observed research tab in this owned browser; URL metadata only")
    p.add_argument("--target-id", required=True)
    p = commands.add_parser("comments")
    p.add_argument("note")
    p.add_argument("--limit", type=int, choices=range(1, 51), metavar="1..50", default=50)
    p = commands.add_parser("deep", help="Bounded UI expansion beyond the adapter's 50-root/three-round limits")
    p.add_argument("note")
    p.add_argument("--roots", type=int, choices=range(1, 201), metavar="1..200", default=100)
    p.add_argument("--reply-clicks", type=int, choices=range(1, 121), metavar="1..120", default=60)
    p.add_argument("--budget-seconds", type=int, choices=range(10, 151), metavar="10..150", default=90)
    p.add_argument("--resume", action="store_true", help="Continue unfinished root threads; write a new snapshot")
    p.add_argument("--progress-path", type=Path, help="Project-owned progress file, independent of temporary session")
    commands.add_parser("cleanup")
    args = parser.parse_args()
    try:
        run = load_run(args.run)
        with run_lock(run):
            if args.command == "start": result = start_browser(run, args)
            elif args.command == "bind-target": result = bind_target(run, args.target_id)
            elif args.command == "comments": result = browser_comments(run, args.note, args.limit)
            elif args.command == "deep": result = deep_comments(run, args.note, args.roots, args.reply_clicks, args.budget_seconds, args.resume, args.progress_path)
            else: result = cleanup(run)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        message = str(exc) if isinstance(exc, ResearchError) else "Inspect local environment: " + type(exc).__name__
        print(json.dumps({"ok": False, "type": type(exc).__name__, "message": message, "code": getattr(exc, "code", None)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
