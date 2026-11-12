#!/usr/bin/env python3
"""Read-only, credential-free startup and analyst-handoff diagnostics."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile

# Importing the scheduler must not create local bytecode as a side effect of check.
sys.dont_write_bytecode = True
import xhs_batch as batch

FORMAT = "xhs-flow-check-v1"


class DiagnosticError(RuntimeError):
    pass


def read_metadata(path):
    """Only callers' fixed metadata filenames; never cookie/profile databases."""
    path = Path(path)
    if path.is_symlink():
        raise DiagnosticError("unsafe_metadata_path")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise DiagnosticError("invalid_metadata_file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DiagnosticError("unreadable_metadata") from None
    if not isinstance(value, dict):
        raise DiagnosticError("invalid_metadata_shape")
    return value


def account_guard_root():
    # Do not call legacy.account_root/account_dir: those helpers create folders.
    return Path(tempfile.gettempdir()) / ("xhs-research-accounts-" + str(os.getuid()))


def access_category(value):
    """Classify only known stop signals; never return raw errors or URLs."""
    reason = value.get("reason", "") if isinstance(value, dict) else value
    if not isinstance(reason, str):
        return "access_state_unknown"
    if "SessionExpiredError" in reason or "LOGIN_REQUIRED" in reason or "登录查看" in reason:
        return "login_required"
    if "NeedVerifyError" in reason or "CAPTCHA" in reason or "验证码" in reason:
        return "challenge_required"
    match = re.search(r"HTTP access/login/verification/rate restriction:\s*(\d{3})", reason)
    if match:
        return {"401": "login_required", "461": "challenge_required", "471": "challenge_required",
                "429": "rate_restricted", "403": "access_restricted"}.get(match[1], "access_state_unknown")
    if "IpBlockedError" in reason or "SignatureError" in reason:
        return "access_restricted"
    return "access_state_unknown"


def session_facts(run):
    facts = {"state": "not_selected", "online_login_verified": False,
             "browser": "not_inspected", "credential_contents_read": False}
    if run is None:
        return facts, [], None
    path = Path(run).expanduser()
    if path.is_symlink():
        raise DiagnosticError("unsafe_run_path")
    path = path.resolve()
    for child in (".session", "browser", "raw"):
        if (path / child).is_symlink():
            raise DiagnosticError("unsafe_managed_path")
    stops = []
    stop = read_metadata(path / "STOP.json")
    if stop is not None:
        stops.append({"scope": "run", "category": access_category(stop)})
    try:
        account = read_metadata(path / "account.json")
    except DiagnosticError:
        if stops:
            facts["state"] = "access_stopped"
            return facts, stops, None
        raise
    facts["account_binding_present"] = bool(account and account.get("key"))
    if account is not None:
        key = account.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise DiagnosticError("invalid_account_binding")
        root = account_guard_root()
        guard = root / key
        for folder in (root, guard):
            if folder.is_symlink() or (folder.exists() and (
                    not folder.is_dir() or folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o077)):
                raise DiagnosticError("unsafe_account_guard")
        stop = read_metadata(guard / "STOP.json")
        if stop is not None:
            stops.append({"scope": "account", "category": access_category(stop)})
    if stops:
        # Once a stop is known, missing dependencies or damaged run metadata must
        # never replace it with a preparation/recovery recommendation.
        facts["state"] = "access_stopped"
        return facts, stops, None
    manifest = read_metadata(path / "run.json")
    if not manifest or manifest.get("format") != "xiaohongshu-research-v1" or manifest.get("directory") != str(path):
        raise DiagnosticError("invalid_run_manifest")
    cleanup = read_metadata(path / "cleanup.json")
    cleaned = bool(cleanup and cleanup.get("temporary_browser_removed") is True and
                   cleanup.get("temporary_credentials_removed") is True)
    cookie_path = path / ".session" / "cookies.json"
    # A local copy's presence is not proof that the platform login is valid.
    cookie_private = (cookie_path.is_file() and not cookie_path.is_symlink() and
                      not cookie_path.stat().st_mode & 0o077)
    facts["private_login_copy_present"] = bool(cookie_private)
    browser = read_metadata(path / "browser.json")
    facts["browser"] = "metadata_present_unverified" if browser else "not_started"
    if browser:
        facts["fixed_target_recorded"] = bool(browser.get("target_id"))
    facts["state"] = ("cleaned" if cleaned else "closed" if manifest.get("closed") else
                      "login_copy_missing" if not cookie_private else
                      "account_binding_missing" if not facts["account_binding_present"] else "locally_reusable")
    facts["run_lock_present"] = (path / ".collection.lock").exists()
    return facts, stops, browser


def dependency_facts(browser=None):
    """Installed metadata/file presence only; no imports of clients or processes."""
    missing, versions = [], {}
    def version_label(value):
        # Do not echo arbitrary strings from dependency metadata.
        return value if isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+){1,3}(?:[-+.]?[0-9A-Za-z.]{1,32})?", value) else "unrecognized"
    if sys.version_info < (3, 10):
        missing.append("python_3_10_or_newer")
    try:
        versions["xiaohongshu_cli"] = version_label(importlib.metadata.version("xiaohongshu-cli"))
    except importlib.metadata.PackageNotFoundError:
        missing.append("xiaohongshu_cli_in_current_interpreter")
    if browser:
        node = browser.get("node")
        if not isinstance(node, str) or not (shutil.which(node) or Path(node).is_file()):
            missing.append("recorded_node_executable")
        root = browser.get("opencli_root")
        if not isinstance(root, str) or not root:
            missing.append("recorded_opencli_package")
        else:
            package = Path(root)
            metadata = read_metadata(package / "package.json")
            if not metadata or metadata.get("name") != "@jackwener/opencli":
                missing.append("recorded_opencli_package")
            else:
                versions["opencli"] = version_label(metadata.get("version"))
            if any(not (package / item).is_file() for item in ("dist/src/main.js", "dist/src/browser/cdp.js")):
                missing.append("recorded_opencli_layout")
    return {"missing": sorted(set(missing)), "versions": versions,
            "checked": "local_metadata_only", "browser_paths_checked": bool(browser)}


