#!/usr/bin/env python3
"""Serial project batches with explicit handoffs; never clears access STOPs."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import xhs_project as xp
import xhs_research as legacy

FORMAT = "xhs-serial-batch-v1"
NETWORK = {"note", "comments", "browser", "author", "search"}


def verified_text_reviews(db, tasks):
    """Only explicit analyst receipts matching this task's current text qualify."""
    notes = {t["id"]: t for t in tasks if t["kind"] == "note"}
    documents = {r["id"]: dict(r) for r in db.execute("SELECT id,note_id,text FROM documents WHERE kind='note'")}
    receipts = {}
    for row in db.execute("SELECT id,task_id,decision,annotations FROM reviews ORDER BY id"):
        note = notes.get(row["task_id"])
        if not note or row["decision"] not in {"include", "relevant", "reviewed"}: continue
        annotations = json.loads(row["annotations"])
        proof = annotations.get("text_review") if isinstance(annotations, dict) else None
        if not isinstance(proof, dict): continue
        document_id = proof.get("document_id")
        if document_id != "note:" + note["entity_key"]: continue
        document = documents.get(document_id)
        if not document or document["note_id"] != note["entity_key"]: continue
        text_hash = hashlib.sha256(document["text"].encode("utf-8")).hexdigest()
        if proof.get("text_sha256") != text_hash: continue
        receipts[note["id"]] = {"review_id": row["id"], "document_id": document_id, "text_sha256": text_hash}
    return receipts


def read_view(project):
    """Read-only inspection: even planning does not instantiate mutable Project."""
    database = Path(project) / "project.sqlite3"
    if database.is_symlink() or not database.is_file():
        raise legacy.ResearchError("Expected an existing regular project database")
    db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        stop = db.execute("SELECT value FROM meta WHERE key='stop'").fetchone()
        tasks = []
        for row in db.execute("SELECT * FROM tasks ORDER BY priority,turn,updated,id"):
            task = dict(row)
            task["payload"] = json.loads(task["payload"])
            tasks.append(task)
        decisions = {}
        for row in db.execute("SELECT task_id,decision FROM reviews ORDER BY id"):
            decisions[row["task_id"]] = row["decision"]
        return {"tasks": tasks, "decisions": decisions, "text_review_receipts": verified_text_reviews(db, tasks),
                "stop": json.loads(stop[0]) if stop else None}
    finally:
        db.close()


def task_brief(task):
    return {key: task.get(key) for key in
            ("id", "kind", "status", "review_status", "next_page", "reason")}


def counts(view):
    result = {"review": 0, "triage": 0, "media": 0, "blocked": 0, "pending": 0}
    for task in view["tasks"]:
        if xp.xhs_search.historical(task): continue
        if task["status"] == "blocked": result["blocked"] += 1
        if task["status"] == "pending": result["pending"] += 1
        if task["status"] == "complete" and task["review_status"] == "pending": result["review"] += 1
        if task["status"] == "pending" and (task["payload"].get("needs_triage") or
                (task["kind"] == "note" and not eligible(task, view["decisions"]))): result["triage"] += 1
        if task["kind"] == "media" and task["status"] not in {"complete", "excluded"}: result["media"] += 1
    return result


def eligible(task, decisions, explicit=False):
    if not xp.xhs_search.collection_allowed(task): return False
    if task["status"] != "pending" or task["kind"] not in NETWORK or task["payload"].get("needs_triage"):
        return False
    if task["kind"] == "note" and not explicit:
        # Search import historically sets needs_triage=False. That alone is not
        # an analyst's relevance decision; keep it in the handoff queue.
        return bool(task["payload"].get("title_scope_review") or task["payload"].get("scope_included") or
                    decisions.get(task["id"]) in {"include", "relevant", "reviewed", "retry"})
    return True


def routing_group(task):
    d=task['payload']
    return '|'.join(d.get('routing_doctors',[])) or d.get('doctor') or d.get('target') or task['entity_key']


