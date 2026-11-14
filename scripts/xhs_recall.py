#!/usr/bin/env python3
"""Read-only, source-bound recall across explicitly selected research projects."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

from xhs_paths import external_path
from xhs_research_desk import note_id

FORMAT = 'xhs-recall-v1'


def digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def normalized(text):
    """Keep offsets into the original displayed text, including expanding folds."""
    chars, offsets = [], []
    for i, char in enumerate(text):
        folded = unicodedata.normalize('NFKC', char).casefold()
        chars.extend(folded)
        offsets.extend([(i, i + 1)] * len(folded))
    return ''.join(chars), offsets


def compile_terms(terms):
    if not isinstance(terms, list) or not terms:
        raise ValueError('Nonempty external terms required')
    compiled, ids = [], set()
    for term in terms:
        if not isinstance(term, dict) or not isinstance(term.get('id'), str) or not term['id'] or term['id'] in ids:
            raise ValueError('Unique nonempty term IDs required')
        if not isinstance(term.get('text'), str) or not term['text'].strip():
            raise ValueError('Nonempty term text required')
        ids.add(term['id'])
        word = normalized(term['text'])[0]
        mode = term.get('mode', 'literal')
        if mode == 'initials':
            word = re.sub(r'[\s.·]+', '', word)
            if not re.fullmatch('[a-z]{2,12}', word):
                raise ValueError('Initials must contain 2..12 Latin letters')
            pattern = r'(?<![a-z0-9])' + r'[\s.·]*'.join(word) + r'(?![a-z0-9])'
        elif mode == 'literal':
            pattern = re.escape(word)
        else:
            raise ValueError('Unsupported term mode')
        ambiguity = term.get('ambiguities', [])
        if not isinstance(ambiguity, list) or any(not isinstance(x, str) for x in ambiguity):
            raise ValueError('Ambiguities must be strings')
        compiled.append((term, re.compile(pattern)))
    return compiled


def matches(text, terms):
    folded, offsets = normalized(text)
    result = []
    for term, pattern in terms:
        for match in pattern.finditer(folded):
            start, end = offsets[match.start()][0], offsets[match.end() - 1][1]
            result.append({'term_id': term['id'], 'start': start, 'end': end,
                           'original': text[start:end], 'mode': term.get('mode', 'literal'),
                           'ambiguities': term.get('ambiguities', []), 'identity_confirmed': False})
    return result


def regular_within(root, value):
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or any(p.is_symlink() for p in (path, *path.parents) if p != root):
        raise ValueError('Source outside selected project or symlink')
    if not path.is_file():
        raise ValueError('Source missing')
    return path


def load_config(path):
    if Path(path).is_symlink():
        raise ValueError('Regular external config required')
    path = external_path(path)
    if path.is_symlink():
        raise ValueError('Regular external config required')
    cfg = json.loads(path.read_text())
    if cfg.get('format') != FORMAT or not isinstance(cfg.get('projects'), list):
        raise ValueError('Expected recall config and explicit project list')
    compile_terms(cfg.get('terms'))
    if not cfg['projects'] and not cfg.get('references') and not cfg.get('media_reviews'):
        raise ValueError('Select at least one project or reference')
    return cfg


def author_id(row):
    user = row.get('user_info') or row.get('user') or {}
    uid = row.get('userId') or row.get('authorId') or user.get('user_id')
    href = row.get('authorHrefRaw') or ''
    match = re.search(r'/user/profile/([a-f0-9]{24})(?:[/?]|$)', href)
    return uid or (match[1] if match else None)


def text_fields(row, fields):
    return '\n'.join(row[k] for k in fields if isinstance(row.get(k), str) and row[k])


def extract(record, ref):
    """Extract observed text only; navigation metadata is never patient wording."""
    if not isinstance(record, dict):
        raise ValueError('Expected source object')
    rows = []
    op = record.get('operation')
    url = record.get('url') or record.get('pageUrl')
    nid = record.get('note_id') or note_id(url)
    if record.get('note_id') and note_id(url) and record['note_id'] != note_id(url):
        raise ValueError('Snapshot note identity mismatch')

    def add(row, kind, loc, text, note=nid, link=url, cid=None, parent=None, reply=None, reply_basis=None):
        if not isinstance(text, str):
            raise ValueError('Observed text must be a string')
        if not text:
            return
        user = row.get('user_info') or row.get('user') or {}
        rows.append({'kind': kind, 'note_id': note, 'comment_id': cid,
                     'parent_comment_id': parent, 'reply_to_comment_id': reply,
                     'reply_to_identity': reply_basis or row.get('replyToIdentity'),
                     'reply_to_label': row.get('reply_to'), 'author_id': author_id(row),
                     'author_label': row.get('author') or user.get('nickname'),
                     'ai_agent': user.get('ai_agent', row.get('ai_agent')),
                     'published': row.get('published_at') or row.get('publishedAt') or row.get('create_time') or row.get('time'),
                     'url': link, 'text': text, 'source': dict(ref, locator=loc)})

    def comments(items, api=False, prefix='results', parent=None, nested=False):
        for i, row in enumerate(items):
            if not isinstance(row, dict):
                raise ValueError('Malformed comment row')
            cid = row.get('id') if api else row.get('commentId') or row.get('comment_id')
            root = parent if api else row.get('parentCommentId')
            target = (row.get('target_comment') or {}).get('id') if api else row.get('replyToCommentId')
            is_reply = nested if api else bool(row.get('is_reply'))
            loc = f'{prefix}/{i}'
            add(row, 'reply' if is_reply else 'root_comment', loc,
                row.get('content', '') if api else row.get('text', ''), cid=cid, parent=root, reply=target,
                reply_basis='api_target' if api and target else None)
            if api:
                comments(row.get('sub_comments') or [], True, loc + '/sub_comments', cid, True)

    data = record.get('data')
    if op == 'comments' and isinstance(data, dict):
        comments(data.get('comments') or [], True, 'data/comments')
    elif op == 'browser_comments' and isinstance(data, list):
        comments(data, prefix='data')
    elif op in {'note', 'search', 'author_notes'} and isinstance(data, dict):
        items = data.get('notes' if op == 'author_notes' else 'items') or []
        for i, item in enumerate(items):
            row = item.get('note_card') or item
            current = row.get('note_id') or item.get('id') or item.get('note_id')
            if op == 'note' and current != nid:
                raise ValueError('Snapshot note identity mismatch')
            add(row, 'note' if op == 'note' else 'card', f'data/{"notes" if op == "author_notes" else "items"}/{i}',
                text_fields(row, ('title', 'display_title', 'desc')), note=current, link=url if op == 'note' else None)
    elif any(k in record for k in ('body', 'cards', 'results', 'media', 'deepCoverage')):
        # Top-level uid is the collector account; only explicit author fields count.
        if record.get('body') or record.get('title'):
            add(record, 'note', 'body', text_fields(record, ('title', 'body')))
        comments(record.get('results') or [])
        for i, card in enumerate(record.get('cards') or []):
            add(card, 'card', f'cards/{i}', text_fields(card, ('title', 'text', 'description', 'desc', 'content', 'excerpt')),
                note=note_id(card.get('url')), link=card.get('url'))
    else:
        raise ValueError('Unsupported source schema')
    return rows


def project_records(selection):
    root = external_path(selection['path'])
    database = regular_within(root, 'project.sqlite3')
    db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    records, gaps, catalog, coverage = [], [], [], []
    try:
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'tasks', 'snapshots'} <= tables:
            raise ValueError('Unsupported research database')
        all_tasks = selection.get('all_tasks') is True
        wanted = selection.get('task_ids')
        if all_tasks == (wanted is not None) or (not all_tasks and (not isinstance(wanted, list) or not wanted)):
            raise ValueError('Select all_tasks=true or a nonempty task_ids list')
        tasks = [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY id')]
        if not all_tasks:
            if len(set(wanted)) != len(wanted) or set(wanted) - {t['id'] for t in tasks}:
                raise ValueError('Unknown or duplicate selected task')
            tasks = [t for t in tasks if t['id'] in wanted]
        hashes, notes, hash_tasks = set(), set(), defaultdict(list)
        expected_notes, legacy_scopes = defaultdict(set), []
        media_notes, media_ids = set(), set()
        for task in tasks:
            payload = json.loads(task['payload']); bridge = payload.get('bridge', {})
            stage = payload.get('stage', task['kind'])
            target = payload.get('target') or payload.get('note_id') or task.get('entity_key')
            if stage not in {'search', 'author'} and target:
                notes.add(target)
            if task['kind'] == 'media':
                media_ids.add(payload.get('media_id') or task['entity_key'])
            elif stage in {'images', 'video'}:
                media_notes.add(target)
            for h in bridge.get('hashes', []):
                hashes.add(h); hash_tasks[h].append(task['id'])
                if stage not in {'search', 'author'} and target:
                    expected_notes[h].add(target)
            if 'pages' in tables:
                for row in db.execute('SELECT snapshot_id FROM pages WHERE task_id=?', (task['id'],)):
                    hashes.add(row[0]); hash_tasks[row[0]].append(task['id'])
                    if stage not in {'search', 'author'} and target:
                        expected_notes[row[0]].add(target)
            legacy_op = {'note': 'note', 'comments': 'comments', 'browser': 'browser_comments'}.get(task['kind'])
            if legacy_op and target:
                legacy_scopes.append((task['id'], target, legacy_op))
            coverage.append({'project': str(root), 'task_id': task['id'], 'stage': stage,
                             'query': payload.get('query', target) if stage == 'search' else None,
                             'sort': payload.get('sort'), 'target': target,
                             'status_recorded': task['status'], 'review_status_recorded': task.get('review_status'),
                             'end_recorded': bridge.get('collection_end'), 'reason': task.get('reason'),
                             'fault_recorded': bridge.get('fault'),
                             'unreviewed_source_count': len(set(bridge.get('hashes', [])) - set(bridge.get('reviewed_hashes', []))),
                             'observed_note_count': len(set(bridge.get('observed_note_ids', []))),
                             'completion_not_revalidated': True})
        if legacy_scopes and {'documents', 'document_sources'} <= tables:
            for tid, n, op in legacy_scopes:
                for row in db.execute('SELECT DISTINCT s.id FROM document_sources ds JOIN documents d ON d.id=ds.document_id JOIN snapshots s ON s.id=ds.snapshot_id WHERE d.note_id=? AND s.operation=?', (n, op)):
                    hashes.add(row[0]); hash_tasks[row[0]].append(tid); expected_notes[row[0]].add(n)
        refs = {r['id']: dict(r) for r in db.execute('SELECT * FROM snapshots ORDER BY id') if all_tasks or r['id'] in hashes}
        for h in hashes - set(refs):
            refs[h] = {'id': h, 'path': 'bridge/raw/' + h + '.json', 'captured_at': None}
        for sid, source in sorted(refs.items()):
            ref = {'project': str(root), 'sha256': sid, 'file': source['path'],
                   'captured_at': source.get('captured_at'), 'task_ids': sorted(hash_tasks[sid])}
            try:
                if not re.fullmatch('[a-f0-9]{64}', sid):
                    raise ValueError('Invalid source SHA256')
                path = regular_within(root, source['path'])
                raw = path.read_bytes()
                if digest(raw) != sid:
                    raise ValueError('Source checksum mismatch')
                ref['file'] = str(path)
                value = json.loads(raw)
                if expected_notes[sid]:
                    observed = value.get('note_id') or note_id(value.get('url') or value.get('pageUrl'))
                    if expected_notes[sid] != {observed}:
                        raise ValueError('Source does not match selected task note identity')
                records.extend(extract(value, ref))
                catalog.append(dict(ref, status='verified'))
            except (ValueError, OSError, TypeError, KeyError) as exc:
                gaps.append(dict(ref, reason=str(exc)))
                catalog.append(dict(ref, status='gap', reason=str(exc)))
        if 'documents' in tables:
            if 'document_sources' in tables:
                for row in db.execute("SELECT d.id,d.note_id FROM documents d LEFT JOIN document_sources s ON d.id=s.document_id WHERE d.kind!='media' GROUP BY d.id HAVING COUNT(s.snapshot_id)=0"):
                    if all_tasks or row['note_id'] in notes:
                        gaps.append({'project': str(root), 'document_id': row['id'], 'reason': 'indexed_text_has_no_immutable_source'})
            for row in db.execute("SELECT * FROM documents WHERE kind='media' ORDER BY id"):
                item = json.loads(row['metadata'])
                if not all_tasks and row['note_id'] not in media_notes and item.get('media_id') not in media_ids:
                    continue
                if item.get('review_status') != 'reviewed':
                    gaps.append({'project': str(root), 'document_id': row['id'], 'reason': 'media_text_not_reviewed'})
                    continue
                try:
                    for field in ('file', 'transcript', 'manifest', 'source_path'):
                        if item.get(field):
                            regular_within(root, item[field])
                    from xhs_media import review_binding_gaps
                    if item.get('kind') not in {'image', 'subtitle', 'video'} or review_binding_gaps([item]):
                        raise ValueError('Media reading binding invalid')
                    body = item.get('observation', '')
                    if item.get('transcript'):
                        raw = regular_within(root, item['transcript']).read_bytes()
                        if digest(raw) != item.get('transcript_sha256'):
                            raise ValueError('Transcript checksum mismatch')
                        parsed = json.loads(raw)
                        segments = parsed.get('segments', []) if isinstance(parsed, dict) else parsed
                        body += '\n' + '\n'.join(str(s.get('text', '')) for s in segments)
                    if body != row['text']:
                        raise ValueError('Indexed media text differs from bound reading')
                    artifacts = [{'file': str(regular_within(root, item[field])), 'sha256': item[sha], 'role': field}
                                 for field, sha in (('file', 'sha256'), ('transcript', 'transcript_sha256')) if item.get(field) and item.get(sha)]
                    ref = {'project': str(root), 'document_id': row['id'], 'row_sha256': digest(dict(row)),
                           'hash_scope': 'sqlite_row', 'artifact_sources': artifacts,
                           'file': str(database), 'locator': 'documents/' + row['id'], 'captured_at': row['captured_at']}
                    records.append({'kind': 'media_observation', 'note_id': row['note_id'], 'comment_id': item.get('comment_id'),
                                    'text': body, 'source': ref, 'url': None, 'media_locator': item.get('locator'),
                                    'reading_limits': item.get('reading_limits', []), 'verbatim_patient_text_not_assumed': True})
                    catalog.append(dict(ref, status='verified_reading'))
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    gaps.append({'project': str(root), 'document_id': row['id'], 'reason': str(exc)})
        return records, catalog, coverage, gaps
    finally:
        db.close()


def collect(cfg):
    records, catalog, coverage, gaps = [], [], [], []
    roots = set()
    for selection in cfg['projects']:
        root = str(external_path(selection['path']))
        if root in roots:
            raise ValueError('Duplicate selected project')
        roots.add(root)
        r, s, c, g = project_records(selection)
        records.extend(r); catalog.extend(s); coverage.extend(c); gaps.extend(g)
    # Explicit legacy notes are discovery aids, never reclassified as original posts.
    for entry in cfg.get('references', []):
        if Path(entry['path']).is_symlink():
            raise ValueError('Regular external reference required')
        path = external_path(entry['path'])
        raw = path.read_bytes()
        if path.is_symlink() or digest(raw) != entry['sha256']:
            raise ValueError('Explicit reference checksum mismatch')
        ref = {'file': str(path), 'sha256': entry['sha256'], 'locator': 'text', 'captured_at': None}
        records.append({'kind': 'reference_text', 'text': raw.decode('utf-8'), 'source': ref,
                        'note_id': None, 'comment_id': None, 'url': None, 'original_source_recheck_required': True})
        catalog.append(dict(ref, status='reference_only'))
    for entry in cfg.get('media_reviews', []):
        root = external_path(entry['project'])
        ref = {'project': str(root), 'file': entry['file'], 'sha256': entry['sha256'], 'locator': 'observation'}
        try:
            path = regular_within(root, entry['file'])
            raw = path.read_bytes()
            if digest(raw) != entry['sha256']:
                raise ValueError('Media review checksum mismatch')
            value = json.loads(raw)
            regular_within(root, value['file'])
            if value.get('capture_metadata'):
                meta = json.loads(regular_within(root, value['capture_metadata']).read_text())
                if meta.get('file'):
                    regular_within(root, meta['file'])
            from xhs_media_report import observation
            item = observation({'file': str(path), 'sha256': entry['sha256']})
            ref['file'] = str(path)
            records.append({'kind': 'media_observation', 'text': item['observation'], 'source': ref,
                            'note_id': item['note_id'], 'comment_id': None, 'url': None,
                            'media_locator': 'body_image/' + str(item['position']),
                            'verbatim_patient_text_not_assumed': True})
            catalog.append(dict(ref, status='verified_reading'))
        except (ValueError, OSError, TypeError, KeyError) as exc:
            gaps.append(dict(ref, reason=str(exc)))
            catalog.append(dict(ref, status='gap', reason=str(exc)))
    groups = {}
    for row in records:
        source = row['source']
        identity = row.get('comment_id') or (row.get('note_id') if row['kind'] in {'note', 'card'} else None)
        semantic = {k: row.get(k) for k in ('kind', 'note_id', 'comment_id', 'text', 'author_id', 'author_label',
                    'ai_agent', 'parent_comment_id', 'reply_to_comment_id', 'reply_to_identity', 'reply_to_label', 'media_locator')}
        # Missing note/real identity must never merge across observations or nicknames.
        if not identity or not row.get('note_id'):
            semantic['local_source'] = source
        key = digest(semantic)
        if key not in groups:
            groups[key] = dict(row, id=key, sources=[], observations=[])
            del groups[key]['source']
        g = groups[key]
        if source not in g['sources']:
            g['sources'].append(source)
            g['observations'].append({'source': source, 'url': row.get('url'), 'published': row.get('published')})
    return {'records': sorted(groups.values(), key=lambda r: r['id']), 'catalog': catalog, 'coverage': coverage,
            'gaps': gaps, 'scope_hash': digest([cfg, catalog, coverage, gaps])}


def related(hit, all_rows):
    contexts = {}
    for name, field in [('parent', 'parent_comment_id'), ('direct_reply', 'reply_to_comment_id')]:
        cid = hit.get(field)
        if not cid:
            contexts[name] = {'status': 'identity_not_observed', 'versions': []}
            continue
        found = [r for r in all_rows if r['kind'] in {'root_comment', 'reply'} and r.get('note_id') == hit.get('note_id') and r.get('comment_id') == cid]
        here = {s.get('sha256') or s.get('row_sha256') for s in hit['sources']}
        contexts[name] = {'status': 'observed_versions' if found else 'body_missing', 'comment_id': cid,
                         'versions': [{'id': r['id'], 'kind': r['kind'], 'text': r['text'], 'author_id': r.get('author_id'),
                                       'sources': r['sources'], 'same_source': bool(here & {s['sha256'] for s in r['sources']})} for r in found],
                         'unique_identity_not_assumed': len({r.get('author_id') for r in found}) > 1}
        if name == 'direct_reply':
            basis = hit.get('reply_to_identity')
            contexts[name]['relationship_basis'] = basis
            contexts[name]['direct_target_confirmed'] = basis in {'observed_id', 'api_target'}
            if basis == 'thread_root':
                contexts[name]['status'] = 'thread_root_fallback_not_direct_reply'
            elif basis not in {'observed_id', 'api_target'}:
                contexts[name]['status'] = 'relationship_basis_unknown'
    return contexts


def thread_rows(rows, note, comment):
    selected = {comment}
    candidates = [r for r in rows if r.get('note_id') == note and r.get('comment_id')]
    if not any(r['comment_id'] == comment for r in candidates):
        raise ValueError('Comment identity not observed in selected scope')
    changed = True
    while changed:
        changed = False
        for row in candidates:
            ids = {row.get(k) for k in ('comment_id', 'parent_comment_id', 'reply_to_comment_id')} - {None, ''}
            if ids & selected and not ids <= selected:
                selected.update(ids); changed = True
    return [r for r in candidates if r['comment_id'] in selected]


def recall(cfg, *, limit=50, after=None, thread=None, coverage_only=False):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('Limit must be 1..200')
    terms = compile_terms(cfg.get('terms'))
    inventory = collect(cfg)
    scope = digest([inventory['scope_hash'], thread, coverage_only])
    last = ''
    if after:
        parts = after.split(':')
        if len(parts) != 2 or parts[0] != scope:
            raise ValueError('Recall scope changed; restart pagination')
        last = parts[1]
    found = []
    if not coverage_only:
        candidates = thread_rows(inventory['records'], *thread) if thread else inventory['records']
        for row in candidates:
            hits = matches(row['text'], terms)
            if thread or hits:
                found.append(dict(row, matches=hits, attribution='unassessed'))
    if last and not any(r['id'] == last for r in found):
        raise ValueError('Unknown pagination position')
    remaining = [r for r in found if r['id'] > last]
    chosen = remaining[:limit]
    for row in chosen:
        row['context'] = related(row, inventory['records'])
    stages = Counter(t['stage'] for t in inventory['coverage'])
    more = len(remaining) > len(chosen)
    return {'schema': FORMAT, 'scope_hash': scope, 'items': chosen, 'total_matches': len(found),
            'more': more, 'next_after': scope + ':' + chosen[-1]['id'] if more else None,
            'coverage': {'tasks': inventory['coverage'], 'stages': dict(stages),
                         'source_records': len(inventory['catalog']), 'text_versions': len(inventory['records']),
                         'gaps': inventory['gaps'], 'sources_complete': not inventory['gaps'],
                         'completion_not_revalidated': True, 'no_platform_recall_estimate': True},
            'read_only': True, 'network_permission': False, 'matching_is_not_reading_or_identity': True,
            'counts_are_not_patients': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['search', 'thread', 'coverage'])
    parser.add_argument('--config', required=True)
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--after')
    parser.add_argument('--note-id'); parser.add_argument('--comment-id')
    args = parser.parse_args()
    if args.command == 'thread' and not (args.note_id and args.comment_id):
        parser.error('thread needs observed --note-id and --comment-id')
    try:
        result = recall(load_config(args.config), limit=args.limit, after=args.after,
                        thread=(args.note_id, args.comment_id) if args.command == 'thread' else None,
                        coverage_only=args.command == 'coverage')
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
