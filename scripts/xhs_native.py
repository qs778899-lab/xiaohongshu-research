#!/usr/bin/env python3
"""Offline queue/receipt bridge for Codex-operated native UI. Never controls a browser."""
import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import xhs_project as xp

STAGES = {"search", "body", "roots", "replies", "author", "images", "video", "identity", "employment", "legacy_audit"}
END = {"search": {"platform_end"}, "roots": {"platform_end", "explicit_zero"},
       "replies": {"all_discovered_threads_read"}, "author": {"platform_end"},
       "body": {"body_text_read"}, "images": {"all_positions_read", "explicit_zero"},
       "video": {"all_relevant_segments_verified", "explicit_zero"},
       "identity": {"assessment_recorded"}, "employment": {"assessment_recorded"},
       "legacy_audit": {"claims_reconciled"}}
FAIL = {"input_stale", "content_loading", "snapshot_mismatch", "no_effect", "window_missing", "scope_changed"}
ACTIONS = {"input", "navigate", "read", "scroll", "expand", "media", "offline"}
DEFAULT_ACTION = {"search": "navigate", "body": "read", "roots": "scroll", "replies": "expand",
                  "author": "navigate", "images": "media", "video": "media", "identity": "offline",
                  "employment": "offline", "legacy_audit": "offline"}
DEPENDENCIES = {"search": {"input", "navigate", "read", "scroll"},
                "body": {"navigate", "read"}, "roots": {"navigate", "read", "scroll"},
                "replies": {"navigate", "read", "scroll", "expand"},
                "author": {"navigate", "read", "scroll"},
                "images": {"navigate", "read", "media"}, "video": {"navigate", "read", "media"}}


def require(condition, message):
    if not condition:
        raise xp.legacy.ResearchError(message)


def setup(project):
    project.db.execute("""CREATE TABLE IF NOT EXISTS native_events(
      id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
      action TEXT NOT NULL, outcome TEXT NOT NULL, receipt TEXT NOT NULL,
      artifact TEXT NOT NULL, sha256 TEXT NOT NULL, at TEXT NOT NULL)""")
    project.db.execute("""CREATE TABLE IF NOT EXISTS native_scope_migrations(
      old_key TEXT PRIMARY KEY, new_key TEXT NOT NULL UNIQUE,
      task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
      artifact TEXT NOT NULL, sha256 TEXT NOT NULL, at TEXT NOT NULL)""")


