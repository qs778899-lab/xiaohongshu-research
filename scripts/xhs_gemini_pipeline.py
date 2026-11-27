#!/usr/bin/env python3
"""Dual-mode Gemini async collect + parallel review pipeline for xiaohongshu-research.

Preserves single-account serial browser locking, immutable SHA-256 snapshots,
and fail-closed STOP guardrails while running Gemini review/triage/vision
asynchronously in parallel with collection.
"""
from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import threading
import time
import uuid

import xhs_account_identity as identity
import xhs_bridge as bridge
import xhs_gemini_client as gc
import xhs_native as native
import xhs_project as xp
import xhs_research as legacy
import xhs_rounds as rounds

DEFAULT_DEEP_OPTIONS = {'deepRoots': 50, 'deepReplyClicks': 5, 'deepBudgetMs': 14000}
STAGE_REVIEW_ORDER = {'body': 0, 'roots': 1, 'replies': 2, 'search': 3, 'author': 4, 'images': 5}
STAGE_COLLECT_ORDER = {'body': 0, 'roots': 1, 'replies': 2, 'author': 3, 'search': 4, 'images': 5, 'video': 6}
HEX_24_RE = re.compile(r'^[a-f0-9]{24}$')


def _gemini_dir(project: Path) -> Path:
    root = Path(project).resolve() / 'gemini'
    if root.is_symlink():
        raise legacy.ResearchError('Project gemini directory must not be a symlink')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise legacy.ResearchError('Journal path must not be a symlink')
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def enable_wal(project: Path) -> None:
    """Enable SQLite WAL mode for smooth concurrent reader/short-writer access."""
    database = Path(project).resolve() / 'project.sqlite3'
    if not database.is_file() or database.is_symlink():
        return
    conn = sqlite3.connect(database, timeout=15)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=15000')
        conn.commit()
    finally:
        conn.close()


def _open_project(project: Path) -> xp.Project:
    db = xp.Project(project)
    db.db.execute('PRAGMA busy_timeout=15000')
    rounds.schema(db)
    return db


def _extract_cards_from_packet(packet: dict) -> list[dict]:
    cards = []
    seen = set()
    for snap in packet.get('snapshots') or []:
        h = snap.get('hash', '')
        raw_path = Path(snap.get('file', ''))
        if not raw_path.is_file():
            continue
        raw = json.loads(raw_path.read_text(encoding='utf-8'))
        raw_cards = raw.get('cards') or []
        reading_cards = (snap.get('reading') or {}).get('cards')
        target_urls = {c.get('url') for c in reading_cards if isinstance(c, dict)} if isinstance(reading_cards, list) else None
        for idx, card in enumerate(raw_cards):
            if not isinstance(card, dict):
                continue
            url = card.get('url') or ''
            if target_urls is not None and url not in target_urls:
                continue
            key = rounds.card_review_key(card)
            if not key:
                continue
            nid = key[0]
            locator = f'cards/{idx}'
            dedupe_key = (nid, locator, h)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            cards.append({
                'note_id': nid,
                'locator': locator,
                'sha256': h,
                'url': url,
                'source_file': str(raw_path),
                'text': card.get('text') or '',
                'title': card.get('title') or '',
                'author': card.get('author') or '',
            })
    return cards


def _read_unreviewed_media_items(db: xp.Project, task: dict) -> list[dict]:
    note_id = task['payload']['target']
    rows = db.db.execute(
        "SELECT media_key, kind, body, state FROM media_inventory WHERE note_id=? AND state='captured_unreviewed' ORDER BY media_key",
        (note_id,),
    ).fetchall()
    out = []
    for row in rows:
        if row['kind'] != 'image' or not str(row['media_key']).startswith('body:'):
            continue
        body = json.loads(row['body'])
        file_path = body.get('file', '')
        sha = body.get('sha256', '')
        pos = body.get('position')
        if file_path and sha and isinstance(pos, int) and Path(file_path).is_file():
            out.append({
                'media_key': row['media_key'],
                'note_id': note_id,
                'position': pos,
                'file': file_path,
                'sha256': sha,
                'url': body.get('url', ''),
            })
    return out


def find_reviewable_tasks(db: xp.Project, *, round_id: str | None = None,
                          include_media: bool = False, skip_ids: set[str] | None = None) -> list[dict]:
    skip = skip_ids or set()
    allowed = set(rounds.task_ids(db, round_id)) if round_id else None
    dispatch = native.dispatch_snapshot(db)
    candidates = []
    for row in db.db.execute("SELECT id FROM tasks WHERE kind='native' AND status='pending' ORDER BY priority, turn, id"):
        tid = row['id']
        if tid in skip or (allowed is not None and tid not in allowed):
            continue
        task = db.task(tid, native_dispatch=dispatch)
        p = task['payload']
        stage = p.get('stage')
        if stage not in STAGE_REVIEW_ORDER:
            continue
        if stage == 'images' and not include_media:
            continue
        pr = p.get('bridge') or {}
        known = list(dict.fromkeys(pr.get('hashes') or []))
        if not known:
            continue
        reviewed = set(pr.get('reviewed_hashes') or [])
        diagnosed = set(bridge.capture_diagnostic_status(db, task)['confirmed_hashes']) if stage == 'images' else set()
        unread_hashes = [h for h in known if h not in reviewed and h not in diagnosed]
        has_unread_media = stage == 'images' and bool(_read_unreviewed_media_items(db, task))
        if not unread_hashes and not has_unread_media:
            continue
        candidates.append(task)
    candidates.sort(key=lambda t: (STAGE_REVIEW_ORDER.get(t['payload'].get('stage'), 99), t.get('priority', 50), t['id']))
    return candidates


def _update_discoveries(project: Path, *, task_id: str, stage: str, source_hashes: list[str],
                        aliases: list[str], queries: list[str], authors: list[str]) -> None:
    if not (aliases or queries or authors):
        return
    path = _gemini_dir(project) / 'discoveries.json'
    data = legacy.read_json(path, {'format': 'xhs-gemini-discoveries-v1', 'aliases': {}, 'queries': {}, 'authors': {}})
    for bucket, values in (('aliases', aliases), ('queries', queries), ('authors', authors)):
        store = data.setdefault(bucket, {})
        for value in values:
            entry = store.setdefault(value, {'count': 0, 'sources': [], 'first_seen': legacy.now()})
            entry['count'] += 1
            entry['last_seen'] = legacy.now()
            ref = {'task_id': task_id, 'stage': stage, 'hashes': source_hashes[:4]}
            if ref not in entry['sources']:
                entry['sources'].append(ref)
    legacy.write_json(path, data)