def select_ready(tasks, state=None, explicit_ids=None):
    """Shared fairness primitive; callers enforce transport-specific eligibility first."""
    state=state or {}
    if explicit_ids is not None:
        rank={tid:i for i,tid in enumerate(explicit_ids)}
        selected=[t for t in tasks if t['id'] in rank]
        task=min(selected,key=lambda t:rank[t['id']],default=None)
        if task is not None:
            progress=task['payload'].get('bridge',{});known=progress.get('hashes');reviewed=progress.get('reviewed_hashes')
            payload=task['payload'];stage=payload.get('stage')
            same_scope=all(t.get('kind')=='native' and t.get('status')=='pending'
                           and t['payload'].get('target')==payload.get('target') for t in selected)
            if stage in {'roots','replies'}:
                same_scope=same_scope and all(t['payload'].get('stage') in {'roots','replies'} for t in selected)
            elif stage in {'search','author'}:
                same_scope=same_scope and bool(task.get('entity_key')) and all(
                    t.get('entity_key')==task['entity_key'] and t['payload'].get('stage')==stage for t in selected)
                if stage=='search':
                    same_scope=same_scope and payload.get('sort') in xp.SORTS and all(
                        t['payload'].get('sort')==payload['sort'] for t in selected)
            else:same_scope=False
            # Explicitly resume one reviewed comment or listing scope with a fresh scheduling
            # quota. This never repairs collection faults or marks content read.
            if (state.get('group_units',0)>=10 and state.get('active_group')==routing_group(task)
                    and state.get('active_note')==task['payload'].get('target')
                    and same_scope
                    and progress.get('awaiting_review') is False and progress.get('last_review_event')
                    and not progress.get('fault',{}).get('hold')
                    and isinstance(known,list) and known and isinstance(reviewed,list) and set(known)<=set(reviewed)):
                state['group_units']=0
        return task
    group=routing_group
    active=state.get('active_group');count=state.get('group_units',0)
    candidates=tasks
    if active and count<10:
        same=[t for t in tasks if group(t)==active and t['payload'].get('stage') in {'roots','replies'} and (not state.get('active_note') or t['payload'].get('target')==state['active_note'])]
        if same:candidates=same
    elif active:
        other=[t for t in tasks if group(t)!=active]
        if other:candidates=other
    return min(candidates,key=lambda t:(t.get('turn',0),t.get('priority',50),t.get('updated',''),t['id']),default=None)