def author_uid(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{24}", value) is not None


def migrate_author_placeholder(project, task_id, source_path, reason):
    """Reclassify only unread legacy placeholders; never rewrite their events."""
    require(isinstance(reason, str) and reason.strip(), "Migration reason required")
    source = Path(source_path)
    require(source.is_absolute(), "Explicit migration source required")
    task = project.task(task_id)
    setup(project)
    previous = project.db.execute("SELECT * FROM native_scope_migrations WHERE task_id=?", (task_id,)).fetchone()
    if previous:
        require(valid_migration_source(project, previous), "Migration audit source missing or changed")
        return {"task_id": task_id, "duplicate": True, "stage": "identity"}
    payload = dict(task["payload"])
    require(task["kind"] == "native" and payload.get("stage") == "author"
            and not author_uid(payload.get("target")), "Only legacy author placeholders can migrate")
    require(task["status"] == "pending" and not payload.get("completion_event"),
            "Only pending scopes without completion history can migrate")
    # Unknown old receipts are not proof of zero reading. Corrections do not erase history here.
    rows = project.db.execute("SELECT * FROM native_events WHERE task_id=?", (task_id,)).fetchall()
    provenance = payload.get("provenance", [])
    require(isinstance(provenance, list) and (provenance or rows), "Original task provenance required")
    for origin in provenance:
        archived = project.path / origin.get("archive", "")
        require(archived.is_file() and not archived.is_symlink()
                and hashlib.sha256(archived.read_bytes()).hexdigest() == origin.get("sha256"),
                "Original import source missing or changed")
    for row in rows:
        receipt = json.loads(row["receipt"])
        require(not receipt.get("complete") and receipt.get("reviewed") is not True
                and (row["outcome"] in FAIL | {"access_stop"} or receipt.get("content_observed") is False),
                "Observed or ambiguous author history requires manual audit")
        require(valid_event_source(project, row), "Original event source missing or changed")
    new_key = xp.dumps(["identity", payload["target"], payload.get("sort", "")])
    require(not project.db.execute("SELECT 1 FROM tasks WHERE kind='native' AND entity_key=?", (new_key,)).fetchone(),
            "Identity scope already exists; do not merge histories automatically")
    artifact, sha = archive(project, source)
    audit = {"from_stage": "author", "to_stage": "identity", "old_key": task["entity_key"],
             "new_key": new_key, "reason": reason, "source_archive": artifact, "source_sha256": sha,
             "content_observed": False, "reviewed": False, "at": xp.legacy.now()}
    payload.update(stage="identity", scope_migration=audit)
    # Keep target, task ID, checkpoints, pending_action, failure events, and STOP unchanged.
    with project.db:
        project.db.execute("INSERT INTO native_scope_migrations VALUES(?,?,?,?,?,?)",
                           (task["entity_key"], new_key, task_id, artifact, sha, audit["at"]))
        project.db.execute("UPDATE tasks SET entity_key=?,payload=?,updated=? WHERE id=?",
                           (new_key, xp.dumps(payload), audit["at"], task_id))
    return {"task_id": task_id, "stage": "identity", "migration": audit}


def valid_migration_source(project, row):
    source = project.path / row["artifact"]
    return (source.is_file() and not source.is_symlink()
            and hashlib.sha256(source.read_bytes()).hexdigest() == row["sha256"])


def archive(project, path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "Explicit regular source file required")
    data = path.read_bytes()
    require(bool(data), "Empty source is not an observation")
    sha = hashlib.sha256(data).hexdigest()
    folder = project.path / "native-sources"
    require(not folder.is_symlink(), "Source archive cannot be a symlink")
    folder.mkdir(exist_ok=True, mode=0o700)
    target = folder / sha
    if target.exists():
        require(not target.is_symlink() and target.read_bytes() == data, "Immutable source changed")
    else:
        with target.open("xb") as out:
            out.write(data)
        target.chmod(0o600)
    return str(target.relative_to(project.path)), sha


def import_manifest(project, manifest_path):
    manifest = xp.load(manifest_path)
    units = manifest.get("units")
    require(isinstance(units, list) and units, "Manifest needs explicit units")
    setup(project)
    # Resolve explicit legacy aliases before validation; never recreate a migrated placeholder.
    units = [dict(unit) for unit in units]
    for unit in units:
        key = xp.dumps([unit.get("stage"), unit.get("target"), unit.get("sort", "")])
        migration = project.db.execute("SELECT * FROM native_scope_migrations WHERE old_key=? OR new_key=?", (key, key)).fetchone()
        if migration:
            require(valid_migration_source(project, migration), "Migration audit source missing or changed")
            unit["stage"] = "identity"
    # Validate the entire batch before changing the queue.
    for unit in units:
        require(unit.get("stage") in STAGES and isinstance(unit.get("target"), str)
                and unit["target"].strip() and unit.get("next"), "Each unit needs stage, target and next")
        require(isinstance(unit.get("sources"), list) and unit["sources"], "Source records required")
        for name in unit["sources"]:
            source = Path(name)
            require(source.is_absolute() and source.is_file() and not source.is_symlink(), "Use explicit existing absolute sources")
        if unit["stage"] == "search":
            require(unit.get("sort") in xp.SORTS, "Search sort must be explicit")
        if unit["stage"] == "author":
            require(author_uid(unit["target"]), "Author target requires observed public UID; use identity for unknown UID")
    setup(project)
    imported = []
    for unit in units:
        sources = [{"archive": a, "sha256": h, "original_path": str(Path(s)),
                    "kind": "legacy_record_not_fresh_collection"}
                   for s in unit["sources"] for a, h in [archive(project, s)]]
        key = xp.dumps([unit["stage"], unit["target"], unit.get("sort", "")])
        payload = dict(unit, provenance=sources, native_import=True)
        payload.pop("sources")
        previous = project.db.execute("SELECT payload FROM tasks WHERE kind='native' AND entity_key=?", (key,)).fetchone()
        if previous:
            # Old manifests add provenance, but cannot rewind a newer live checkpoint.
            previous = json.loads(previous[0])
            if (previous.get("latest_observation") or previous.get("scope_migration")) and previous.get("next"):
                payload["next"] = previous["next"]
        tid = project.add_task("native", key, payload, unit.get("priority", 30))
        imported.append(tid)
    project.db.commit()
    from xhs_rounds import inherit_from_sources
    inherit_from_sources(project,imported)
    return {"task_ids": imported, "legacy_claims_not_promoted_to_completion": True}


def channel_health(project):
    if not project.db.execute("SELECT 1 FROM sqlite_master WHERE name='native_events'").fetchone():
        return {}
    health = {}
    from xhs_native_recovery import valid_rows
    recovered = {eid for eid, _ in valid_rows(project)}
    events, _ = effective_events(project)
    for row, receipt in events:
        item = health.setdefault(row["action"], {"consecutive_failures": 0, "hold": False})
        if row["outcome"] == "access_stop":
            if row['id'] not in recovered:
                health["access"] = {"hold": True, "requires_access_recovery": True}
            else:
                health.setdefault("access", {"hold": False, "scope": "native_ui_only",
                                             "historical_stop_preserved": True})
        elif row["outcome"] in FAIL:
            item["consecutive_failures"] += 1
            item["hold"] = item["consecutive_failures"] >= 2
        elif row["outcome"] == "ready":
            item.update(consecutive_failures=0, hold=False)
    return health


def dispatch_snapshot(project):
    """One decision's advisory queue health; never substitutes for browser guards."""
    from xhs_native_recovery import native_project_stop_applies
    return {'health':channel_health(project),'project_stop_applies':native_project_stop_applies(project)}


def hint(project, task, *, dispatch=None):
    action = task["payload"].get("pending_action") or DEFAULT_ACTION[task["payload"]["stage"]]
    if dispatch is None:
        health = channel_health(project)
        from xhs_native_recovery import native_project_stop_applies
        stopped = native_project_stop_applies(project) if action != "offline" else False
    else:
        health = dispatch['health']
        stopped = dispatch['project_stop_applies']
    if action != "offline" and (stopped or health.get("access", {}).get("hold")):
        return "preserve_access_stop"
    needed = DEPENDENCIES.get(task["payload"]["stage"], {action}) | {action}
    if any(health.get(operation, {}).get("hold") for operation in needed):
        return "channel_probe_required_no_repeat_action"
    return "codex_offline_review" if action == "offline" else "codex_native_ui"


def validate_url(task, url):
    parts = urlparse(url)
    require(parts.scheme == "https" and parts.hostname in {"www.xiaohongshu.com", "xiaohongshu.com"}, "Public XHS URL required")
    stage, target = task["payload"]["stage"], task["payload"]["target"]
    if stage == "search":
        value = parse_qs(parts.query).get("keyword", [""])[0]
        for _ in range(2):
            value = unquote(value)
        require(parts.path.rstrip("/") == "/search_result" and value == target, "Search URL does not match query")
    elif stage == "author":
        require(parts.path.rstrip("/") == "/user/profile/" + target, "Author URL does not match target")
    elif stage in {"body", "roots", "replies", "images", "video"}:
        require(parts.path.rstrip("/") in {"/explore/" + target, "/search_result/" + target} or re.fullmatch(r"/user/profile/[a-f0-9]{24}/" + re.escape(target),parts.path.rstrip("/")), "Note URL does not match target")


def valid_completion(project, task):
    if not project.db.execute("SELECT 1 FROM sqlite_master WHERE name='native_events'").fetchone():
        return False
    event = project.db.execute("SELECT * FROM native_events WHERE id=? AND task_id=?",
                               (task["payload"].get("completion_event"), task["id"])).fetchone()
    if not event:
        return False
    return valid_event_source(project, event)


def valid_event_source(project, event):
    receipt = json.loads(event["receipt"])
    for relative, sha in [(event["artifact"], event["sha256"]),
                          (receipt.get("receipt_archive", ""), receipt.get("receipt_sha256", ""))]:
        artifact = project.path / relative
        if (not artifact.is_file() or artifact.is_symlink()
                or hashlib.sha256(artifact.read_bytes()).hexdigest() != sha):
            return False
    return True


def effective_events(project, task_id=None):
    """Apply explicit audit corrections without deleting earlier evidence."""
    query = "SELECT * FROM native_events"
    args = ()
    if task_id is not None:
        query += " WHERE task_id=?"
        args = (task_id,)
    rows = [(row, json.loads(row['receipt'])) for row in
            project.db.execute(query + " ORDER BY rowid", args)]
    corrected, invalid = set(), []
    for row, receipt in rows:
        target = receipt.get('corrects_observation_event')
        if target:
            if valid_event_source(project, row):
                corrected.add(target)
            else:
                invalid.append({'task_id': row['task_id'], 'kind': 'native',
                                'reason': 'native_observation_correction_source_missing_or_changed'})
    # Content-count corrections must never remove failures or access stops.
    return [(row, receipt) for row, receipt in rows
            if row['outcome'] in FAIL | {'access_stop'} or
            (row['id'] not in corrected and receipt.get('content_observed') is not False)], invalid


def validate_saved_original_body(task, receipt):
    """Validate offline reading of a saved full note-info title and description.

    This accepts the existing operation=note archive format only. Codex must
    actually read the complete text; structural validation cannot prove that
    human assertion or detect an unmarked upstream omission. Media, comments,
    identity assessments and present-day page availability are outside scope.
    """
    require(task['payload'].get('stage') == 'body' and receipt.get('task_id') == task['id'],
            'Saved original body review must bind this body task')
    require(receipt.get('action') == 'offline' and receipt.get('outcome') == 'offline'
            and receipt.get('complete') is True and receipt.get('reviewed') is True
            and receipt.get('content_observed') is True,
            'Saved original body reading is an explicit offline text review')
    require(receipt.get('reader') == 'codex' and receipt.get('full_body_read') is True
            and receipt.get('title_read') is True and receipt.get('source_body_complete') is True
            and receipt.get('body_text_truncated') is False and receipt.get('unread_body_segments') == [],
            'Actual complete saved body reading must be attested')
    require(all(receipt.get(k) is False for k in ('ui_observed', 'content_ready', 'view_consistent')),
            'Saved body review must not claim current UI readiness')
    require(not receipt.get('corrects_observation_event') and not receipt.get('new_gap')
            and not receipt.get('media_verified') and not receipt.get('media_reviewed_keys')
            and not receipt.get('comment_media_scan_complete'),
            'Saved body review cannot complete or correct other scopes')
    title_path = '/data/items/0/note_card/title'
    body_path = '/data/items/0/note_card/desc'
    require(receipt.get('positions') == [title_path, body_path]
            and receipt.get('body_pointer') == body_path and receipt.get('title_pointer') == title_path,
            'Exact saved note title and description positions required')
    source = Path(receipt.get('source_file', ''))
    require(source.is_absolute() and source.is_file() and not source.is_symlink(),
            'Saved original note must be an existing regular file')
    raw = source.read_bytes(); sha = hashlib.sha256(raw).hexdigest()
    require(receipt.get('source_sha256') == sha, 'Saved original note hash differs')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise xp.legacy.ResearchError('Saved original note must be JSON')
    require(isinstance(value, dict) and value.get('operation') == 'note',
            'Original note-info archive required; summaries and search cards are not full bodies')
    target = task['payload']['target']; data = value.get('data')
    require(re.fullmatch(r'[a-f0-9]{24}', target or '') and value.get('note_id') == target
            and isinstance(data, dict) and isinstance(data.get('items'), list) and len(data['items']) == 1,
            'Saved original note-info target or item list differs')
    item = data['items'][0]
    require(isinstance(item, dict) and item.get('model_type') == 'note'
            and item.get('id') == target and item.get('ignore') is False,
            'Saved original note item differs or is unavailable')
    card = item.get('note_card')
    require(isinstance(card, dict) and card.get('note_id') == target and card.get('type') == 'normal'
            and isinstance(card.get('user'), dict) and author_uid(card['user'].get('user_id'))
            and isinstance(card.get('interact_info'), dict) and isinstance(card.get('image_list'), list)
            and isinstance(card.get('tag_list'), list), 'Complete original normal-note structure required')
    error_flags = {'error', 'errors', 'error_code', 'error_message', 'failure', 'partial', 'incomplete',
                   'truncated', 'is_truncated', 'desc_truncated', 'body_truncated', 'has_more_text',
                   'isTruncated', 'hasMoreText'}
    for block in (value, data, item, card):
        require(not any(block.get(k) for k in error_flags)
                and block.get('ok') is not False and block.get('success') is not False
                and block.get('code') in (None, 0), 'Failed or truncated note-info is not a complete body')
    title, body = card.get('title'), card.get('desc')
    require(isinstance(title, str) and isinstance(body, str) and body.strip(),
            'Nonempty original description required')
    require(not body.rstrip().endswith(('...', '…', '[truncated]', '【截断】')),
            'Ambiguous or truncated saved description needs further verification')
    require(receipt.get('body_sha256') == hashlib.sha256(body.encode()).hexdigest()
            and receipt.get('title_sha256') == hashlib.sha256(title.encode()).hexdigest(),
            'Saved body/title text hash differs')
    validate_url(task, value.get('url', ''))
    require(receipt.get('url') == value.get('url'), 'Saved original URL differs')
    require(receipt.get('source_captured_at') == value.get('captured_at'),
            'Original capture date must be preserved')
    try:
        captured = datetime.fromisoformat(value['captured_at'].replace('Z', '+00:00'))
        read_at = datetime.fromisoformat(receipt['observed_at'].replace('Z', '+00:00'))
        require(captured.tzinfo is not None and read_at.tzinfo is not None and read_at >= captured,
                'Saved capture/read dates need timezones and chronological order')
    except (KeyError, ValueError, TypeError, AttributeError):
        raise xp.legacy.ResearchError('Saved capture/read dates need timezones and chronological order')
    return sha


def record(project, task_id, receipt_path):
    task = project.task(task_id)
    require(task["kind"] == "native", "Native task required")
    # Validate exactly the bytes later bound to the immutable receipt archive.
    # A caller replacing this path during validation must not change evidence.
    raw = Path(receipt_path).read_bytes()
    receipt = json.loads(raw.decode('utf-8'))
    validated_receipt_sha = hashlib.sha256(raw).hexdigest()
    require('task_id' not in receipt or receipt['task_id'] == task_id,
            'Receipt task ID differs from task')
    require(receipt.get("target") == task["payload"]["target"], "Receipt target differs from task")
    require(receipt.get("action") in ACTIONS, "Unknown UI action")
    outcome = receipt.get("outcome")
    require(outcome in FAIL | {"ready", "access_stop", "offline"}, "Unknown outcome")
    require(receipt.get("observed_at") and receipt.get("summary"), "Timestamp and actual observation required")
    require(isinstance(receipt.get("gaps"), list), "Explicit gaps list required")
    source = Path(receipt.get("source_file", ""))
    require(source.is_absolute(), "Explicit observation source required")
    if outcome == "ready":
        require(receipt.get("target_verified") is True and receipt.get("content_ready") is True
                and receipt.get("view_consistent") is True, "Ready needs verified target, content and consistent view")
        require(receipt["action"] != "offline", "Live readiness requires an observed UI action")
        validate_url(task, receipt.get("url", ""))
    complete = receipt.get("complete") is True
    stage = task["payload"]["stage"]
    saved_body_sha = (validate_saved_original_body(task, receipt)
                      if receipt.get('review_scope') == 'saved_original_body_text_v1' else None)
    if 'content_observed' in receipt:
        require(type(receipt['content_observed']) is bool, "content_observed must be boolean")
    if receipt.get('content_observed') is False:
        require(not complete and receipt.get('reviewed') is not True,
                "Gap registration is neither reading nor completion")
        require(outcome == 'offline' and receipt['action'] == 'offline',
                "Unobserved scope registration must not claim UI readiness")
    correction = receipt.get('corrects_observation_event')
    if correction:
        require(receipt.get('content_observed') is False and receipt.get('reviewed') is False,
                "Observation correction must explicitly register no content read")
        setup(project)
        prior = project.db.execute('SELECT receipt FROM native_events WHERE id=? AND task_id=?',
                                   (correction, task_id)).fetchone()
        require(prior is not None, "Correction must identify an existing event in the same scope")
        prior_receipt = json.loads(prior[0])
        require(prior_receipt.get('outcome') in {'ready', 'offline'},
                "Content observation correction cannot change a failure or access stop")
        require(prior_receipt.get('complete') is not True,
                "Observation correction cannot revoke completion; use a new gap")
        require(prior_receipt.get('content_observed') is not False,
                "Correction must refer to an observation, not another correction")
    if complete:
        require(outcome == ("offline" if saved_body_sha or DEFAULT_ACTION[stage] == "offline" else "ready"), "Cannot complete a failed operation")
        require(receipt.get("end") in END[stage] and not receipt["gaps"], "Explicit stage end and no gaps required")
        require(receipt.get("reviewed") is True, "Collected is not reviewed")
        require(receipt.get("positions") and isinstance(receipt["positions"], list), "Source positions required")
        if stage in {"images", "video"}:
            require(receipt.get("media_verified") is True, "Original media must actually be viewed")
        if stage == "search":
            require(receipt.get("sort") == task["payload"]["sort"], "Sort must match observed selection")
    setup(project)
    eid = xp.digest([task_id, validated_receipt_sha])
    if project.db.execute("SELECT 1 FROM native_events WHERE id=?", (eid,)).fetchone():
        return {"duplicate": True, "event_id": eid}
    artifact, sha = archive(project, source)
    if saved_body_sha:
        require(sha == saved_body_sha, 'Saved original note changed during archival')
    receipt_archive, receipt_sha = archive(project, receipt_path)
    require(receipt_sha == validated_receipt_sha, 'Receipt changed during archival')
    project.db.execute("INSERT INTO native_events VALUES(?,?,?,?,?,?,?,?)",
                       (eid, task_id, receipt["action"], outcome, xp.dumps(dict(receipt, receipt_archive=receipt_archive, receipt_sha256=receipt_sha)),
                        artifact, sha, receipt["observed_at"]))
    payload = task["payload"]
    payload["latest_observation"] = {"event_id": eid, "summary": receipt["summary"], "gaps": receipt["gaps"]}
    if outcome in FAIL:
        payload["pending_action"] = receipt["action"]
    elif outcome == "ready" and payload.get("pending_action") == receipt["action"]:
        payload.pop("pending_action")
    if complete:
        payload["completion_event"] = eid
        project.db.execute("UPDATE tasks SET status='complete',review_status='reviewed',reason='' WHERE id=?", (task_id,))
    # A smaller later observation never erases earlier reviewed source scope.
    # Explicit newly discovered gaps reopen the unit but retain completion history.
    elif receipt.get("new_gap") is True:
        project.db.execute("UPDATE tasks SET status='pending',review_status='pending',reason='new_native_gap' WHERE id=?", (task_id,))
    if receipt.get("next"):
        payload["next"] = receipt["next"]
    project.db.execute("UPDATE tasks SET payload=?,turn=turn+1,updated=? WHERE id=?", (xp.dumps(payload), xp.legacy.now(), task_id))
    project.db.commit()
    return {"event_id": eid, "channel_health": channel_health(project), "task": project.task(task_id)}


def coverage(project):
    counts, invalid = {}, []
    rows = project.db.execute("SELECT id FROM tasks WHERE kind='native'").fetchall()
    for row in rows:
        task = project.task(row[0]); stage = task["payload"]["stage"]
        group = counts.setdefault(stage, {"known_units": 0, "observed_units": 0, "reviewed_observation_units": 0,
                                          "processed_units": 0, "pending_units": 0})
        group["known_units"] += 1
        events, errors = effective_events(project, task['id']) if project.db.execute("SELECT 1 FROM sqlite_master WHERE name='native_events'").fetchone() else ([], [])
        invalid.extend(errors)
        events = [(row, receipt) for row, receipt in events if row['outcome'] in ('ready', 'offline')]
        if events:
            group['observed_units'] += 1
            if any(receipt.get('reviewed') is True for _, receipt in events):
                group['reviewed_observation_units'] += 1
        done = task["status"] == "complete" and task["review_status"] == "reviewed"
        if done:
            if not valid_completion(project, task):
                invalid.append({"task_id": task["id"], "kind": "native", "reason": "native_completion_source_missing_or_changed"})
                done = False
        group["processed_units" if done else "pending_units"] += 1
    if project.db.execute("SELECT 1 FROM sqlite_master WHERE name='native_scope_migrations'").fetchone():
        invalid.extend({"task_id": row["task_id"], "kind": "native", "reason": "native_scope_migration_source_missing_or_changed"}
                       for row in project.db.execute("SELECT * FROM native_scope_migrations")
                       if not valid_migration_source(project, row))
    from xhs_native_recovery import valid_rows, native_project_stop_applies
    return {"stages": counts, "invalid_review_bindings": invalid, "channel_health": channel_health(project),
            "native_web_recovery": {"valid_event_resolutions": [eid for eid, _ in valid_rows(project)],
                                    "project_stop_applies_to_native_ui": native_project_stop_applies(project),
                                    "api_stop_metadata_unchanged": True},
            "units_are_scopes_not_comment_or_patient_counts": True, "observation_counts_include_partial_scopes": True,
            "no_platform_recall_estimate": True}


def open_lookup_project(path):
    """Reuse project readers without invoking the schema-writing constructor."""
    project = xp.Project.__new__(xp.Project)
    project.path = Path(path).expanduser().resolve()
    database = project.path / "project.sqlite3"
    require(database.is_file() and not database.is_symlink(), "Lookup requires an existing project database")
    project.db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
    project.db.row_factory = sqlite3.Row
    try:
        project.db.execute("PRAGMA query_only=ON")
        project.db.execute("BEGIN")
        require(project.get_meta("format") == xp.VERSION, "Unsupported project schema")
    except Exception:
        project.close()
        raise
    return project


def lookup(project, target):
    """Inspect exact registered target before UI work; no queue or receipt mutations."""
    require(isinstance(target, str) and target.strip(), "Lookup needs an exact target")
    matches, related = [], []
    for row in project.db.execute("SELECT * FROM tasks WHERE kind='native'"):
        payload = json.loads(row["payload"])
        actual = payload.get("target", "")
        is_related = author_uid(target) and actual.startswith(target + ":")
        if actual != target and not is_related:
            continue
        task = dict(row, payload=payload)
        done = (task["status"] == "complete" and task["review_status"] == "reviewed"
                and valid_completion(project, task))
        sources = []
        events = project.db.execute("SELECT * FROM native_events WHERE task_id=? ORDER BY rowid",
                                    (task["id"],)).fetchall()
        for event in events:
            receipt = json.loads(event["receipt"])
            sources.append({"event_id": event["id"], "archive": str(project.path / event["artifact"]),
                            "source_valid": valid_event_source(project, event),
                            "summary": receipt.get("summary"), "positions": receipt.get("positions", [])})
        invalid_done = task["status"] == "complete" and task["review_status"] == "reviewed" and not done
        item = {"task_id": task["id"], "target": actual, "stage": payload["stage"], "sort": payload.get("sort", ""),
                        "processed": done, "next": None if done else payload.get("next"),
                        "next_action": (None if done else "audit_completion_source" if invalid_done else hint(project, task)),
                        "sources": sources,
                        "imported_sources": payload.get("provenance", [])}
        (related if is_related else matches).append(item)
    return {"target": target, "registered_scopes": matches, "related_scopes": related,
            "exact_match_only": True, "related_match_rule": "full_24_hex_id_followed_by_colon_separate_scopes_not_merged",
            "unregistered_legacy_sources_not_searched": True,
            "does_not_authorize_ui_or_clear_stop": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("import"); p.add_argument("--manifest", required=True, type=Path)
    p = sub.add_parser("record"); p.add_argument("--task", required=True); p.add_argument("--receipt", required=True, type=Path)
    p = sub.add_parser("migrate-author-placeholder")
    p.add_argument("--task", required=True); p.add_argument("--source", required=True, type=Path)
    p.add_argument("--reason", required=True)
    sub.add_parser("status")
    p = sub.add_parser("lookup"); p.add_argument("--target", required=True)
    args = parser.parse_args()
    if args.command == "lookup":
        project = open_lookup_project(args.project)
        try:
            print(xp.dumps(lookup(project, args.target)))
        finally:
            project.close()
        return
    with xp.project_lock(args.project):
        project = xp.Project(args.project, create=args.command == "import")
        try:
            if args.command == "import": result = import_manifest(project, args.manifest)
            elif args.command == "record": result = record(project, args.task, args.receipt)
            elif args.command == "migrate-author-placeholder":
                result = migrate_author_placeholder(project, args.task, args.source, args.reason)
            else: result = coverage(project)
            print(xp.dumps(result))
        finally:
            project.close()


if __name__ == "__main__":
    main()