def _enqueue_downstream(db: xp.Project, project: Path, *, stage: str, task: dict,
                        triage_cards: list[dict] | None = None, triage_result: dict | None = None,
                        body_result: dict | None = None, discovered_queries: list[str] | None = None,
                        round_id: str | None = None, auto_enqueue: bool = True,
                        auto_expand_queries: bool = False, expand_budget: list[int] | None = None) -> list[str]:
    if not auto_enqueue:
        return []
    units = []
    if stage in {'search', 'author'} and triage_result and triage_cards:
        by_key = {(c['note_id'], c['locator']): c for c in triage_cards}
        for dec in triage_result.get('decisions') or []:
            if dec.get('decision') != 'include':
                continue
            card = by_key.get((dec['note_id'], dec['locator']))
            if not card or not card.get('source_file'):
                continue
            units.append({
                'stage': 'body',
                'target': dec['note_id'],
                'priority': int(dec.get('priority', 30)),
                'sources': [card['source_file']],
                'next': f'Gemini triaged include ({dec["reason"][:120]}); collect full note body.',
            })
    elif stage == 'body' and body_result:
        if body_result.get('relevant') or body_result.get('should_collect_comments'):
            hashes = (task['payload'].get('bridge') or {}).get('hashes') or []
            if hashes:
                src = str(project / 'bridge' / 'raw' / (hashes[-1] + '.json'))
                if Path(src).is_file():
                    target = task['payload']['target']
                    units.append({'stage': 'roots', 'target': target, 'priority': 20, 'sources': [src],
                                  'next': 'Gemini body review marked relevant/comment-worthy; collect root comments.'})
                    units.append({'stage': 'replies', 'target': target, 'priority': 25, 'sources': [src],
                                  'next': 'Collect nested replies after root comments.'})
    if auto_expand_queries and round_id is None and discovered_queries and expand_budget is not None and expand_budget[0] > 0:
        hashes = (task['payload'].get('bridge') or {}).get('hashes') or []
        src = str(project / 'bridge' / 'raw' / (hashes[-1] + '.json')) if hashes else ''
        if src and Path(src).is_file():
            for q in discovered_queries:
                if expand_budget[0] <= 0:
                    break
                q_clean = gc.normalize_ws(q)
                if not q_clean or len(q_clean) > 80:
                    continue
                for sort in xp.SORTS:
                    units.append({'stage': 'search', 'target': q_clean, 'sort': sort, 'priority': 45,
                                  'sources': [src], 'next': 'Gemini dynamic query expansion from grounded evidence.'})
                expand_budget[0] -= 1
    if not units:
        return []
    manifest_dir = _gemini_dir(project) / 'manifests'
    manifest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest_path = manifest_dir / f'{task["id"]}-{uuid.uuid4().hex[:12]}.json'
    legacy.write_json(manifest_path, {'units': units})
    imported = native.import_manifest(db, manifest_path)['task_ids']
    if round_id and imported:
        rounds.inherit_tasks(db, task['id'], imported)
    return imported


def _build_receipt(db: xp.Project, project: Path, task: dict, packet: dict, *,
                   summary: str, model: str, gaps: list[str], positions: list[str],
                   extra: dict | None = None, media_reviewed_keys: list[str] | None = None) -> dict:
    p = task['payload']
    stage = p['stage']
    pr = p.get('bridge') or {}
    unread_hashes = [s['hash'] for s in (packet.get('snapshots') or [])]
    more_entities = bool(packet.get('more_entities'))
    snapshot_hashes = [] if more_entities else unread_hashes
    source_file = (packet['snapshots'][-1]['file'] if packet.get('snapshots')
                   else str(project / 'bridge' / 'raw' / ((pr.get('hashes') or [''])[0] + '.json')))
    url = pr.get('last_url') or ''
    if not url and packet.get('snapshots'):
        snap_data = json.loads(Path(packet['snapshots'][-1]['file']).read_text(encoding='utf-8'))
        url = snap_data.get('url') or ''
    if not url:
        url = bridge.observed_url(project, task)

    complete = False
    end_token = None
    if not more_entities and not gaps and not (pr.get('fault') or {}).get('hold'):
        if stage == 'body' and snapshot_hashes:
            complete = True
            end_token = 'body_text_read'
        elif stage in {'search', 'author'} and pr.get('collection_end') is True:
            complete = True
            end_token = 'platform_end'
        elif stage in {'roots', 'replies'}:
            cs = bridge.reviewed_comment_state(db, task)
            if cs and cs.get('complete') is True:
                complete = True
                end_token = 'platform_end' if stage == 'roots' else 'all_discovered_threads_read'

    receipt = {
        'task_id': task['id'],
        'target': p['target'],
        'action': native.DEFAULT_ACTION.get(stage, 'read'),
        'outcome': 'ready',
        'observed_at': legacy.now(),
        'summary': summary,
        'source_file': source_file,
        'url': url,
        'target_verified': True,
        'content_ready': True,
        'view_consistent': True,
        'content_observed': True,
        'reviewed': True,
        'reviewer': 'gemini',
        'model': model,
        'prompt_version': gc.PROMPT_VERSION,
        'review_packet_id': packet['id'],
        'bridge_snapshot_hashes': snapshot_hashes,
        'positions': positions or ['bridge/raw/' + (snapshot_hashes[-1] if snapshot_hashes else 'partial') + '.json'],
        'gaps': gaps,
        'complete': complete,
    }
    if stage == 'search':
        receipt['sort'] = p['sort']
    if complete and end_token:
        receipt['end'] = end_token
    if media_reviewed_keys is not None:
        receipt['media_verified'] = bool(media_reviewed_keys)
        receipt['media_reviewed_keys'] = media_reviewed_keys
        receipt['complete'] = False  # Carousel/comment media completion remains explicit.
        receipt.pop('end', None)
    if extra:
        receipt['gemini_review'] = extra
    return receipt


