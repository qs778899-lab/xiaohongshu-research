"""Persist credential-free account guards without reviving old collection grants."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from xhs_paths import external_path, REPO_ROOT


def require(ok, message):
    if not ok:
        raise ValueError(message)


def secure_directory(path, create=False):
    lexical = Path(path).expanduser().absolute()
    require(not lexical.is_symlink(), 'ACCOUNT_STATE_SYMLINK')
    path = external_path(lexical)
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(path.is_dir(), 'ACCOUNT_STATE_DIRECTORY_MISSING')
    st = path.stat()
    require(st.st_uid == os.getuid() and not st.st_mode & 0o077, 'ACCOUNT_STATE_PERMISSIONS')
    return path


def configured_root(repo=REPO_ROOT):
    from xhs_local import config_path, read_config
    if not config_path(repo).exists():
        return None
    value = read_config(repo)['defaults'].get('account_state_root')
    # Missing persistent state must fail closed, never silently create an empty guard.
    return secure_directory(value) if value else None


def sha(data):
    return hashlib.sha256(data).hexdigest()


def original_stop_bytes(value, archived_raw, expected_sha):
    if sha(archived_raw) == expected_sha:
        return archived_raw
    # Older freeze wrote sorted JSON, while stop_all wrote these four fields
    # in insertion order. Accept reconstruction only on the original byte hash.
    fields = ('at', 'reason', 'channel', 'credential_free')
    require(set(value) == set(fields), 'STATE_ARCHIVE_STOP_CHANGED')
    raw = json.dumps({k:value[k] for k in fields}, ensure_ascii=False, indent=2).encode()
    require(sha(raw) == expected_sha, 'STATE_ARCHIVE_STOP_CHANGED')
    return raw


def archive_guards(binding_file, source):
    """Validate the existing evidence chain; archives only restore protection."""
    import xhs_manual_tab_recovery as manual
    import xhs_account_identity as identity
    binding_file = Path(binding_file).resolve()
    b = manual.nr.read_json(binding_file)[0]
    require(binding_file == manual.bridge.binding_path(b['project'], b['binding_name']), 'STATE_BINDING_LOCATION_CHANGED')
    g = manual.check_pointer(b['manual_tab_revalidation'])
    require(g.get('kind') == manual.GRANT_KIND and g.get('scope') == 'rendered_public_web_only', 'STATE_GRANT_SCOPE_CHANGED')
    require(g.get('authority') == manual.authority(b), 'STATE_BINDING_AUTHORITY_CHANGED')
    _, attempt, _ = manual.validated_probe(g['probe'])
    manual.validate_review(manual.check_pointer(g['review']), manual.check_pointer(g['probe']))
    require(g['stops'] == attempt['stops'], 'STATE_STOP_HISTORY_CHANGED')
    uid_key = identity.uid_key(b['account_uid'])
    require(re.fullmatch(r'[a-f0-9]{64}', b['profile_guard_key']), 'STATE_PROFILE_KEY_REQUIRED')
    require({uid_key, b['profile_guard_key']} <= set(g['stops']), 'STATE_ACCOUNT_STOP_MISSING')
    records = {}
    for key, old in g['stops'].items():
        require(re.fullmatch(r'[a-f0-9]{64}', key), 'STATE_INVALID_GUARD_KEY')
        require(Path(old['file']).resolve() == source / key / 'STOP.json', 'STATE_SOURCE_LOCATION_CHANGED')
        ref = attempt['frozen_stops'][key]
        value = manual.check_pointer(ref)
        raw = original_stop_bytes(value, Path(ref['file']).read_bytes(), old['sha256'])
        require(value.get('at') == old['at'], 'STATE_ARCHIVE_STOP_CHANGED')
        require(value.get('reason') == manual.nr.STOP_REASON and value.get('channel') == 'bridge'
                and value.get('credential_free') is True, 'STATE_ARCHIVE_KIND_CHANGED')
        records[key] = raw
    return {'uid': b['account_uid'], 'uid_key': uid_key, 'profile_key': b['profile_guard_key'],
            'guards': records, 'binding': str(binding_file), 'binding_sha256': sha(binding_file.read_bytes())}


def migrate(source, destination, bindings, activate=False, repo=REPO_ROOT):
    """Copy under all old locks. Keep original bytes; never overwrite live STOPs."""
    import xhs_account_identity as identity
    source = secure_directory(source)
    destination = external_path(destination)
    require(source != destination and source not in destination.parents
            and destination not in source.parents, 'STATE_SEPARATE_DESTINATION_REQUIRED')
    require(bindings, 'STATE_EXPLICIT_BINDINGS_REQUIRED')
    require(not destination.exists() and not destination.is_symlink(), 'STATE_DESTINATION_EXISTS')
    if activate:
        from xhs_local import read_config
        require(not read_config(repo)['defaults'].get('account_state_root'), 'STATE_ALREADY_CONFIGURED')
    records = [archive_guards(p, source) for p in bindings]
    archived = {}
    for record in records:
        for key, raw in record['guards'].items():
            require(key not in archived or archived[key] == raw, 'STATE_CONFLICTING_ARCHIVES')
            archived[key] = raw
    with identity.lock_file(source / 'identity-registry.lock'), ExitStack() as locks:
        keys = set(archived) | {p.name for p in source.iterdir() if re.fullmatch(r'[a-f0-9]{64}', p.name)}
        for key in sorted(keys):
            path = source / key
            require(not path.is_symlink(), 'STATE_SOURCE_SYMLINK')
            path.mkdir(mode=0o700, exist_ok=True)
            locks.enter_context(identity.lock_file(path / 'collection.lock'))
        files = {}
        for p in source.rglob('*'):
            require(not p.is_symlink(), 'STATE_SOURCE_SYMLINK')
            if p.is_file() and p.suffix != '.lock':
                require(p.suffix == '.json', 'STATE_UNEXPECTED_FILE')
                files[str(p.relative_to(source))] = p.read_bytes()
        source_hashes = {name: sha(raw) for name, raw in files.items()}
        restored = []
        for key, raw in archived.items():
            name = key + '/STOP.json'
            if name not in files:
                files[name] = raw
                restored.append(key)
        registry = json.loads(files.get('public-identities.json', b'{}'))
        for r in records:
            group = registry.setdefault(r['uid_key'], {'uid': r['uid'], 'profile_keys': []})
            require(group['uid'] == r['uid'], 'STATE_REGISTRY_IDENTITY_CHANGED')
            # Keep the full historical guard closure even if its registry was lost.
            group['profile_keys'] = sorted(set(group['profile_keys']) | (set(r['guards']) - {r['uid_key']}))
        files['public-identities.json'] = json.dumps(registry, sort_keys=True).encode()
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.account-state-', dir=destination.parent))
        try:
            for name, raw in files.items():
                target = staging / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with target.open('xb') as out:
                    out.write(raw)
                target.chmod(0o600)
            report = {'format': 1, 'source': str(source), 'destination': str(destination),
                'restored_protection_keys': sorted(restored), 'copied_files': len(files),
                'files': {name: sha(raw) for name, raw in sorted(files.items())},
                'source_files': source_hashes,
                'bindings': [{k:v for k,v in r.items() if k != 'guards'} for r in records],
                'collection_permitted': False, 'fresh_web_validation_required': True,
                'source_unchanged': True, 'api_and_media_restrictions_preserved': True}
            (staging / 'state-migration.json').write_text(json.dumps(report, indent=2))
            (staging / 'state-migration.json').chmod(0o600)
            # Original records still resolve; the new path deliberately invalidates old grants.
            require(not destination.exists(), 'STATE_DESTINATION_EXISTS')
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        if activate:
            activate_state(destination, repo, already_locked=True)
        return report | {'activated': activate}


def activate_state(destination, repo=REPO_ROOT, already_locked=False):
    """Finish a copy-before-activation interruption; no browser or replayed probes."""
    import xhs_account_identity as identity
    from xhs_local import config_path, read_config
    destination = secure_directory(destination)
    manifest = destination / 'state-migration.json'
    require(not manifest.is_symlink(), 'STATE_MANIFEST_SYMLINK')
    report = json.loads(manifest.read_text())
    require(report.get('format') == 1 and report.get('destination') == str(destination)
            and report.get('collection_permitted') is False, 'STATE_MANIFEST_INVALID')
    source = secure_directory(report['source'])
    with ExitStack() as locks:
        if not already_locked:
            locks.enter_context(identity.lock_file(source / 'identity-registry.lock'))
            for p in sorted(source.iterdir()):
                if re.fullmatch(r'[a-f0-9]{64}', p.name):
                    require(not p.is_symlink(), 'STATE_SOURCE_SYMLINK')
                    locks.enter_context(identity.lock_file(p / 'collection.lock'))
        config = read_config(repo)
        current = config['defaults'].get('account_state_root')
        if current:
            require(Path(current).resolve() == destination, 'STATE_ALREADY_CONFIGURED')
            return {'activated': True, 'already_active': True}
        require({str(p.relative_to(source)):sha(p.read_bytes()) for p in source.rglob('*')
                 if p.is_file() and p.suffix != '.lock'} == report['source_files'], 'STATE_SOURCE_CHANGED')
        for name, digest in report['files'].items():
            p = destination / name
            require(p.resolve().is_relative_to(destination) and not p.is_symlink()
                    and p.is_file() and sha(p.read_bytes()) == digest, 'STATE_DESTINATION_CHANGED')
        path = config_path(repo)
        config['defaults']['account_state_root'] = str(destination)
        fd, tmp = tempfile.mkstemp(prefix='.state-config-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(config, out, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if Path(tmp).exists():
                Path(tmp).unlink()
        return {'activated': True, 'already_active': False, 'collection_permitted': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest='command', required=True)
    m = s.add_parser('migrate')
    m.add_argument('--source', required=True, type=Path)
    m.add_argument('--destination', required=True, type=Path)
    m.add_argument('--binding-file', action='append', required=True, type=Path)
    m.add_argument('--activate', action='store_true')
    a = s.add_parser('activate')
    a.add_argument('--destination', required=True, type=Path)
    a = p.parse_args()
    result = (migrate(a.source, a.destination, a.binding_file, a.activate) if a.command == 'migrate'
              else activate_state(a.destination))
    print(json.dumps({k:v for k,v in result.items() if k not in {'files','source_files','bindings'}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
