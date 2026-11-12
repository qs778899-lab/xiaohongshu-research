"""Optional SHA-pinned binding compatibility; no general version bypass."""
import json,hashlib,re
from pathlib import Path

KEYS=('format', 'project', 'binding_name', 'profile_label', 'context_id', 'account_uid', 'profile_guard_key', 'session', 'runtime', 'extension_version', 'web_channel_authorization', 'authorization')


def effective_extension_version(binding):
    version=binding['extension_version']
    if 'extension_compatibility_policy' not in binding:
        return version
    reference=binding['extension_compatibility_policy']
    if (not isinstance(reference,dict) or set(reference)!={'file','sha256'}
            or not isinstance(reference.get('file'),str)
            or not re.fullmatch(r'[a-f0-9]{64}',str(reference.get('sha256') or ''))):
        raise RuntimeError('EXTENSION_COMPATIBILITY_POLICY_REFERENCE_INVALID')
    policy=Path(reference['file'])
    if not policy.is_absolute() or policy.is_symlink() or not policy.is_file():
        raise RuntimeError('EXTENSION_COMPATIBILITY_POLICY_REFERENCE_INVALID')
    raw=policy.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=reference['sha256']:
        raise RuntimeError('EXTENSION_COMPATIBILITY_POLICY_CHANGED')
    p=json.loads(raw)
    authority={k:binding.get(k) for k in KEYS}
    if authority!=p['authority_projection']:
        raise RuntimeError('EXTENSION_COMPATIBILITY_BINDING_CHANGED')
    if version!='1.0.26' or p['to_version']!='1.0.27':
        raise RuntimeError('EXTENSION_COMPATIBILITY_SCOPE_CHANGED')
    for source in p['source_files']:
        f=Path(source['file'])
        if f.is_symlink() or hashlib.sha256(f.read_bytes()).hexdigest()!=source['sha256']:
            raise RuntimeError('EXTENSION_COMPATIBILITY_SOURCE_CHANGED')
    return '1.0.27'