def review_one_task(project: Path, task_id: str, client: gc.GeminiClient, research_goal: str, *,
                    db_lock: threading.RLock, round_id: str | None = None,
                    auto_enqueue: bool = True, auto_expand_queries: bool = False,
                    expand_budget: list[int] | None = None, access_stopped: threading.Event | None = None,
                    deep_entity_threshold: int = 60, packet_limit: int = 150) -> dict:
    """Prepare packet under lock, call Gemini outside lock, commit verified receipt under lock."""
    project = Path(project).resolve()
    with db_lock, xp.project_lock(project):
        db = _open_project(project)
        try:
            task = db.task(task_id)
            stage = task['payload']['stage']
            packet = rounds.review_next(db, task_id, limit=packet_limit, round_id=round_id)
            media_items = _read_unreviewed_media_items(db, task) if stage == 'images' else []
            cards = _extract_cards_from_packet(packet) if stage in {'search', 'author'} else []
            latest_body = None
            if stage == 'body' and packet.get('snapshots'):
                latest_body = json.loads(Path(packet['snapshots'][-1]['file']).read_text(encoding='utf-8'))
        finally:
            db.close()

    # Execute network Gemini API call completely outside DB/project locks.
    triage_res = body_res = comment_res = None
    media_obs = []
    aliases: list[str] = []
    queries: list[str] = []
    authors: list[str] = []
    findings_records: list[dict] = []
    gaps: list[str] = []
    positions: list[str] = []
    model_used = client.config.fast_model
    summary = ''
    media_keys: list[str] | None = None

    if stage in {'search', 'author'}:
        triage_res = client.triage_search_cards(cards, research_goal, query=task['payload']['target'],
                                                sort=task['payload'].get('sort', ''))
        summary = triage_res['summary']
        model_used = triage_res['model']
        aliases = triage_res['discovered_aliases']
        queries = triage_res['discovered_queries']
        positions = [f'{c["sha256"][:12]}:{c["locator"]}' for c in cards[:30]] or ['cards/empty']
        if not (task['payload'].get('bridge') or {}).get('collection_end'):
            gaps.append('pagination_not_at_platform_end')
    elif stage == 'body':
        if latest_body is None:
            raise gc.GeminiClientError('BODY_SNAPSHOT_MISSING', f'No unread body snapshot for {task_id}')
        body_res = client.review_note_body(task['payload']['target'], latest_body, research_goal)
        summary = body_res['summary']
        model_used = body_res['model']
        aliases = body_res['discovered_aliases']
        authors = body_res['discovered_authors']
        gaps = list(body_res['gaps'])
        positions = ['/title', '/body', '/authors']
        for f in body_res['findings']:
            findings_records.append({
                'at': legacy.now(), 'task_id': task_id, 'stage': 'body',
                'note_id': task['payload']['target'], 'url': latest_body.get('url', ''),
                'source_hashes': [s['hash'] for s in packet.get('snapshots', [])],
                'reviewer': 'gemini', 'model': model_used, **f,
            })
    elif stage in {'roots', 'replies'}:
        use_deep = len(packet.get('items') or []) >= deep_entity_threshold
        comment_res = client.review_comment_packet(packet, research_goal, use_deep=use_deep)
        summary = comment_res['summary']
        model_used = comment_res['model']
        aliases = comment_res['discovered_aliases']
        authors = comment_res['discovered_authors']
        gaps = list(comment_res['gaps'])
        positions = [f'entity:{eid}' for eid in comment_res['reviewed_entity_ids'][:40]] or ['comments/empty']
        for f in comment_res['findings']:
            findings_records.append({
                'at': legacy.now(), 'task_id': task_id, 'stage': stage,
                'note_id': task['payload']['target'],
                'source_hashes': [s['hash'] for s in packet.get('snapshots', [])],
                'reviewer': 'gemini', 'model': model_used, **f,
            })
    elif stage == 'images':
        media_keys = []
        for item in media_items:
            obs = client.review_media_image(item['note_id'], item['position'], item['file'],
                                            item['sha256'], research_goal)
            media_obs.append(obs)
            model_used = obs['model']
            aliases.extend(obs['discovered_aliases'])
            if obs['readable'] and not obs['blank_or_unloaded']:
                media_keys.append(item['media_key'])
                positions.append(item['media_key'])
            else:
                gaps.append(f'{item["media_key"]}:unreadable_or_blank')
        if not media_obs:
            summary = 'No captured unreviewed image positions available for visual inspection.'
            gaps.append('no_captured_unreviewed_images')
        else:
            summary = '; '.join(f'pos {o["position"]}: {o["observation"][:120]}' for o in media_obs)
            gaps.append('comment_attachments_and_video_separate')
        for o in media_obs:
            findings_records.append({
                'at': legacy.now(), 'task_id': task_id, 'stage': 'images',
                'note_id': o['note_id'], 'position': o['position'], 'sha256': o['sha256'],
                'reviewer': 'gemini', 'model': o['model'], 'readable': o['readable'],
                'observation': o['observation'], 'ocr_key_quotes': o['ocr_key_quotes'],
            })
    else:
        raise gc.GeminiClientError('UNSUPPORTED_REVIEW_STAGE', f'Stage {stage} unsupported')

    with db_lock, xp.project_lock(project):
        db = _open_project(project)
        try:
            fresh_task = db.task(task_id)
            receipt = _build_receipt(
                db, project, fresh_task, packet,
                summary=summary, model=model_used, gaps=gaps, positions=positions,
                media_reviewed_keys=media_keys,
                extra={
                    'triage_count': len((triage_res or {}).get('decisions') or []),
                    'findings_count': len(findings_records),
                    'media_reviewed_count': len(media_keys or []),
                },
            )
            if stage == 'images' and media_items:
                receipt['source_file'] = media_items[0]['file']
            receipt_dir = _gemini_dir(project) / 'receipts'
            receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            receipt_path = receipt_dir / f'{task_id}-{packet["id"][:16]}-{uuid.uuid4().hex[:8]}.json'
            legacy.write_json(receipt_path, receipt)
            committed = bridge.review(db, task_id, receipt_path)
            for rec in findings_records:
                _append_jsonl(_gemini_dir(project) / 'findings.jsonl', rec)
            _update_discoveries(project, task_id=task_id, stage=stage,
                                source_hashes=receipt['bridge_snapshot_hashes'],
                                aliases=aliases, queries=queries, authors=authors)
            allow_enqueue = auto_enqueue and not (access_stopped is not None and access_stopped.is_set())
            enqueued = _enqueue_downstream(
                db, project, stage=stage, task=fresh_task,
                triage_cards=cards, triage_result=triage_res, body_result=body_res,
                discovered_queries=queries, round_id=round_id,
                auto_enqueue=allow_enqueue, auto_expand_queries=auto_expand_queries,
                expand_budget=expand_budget,
            )
        finally:
            db.close()

    return {
        'event': 'review_committed',
        'task_id': task_id,
        'stage': stage,
        'model': model_used,
        'event_id': committed['event_id'],
        'status': committed['status'],
        'awaiting_review': committed['awaiting_review'],
        'findings_count': len(findings_records),
        'enqueued_tasks': enqueued,
    }


