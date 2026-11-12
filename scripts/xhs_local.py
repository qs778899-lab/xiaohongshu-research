#!/usr/bin/env python3
"""Private path registry; never reads cookies, changes bindings or grants authority."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
from xhs_paths import REPO_ROOT, external_path


def config_path(root):
    root = Path(root).resolve()
    local = root / '.local'
    if local.is_symlink() or (local / 'config.json').is_symlink():
        raise ValueError('Private configuration must not be a symlink')
    return local / 'config.json'


def initialize(root=REPO_ROOT):
    path = config_path(root)
    if path.exists():
        read_config(root)
        return {'created': False}
    path.parent.mkdir(mode=0o700, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        json.dump({'format': 1, 'defaults': {}, 'projects': {}}, f, indent=2)
    return {'created': True}


def read_config(root=REPO_ROOT):
    data = json.loads(config_path(root).read_text())
    if data.get('format') != 1 or not isinstance(data.get('projects'), dict) or not isinstance(data.get('defaults'), dict):
        raise ValueError('Invalid configuration format')
    return data


def resolve(root=REPO_ROOT, alias=None, project=None, binding=None, runtime=None):
    config = read_config(root)
    if alias is not None and alias not in config['projects']:
        raise ValueError('Unknown project alias')
    entry = config['projects'].get(alias, {})
    value = project or entry.get('path')
    if not value:
        raise ValueError('Supply --project or a registered --project-alias')
    path = external_path(value, root)
    # Explicit project overrides must not inherit another project's binding.
    same = not project or (entry.get('path') and path == external_path(entry['path'], root))
    selected = binding or (entry.get('binding') if same else None)
    result = {'project': str(path)}
    if selected:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', selected):
            raise ValueError('Invalid binding name')
        bp = path / 'bridge' / 'bindings' / (selected + '.json')
        if bp.resolve() != bp or not bp.is_file():
            raise ValueError('Binding missing or symlinked')
        b = json.loads(bp.read_text())
        if b.get('binding_name') != selected or Path(b.get('project', '')).resolve() != path:
            raise ValueError('Binding belongs to a different project')
        result['binding'] = selected
    rt = runtime or (entry.get('runtime') if same else None) or config['defaults'].get('runtime')
    if rt:
        result['runtime'] = str(external_path(rt, root))
    return result


def doctor(root=REPO_ROOT):
    config = read_config(root)
    checks = []
    for alias in config['projects']:
        try:
            result = resolve(root, alias=alias)
            ok = Path(result['project']).is_dir()
            checks.append({'project_alias': alias, 'ok': ok, 'reason': '' if ok else 'project_missing'})
        except (ValueError, OSError, KeyError):
            checks.append({'project_alias': alias, 'ok': False, 'reason': 'invalid_project_or_binding'})
    for key in ('python', 'node', 'opencli_root', 'runtime', 'data_root'):
        if config['defaults'].get(key):
            try:
                ok = external_path(config['defaults'][key], root).exists()
            except ValueError:
                ok = False
            checks.append({'default': key, 'ok': ok})
    return {'ok': all(c['ok'] for c in checks), 'checks': checks,
            'browser_checked': False, 'authorization_granted': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('init'); sub.add_parser('doctor')
    r = sub.add_parser('resolve')
    r.add_argument('--project-alias'); r.add_argument('--project')
    r.add_argument('--binding'); r.add_argument('--runtime')
    a = p.parse_args()
    try:
        if a.command == 'init': result = initialize()
        elif a.command == 'doctor': result = doctor()
        else: result = resolve(alias=a.project_alias, project=a.project, binding=a.binding, runtime=a.runtime)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('ok', True) else 1
    except (ValueError, OSError, KeyError):
        print('Local configuration invalid or unavailable; check .local/config.json', file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