def result(action, actor, reason, steps, **facts):
    return {"format": FORMAT, "read_only": True, "action": action, "actor": actor,
            "reason": reason, "needs_user_action": actor == "user", "next_steps": steps,
            "authorization": {"routine_scope": "reuse_existing_user_authorization_only",
                              "broad_browser_state_permission": "one_time_permission_is_not_reusable",
                              "system_approval_policy_unchanged": True}, **facts}


def stop_result(stops, **facts):
    human = next((s["category"] for s in stops if s["category"] in {"login_required", "challenge_required"}), None)
    return result("resolve_access_stop", "user" if human else "agent", human or stops[0]["category"],
                  ["Preserve all STOP records and suspend this account's collection.",
                   "Use the existing access-recovery procedure; do not switch account, endpoint, or session to bypass a restriction.",
                   "Ask only for the concrete login/challenge action if required; do not ask to renew already-authorized routine research."],
                  access_stops=stops, **facts)


def check(project, run=None, probe_dependencies=dependency_facts):
    """Diagnose, never execute a next step or silently reopen an interrupted run."""
    try:
        project = Path(project).expanduser()
        if project.is_symlink():
            raise DiagnosticError("unsafe_project_path")
        if not (project / "project.sqlite3").exists():
            session, stops, _ = session_facts(run)
            if stops:
                return stop_result(stops, session=session)
            return result("prepare_project", "agent", "project_not_initialized",
                          ["Create the requested project within the authorized scope; do not ask again for routine read-only research."])
        view = batch.read_view(project)
        # Project STOP must be reportable even if a run path/dependency is broken.
        project_stops = [{"scope": "project", "category": access_category(view["stop"])}] if view.get("stop") else []
        try:
            session, stops, browser = session_facts(run)
        except DiagnosticError:
            if not project_stops:
                raise
            session, stops, browser = {"state": "metadata_needs_inspection"}, [], None
        stops = project_stops + stops
        common = {"session": session, "counts": batch.counts(view)}
        if stops:
            return stop_result(stops, **common)
        pause = project / "batch" / "PAUSE"
        if pause.exists() or pause.is_symlink():
            return result("preserve_pause", "agent", "project_pause_marker_present",
                          ["Preserve the pause marker and do not resume collection automatically.",
                           "Only after the user explicitly resumes may Codex handle its own pause marker; this check never reads or removes it."], **common)
        state = read_metadata(project / "batch" / "state.json") or {}
        choice = batch.choose(view, state)
        if choice["action"] == "scope_processed":
            return result("report_scope_processed", "agent", "defined_known_scope_only",
                          ["Report the defined known scope and remaining external limitations; do not claim platform-wide recall."], **common)
        if choice["action"] == "handoff":
            blocked = [t for t in view["tasks"] if t["status"] == "blocked"]
            timeouts = [t for t in blocked if t["kind"] != "browser" and t.get("reason") == "Transport failed: ReadTimeout"]
            if timeouts and not any(common["counts"][k] for k in ("review", "triage", "media")):
                return result("inspect_transport_checkpoint", "agent", "read_timeout_requires_local_diagnosis",
                              ["Inspect the saved transport failure and cursor; a legacy reason alone is not permission to retry.",
                               "Use structured retry evidence and bounded recovery if supported; otherwise preserve the gap without asking the user to re-authorize research."],
                              timeout_task_ids=[t["id"] for t in timeouts], **common)
            return result("review_collected_material", "agent", "analyst_handoff_not_user_approval",
                          ["Read and record the queued text, title triage, or media locally before collecting more.",
                           "Resume the authorized workflow after review; keep uncollected gaps and do not label materials read automatically."],
                          handoff_reason=choice.get("reason"), **common)
        browser_work = choice.get("task", {}).get("kind") == "browser"
        deps = probe_dependencies(browser if browser_work else None)
        common["dependencies"] = deps
        if deps["missing"]:
            return result("repair_local_environment", "agent", "recorded_dependency_missing",
                          ["Reuse an existing compatible runtime or repair only the task-local dependency; do not change global settings or ask for login."], **common)
        if session["state"] in {"cleaned", "closed", "not_selected"}:
            return result("prepare_authorized_session", "agent", "session_cleaned_or_not_selected",
                          ["Reuse a known active authorized run when available; otherwise prepare one task-owned temporary run under existing authorization.",
                           "Do not read broad Chrome window state or ask for a new login merely because the old temporary run was cleaned."], **common)
        if session["state"] in {"login_copy_missing", "account_binding_missing"}:
            return result("prepare_authorized_login_copy", "agent", session["state"],
                          ["Use the already-authorized exact account/profile procedure; this check did not inspect credentials.",
                           "Ask for user login only if that procedure actually confirms an expired or missing login; never inspect other profiles."], **common)
        if session.get("run_lock_present"):
            return result("inspect_existing_runner", "agent", "run_lock_present",
                          ["Check the already-owned process before continuing; do not remove its lock or start another collector."], **common)
        if browser_work and not browser:
            return result("start_owned_browser", "agent", "browser_not_started",
                          ["Start the dedicated research browser using the existing private login copy and known runtime; leave original Chrome unchanged."], **common)
        if browser_work and not session.get("fixed_target_recorded"):
            return result("inspect_owned_target", "agent", "owned_target_not_bound",
                          ["Inspect URL-only target metadata in the already-owned temporary browser and bind the observed research target if appropriate.",
                           "Do not inspect original Chrome windows or switch to another account; normal browser guards must still verify ownership and access."], **common)
        return result("continue_serial_collection", "agent", "local_preflight_ready",
                      ["Continue the serial collector under existing authorization; normal collectors must still verify live access, fixed target, and account STOP.",
                       "Keep the owned session across ordinary budget checkpoints; analyst handoffs go to Codex, not to the user."],
                      next_task_id=choice["task"]["id"], next_task_kind=choice["task"]["kind"], **common)
    except (DiagnosticError, OSError, ValueError, KeyError, TypeError, AttributeError,
            sqlite3.Error, batch.legacy.ResearchError) as error:
        code = str(error) if isinstance(error, DiagnosticError) else "local_metadata_needs_inspection"
        return result("inspect_local_metadata", "agent", code,
                      ["Inspect the local project/run metadata without network access; preserve records and do not broaden browser or credential access."])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("check")
    command.add_argument("--project", type=Path, required=True)
    command.add_argument("--run", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.project, args.run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