def _select_sticky_task(rows: list[dict], active_note: str | None) -> dict:
    """Prefer finishing the currently open detail note (body->roots->replies) before switching tabs."""
    if active_note:
        same_note = [t for t in rows if t['payload'].get('target') == active_note
                     and t['payload'].get('stage') in {'body', 'roots', 'replies'}]
        if same_note:
            same_note.sort(key=lambda t: (STAGE_COLLECT_ORDER.get(t['payload']['stage'], 99), t.get('priority', 50), t['id']))
            return same_note[0]
    rows.sort(key=lambda t: (STAGE_COLLECT_ORDER.get(t['payload'].get('stage'), 99),
                             t.get('turn', 0), t.get('priority', 50), t['id']))
    return rows[0]


def _pick_next_collect_task(db: xp.Project, binding: dict, *, round_id: str | None,
                            include_media: bool, active_note: str | None) -> tuple[dict | None, int]:
    allowed = set(rounds.task_ids(db, round_id)) if round_id else None
    dispatch = native.dispatch_snapshot(db)
    allrows = [db.task(r[0], native_dispatch=dispatch) for r in db.db.execute("SELECT id FROM tasks WHERE kind='native'")]
    pending_reviews = sum(
        len(set((t['payload'].get('bridge') or {}).get('hashes') or [])
            - set((t['payload'].get('bridge') or {}).get('reviewed_hashes') or []))
        for t in allrows if t['status'] != 'complete'
    )
    rows = []
    for t in allrows:
        p = t['payload']
        stage = p.get('stage')
        if t['status'] != 'pending' or stage not in bridge.STAGES:
            continue
        if stage in {'images', 'video'} and not include_media:
            continue
        if allowed is not None and t['id'] not in allowed:
            continue
        if stage in {'roots', 'replies'}:
            t = bridge.sync_shared_comment_stall(db, t)
        p = t['payload']
        pr = p.get('bridge') or {}
        fault = pr.get('fault') or {}
        reply_scope = bridge.active_discovered_reply_scope(db, t) if stage == 'replies' else None
        repair = fault.get('code') == 'PAGINATION_STALLED' and not fault.get('recovery', {}).get('exhausted')
        vis_repair = bridge.background_visibility_candidate(t, binding)
        if fault.get('hold') and not (repair or vis_repair):
            continue
        if pr.get('awaiting_review') and not repair:
            continue
        if stage == 'body' and pr.get('hashes') and not (repair or vis_repair):
            continue
        if reply_scope and pr.get('discovered_reply_gap'):
            continue
        if stage == 'replies':
            roots = [x for x in allrows if x['payload'].get('target') == p['target'] and x['payload'].get('stage') == 'roots']
            if not reply_scope and roots and roots[0]['status'] != 'complete' and not bridge.root_pagination_ended(roots[0]['payload'].get('bridge') or {}):
                continue
        rows.append(t)
    if (binding.get('web_channel_authorization') or binding.get('web_parent_authorization')) and not binding.get('web_validation'):
        rows = [t for t in rows if t['payload']['stage'] == 'body']
    if not rows:
        return None, pending_reviews
    return _select_sticky_task(rows, active_note), pending_reviews