def choose(view, state=None, task_ids=None, search_pages=5, review_limit=20):
    state = state or {}
    tasks = [task for task in view["tasks"] if not xp.xhs_search.historical(task)]
    by_id = {task["id"]: task for task in view["tasks"]}
    summary = counts(view)
    if view["stop"]:
        return {"action": "stop", "reason": "project_access_stop", "counts": summary}
    # A list is analyst-selected priority work. It never makes an excluded,
    # blocked, unscreened-author or already-complete task executable.
    comments = {t["entity_key"]: t for t in tasks if t["kind"] == "comments"}
    if task_ids is not None:
        unknown = [tid for tid in task_ids if tid not in by_id]
        if unknown: raise legacy.ResearchError("Task list contains unknown task IDs")
        for tid in task_ids:
            task = by_id[tid]
            if eligible(task, view["decisions"], explicit=True):
                roots = comments.get(task["entity_key"]) if task["kind"] == "browser" else None
                if roots and roots["status"] != "complete":
                    if eligible(roots, view["decisions"], explicit=True):
                        return {"action": "collect", "task": roots, "counts": summary,
                                "selection": "explicit_root_dependency", "requested_task": task["id"]}
                    continue  # A blocked root list is a gap, never a silent retry.
                return {"action": "collect", "task": task, "counts": summary, "selection": "explicit"}
        return {"action": "handoff", "reason": "selected_tasks_need_review_or_resolution",
                "counts": summary, "tasks": [task_brief(by_id[tid]) for tid in task_ids]}
    reviews = [task for task in tasks if task["status"] == "complete" and
               task["review_status"] == "pending" and task["kind"] != "media"]
    downstream = [task for task in tasks if task["kind"] != "search" and eligible(task, view["decisions"])]
    # Browser expansion follows complete root enumeration when a root task exists.
    downstream = [t for t in downstream if t["kind"] != "browser" or
                  t["entity_key"] not in comments or comments[t["entity_key"]]["status"] == "complete"]
    if len(reviews) >= review_limit:
        unresolved_media_notes = {t["payload"].get("note_id") for t in tasks if t["kind"] == "media" and
            (t["status"] in {"pending", "running", "blocked"} or
             (t["status"] == "complete" and t["review_status"] == "pending"))}
        media_dependent = [t for t in reviews if t["kind"] == "note" and t["entity_key"] in unresolved_media_notes
                           and t["id"] in view.get("text_review_receipts", {})]
        immediate_reviews = [t for t in reviews if t not in media_dependent]
        # Only hash-bound human text-read receipts can defer a media-only gap.
        # Merely having images never makes the body's text read.
        # Pending media does not invalidate independent, already-enqueued
        # comment/author work. It still blocks new bodies and search expansion;
        # no old body is reclassified as reviewed to unlock this progress.
        followups = [t for t in downstream if t["kind"] in {"comments", "browser", "author"}]
        if media_dependent and len(immediate_reviews) < review_limit and followups:
            return {"action": "collect", "task": followups[0], "counts": summary,
                    "selection": "followup_while_media_pending", "media_dependent_note_reviews": len(media_dependent),
                    "immediate_text_reviews": len(immediate_reviews)}
        return {"action": "handoff", "reason": "acquired_text_review_checkpoint",
                "counts": summary, "tasks": [task_brief(t) for t in (immediate_reviews or reviews)[:review_limit]]}
    if downstream:
        return {"action": "collect", "task": select_ready(downstream,state), "counts": summary, "selection": "downstream"}
    active = by_id.get(state.get("search_task"))
    # A small page group may finish before handing newly discovered cards back
    # for triage. The group is finite and survives normal batch checkpoints.
    if (active and active["kind"] == "search" and eligible(active, view["decisions"]) and
            0 < state.get("search_units", 0) < search_pages):
        return {"action": "collect", "task": active, "counts": summary, "selection": "search_group"}
    manual = [task for task in tasks if task["status"] in {"pending", "blocked"} or
              (task["status"] == "complete" and task["review_status"] == "pending")]
    manual = [t for t in manual if t["kind"] != "search"]
    if manual:
        return {"action": "handoff", "reason": "downstream_review_media_or_scope_pending",
                "counts": summary, "tasks": [task_brief(t) for t in manual[:50]]}
    searches = [t for t in tasks if t["kind"] == "search" and eligible(t, view["decisions"])]
    if searches:
        # Round-robin between finite groups, adjacent pages inside each group.
        searches.sort(key=lambda t: (t["turn"], t["priority"], t["updated"], t["id"]))
        return {"action": "collect", "task": searches[0], "counts": summary, "selection": "search_group_start"}
    unresolved = [t for t in tasks if t["status"] in {"pending", "running", "blocked"} or
                  (t["review_status"] == "pending" and t["status"] != "excluded")]
    return {"action": "handoff" if unresolved else "scope_processed",
            "reason": "remaining_gaps" if unresolved else "defined_known_scope_only", "counts": summary}


