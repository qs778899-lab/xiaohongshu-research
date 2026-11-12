"""FIFO collection turns; queued work never clears a run lock or STOP."""
from contextlib import contextmanager, ExitStack
from pathlib import Path
import fcntl
import os
import time
import uuid
import sqlite3
import json
from urllib.parse import quote
import xhs_research as legacy
import xhs_project as xp


class QueueCheckpoint(Exception):
    def __init__(self, reason):
        self.reason = reason


class ProjectStopped(legacy.StopRun):
    """Preserve the batch planner's returned project-stop contract."""


def ensure_project_active(project):
    database = Path(project) / 'project.sqlite3'
    if not database.exists():
        return
    db = sqlite3.connect('file:' + quote(str(database.resolve())) + '?mode=ro',
                         uri=True, timeout=0)
    try:
        row = db.execute("SELECT value FROM meta WHERE key='stop'").fetchone()
        if row and json.loads(row[0]):
            raise ProjectStopped('This project has an unresolved STOP; preserve it before continuing')
    except sqlite3.OperationalError as exc:
        # A writer may hold SQLite's EXCLUSIVE lock before we can inspect STOP.
        # An unreadable STOP is unknown, never permission to collect. Retry only
        # SQLite's typed contention codes; corruption/schema/I/O errors propagate.
        code = getattr(exc, 'sqlite_errorcode', None)
        if isinstance(code, int) and code & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise legacy.CollectionBusy('Project STOP inspection waits for a local database writer') from None
        raise
    finally:
        db.close()


@contextmanager
def queue_guard(root):
    fd = os.open(root / 'guard.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def collection_turn(project, run, deadline, checkpoint, emit=None,
                    clock=time.time, sleep=time.sleep):
    root = (legacy.account_dir(run) or Path(run)) / 'collection-queue'
    if root.is_symlink():
        raise legacy.ResearchError('Queue directory must not be a symlink')
    root.mkdir(mode=0o700, exist_ok=True)
    with queue_guard(root):
        sequence = legacy.read_json(root / 'sequence.json', {}).get('last', 0)
        if type(sequence) is not int or sequence < 0:
            raise legacy.ResearchError('Invalid queue sequence; preserve it for inspection')
        sequence += 1
        legacy.write_json(root / 'sequence.json', {'last': sequence})
        ticket = root / (str(sequence).zfill(24) + '-' + uuid.uuid4().hex + '.ticket')
        fd = os.open(ticket, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    last_notice = None
    try:
        while True:
            legacy.ensure_active(run)
            if checkpoint and Path(checkpoint).exists():
                raise QueueCheckpoint('requested')
            if clock() >= deadline:
                raise QueueCheckpoint('time_budget')
            try:
                ensure_project_active(project)
            except legacy.CollectionBusy:
                ahead = True
            else:
                with queue_guard(root):
                    ahead = False
                    for other in sorted(root.glob('*.ticket')):
                        if other == ticket:
                            break
                        other_fd = os.open(other, os.O_RDWR | os.O_NOFOLLOW)
                        try:
                            try:
                                fcntl.flock(other_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                ahead = True
                                break
                            # Kernel confirms this ticket has no live owner. This
                            # never deletes legacy run/account locks or STOP files.
                            other.unlink()
                        finally:
                            os.close(other_fd)
            if not ahead:
                stack = ExitStack()
                try:
                    stack.enter_context(xp.project_lock(project))
                    stack.enter_context(legacy.run_lock(run))
                    stack.enter_context(legacy.account_lock(run))
                    legacy.ensure_active(run)
                    ensure_project_active(project)
                    if checkpoint and Path(checkpoint).exists():
                        raise QueueCheckpoint('requested')
                    if clock() >= deadline:
                        raise QueueCheckpoint('time_budget')
                except legacy.CollectionBusy:
                    stack.close()
                except BaseException:
                    stack.close()
                    raise
                else:
                    with stack:
                        yield
                    return
            if emit and (last_notice is None or clock() - last_notice >= 20):
                emit({'event': 'waiting', 'reason': 'shared_account_queue',
                      'actor': 'runner', 'requires_user_input': False})
                last_notice = clock()
            sleep(max(0, min(1, deadline - clock())))
    finally:
        with queue_guard(root):
            ticket.unlink(missing_ok=True)
            os.close(fd)