def run_pipeline(project: Path, binding_name: str, *, research_goal: str = '',
                 mode: str = 'api-async', units: int = 40, max_seconds: int = 1800,
                 interval: float = 5.0, jitter: float = 1.2, review_workers: int = 4,
                 max_review_backlog: int = 20, round_id: str | None = None,
                 auto_enqueue: bool = True, auto_expand_queries: bool = False,
                 max_expanded_queries: int = 0, include_media: bool = False,
                 deep_options: dict | None = None, client: gc.GeminiClient | None = None,
                 driver=None, emit=None, clock=time.monotonic, sleep=time.sleep) -> dict:
    """Run dual-track async collection + parallel Gemini review (or collect-only / review-only)."""
    if mode not in {'api-async', 'collect-only', 'review-only'}:
        raise legacy.ResearchError('Mode must be api-async, collect-only, or review-only')
    if not 1 <= units <= 500 or not 1 <= max_seconds <= 7200 or interval < 3.0 or jitter < 0 or jitter > interval:
        raise legacy.ResearchError('Invalid pipeline budget or pacing interval')
    if not 1 <= review_workers <= 16 or max_review_backlog < 1:
        raise legacy.ResearchError('Invalid reviewer worker count or backlog threshold')

    project = Path(project).resolve()
    enable_wal(project)
    journal_path = _gemini_dir(project) / 'pipeline-journal.jsonl'
    pause_file = project / 'bridge' / 'PAUSE'
    db_lock = threading.RLock()
    state_lock = threading.Lock()
    stop_event = threading.Event()
    access_stopped = threading.Event()
    collector_done = threading.Event()
    review_progress_event = threading.Event()
    in_flight: set[str] = set()
    failed_reviews: set[str] = set()
    expand_budget = [max(0, int(max_expanded_queries))]
    started = clock()

    stats = {
        'mode': mode,
        'units_collected': 0,
        'reviews_committed': 0,
        'review_errors': 0,
        'findings_extracted': 0,
        'tasks_enqueued': 0,
        'backpressure_waits': 0,
        'stop_reason': '',
    }

    def record_event(ev: dict) -> None:
        entry = {'at': legacy.now(), **ev}
        with state_lock:
            _append_jsonl(journal_path, entry)
        if emit:
            emit(entry)

    if mode in {'api-async', 'review-only'}:
        client = client or gc.GeminiClient()
        if not research_goal.strip():
            raise legacy.ResearchError('Research goal is required for Gemini review modes')

    active_futures: set[Future] = set()

    def review_worker_loop(executor: ThreadPoolExecutor) -> None:
        while True:
            # Harvest finished futures.
            done_futs = {f for f in list(active_futures) if f.done()}
            for fut in done_futs:
                active_futures.discard(fut)
                tid_done = getattr(fut, '_xhs_task_id', '')
                with state_lock:
                    in_flight.discard(tid_done)
                try:
                    res = fut.result()
                    with state_lock:
                        stats['reviews_committed'] += 1
                        stats['findings_extracted'] += res.get('findings_count', 0)
                        stats['tasks_enqueued'] += len(res.get('enqueued_tasks') or [])
                    record_event(res)
                except Exception as exc:
                    with state_lock:
                        stats['review_errors'] += 1
                        if tid_done:
                            failed_reviews.add(tid_done)
                    record_event({'event': 'review_error', 'task_id': tid_done,
                                  'code': getattr(exc, 'code', type(exc).__name__),
                                  'detail': gc.sanitize_error_text(str(exc))})
                review_progress_event.set()

            # Submit new reviewable tasks while capacity exists.
            submitted = 0
            if len(active_futures) < review_workers and not (stop_event.is_set() and not active_futures and collector_done.is_set()):
                with db_lock, xp.project_lock(project):
                    db = _open_project(project)
                    try:
                        with state_lock:
                            skip = set(in_flight) | set(failed_reviews)
                        ready = find_reviewable_tasks(db, round_id=round_id, include_media=include_media, skip_ids=skip)
                    finally:
                        db.close()
                for task in ready:
                    if len(active_futures) >= review_workers:
                        break
                    tid = task['id']
                    with state_lock:
                        if tid in in_flight:
                            continue
                        in_flight.add(tid)
                    fut = executor.submit(
                        review_one_task, project, tid, client, research_goal,
                        db_lock=db_lock, round_id=round_id, auto_enqueue=auto_enqueue,
                        auto_expand_queries=auto_expand_queries, expand_budget=expand_budget,
                        access_stopped=access_stopped,
                    )
                    fut._xhs_task_id = tid
                    active_futures.add(fut)
                    submitted += 1

            if collector_done.is_set() and not active_futures and submitted == 0:
                break
            if clock() - started >= max_seconds and not active_futures:
                break
            sleep(0.15)

    def collector_loop() -> None:
        if mode == 'review-only':
            collector_done.set()
            return
        if pause_file.exists():
            stats['stop_reason'] = 'paused_requested'
            collector_done.set()
            return
        b = bridge.load_binding(project, binding_name)
        owned_driver = driver is None
        drv = driver or bridge.Driver(b['runtime'])
        validated_deep = bridge._validate_deep_options(deep_options or DEFAULT_DEEP_OPTIONS)
        active_note: str | None = None
        idle_polls = 0
        try:
            while stats['units_collected'] < units and clock() - started < max_seconds:
                if pause_file.exists():
                    stats['stop_reason'] = 'paused_requested'
                    break
                if stop_event.is_set():
                    stats['stop_reason'] = stats['stop_reason'] or 'stop_requested'
                    break
                with db_lock, xp.project_lock(project):
                    db = _open_project(project)
                    try:
                        b = bridge.load_binding(project, binding_name)
                        task, pending_reviews = _pick_next_collect_task(
                            db, b, round_id=round_id, include_media=include_media, active_note=active_note)
                    finally:
                        db.close()
                if pending_reviews >= max_review_backlog:
                    if mode == 'collect-only':
                        stats['stop_reason'] = 'review_backlog_backpressure'
                        break
                    with state_lock:
                        stats['backpressure_waits'] += 1
                    record_event({'event': 'backpressure_wait', 'pending_reviews': pending_reviews,
                                  'max_review_backlog': max_review_backlog})
                    review_progress_event.clear()
                    review_progress_event.wait(timeout=0.5)
                    continue
                if task is None:
                    with state_lock:
                        reviewers_busy = bool(active_futures or in_flight)
                        failed_count = len(failed_reviews)
                    unfailed_pending = max(0, pending_reviews - failed_count)
                    if mode == 'api-async' and (reviewers_busy or unfailed_pending > 0) and idle_polls < 60:
                        idle_polls += 1
                        review_progress_event.clear()
                        review_progress_event.wait(timeout=0.25)
                        continue
                    stats['stop_reason'] = 'no_runnable_tasks'
                    break
                idle_polls = 0
                stage = task['payload']['stage']
                unit_start = clock()
                try:
                    if stage in {'images', 'video'}:
                        from xhs_bridge_media import collect_inventory
                        with db_lock, xp.project_lock(project):
                            db = _open_project(project)
                            try:
                                r = collect_inventory(db, binding_name, task, drv)
                            finally:
                                db.close()
                    else:
                        with bridge.lease(project, b) as keys:
                            try:
                                with db_lock, xp.project_lock(project):
                                    db = _open_project(project)
                                    try:
                                        if bridge.background_visibility_candidate(task, b):
                                            r = bridge.collect_background_visibility_probe(
                                                db, b, drv, task, 'Gemini pipeline background visibility check')
                                        elif (task['payload'].get('bridge') or {}).get('fault', {}).get('code') == 'PAGINATION_STALLED':
                                            r = bridge.repair_pagination(db, b, drv, task)
                                        else:
                                            r = bridge.collect_one(db, b, drv, task, fast_mode=True, deep_options=validated_deep)
                                    finally:
                                        db.close()
                            except Exception as exc:
                                bridge.save_failure(project, binding_name, b, exc, keys)
                                raise
                            b['state'] = 'ready'
                            b['last_success'] = {'task': task['id'], 'at': legacy.now()}
                            bridge.save_binding(project, binding_name, b)
                except legacy.CollectionBusy:
                    sleep(min(interval, 2.0))
                    continue
                except legacy.ResearchError as exc:
                    reason = 'SHARED_STOP' if isinstance(exc, legacy.StopRun) else getattr(exc, 'code', 'CONNECTOR_ERROR')
                    if reason in bridge.LOCAL_FAULTS:
                        with db_lock, xp.project_lock(project):
                            db = _open_project(project)
                            try:
                                bridge.hold_task(db, task['id'], reason)
                            finally:
                                db.close()
                        r = {'local_gap': reason}
                    else:
                        access_stopped.set()
                        stop_event.set()
                        stats['stop_reason'] = reason
                        record_event({'event': 'access_stop_or_blocked', 'reason': reason, 'task_id': task['id']})
                        break

                with db_lock, xp.project_lock(project):
                    db = _open_project(project)
                    try:
                        db.db.execute('UPDATE tasks SET turn=turn+1 WHERE id=?', (task['id'],))
                        db.db.commit()
                    finally:
                        db.close()
                if stage in {'body', 'roots', 'replies'}:
                    active_note = task['payload']['target']
                stats['units_collected'] += 1
                record_event({'event': 'unit_collected', 'task_id': task['id'], 'stage': stage,
                              'target': task['payload']['target'], 'elapsed_s': round(clock() - unit_start, 3),
                              'local_gap': r.get('local_gap'), 'snapshot': r.get('snapshot')})
                if stats['units_collected'] < units and clock() - started < max_seconds:
                    wait_s = max(3.0, interval + random.uniform(-jitter, jitter)) if jitter else interval
                    if r.get('local_gap') == 'CONTENT_LOADING':
                        wait_s = min(60.0, wait_s * 1.5)
                    deadline = clock() + wait_s
                    while clock() < deadline:
                        if pause_file.exists() or stop_event.is_set():
                            break
                        sleep(min(0.25, max(0.0, deadline - clock())))
            if not stats['stop_reason']:
                stats['stop_reason'] = 'unit_budget' if stats['units_collected'] >= units else 'time_budget'
        finally:
            if owned_driver:
                drv.close()
            collector_done.set()

    try:
        if mode == 'collect-only':
            collector_loop()
        else:
            with ThreadPoolExecutor(max_workers=review_workers) as executor:
                collector_thread = threading.Thread(target=collector_loop, daemon=True)
                collector_thread.start()
                review_worker_loop(executor)
                collector_thread.join(timeout=10)
    except KeyboardInterrupt:
        stop_event.set()
        stats['stop_reason'] = 'user_interrupted'
        if not pause_file.exists():
            legacy.write_json(pause_file, {'at': legacy.now(), 'reason': 'user_interrupted'})

    summary = {
        'event': 'pipeline_summary',
        'project': str(project),
        'wall_seconds': round(clock() - started, 3),
        'access_stopped': access_stopped.is_set(),
        **stats,
    }
    record_event(summary)
    return summary


