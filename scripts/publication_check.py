#!/usr/bin/env python3
"""Audit explicit public inventory, index and every reachable Git commit. Never print matched values."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'PUBLIC_FILES.txt'
PATTERNS = [
    ('personal_home', re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/')),
    ('private_key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
    ('github_credential', re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})')),
    ('bearer_credential', re.compile(r'Bearer\s+[A-Za-z0-9_./+-]{32,}')),
    ('signed_source', re.compile(r'(?:xsec_token|web_session|a1)=[A-Za-z0-9_%+/-]{20,}')),
]


def inventory(text):
    names = set(text.splitlines())
    if not names or MANIFEST not in names:
        raise ValueError('Missing public inventory')
    for name in names:
        p = PurePosixPath(name)
        if not name or str(p) != name or p.is_absolute() or '..' in p.parts or '.local' in p.parts or '.git' in p.parts:
            raise ValueError('Invalid public inventory entry')
        if p.suffix.lower() not in ('.py', '.mjs', '.md', '.txt', '.yaml', '.yml', '.json') and name not in ('LICENSE', 'NOTICE', '.gitignore', '.githooks/pre-commit', '.githooks/pre-push'):
            raise ValueError('Non-public artifact type')
    return names


def content_findings(data, forbidden=()):
    try: text = data.decode('utf-8')
    except UnicodeDecodeError: return ['binary_file']
    problems = [label for label, pattern in PATTERNS if pattern.search(text)]
    decoded = unquote(unquote(text))
    if any(term and (term in text or term in decoded) for term in forbidden): problems.append('private_policy_match')
    return problems


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)


def check(root=ROOT, staged=False, history=False):
    root = Path(root)
    allowed = inventory((root / MANIFEST).read_text())
    policy = root / '.local' / 'publication-policy.json'
    forbidden = json.loads(policy.read_text()).get('forbidden_strings', []) if policy.exists() else []
    errors = []
    checked = 0
    seen = set()
    def scan(label, entries, read):
        nonlocal checked
        for mode, oid, name in entries:
            if name not in allowed:
                errors.append((label, name, 'not_in_public_inventory')); continue
            if mode not in ('100644', '100755'):
                errors.append((label, name, 'non_regular_file')); continue
            if oid and oid in seen: continue
            if oid: seen.add(oid)
            checked += 1
            errors.extend((label, name, reason) for reason in content_findings(read(oid, name), forbidden))
    if not staged and not history:
        for name in sorted(allowed):
            path = root / name
            if not path.is_file() or path.is_symlink():
                errors.append(('working', name, 'missing_or_symlink')); continue
            checked += 1
            errors.extend(('working', name, reason) for reason in content_findings(path.read_bytes(), forbidden))
    if staged:
        entries = []
        for row in git(root, 'ls-files', '--stage', '-z').split(b'\0'):
            if not row: continue
            meta, name = row.split(b'\t', 1); mode, oid, stage = meta.decode().split()
            if stage != '0': errors.append(('index', name.decode(), 'unmerged'))
            entries.append((mode, oid, name.decode()))
        scan('index', entries, lambda oid, name: git(root, 'cat-file', 'blob', oid))
        staged_manifest = git(root, 'show', ':' + MANIFEST).decode()
        if inventory(staged_manifest) != allowed: errors.append(('index', MANIFEST, 'manifest_differs'))
    if history:
        # Include all local refs and tags: an old removed secret must still block publication.
        for commit in git(root, 'rev-list', '--all').decode().splitlines():
            entries = []
            for row in git(root, 'ls-tree', '-r', '-z', commit).split(b'\0'):
                if not row: continue
                meta, name = row.split(b'\t', 1); mode, kind, oid = meta.decode().split()
                entries.append((mode, oid, name.decode()))
            scan(commit[:12], entries, lambda oid, name: git(root, 'cat-file', 'blob', oid))
            # Identity is intentionally inherited from existing Git config; scan commit messages only.
            errors.extend((commit[:12], '<commit-message>', x) for x in content_findings(git(root, 'show', '-s', '--format=%B', commit), forbidden))
        for tag in git(root, 'for-each-ref', '--format=%(refname)', 'refs/tags').decode().splitlines():
            errors.extend(('tag', '<tag-message>', x) for x in content_findings(git(root, 'for-each-ref', '--format=%(contents)', tag), forbidden))
    return {'ok': not errors, 'checked_blobs': checked, 'errors': errors}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--staged', action='store_true'); p.add_argument('--history', action='store_true')
    a = p.parse_args()
    try: result = check(staged=a.staged, history=a.history)
    except (ValueError, OSError, subprocess.CalledProcessError):
        print('Publication audit could not finish; publication blocked', file=sys.stderr); return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1

if __name__ == '__main__': sys.exit(main())
