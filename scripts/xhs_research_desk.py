#!/usr/bin/env python3
"""Offline research inventory and next-work suggestions. Never opens a browser.

Only explicitly selected existing tasks are read, in one read-only transaction.
Outputs are derived artifacts, never completion receipts or access permission.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlparse

import xhs_rounds as rounds


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def note_id(url):
    try:
        u = urlparse(url)
        if u.scheme != 'https' or u.hostname not in {'www.xiaohongshu.com', 'xiaohongshu.com'}:
            return None
        if u.username or u.password or u.port:
            return None
        m = re.fullmatch(r'/(?:explore|search_result|user/profile/[a-f0-9]{24})/([a-f0-9]{24})/?', u.path)
        return m[1] if m else None
    except (ValueError, TypeError):
        return None


def read_selected(project, task_ids):
    if not isinstance(task_ids, list) or not task_ids or any(not isinstance(x, str) for x in task_ids):
        raise ValueError('A nonempty explicit task-ID list is required')
    if len(task_ids) != len(set(task_ids)):
        raise ValueError('Duplicate task IDs')
    path = Path(project) / 'project.sqlite3'
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected existing project database')
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        tasks = []
        for tid in task_ids:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
            if not row:
                raise ValueError('Unknown selected task: ' + tid)
            t = dict(row)
            t['payload'] = json.loads(t['payload'])
            tasks.append(t)
        stop = db.execute("SELECT value FROM meta WHERE key='stop'").fetchone()
        # Media states are reported as recorded, not re-certified by this scan.
        media = []
        notes = {t['payload'].get('target') for t in tasks if t['payload'].get('stage') != 'search'}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='media_inventory'").fetchone():
            for note in sorted(x for x in notes if isinstance(x, str)):
                for row in db.execute('SELECT * FROM media_inventory WHERE note_id=?', (note,)):
                    item = dict(row)
                    item['body'] = json.loads(item['body'])
                    media.append(item)
        return tasks, json.loads(stop[0]) if stop else None, media
    finally:
        db.close()


def fault_class(fault):
    """Routing diagnosis only. An existing STOP remains authoritative."""
    if not fault:
        return None
    code = str(fault.get('code') or fault.get('reason') or 'unknown')
    if any(x in code.upper() for x in ('ACCESS_STOP', 'CAPTCHA', 'SECURITY', 'LOGIN', 'RATE_LIMIT')):
        return 'account_or_access_requires_original_recovery'
    if any(x in code.upper() for x in ('MEDIA', 'ASSET', 'CAPTURE')):
        return 'media_readability_or_format'
    if any(x in code.upper() for x in ('PAGINATION', 'LOADING', 'SCROLL')):
        return 'page_progress_diagnosis'
    return 'unclassified_preserve_stop_and_fault'


def scan(project, task_ids, decisions=None):
    tasks, stop, recorded_media = read_selected(project, task_ids)
    candidates, sources, media, task_rows, gaps = {}, {}, {}, [], []
    decoded = {}

    def candidate(nid):
        return candidates.setdefault(nid, {'note_id': nid, 'title_versions': [], 'sources': [],
                                          'task_ids': [], 'decision': 'unassessed'})

    def media_item(nid, key, kind, ref, body):
        ident = (nid, key)
        value = media.setdefault(ident, {'note_id': nid, 'media_key': key, 'kind': kind,
                                         'sources': [], 'observations': [], 'recorded_state': 'uninspected'})
        if ref not in value['sources']:
            value['sources'].append(ref)
        if body not in value['observations']:
            value['observations'].append(body)

    for t in tasks:
        d = t['payload']; b = d.get('bridge', {}); stage = d.get('stage', t['kind'])
        target = d.get('target', t['entity_key'])
        hashes = list(dict.fromkeys(b.get('hashes', [])))
        reviewed = set(b.get('reviewed_hashes', []))
        fault = b.get('fault') or {}
        tr = {'task_id': t['id'], 'stage': stage, 'target': target, 'sort': d.get('sort'),
              'status': t['status'], 'review_status': t.get('review_status'),
              'turn': t.get('turn', 0), 'priority': t.get('priority', 50),
              'sources': len(hashes), 'unreviewed_source_hashes': [h for h in hashes if h not in reviewed],
              'collection_end_recorded': b.get('collection_end') is True,
              'fault': fault, 'fault_class': fault_class(fault),
              'stalled_count': b.get('stalled_count', 0), 'needs_triage': bool(d.get('needs_triage')),
              'dependencies': d.get('depends_on', []), 'reason': t.get('reason'),
              'source_integrity_gaps': [], 'adapter_supported': t['kind'] == 'native',
              'media_scan_status': 'not_checked',
              'scope_included_recorded': bool(d.get('scope_included') or d.get('title_scope_review'))}
        task_rows.append(tr)
        if not tr['adapter_supported']:
            gaps.append({'task_id': t['id'], 'reason': 'legacy_or_non_bridge_task_use_original_adapter_not_zero_coverage'})
        if stage != 'search' and isinstance(target, str) and re.fullmatch('[a-f0-9]{24}', target) and stage != 'author':
            c = candidate(target); c['task_ids'].append(t['id'])
        for h in hashes:
            source = Path(project) / 'bridge' / 'raw' / (str(h) + '.json')
            if not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h):
                tr['source_integrity_gaps'].append(str(h)); continue
            if h not in decoded:
                try:
                    raw = source.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != h or source.is_symlink():
                        raise ValueError('Source hash mismatch')
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError('Source must be object')
                    decoded[h] = value
                    sources[h] = {'file': str(source.resolve()), 'sha256': h, 'task_ids': []}
                except (OSError, ValueError) as e:
                    tr['source_integrity_gaps'].append(h)
                    gaps.append({'task_id': t['id'], 'source_sha256': h, 'reason': str(e)})
                    continue
            raw = decoded[h]
            if raw.get('comment_media_scan_complete') is True:
                tr['media_scan_status'] = 'recorded_complete_not_revalidated'
            elif raw.get('comment_media_scan_complete') is False:
                tr['media_scan_status'] = 'recorded_incomplete'
            if t['id'] not in sources[h]['task_ids']:
                sources[h]['task_ids'].append(t['id'])
            for i, card in enumerate(raw.get('cards', [])):
                if not isinstance(card, dict): continue
                nid = note_id(card.get('url'))
                if not nid:
                    gaps.append({'task_id': t['id'], 'sha256': h, 'locator': f'cards/{i}', 'reason': 'unconfirmed_note_identity'})
                    continue
                c = candidate(nid)
                words = {k: card[k] for k in rounds.CARD_WORD_FIELDS if k in card}
                version = digest(words)
                ref = {'sha256': h, 'locator': f'cards/{i}', 'task_id': t['id'],
                       'query': target if stage == 'search' else None, 'sort': d.get('sort'),
                       'url': card['url'], 'words_hash': version}
                if ref not in c['sources']: c['sources'].append(ref)
                if not any(x['words_hash'] == version for x in c['title_versions']):
                    c['title_versions'].append({'words_hash': version, 'words': words})
            if stage in {'body', 'note', 'roots', 'replies', 'images', 'video'}:
                actual = note_id(raw.get('url') or raw.get('pageUrl'))
                if actual and actual != target:
                    gaps.append({'task_id': t['id'], 'sha256': h, 'reason': 'source_target_mismatch'})
                    tr['source_integrity_gaps'].append(h)
                    continue
                if stage in {'body', 'note'} and actual == target:
                    c = candidate(target)
                    # Top-level uid is the collector account, not the author.
                    words = {k: raw[k] for k in ('title', 'body', 'authors', 'author', 'authorId', 'authorHrefRaw') if k in raw}
                    version = digest(words)
                    ref = {'sha256': h, 'locator': 'body', 'task_id': t['id'], 'query': None,
                           'sort': None, 'url': raw.get('url') or raw.get('pageUrl'), 'words_hash': version}
                    if ref not in c['sources']: c['sources'].append(ref)
                    if not any(v['words_hash'] == version for v in c['title_versions']):
                        c['title_versions'].append({'words_hash': version, 'words': words, 'version_kind': 'body'})
                for i, row in enumerate(raw.get('results', [])):
                    cid = row.get('commentId')
                    for n, img in enumerate(row.get('images', [])):
                        key = f'comment:{cid or "uncertain:" + h + ":" + str(i)}:{n}'
                        media_item(target, key, 'comment_image', {'sha256': h, 'locator': f'results/{i}/images/{n}'},
                                   {'comment_id': cid, 'identity_uncertain': not bool(cid), 'media': img})
                for i, m in enumerate(raw.get('media_captures', [])):
                    pos = m.get('position')
                    key = f'body:{pos}' if type(pos) is int and pos > 0 else f'uncertain:{h}:{i}'
                    media_item(target, key, m.get('kind', 'unknown'), {'sha256': h, 'locator': f'media_captures/{i}'},
                               {k: m[k] for k in ('file', 'sha256', 'position', 'count', 'kind') if k in m})
                # Undownloaded body assets are distinct from verified carousel positions.
                for i, m in enumerate(raw.get('media', [])):
                    media_item(target, 'discovered:' + digest(m), m.get('kind', m.get('type', 'unknown')) if isinstance(m, dict) else 'unknown',
                               {'sha256': h, 'locator': f'media/{i}'}, {'media': m, 'position_mapping_unconfirmed': True})
    for m in recorded_media:
        key = (m['note_id'], m['media_key'])
        if key not in media:
            media_item(*key, m['kind'], {'recorded_inventory': True}, m['body'])
        media[key]['recorded_state'] = m['state']
        media[key]['reading_not_revalidated'] = True
    apply_decisions(candidates, decisions or [], sources)
    counts = Counter(c['decision'] for c in candidates.values())
    rows = list(media.values())
    summary = {'selected_tasks': len(tasks), 'unique_note_candidates': len(candidates),
               'candidate_decisions': dict(counts), 'verified_source_files': len(sources),
               'unreviewed_source_bindings': sum(len(t['unreviewed_source_hashes']) for t in task_rows),
               'held_tasks': sum(bool(t['fault'].get('hold')) or t['status'] == 'blocked' for t in task_rows),
               'search_scopes': sum(t['stage'] == 'search' for t in task_rows),
               'search_end_recorded': sum(t['stage'] == 'search' and t['collection_end_recorded'] for t in task_rows),
               'media_records': len(rows), 'media_position_mapping_unconfirmed': sum(m['media_key'].startswith('discovered:') for m in rows),
               'integrity_gaps': len(gaps), 'not_patient_counts': True}
    summary['unsupported_adapter_tasks'] = sum(not t['adapter_supported'] for t in task_rows)
    return {'schema': 'xhs-research-desk-v1', 'created_at': datetime.now(timezone.utc).isoformat(),
            'project': str(Path(project).resolve()), 'task_ids': task_ids, 'project_stop_recorded': stop,
            'summary': summary, 'candidates': list(candidates.values()), 'tasks': task_rows,
            'media': rows, 'sources': sources, 'gaps': gaps, 'read_only': True,
            'completion_not_revalidated': True, 'network_permission': False,
            'media_absence_not_inferred': True}


def apply_decisions(candidates, decisions, sources):
    """Explicit analyst screening only, source- and word-version-bound."""
    if not isinstance(decisions, list): raise ValueError('Decisions must be a list')
    for d in decisions:
        if d.get('decision') not in {'include', 'exclude', 'defer'} or not d.get('reason') or not d.get('reviewer'):
            raise ValueError('Explicit decision, reviewer and reason required')
        c = candidates.get(d.get('note_id'))
        if not c: raise ValueError('Unknown decision candidate')
        refs = d.get('reviewed_sources', [])
        if not refs: raise ValueError('Decision requires exact reviewed source positions')
        bound = []
        for ref in refs:
            match = next((r for r in c['sources'] if r['sha256'] == ref.get('sha256') and r['locator'] == ref.get('locator')), None)
            if not match or match['sha256'] not in sources: raise ValueError('Decision source is not this candidate')
            bound.append(match['words_hash'])
        c['decision'] = d['decision']; c['decision_record'] = d
        c['decision_words_hashes'] = sorted(set(bound))
        c['unassessed_title_versions'] = [v['words_hash'] for v in c['title_versions'] if v['words_hash'] not in bound]
        # A past exclusion never silently discards subsequently changed title/identity.
        if c['unassessed_title_versions']:
            c['decision'] = 'needs_reassessment'


def triage_pack(inventory, limit=40, after=None):
    if not 1 <= limit <= 200: raise ValueError('Limit must be 1..200')
    candidates = sorted((c for c in inventory['candidates'] if c['decision'] in {'unassessed', 'needs_reassessment'}), key=lambda c: c['note_id'])
    if after: candidates = [c for c in candidates if c['note_id'] > after]
    chosen = candidates[:limit]
    items = []
    for c in chosen:
        versions = []
        for v in c['title_versions']:
            if c['decision'] == 'needs_reassessment' and v['words_hash'] not in c['unassessed_title_versions']: continue
            refs = [r for r in c['sources'] if r['words_hash'] == v['words_hash']]
            versions.append(dict(v, source=refs[0] if refs else None, additional_source_count=max(0, len(refs)-1)))
        items.append({'note_id': c['note_id'], 'versions': versions, 'task_ids': c['task_ids'],
                      'previous_decision': c.get('decision_record'), 'empty_title_is_not_exclusion': not bool(versions),
                      'next_action_if_no_source': 'inspect_original_scope_registration' if not versions else None})
    return {'items': items, 'next_after': chosen[-1]['note_id'] if chosen else after,
            'more': len(candidates) > limit, 'full_sources_in_inventory': True,
            'requires_codex_reading': True, 'automatically_reviewed': False}


def recommend(inventory, limit=10, preferred=None):
    """Bounded suggestions, not dispatch: original worker guards remain required."""
    if not 1 <= limit <= 100: raise ValueError('Limit must be 1..100')
    all_tasks = inventory['tasks']; tasks = all_tasks
    by_id = {t['task_id']: t for t in all_tasks}
    if preferred is not None:
        if any(x not in by_id for x in preferred): raise ValueError('Preferred task outside selected scope')
        tasks = [by_id[x] for x in preferred]
    pending = [t for t in tasks if t['status'] not in {'complete', 'excluded'}]
    reviews = [t for t in tasks if t['unreviewed_source_hashes'] and not t['source_integrity_gaps']]
    held = [t for t in pending if t['fault'].get('hold') or t['status'] == 'blocked' or t['source_integrity_gaps']]
    ready = [t for t in pending if t not in held and t['adapter_supported'] and not t['needs_triage'] and not t['unreviewed_source_hashes'] and not t['collection_end_recorded']]
    ended = [t for t in pending if t['collection_end_recorded'] and t not in held and not t['unreviewed_source_hashes']]
    complete = {t['task_id'] for t in all_tasks if t['status'] == 'complete'}
    ready = [t for t in ready if isinstance(t['dependencies'], list) and set(t['dependencies']) <= complete]
    # Existing roots must finish before a separately registered replies task.
    ready = [t for t in ready if t['stage'] != 'replies' or not any(r['target'] == t['target'] and r['stage'] == 'roots' and r['status'] != 'complete' for r in all_tasks)]
    unassessed = inventory['summary']['candidate_decisions'].get('unassessed', 0) + inventory['summary']['candidate_decisions'].get('needs_reassessment', 0)
    candidates = {c['note_id']: c for c in inventory['candidates']}
    missing_downstream = [c['note_id'] for c in candidates.values() if c['decision'] == 'include' and
                          not any(t['target'] == c['note_id'] and t['stage'] in {'body', 'note', 'roots', 'replies'} for t in all_tasks)]
    # New body acquisition requires an explicit inclusion; existing downstream survives.
    ready = [t for t in ready if t['stage'] not in {'body', 'note'} or t['scope_included_recorded'] or candidates.get(t['target'], {}).get('decision') == 'include']
    downstream = [t for t in ready if t['stage'] != 'search']
    if preferred is None:
        # Least serviced first; deterministic and no sentiment-based prioritization.
        downstream.sort(key=lambda t: (t['turn'], t['priority'], t['task_id']))
    choices = []
    if reviews:
        choices = [{'action': 'review', 'task_id': t['task_id'], 'stage': t['stage'], 'target': t['target']} for t in reviews[:limit]]
    elif ended:
        choices = [{'action': 'verify_end_receipt', 'task_id': t['task_id'], 'stage': t['stage'],
                    'completion_not_assumed': True} for t in ended[:limit]]
    elif downstream:
        choices = [{'action': 'media' if t['stage'] in {'images', 'video'} else 'collect',
                    'task_id': t['task_id'], 'stage': t['stage'], 'target': t['target']} for t in downstream[:limit]]
    elif missing_downstream and preferred is None:
        choices = [{'action': 'register_scope', 'note_ids': missing_downstream[:limit],
                    'remaining': len(missing_downstream), 'use_original_project_interface': True}]
    elif unassessed:
        choices = [{'action': 'triage', 'remaining_candidates': unassessed}]
    elif ready:
        if preferred is None: ready.sort(key=lambda t: (t['turn'], t['priority'], t['task_id']))
        choices = [{'action': 'collect', 'task_id': t['task_id'], 'stage': t['stage'], 'target': t['target']} for t in ready[:limit]]
    else:
        choices = [{'action': 'inspect_gaps', 'remaining_tasks': len(pending), 'held_tasks': len(held)}]
    if inventory['project_stop_recorded']:
        choices = [c for c in choices if c['action'] in {'review', 'triage', 'register_scope', 'verify_end_receipt', 'inspect_gaps'}] or [{'action': 'inspect_gaps', 'reason': 'project_stop_recorded'}]
    return {'choices': choices, 'network_authorized': False, 'recheck_original_worker_guards': True,
            'held_task_ids': [t['task_id'] for t in held], 'snapshot_at': inventory['created_at'],
            'scope_processed': False, 'requires_user_input': False}


def metrics(manifest):
    """Aggregate explicit measured intervals; missing phases stay unknown."""
    groups, seen = {}, set()
    for record in manifest['measurements']:
        phase = record['phase']; file = Path(record['file']); raw = file.read_bytes()
        if phase not in {'collection', 'review', 'recovery', 'export'}:
            raise ValueError('Unknown measured phase')
        h = hashlib.sha256(raw).hexdigest()
        if h != record['sha256']: raise ValueError('Metrics source changed')
        # File paths alone do not identify distinct executions; require their run IDs.
        ident = record['measurement_id']
        if ident in seen: raise ValueError('Duplicate measurement ID')
        seen.add(ident)
        data = json.loads(raw)
        values = record.get('fields', {'seconds': 'wall_seconds', 'browser_calls': 'metrics.calls', 'units': 'processed_units'})
        g = groups.setdefault(phase, {'measurements': 0, 'seconds': 0, 'browser_calls': 0, 'units': 0, 'missing': []})
        g['measurements'] += 1
        for key in ('seconds', 'browser_calls', 'units'):
            value = data
            for part in values.get(key, '').split('.'):
                value = value.get(part) if isinstance(value, dict) else None
            if value is None:
                g['missing'].append({'measurement_id': ident, 'field': key}); continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0: raise ValueError('Invalid measured value')
            g[key] += value
    return {'phases': groups, 'unmeasured_phases': [x for x in ('collection', 'review', 'recovery', 'export') if x not in groups],
            'end_to_end_seconds': None, 'speedup_multiplier': None,
            'note': 'Phase totals may overlap; not summed into wall time. No inferred clinical evidence count.'}


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('scan'); s.add_argument('--project', required=True); s.add_argument('--task-list', required=True)
    s.add_argument('--decisions'); s.add_argument('--output', required=True)
    for name in ('triage-pack', 'next'):
        s = sub.add_parser(name); s.add_argument('--inventory', required=True); s.add_argument('--limit', type=int, default=40 if name == 'triage-pack' else 10)
        s.add_argument('--output', required=True)
        if name == 'triage-pack': s.add_argument('--after')
        else: s.add_argument('--preferred')
    s = sub.add_parser('metrics'); s.add_argument('--manifest', required=True); s.add_argument('--output', required=True)
    a = parser.parse_args()
    if Path(a.output).exists(): parser.error('Output exists; use a new versioned path')
    if a.command == 'scan':
        result = scan(a.project, load(a.task_list), load(a.decisions) if a.decisions else None)
    elif a.command == 'triage-pack': result = triage_pack(load(a.inventory), a.limit, a.after)
    elif a.command == 'next': result = recommend(load(a.inventory), a.limit, load(a.preferred) if a.preferred else None)
    else: result = metrics(load(a.manifest))
    write_new(a.output, result)
    short = result.get('summary', result.get('choices', {'items': len(result.get('items', []))}))
    if a.command == 'metrics':
        short = {'phases': result['phases'], 'unmeasured_phases': result['unmeasured_phases']}
    print(json.dumps({'output': str(Path(a.output).resolve()), 'sha256': hashlib.sha256(Path(a.output).read_bytes()).hexdigest(),
                      'summary': short,
                      'network_operations': 0, 'original_project_mutated': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