def export_review_batch(project: Path, output_path: Path, *, limit: int = 8,
                        round_id: str | None = None, include_media: bool = True,
                        packet_limit: int = 150) -> dict:
    """Export unread review packets for Antigravity run_workflow parallel subagent review."""
    project = Path(project).resolve()
    out = Path(output_path)
    if out.exists() or out.is_symlink():
        raise legacy.ResearchError('Export destination already exists; pick a unique file')
    with xp.project_lock(project):
        db = _open_project(project)
        try:
            tasks = find_reviewable_tasks(db, round_id=round_id, include_media=include_media)[:limit]
            items = []
            for task in tasks:
                stage = task['payload']['stage']
                packet = rounds.review_next(db, task['id'], limit=packet_limit, round_id=round_id)
                entry = {
                    'task_id': task['id'],
                    'stage': stage,
                    'target': task['payload']['target'],
                    'sort': task['payload'].get('sort', ''),
                    'packet_id': packet['id'],
                    'packet_file': packet['file'],
                    'more_entities': packet['more_entities'],
                    'snapshot_hashes': [s['hash'] for s in packet.get('snapshots', [])],
                }
                if stage in {'search', 'author'}:
                    entry['cards'] = _extract_cards_from_packet(packet)
                elif stage == 'body' and packet.get('snapshots'):
                    snap = json.loads(Path(packet['snapshots'][-1]['file']).read_text(encoding='utf-8'))
                    entry['body_snapshot'] = {k: snap.get(k) for k in ('url', 'title', 'body', 'authors', 'media')}
                elif stage in {'roots', 'replies'}:
                    entry['compact_review'] = rounds.compact_review(packet)
                elif stage == 'images':
                    entry['media_items'] = _read_unreviewed_media_items(db, task)
                items.append(entry)
        finally:
            db.close()
    payload = {
        'format': 'xhs-gemini-hybrid-batch-v1',
        'created_at': legacy.now(),
        'project': str(project),
        'round_id': round_id,
        'task_count': len(items),
        'tasks': items,
    }
    legacy.write_json(out, payload)
    return {'output': str(out), 'task_count': len(items), 'task_ids': [i['task_id'] for i in items]}


