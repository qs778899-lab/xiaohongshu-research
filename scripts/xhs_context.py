#!/usr/bin/env python3
"""Read every immutable observation of a document, without merging its claims."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def observations(record, note_id, comment_id):
    if record.get('note_id') != note_id:
        return []
    op, data = record.get('operation'), record.get('data')
    rows = []
    if op == 'comments' and isinstance(data, dict):
        for root in data.get('comments') or []:
            rows.append((root, 'root_comment', None, 'api'))
            for reply in root.get('sub_comments') or []:
                rows.append((reply, 'reply', root, 'api'))
    elif op == 'browser_comments' and isinstance(data, list):
        roots = {}
        for row in data:
            rid = row.get('commentId') or row.get('comment_id')
            if rid and not row.get('is_reply'):
                roots.setdefault(rid, []).append(row)
        for row in data:
            candidates = roots.get(row.get('parentCommentId'), []) if row.get('parentCommentId') else []
            parent = candidates[0] if len(candidates) == 1 else None
            rows.append((row, 'reply' if row.get('is_reply') else 'root_comment', parent, 'browser'))
    elif op == 'note' and comment_id is None and isinstance(data, dict):
        for item in data.get('items') or []:
            n = item.get('note_card') or {}
            if (n.get('note_id') or item.get('id')) == note_id:
                rows.append((n, 'note', None, 'api'))
    result = []
    for row, kind, parent, transport in rows:
        rid = row.get('id') if transport == 'api' else row.get('commentId') or row.get('comment_id')
        if comment_id is not None and rid != comment_id:
            continue
        user = row.get('user_info') or row.get('user') or {}
        target = row.get('target_comment') or {}
        text = row.get('title', '') + '\n' + row.get('desc', '') if kind == 'note' else row.get('content', '') if transport == 'api' else row.get('text', '')
        result.append({
            'transport': transport, 'kind': kind, 'comment_id': rid,
            'text': text, 'user_id': user.get('user_id') if transport == 'api' else row.get('userId'),
            'ai_agent': user.get('ai_agent'),
            'root_comment_id': (parent or {}).get('id') if transport == 'api' else row.get('parentCommentId'),
            'reply_to_comment_id': target.get('id') if transport == 'api' else row.get('replyToCommentId'),
            'reply_to_user_id': (target.get('user_info') or {}).get('user_id'),
            'reply_to_label': row.get('reply_to'),
            'parent_text': (parent or {}).get('content') if transport == 'api' else (parent or {}).get('text'),
            'parent_user_id': ((parent or {}).get('user_info') or {}).get('user_id') if transport == 'api' else (parent or {}).get('userId'),
            'parent_context_status': ('source_nesting' if transport == 'api' else 'unique_observed_root') if parent is not None else 'not_established',
            'missing_fields_are_unknown': True,
        })
    return result


def read_context(project, document_id):
    project = Path(project).expanduser().resolve()
    db = sqlite3.connect((project / 'project.sqlite3').as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        doc = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
        if doc is None:
            raise ValueError('Unknown document ID')
        if doc['identity_kind'] != 'platform_id':
            raise ValueError('Snapshot-local identity requires direct source review; do not merge it')
        nid = doc['note_id']
        cid = None if doc['kind'] == 'note' else document_id.removeprefix('comment:' + nid + ':')
        if cid == document_id:
            raise ValueError('Unsupported document ID')
        refs = db.execute('SELECT DISTINCT s.id,s.path,s.operation,s.captured_at FROM document_sources ds JOIN snapshots s ON s.id=ds.snapshot_id WHERE ds.document_id=? ORDER BY s.captured_at,s.id', (document_id,)).fetchall()
        result = {'document_id': document_id, 'note_id': nid, 'observations': [], 'gaps': [], 'latest_document_is_not_the_full_history': True}
        for ref in refs:
            path = (project / ref['path']).resolve()
            if not path.is_relative_to(project / 'snapshots'):
                raise ValueError('Snapshot path outside immutable project directory')
            try:
                content = path.read_bytes()
            except OSError:
                result['gaps'].append({'snapshot_id': ref['id'], 'reason': 'snapshot_unreadable'})
                continue
            if hashlib.sha256(content).hexdigest() != ref['id']:
                raise ValueError('Immutable snapshot checksum mismatch: ' + ref['id'])
            found = observations(json.loads(content), nid, cid)
            if len(found) != 1:
                result['gaps'].append({'snapshot_id': ref['id'], 'reason': 'identity_not_unique_in_source', 'matches': len(found)})
                continue
            result['observations'].append(dict(found[0], snapshot_id=ref['id'], path=str(path), captured_at=ref['captured_at']))
        uids = {x['user_id'] for x in result['observations'] if x['user_id']}
        flags = {x['ai_agent'] for x in result['observations'] if isinstance(x['ai_agent'], bool)}
        result['identity_conflict'] = len(uids) > 1
        result['ai_agent_observed_true'] = True in flags
        result['ai_agent_conflict'] = len(flags) > 1
        result['text_variants'] = len({x['text'] for x in result['observations']})
        return result
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--document-id', required=True)
    args = parser.parse_args()
    print(json.dumps(read_context(args.project, args.document_id), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
