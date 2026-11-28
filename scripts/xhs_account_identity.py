"""Credential-free UID/profile association, shared by bridge and legacy collectors."""
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import os
import re
import time

import xhs_research as legacy


def uid_key(uid):
    if not isinstance(uid, str) or not re.fullmatch(r"[a-f0-9]{24}", uid):
        raise legacy.ResearchError("Expected observed public Xiaohongshu UID")
    return hashlib.sha256(("xiaohongshu-public-uid\0" + uid).encode()).hexdigest()


@contextmanager
def lock_file(path, *, wait_seconds=0.0, poll_interval=0.02):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if wait_seconds <= 0 or time.monotonic() >= deadline:
                    raise legacy.CollectionBusy("Account identity or collection is busy") from None
                time.sleep(poll_interval)
        yield
    finally:
        os.close(fd)


def registry():
    path = legacy.account_root() / "public-identities.json"
    if path.is_symlink():
        raise legacy.ResearchError("Unsafe identity registry")
    return legacy.read_json(path, {})


def associate(uid, profile_key):
    key = uid_key(uid)
    if not re.fullmatch(r"[a-f0-9]{64}", profile_key):
        raise legacy.ResearchError("Expected existing profile guard key")
    root = legacy.account_root()
    with lock_file(root / "identity-registry.lock", wait_seconds=5.0):
        data = registry()
        # Preserve old associations: a profile changing login must not clear STOP.
        group = data.setdefault(key, {"uid": uid, "profile_keys": []})
        if profile_key not in group["profile_keys"]:
            group["profile_keys"].append(profile_key)
        legacy.write_json(root / "public-identities.json", data)
    return key


def related_keys(profile_key=None, uid=None):
    keys = {profile_key} if profile_key else set()
    data = registry()
    if uid:
        keys.add(uid_key(uid))
    # Transitive closure keeps all former profile/UID STOPs visible.
    while True:
        before = set(keys)
        for key, group in data.items():
            members = {key, *group["profile_keys"]}
            if members & keys:
                keys.update(members)
        if keys == before:
            return sorted(keys)


def guard_directory(key):
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise legacy.ResearchError("Invalid account guard key")
    directory = legacy.account_root() / key
    if directory.is_symlink():
        raise legacy.ResearchError("Unsafe account guard")
    directory.mkdir(exist_ok=True, mode=0o700)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise legacy.ResearchError("Insecure account guard")
    return directory


def check_stops(keys, web_stop_exceptions=None, web_note_recovery=None):
    note_exceptions={}
    if web_note_recovery:
        from xhs_note_recovery import web_exceptions
        note_exceptions=web_exceptions(web_note_recovery["project"],web_note_recovery["binding"],keys)
    for key in keys:
        path=guard_directory(key) / "STOP.json"
        if path.exists():
            raw=path.read_bytes()
            if (web_stop_exceptions or {}).get(key)==hashlib.sha256(raw).hexdigest() and legacy.read_json(path,{}).get('reason')=='Public media access restriction: HTTP_403':
                continue
            if note_exceptions.get(key)==hashlib.sha256(raw).hexdigest() and legacy.read_json(path,{}).get('reason')=='Observed browser login/verification/access restriction':
                continue
            raise legacy.StopRun("Shared account STOP remains active; bridge cannot clear it")


@contextmanager
def account_lease(profile_key, uid=None, web_stop_exceptions=None, web_note_recovery=None):
    # Hold the global registry lock ONLY while reading aliases and acquiring
    # every per-account collection.lock, then release it before yielding so
    # unrelated accounts can run concurrently without blocking one another.
    with ExitStack() as stack:
        acquired = []
        try:
            with lock_file(legacy.account_root() / "identity-registry.lock", wait_seconds=5.0):
                keys = related_keys(profile_key, uid)
                pid = os.getpid()
                for key in keys:
                    lock = guard_directory(key) / "collection.lock"
                    if legacy._ACCOUNT_LOCKS.get(str(lock)) == pid:
                        continue
                    stack.enter_context(lock_file(lock))
                    legacy._ACCOUNT_LOCKS[str(lock)] = pid
                    acquired.append(str(lock))
                check_stops(keys, web_stop_exceptions, web_note_recovery)
            yield keys
        finally:
            pid = os.getpid()
            for lock in acquired:
                if legacy._ACCOUNT_LOCKS.get(lock) == pid:
                    legacy._ACCOUNT_LOCKS.pop(lock, None)


def stop_all(keys, reason):
    for key in keys:
        legacy.write_json(guard_directory(key) / "STOP.json", {"at": legacy.now(), "reason": reason,
                          "channel": "bridge", "credential_free": True})
    raise legacy.StopRun(reason)