def commit_review_batch(project: Path, results_path: Path, *, auto_enqueue: bool = True,
                        round_id: str | None = None, model_label: str = 'antigravity-gemini-hybrid') -> dict:
    """Validate grounded results produced by Antigravity parallel agents and commit receipts."""
    project = Path(project).resolve()
    data = json.loads(Path(results_path).read_text(encoding='utf-8'))
    results = data.get('results')
    if not isinstance(results, list):
        raise legacy.ResearchError('Batch results file requires a results list')
    committed_items = []
    errors = []
    for entry in results:
        tid = entry.get('task_id')
        pid = entry.get('packet_id')
        review_body = entry.get('review')
        try:
            with xp.project_lock(project):
                db = _open_project(project)
                try:
                    task = db.task(tid)
                    stage = task['payload']['stage']
                    row = db.db.execute('SELECT body FROM review_packets WHERE id=? AND task_id=?', (pid, tid)).fetchone()
                    if not row:
                        raise gc.GeminiClientError('UNKNOWN_PACKET', f'Packet {pid} not found for {tid}')
                    packet = json.loads(row['body'])
                    triage_res = body_res = comment_res = None
                    cards = []
                    findings = []
                    aliases = []
                    queries = []
                    authors = []
                    gaps = []
                    positions = []
                    media_keys = None
                    media_items = []
                    if stage in {'search', 'author'}:
                        cards = _extract_cards_from_packet(packet)
                        triage_res = gc.validate_triage_result(review_body, cards)
                        summary = triage_res['summary']
                        aliases = triage_res['discovered_aliases']
                        queries = triage_res['discovered_queries']
                        positions = [f'{c["sha256"][:12]}:{c["locator"]}' for c in cards[:30]] or ['cards/empty']
                        if not (task['payload'].get('bridge') or {}).get('collection_end'):
                            gaps.append('pagination_not_at_platform_end')
                    elif stage == 'body':
                        snap = json.loads(Path(packet['snapshots'][-1]['file']).read_text(encoding='utf-8'))
                        body_res = gc.validate_body_review_result(review_body, snap)
                        summary = body_res['summary']
                        aliases = body_res['discovered_aliases']
                        authors = body_res['discovered_authors']
                        gaps = list(body_res['gaps'])
                        positions = ['/title', '/body', '/authors']
                        findings = [{**f, 'at': legacy.now(), 'task_id': tid, 'stage': 'body',
                                     'note_id': task['payload']['target'], 'reviewer': 'gemini', 'model': model_label}
                                    for f in body_res['findings']]
                    elif stage in {'roots', 'replies'}:
                        comment_res = gc.validate_comment_packet_result(review_body, packet)
                        summary = comment_res['summary']
                        aliases = comment_res['discovered_aliases']
                        authors = comment_res['discovered_authors']
                        gaps = list(comment_res['gaps'])
                        positions = [f'entity:{e}' for e in comment_res['reviewed_entity_ids'][:40]] or ['comments/empty']
                        findings = [{**f, 'at': legacy.now(), 'task_id': tid, 'stage': stage,
                                     'note_id': task['payload']['target'], 'reviewer': 'gemini', 'model': model_label}
                                    for f in comment_res['findings']]
                    elif stage == 'images':
                        media_items = _read_unreviewed_media_items(db, task)
                        by_pos = {m['position']: m for m in media_items}
                        media_keys = []
                        obs_list = review_body.get('images') if isinstance(review_body, dict) else None
                        if not isinstance(obs_list, list) or not obs_list:
                            raise gc.GeminiClientError('GROUNDING_MEDIA_REQUIRED', 'images list required')
                        summaries = []
                        for img_res in obs_list:
                            pos = img_res.get('position')
                            if pos not in by_pos:
                                raise gc.GeminiClientError('GROUNDING_MEDIA_MISMATCH', f'Unknown position {pos}')
                            m = by_pos[pos]
                            val = gc.validate_media_image_result(img_res, m['note_id'], pos, m['sha256'])
                            summaries.append(f'pos {pos}: {val["observation"][:120]}')
                            if val['readable'] and not val['blank_or_unloaded']:
                                media_keys.append(m['media_key'])
                                positions.append(m['media_key'])
                            else:
                                gaps.append(f'{m["media_key"]}:unreadable_or_blank')
                        summary = '; '.join(summaries)
                        gaps.append('comment_attachments_and_video_separate')
                    else:
                        raise gc.GeminiClientError('UNSUPPORTED_STAGE', stage)

                    receipt = _build_receipt(db, project, task, packet, summary=summary,
                                             model=entry.get('model') or model_label,
                                             gaps=gaps, positions=positions, media_reviewed_keys=media_keys)
                    if stage == 'images' and media_items:
                        receipt['source_file'] = media_items[0]['file']
                    rdir = _gemini_dir(project) / 'receipts'
                    rdir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    rpath = rdir / f'{tid}-{pid[:16]}-hybrid-{uuid.uuid4().hex[:8]}.json'
                    legacy.write_json(rpath, receipt)
                    res = bridge.review(db, tid, rpath)
                    for rec in findings:
                        _append_jsonl(_gemini_dir(project) / 'findings.jsonl', rec)
                    _update_discoveries(project, task_id=tid, stage=stage,
                                        source_hashes=receipt['bridge_snapshot_hashes'],
                                        aliases=aliases, queries=queries, authors=authors)
                    enq = _enqueue_downstream(db, project, stage=stage, task=task,
                                              triage_cards=cards, triage_result=triage_res,
                                              body_result=body_res, round_id=round_id,
                                              auto_enqueue=auto_enqueue)
                    committed_items.append({'task_id': tid, 'event_id': res['event_id'],
                                            'status': res['status'], 'enqueued_tasks': enq})
                finally:
                    db.close()
        except Exception as exc:
            errors.append({'task_id': tid, 'code': getattr(exc, 'code', type(exc).__name__),
                           'detail': gc.sanitize_error_text(str(exc))})
    return {'committed': len(committed_items), 'errors': errors, 'items': committed_items}


def pipeline_status(project: Path, *, round_id: str | None = None) -> dict:
    project = Path(project).resolve()
    db = native.open_lookup_project(project)
    try:
        stop = db.get_meta('stop')
        summary = bridge.summary(db)
        stages: dict[str, dict] = {}
        allowed = set(rounds.task_ids(db, round_id)) if round_id else None
        for row in db.db.execute("SELECT id, status, payload FROM tasks WHERE kind='native'"):
            if allowed is not None and row['id'] not in allowed:
                continue
            p = json.loads(row['payload'])
            st = p.get('stage', 'unknown')
            bucket = stages.setdefault(st, {'total': 0, 'complete': 0, 'pending': 0, 'held': 0, 'awaiting_review': 0})
            bucket['total'] += 1
            if row['status'] == 'complete':
                bucket['complete'] += 1
            else:
                bucket['pending'] += 1
            br = p.get('bridge') or {}
            if (br.get('fault') or {}).get('hold'):
                bucket['held'] += 1
            if br.get('awaiting_review'):
                bucket['awaiting_review'] += 1
    finally:
        db.close()
    gdir = project / 'gemini'
    findings_file = gdir / 'findings.jsonl'
    if findings_file.is_file():
        with findings_file.open(encoding='utf-8') as stream:
            findings_count = sum(1 for _ in stream)
    else:
        findings_count = 0
    discoveries = legacy.read_json(gdir / 'discoveries.json', {'aliases': {}, 'queries': {}, 'authors': {}})
    return {
        'project': str(project),
        'project_stop': stop,
        'stages': stages,
        'bridge_summary': summary,
        'findings_count': findings_count,
        'discovered_counts': {k: len(discoveries.get(k) or {}) for k in ('aliases', 'queries', 'authors')},
        'no_platform_recall_estimate': True,
    }


def doctor(project: Path, binding_name: str | None = None, *, config: gc.GeminiConfig | None = None) -> dict:
    project = Path(project).resolve()
    cfg = config or gc.GeminiConfig.from_env()
    st = pipeline_status(project)
    binding_info = None
    shared_stops = []
    if binding_name:
        b = bridge.load_binding(project, binding_name)
        binding_info = {
            'binding_name': binding_name,
            'state': b.get('state'),
            'background_text_collection': b.get('background_text_collection') is True,
            'manual_single_tab': b.get('manual_single_tab') is True,
            'context_id': b.get('context_id'),
        }
        keys = identity.related_keys(b.get('profile_guard_key'), b.get('account_uid'))
        for k in keys:
            sp = identity.guard_directory(k) / 'STOP.json'
            if sp.is_file():
                shared_stops.append({'guard_key_prefix': k[:12], 'reason': legacy.read_json(sp, {}).get('reason')})
    recommended_mode = 'api-async' if cfg.api_key else 'agent-hybrid'
    return {
        'ok': True,
        'recommended_mode': recommended_mode,
        'gemini': cfg.public_summary(),
        'binding': binding_info,
        'shared_account_stops': shared_stops,
        'status': st,
    }