@contextmanager
def batch_lock(root):
    if root.is_symlink(): raise legacy.ResearchError("Batch directory must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "runner.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise legacy.ResearchError("Another batch runner owns this project") from None
        yield
    finally:
        os.close(fd)


def emit_event(root, state, event, emit=None):
    event = {"at": legacy.now(), **event}
    path = root / "journal.jsonl"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    state["last_event"] = event
    legacy.write_json(root / "state.json", state)
    if emit: emit(event)
    return event


def pacing_path(run):
    guard = legacy.account_dir(run)
    if guard is None:
        raise legacy.ResearchError("Batch collection requires an account-bound run")
    return guard / "batch-pacing.json"


def reserve_pacing(run, interval, now):
    """Call under account_lock. Persist before HTTP to survive interruption."""
    path = pacing_path(run)
    pace = legacy.read_json(path, {})
    due = float(pace.get("next_allowed_at", 0))
    if due > now: return due - now
    legacy.write_json(path, {"next_allowed_at": now + interval, "reserved_at": now,
                             "interval_seconds": interval})
    return 0


def schedule_transport_retry(project, run, root, state, result, chosen, clock, emit=None):
    """One durable, same-request retry of a typed no-response timeout only.

    Caller holds project/run/account locks. Never infer safety from error text.
    A crash may consume the retry allowance; it must never replenish it.
    """
    rows = result.get("results", [])
    failure = rows[-1].get("transient_transport") if rows else None
    paths = {"search": {"/api/sns/web/v1/search/notes"},
             "note": {"/api/sns/web/v1/feed"},
             "comments": {"/api/sns/web/v2/comment/page"},
             "author": {"/api/sns/web/v1/user_posted"}}
    if (not isinstance(failure, dict) or chosen["kind"] not in paths or
            failure.get("schema") != "xhs-transient-transport-v1" or
            failure.get("retryable") is not True or failure.get("response_received") is not False or
            type(failure.get("attempt_count")) is not int or failure.get("attempt_count") != 1 or
            not isinstance(failure.get("failure_id"), str) or not failure["failure_id"] or
            failure.get("timeout_kind") not in {"ConnectTimeout", "ReadTimeout"} or
            failure.get("endpoint") not in paths[chosen["kind"]] or
            failure.get("method") != ("POST" if chosen["kind"] in {"note", "search"} else "GET") or
            failure.get("run") != str(Path(run).resolve()) or failure.get("task_id") != chosen["id"]):
        return False
    p = xp.Project(project)
    try:
        legacy.ensure_active(run)
        if p.get_meta("stop"):
            raise legacy.StopRun("Preserved project STOP prevents retry")
        current = p.task(chosen["id"])
        key = xp.digest([current[k] for k in ("id", "kind", "entity_key", "next_page", "cursor", "payload")])
        if (current["status"] != "blocked" or current["reason"] != failure.get("reason") or
                key != failure.get("request_key") or
                p.get_meta("transient_failure:" + chosen["id"]) != failure):
            return False
        attempts = state.setdefault("transport_retries", {})
        if key in attempts:
            return False
        due = clock() + 15
        attempts[key] = {"task_id": chosen["id"], "failure_id": failure["failure_id"],
                         "run": str(run), "retry_count": 1, "not_before": due}
        # Durable consumption before resetting the task; then preserve account pacing.
        legacy.write_json(root / "state.json", state)
        pacing = legacy.read_json(pacing_path(run), {})
        pacing["next_allowed_at"] = max(float(pacing.get("next_allowed_at", 0)), due)
        legacy.write_json(pacing_path(run), pacing)
        p.review(chosen["id"], "retry", "One same-request retry after a typed no-response transport timeout; no access restriction cleared.",
                 {"transport_failure_id": failure["failure_id"], "request_key": key, "automatic_retry": 1})
        emit_event(root, state, {"event": "retry_scheduled", "task_id": chosen["id"],
                   "timeout_kind": failure["timeout_kind"], "retry_count": 1, "not_before": due,
                   "actor": "runner", "requires_user_input": False}, emit)
        return True
    finally:
        p.close()


def run_batch(project, run, units=10, max_seconds=600, interval=10,
              search_pages=5, review_limit=20, task_ids=None, checkpoint_file=None,
              emit=None, clock=time.time, sleep=time.sleep, auto_transport_retry=False):
    if not 1 <= units <= 100: raise legacy.ResearchError("Choose 1..100 work units")
    if not 1 <= max_seconds <= 3600: raise legacy.ResearchError("Choose 1..3600 seconds")
    if not 3 <= interval <= 600: raise legacy.ResearchError("Choose a 3..600 second interval")
    if not 1 <= search_pages <= 20: raise legacy.ResearchError("Choose 1..20 adjacent search pages")
    if not 1 <= review_limit <= 100: raise legacy.ResearchError("Choose a review checkpoint of 1..100 text tasks")
    project, run = Path(project).resolve(), legacy.load_run(run)
    root = project / "batch"
    checkpoint_file = Path(checkpoint_file) if checkpoint_file else root / "PAUSE"
    from xhs_queue import QueueCheckpoint, ProjectStopped
    started, completed = clock(), 0
    with batch_lock(root):
        state = legacy.read_json(root / "state.json", {})
        if state and (state.get("format") != FORMAT or state.get("project") != str(project)):
            raise legacy.ResearchError("Unknown batch state; preserve it for inspection")
        state.update(format=FORMAT, project=str(project), invocation=uuid.uuid4().hex)
        try:
            while completed < units:
                if checkpoint_file and Path(checkpoint_file).exists():
                    return emit_event(root, state, {"event": "checkpoint", "reason": "requested", "processed_units": completed}, emit)
                if clock() - started >= max_seconds:
                    return emit_event(root, state, {"event": "checkpoint", "reason": "time_budget", "processed_units": completed}, emit)
                wait = 0
                from xhs_queue import collection_turn, QueueCheckpoint
                with collection_turn(project, run, started + max_seconds, checkpoint_file,
                        emit=lambda e: emit_event(root, state, {**e, "processed_units": completed}, emit),
                        clock=clock, sleep=sleep):
                    view = read_view(project)
                    choice = choose(view, state, task_ids, search_pages, review_limit)
                    if choice["action"] != "collect":
                        return emit_event(root, state, {"event": choice["action"], **choice,
                                                       "processed_units": completed}, emit)
                    legacy.ensure_active(run)
                    # Held across reservation + collection so other projects using
                    # this account cannot race the durable spacing decision.
                    with legacy.account_lock(run):
                        wait = reserve_pacing(run, interval, clock())
                        if not wait:
                            chosen = choice["task"]
                            state["in_flight"] = {"task_id": chosen["id"], "run": str(run), "at": legacy.now()}
                            emit_event(root, state, {"event": "attempt", "task": task_brief(chosen)}, emit)
                            p = xp.Project(project)
                            try: result = p.resume(run, steps=1, task_id=chosen["id"])
                            finally: p.close()
                            legacy.write_json(pacing_path(run), {"next_allowed_at": clock() + interval,
                                "completed_at": clock(), "interval_seconds": interval})
                            state.pop("in_flight", None)
                            if result["processed_units"]:
                                completed += result["processed_units"]
                                if chosen["kind"] == "search":
                                    state["search_units"] = (state.get("search_units", 0) + 1
                                        if state.get("search_task") == chosen["id"] and choice["selection"] != "search_group_start" else 1)
                                    state["search_task"] = chosen["id"]
                                else:
                                    state.pop("search_task", None); state.pop("search_units", None)
                            blocked = any("blocked" in row or row.get("task", {}).get("status") == "blocked"
                                          for row in result["results"])
                            latest = next(t for t in read_view(project)["tasks"] if t["id"] == chosen["id"])
                            emit_event(root, state, {"event": "task_gap" if blocked else "unit",
                                "task": task_brief(latest), "processed_units": completed,
                                "remaining": result["remaining"]}, emit)
                            if blocked:
                                if auto_transport_retry and schedule_transport_retry(
                                        project, run, root, state, result, chosen, clock, emit):
                                    continue
                                return emit_event(root, state, {"event": "checkpoint", "reason": "task_gap_requires_review",
                                                               "processed_units": completed}, emit)
                            if not result["processed_units"]:
                                # A recovered immutable snapshot may already have
                                # completed the chosen task. Re-read queue once next loop.
                                state.pop("search_task", None); state.pop("search_units", None)
                                current = next(t for t in read_view(project)["tasks"] if t["id"] == chosen["id"])
                                if current["status"] == "pending":
                                    return emit_event(root, state, {"event": "checkpoint", "reason": "no_progress_requires_review",
                                                                   "processed_units": completed}, emit)
                if wait:
                    due = clock() + wait
                    # Sleep without holding locks or repeatedly reading the full
                    # queue. Revalidate STOP and shared pacing before collecting.
                    while clock() < due and clock() - started < max_seconds:
                        if checkpoint_file and Path(checkpoint_file).exists(): break
                        sleep(min(due - clock(), max_seconds - (clock() - started), 1))
            return emit_event(root, state, {"event": "checkpoint", "reason": "unit_budget", "processed_units": completed}, emit)
        except ProjectStopped as exc:
            return emit_event(root, state, {"event": "stop", "reason": str(exc),
                "processed_units": completed}, emit)
        except QueueCheckpoint as exc:
            return emit_event(root, state, {"event": "checkpoint", "reason": exc.reason,
                "processed_units": completed, "waiting_for_account": True}, emit)
        except KeyboardInterrupt:
            if not checkpoint_file.exists():
                legacy.write_json(checkpoint_file, {"reason": "interrupted", "at": legacy.now(),
                                                   "requires_explicit_resume": True})
            return emit_event(root, state, {"event": "checkpoint", "reason": "interrupted", "processed_units": completed}, emit)
        except Exception as exc:
            emit_event(root, state, {"event": "stop" if isinstance(exc, legacy.StopRun) else "error",
                "reason": str(exc) if isinstance(exc, legacy.ResearchError) else type(exc).__name__,
                "processed_units": completed}, emit)
            raise


def work(project, run, units=10, max_seconds=1800, batch_seconds=300, max_batches=12,
         interval=10, search_pages=5, review_limit=20, task_ids=None, checkpoint_file=None,
         emit=None, clock=time.time, sleep=time.sleep):
    """Continue normal budget checkpoints; hand semantic work to Codex, not the user."""
    if not 1 <= max_batches <= 100 or not 1 <= max_seconds <= 3600 or not 1 <= batch_seconds <= 3600:
        raise legacy.ResearchError("Work needs bounded batches and at most 3600 seconds")
    project = Path(project).resolve()
    started, batches, total = clock(), 0, 0
    checkpoint_file = Path(checkpoint_file) if checkpoint_file else project / "batch" / "PAUSE"
    terminal, error = None, None
    read_tasks = set()
    # Separate whole-work lease: another work invocation cannot interleave its
    # batch transitions. run_batch retains project and shared-account locking.
    with batch_lock(project / "batch" / "work-lease"):
        while batches < max_batches and clock() - started < max_seconds:
            view = read_view(project)
            state = legacy.read_json(project / "batch" / "state.json", {})
            choice = choose(view, state, task_ids, search_pages, review_limit)
            first_kind = choice.get("task", {}).get("kind")
            budget = min(units, search_pages) if first_kind == "search" else units
            events = []
            def forward(event):
                events.append(event)
                if emit: emit(event)
            try:
                terminal = run_batch(project, run, budget,
                    max(1, min(batch_seconds, int(max_seconds - (clock() - started)))), interval,
                    search_pages, review_limit, task_ids, checkpoint_file,
                    emit=forward, clock=clock, sleep=sleep, auto_transport_retry=True)
            except legacy.ResearchError as exc:
                error = exc
                terminal = {"event": "stop" if isinstance(exc, legacy.StopRun) else "error",
                            "reason": str(exc), "processed_units": events[-1].get("processed_units", 0) if events else 0}
            batches += 1
            total += terminal.get("processed_units", 0)
            for e in events:
                t = e.get("task", {})
                if e.get("event") == "unit" and t.get("status") == "complete" and t.get("review_status") == "pending":
                    read_tasks.add(t["id"])
            if terminal.get("event") != "checkpoint" or terminal.get("reason") not in {"unit_budget", "time_budget"}:
                break
            # Explicit priority lists must not silently expand an unread search
            # backlog across many automatic batches. The agent reads this group.
            if any(e.get("event") == "unit" and e.get("task", {}).get("kind") == "search" for e in events):
                terminal = {"event": "handoff", "reason": "search_card_review_checkpoint"}
                break
            if len(read_tasks) >= review_limit:
                terminal = {"event": "handoff", "reason": "acquired_text_review_checkpoint"}
                break
            if not terminal.get("processed_units") and terminal.get("waiting_for_account"):
                continue
            if not terminal.get("processed_units"):
                terminal = {"event": "handoff", "reason": "no_progress_requires_review"}
                break
        if terminal is None:
            terminal = {"event": "checkpoint", "reason": "work_budget"}
        elif terminal.get("event") == "checkpoint" and terminal.get("reason") in {"unit_budget", "time_budget"}:
            terminal = {**terminal, "last_batch_reason": terminal["reason"], "reason": "work_budget"}
        terminal = {**terminal, "batches": batches, "processed_units": total,
                    "actor": "codex", "requires_user_input": False,
                    "session_retained": True, "checkpoint_file": str(checkpoint_file)}
        if terminal["event"] == "scope_processed":
            terminal["actor"] = "none"
        # Minimal handoff contains identifiers and queue status, never Cookie data.
        view = read_view(project)
        terminal["counts"] = counts(view)
        terminal["review_task_ids"] = sorted(read_tasks)
        terminal["instruction"] = "Codex reviews saved evidence or diagnoses the gap, then continues within existing authorization. This packet does not authorize bypassing STOP or tool approval."
        if terminal.get("reason") in {"interrupted", "requested"}:
            terminal["instruction"] = "Preserve this pause and checkpoint. Do not resume collection without the user's explicit continuation. After user interruption, clean up only the task-owned temporary session according to the runtime procedure."
        legacy.write_json(project / "batch" / "handoff.json", terminal)
        if emit: emit(terminal)
        if error: raise error
        return terminal


def parse_task_ids(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(t, str) and t for t in value) or len(set(value)) != len(value):
        raise legacy.ResearchError("Task list must contain unique observed task ID strings")
    if not value: raise legacy.ResearchError("Task list must not be empty")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "run", "work"):
        arg = sub.add_parser(command)
        arg.add_argument("--task-list", type=Path)
        arg.add_argument("--search-pages", type=int, default=5)
        arg.add_argument("--review-limit", type=int, default=20)
        arg.add_argument("--round", dest="round_id")
        if command in {"run", "work"}:
            arg.add_argument("--run", type=Path)
            arg.add_argument("--transport", choices=("legacy", "bridge"), default="legacy")
            arg.add_argument("--binding", help="Explicit project bridge binding")
            arg.add_argument("--home-restore-authorization", help="Existing explicit authorization; only owned homepage recovery")
            arg.add_argument("--units", type=int, default=10)
            arg.add_argument("--max-seconds", type=int, default=1800 if command == "work" else 600)
            arg.add_argument("--interval", type=float, default=10)
            arg.add_argument("--checkpoint-file", type=Path)
            if command == "work":
                arg.add_argument("--batch-seconds", type=int, default=300)
                arg.add_argument("--max-batches", type=int, default=12)
    args = parser.parse_args()
    task_ids = parse_task_ids(args.task_list) if args.task_list else None
    if args.command in {"run", "work"} and args.transport == "bridge":
        if not args.binding or args.run:
            parser.error("bridge requires --binding and must not use a legacy --run")
        if args.checkpoint_file and (args.checkpoint_file.exists() or args.checkpoint_file.is_symlink()):
            raise legacy.ResearchError("Bridge checkpoint output already exists; choose a unique destination")
        from xhs_bridge import work as bridge_work
        result=bridge_work(args.project, args.binding,
                    units=min(100, args.units * (args.max_batches if args.command == "work" else 1)),
                    max_seconds=args.max_seconds, interval=args.interval, task_ids=task_ids,
                    review_limit=args.review_limit, search_pages=args.search_pages,
                    round_id=args.round_id,
                    home_restore_authorization=args.home_restore_authorization,
                    emit=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True) if value.get('event')=='unit' else None)
        if args.checkpoint_file:
            args.checkpoint_file.parent.mkdir(parents=True,exist_ok=True)
            with args.checkpoint_file.open('x',encoding='utf-8') as output:
                json.dump(result,output,ensure_ascii=False,indent=2);output.write('\n')
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return
    if args.command in {"run", "work"} and not args.run:
        parser.error("legacy transport requires --run")
    if args.command == "plan":
        result = choose(read_view(args.project), legacy.read_json(args.project / "batch" / "state.json", {}),
                        task_ids, args.search_pages, args.review_limit)
        if "task" in result: result["task"] = task_brief(result["task"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "work":
        work(args.project, args.run, units=args.units, max_seconds=args.max_seconds,
             batch_seconds=args.batch_seconds, max_batches=args.max_batches, interval=args.interval,
             search_pages=args.search_pages, review_limit=args.review_limit, task_ids=task_ids,
             checkpoint_file=args.checkpoint_file,
             emit=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
    else:
        run_batch(args.project, args.run, args.units, args.max_seconds, args.interval,
                  args.search_pages, args.review_limit, task_ids, args.checkpoint_file,
                  emit=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        print(json.dumps({"ok": False, "type": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, legacy.ResearchError) else "Inspect local environment"}))
        raise SystemExit(1)