def synthesize(project: Path, research_goal: str, output_path: Path, *,
               client: gc.GeminiClient | None = None, offline_markdown: bool = False,
               round_id: str | None = None) -> dict:
    project = Path(project).resolve()
    out = Path(output_path)
    st = pipeline_status(project, round_id=round_id)
    findings_file = project / 'gemini' / 'findings.jsonl'
    findings = [json.loads(line) for line in findings_file.read_text(encoding='utf-8').splitlines() if line.strip()] if findings_file.is_file() else []
    discoveries = legacy.read_json(project / 'gemini' / 'discoveries.json', {'aliases': {}, 'queries': {}, 'authors': {}})
    bundle = {
        'research_goal': research_goal,
        'status': st,
        'findings': findings,
        'discoveries': discoveries,
    }
    appendix_lines = [
        '\n---\n',
        '## 覆盖范围与未完成缺口台账（程序核对）\n',
        '> 本报告仅覆盖已发现且实际审读的公开内容，不构成全站召回率或“无负面评价”断言。\n',
        '| 阶段 | 总任务 | 已完成 | 待处理 | 阻塞/缺口 | 待审读 |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
    ]
    for stage, b in sorted((st.get('stages') or {}).items()):
        appendix_lines.append(f'| {stage} | {b["total"]} | {b["complete"]} | {b["pending"]} | {b["held"]} | {b["awaiting_review"]} |')
    appendix = '\n'.join(appendix_lines) + '\n'

    if offline_markdown or (client is None and not os.environ.get('GEMINI_API_KEY')):
        lines = [f'# 小红书深度检索综合报告（离线汇编）\n', f'- 研究目标：{research_goal}',
                 f'- 已提取落地证据数：{len(findings)}\n', '## 分级证据摘录\n']
        for idx, f in enumerate(findings, 1):
            lines.append(f'{idx}. **[{f.get("attribution", f.get("stage", "obs"))}]** '
                         f'note `{f.get("note_id", "")}`：{f.get("experience_summary") or f.get("observation", "")}')
            if f.get('quote'):
                lines.append(f'   - 原文摘录：「{f["quote"]}」')
            if f.get('missing_confirmation'):
                lines.append(f'   - 待补确认：{f["missing_confirmation"]}')
        md = '\n'.join(lines) + appendix
        model = 'offline-deterministic'
    else:
        cli = client or gc.GeminiClient()
        res = cli.synthesize_report(research_goal, bundle)
        md = res['markdown'].rstrip() + appendix
        model = res['model']

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding='utf-8')
    return {'output': str(out), 'model': model, 'findings_count': len(findings),
            'sha256': hashlib.sha256(out.read_bytes()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    sub = parser.add_subparsers(dest='command', required=True)

    d = sub.add_parser('doctor')
    d.add_argument('--binding')

    s = sub.add_parser('status')
    s.add_argument('--round', dest='round_id')

    r = sub.add_parser('run')
    r.add_argument('--binding', required=True)
    r.add_argument('--mode', choices=('api-async', 'collect-only', 'review-only'), default='api-async')
    r.add_argument('--research-goal', default='')
    r.add_argument('--units', type=int, default=40)
    r.add_argument('--max-seconds', type=int, default=1800)
    r.add_argument('--interval', type=float, default=5.0)
    r.add_argument('--jitter', type=float, default=1.2)
    r.add_argument('--review-workers', type=int, default=4)
    r.add_argument('--max-review-backlog', type=int, default=20)
    r.add_argument('--round', dest='round_id')
    r.add_argument('--auto-enqueue', action=argparse.BooleanOptionalAction, default=True)
    r.add_argument('--auto-expand-queries', action='store_true')
    r.add_argument('--max-expanded-queries', type=int, default=0)
    r.add_argument('--include-media', action='store_true')
    r.add_argument('--fast-model')
    r.add_argument('--deep-model')
    r.add_argument('--vision-model')
    r.add_argument('--api-base')

    ex = sub.add_parser('export-review-batch')
    ex.add_argument('--output', required=True, type=Path)
    ex.add_argument('--limit', type=int, default=8)
    ex.add_argument('--round', dest='round_id')
    ex.add_argument('--include-media', action=argparse.BooleanOptionalAction, default=True)

    cm = sub.add_parser('commit-review-batch')
    cm.add_argument('--file', required=True, type=Path)
    cm.add_argument('--round', dest='round_id')
    cm.add_argument('--auto-enqueue', action=argparse.BooleanOptionalAction, default=True)

    sy = sub.add_parser('synthesize')
    sy.add_argument('--research-goal', required=True)
    sy.add_argument('--output', required=True, type=Path)
    sy.add_argument('--round', dest='round_id')
    sy.add_argument('--offline-markdown', action='store_true')
    sy.add_argument('--deep-model')
    sy.add_argument('--api-base')

    args = parser.parse_args()
    if args.command == 'doctor':
        result = doctor(args.project, args.binding)
    elif args.command == 'status':
        result = pipeline_status(args.project, round_id=args.round_id)
    elif args.command == 'export-review-batch':
        result = export_review_batch(args.project, args.output, limit=args.limit,
                                     round_id=args.round_id, include_media=args.include_media)
    elif args.command == 'commit-review-batch':
        result = commit_review_batch(args.project, args.file, auto_enqueue=args.auto_enqueue,
                                     round_id=args.round_id)
    elif args.command == 'synthesize':
        cli = None if args.offline_markdown else gc.GeminiClient(
            gc.GeminiConfig.from_env(deep_model=args.deep_model, api_base=args.api_base))
        result = synthesize(args.project, args.research_goal, args.output,
                            client=cli, offline_markdown=args.offline_markdown, round_id=args.round_id)
    else:
        cli = None
        if args.mode in {'api-async', 'review-only'}:
            cli = gc.GeminiClient(gc.GeminiConfig.from_env(
                fast_model=args.fast_model, deep_model=args.deep_model,
                vision_model=args.vision_model, api_base=args.api_base))
        result = run_pipeline(
            args.project, args.binding, research_goal=args.research_goal, mode=args.mode,
            units=args.units, max_seconds=args.max_seconds, interval=args.interval, jitter=args.jitter,
            review_workers=args.review_workers, max_review_backlog=args.max_review_backlog,
            round_id=args.round_id, auto_enqueue=args.auto_enqueue,
            auto_expand_queries=args.auto_expand_queries, max_expanded_queries=args.max_expanded_queries,
            include_media=args.include_media, client=cli,
            emit=lambda ev: print(json.dumps(ev, ensure_ascii=False), flush=True),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (legacy.ResearchError, gc.GeminiClientError, ValueError, OSError) as exc:
        print(json.dumps({'ok': False, 'code': getattr(exc, 'code', type(exc).__name__),
                          'reason': gc.sanitize_error_text(str(exc))}, ensure_ascii=False))
        raise SystemExit(1)
