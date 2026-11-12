"""Offline attestation of an already observed same-note native-web recovery.

Does not operate a browser, retry requests, remove STOP, or enable API collection.
The caller must actually review the bound original and recovery observations.
"""
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import json
import hashlib
import xhs_project as xp


def setup(p):
    p.db.execute("""CREATE TABLE IF NOT EXISTS native_web_recoveries(
      event_id TEXT PRIMARY KEY REFERENCES native_events(id),
      receipt TEXT NOT NULL, artifact TEXT NOT NULL, sha256 TEXT NOT NULL,
      observation_artifact TEXT NOT NULL, observation_sha256 TEXT NOT NULL)""")


def same_note(url, note):
    u = urlparse(url)
    return (u.scheme == 'https' and u.hostname in {'www.xiaohongshu.com', 'xiaohongshu.com'}
            and u.path.rstrip('/') in {'/explore/'+note, '/search_result/'+note})


def original_context(p, event):
    """Only explicitly recorded identity fields; never infer a UID from a nickname."""
    import xhs_native as xn
    receipt = json.loads(event['receipt'])
    try:
        source = json.loads((p.path / event['artifact']).read_text())
    except (ValueError, UnicodeError):
        source = {}
    url = receipt.get('url') or source.get('original_stop', {}).get('error_url', '')
    u = urlparse(url)
    if u.hostname in {'www.xiaohongshu.com', 'xiaohongshu.com'} and u.path == '/website-login/error':
        url = parse_qs(u.query).get('redirectPath', [''])[0]
    note = urlparse(url).path.rstrip('/').split('/')[-1]
    account = receipt.get('account_uid') or source.get('account_public_uid')
    xn.require(xn.author_uid(note) and same_note(url, note) and xn.author_uid(account),
               'Original archived stop lacks structured note/account identity; manual audit required')
    if xn.author_uid(receipt.get('target')):
        xn.require(receipt['target'] == note, 'Original stop target and URL disagree')
    return note, account, url


def valid_archive(p, path, sha):
    f = p.path / path
    return f.is_file() and not f.is_symlink() and hashlib.sha256(f.read_bytes()).hexdigest() == sha


def valid_rows(p):
    import xhs_native as xn
    if not p.db.execute("SELECT 1 FROM sqlite_master WHERE name='native_web_recoveries'").fetchone():
        return []
    result = []
    for row in p.db.execute('SELECT * FROM native_web_recoveries'):
        event = p.db.execute('SELECT * FROM native_events WHERE id=?', (row['event_id'],)).fetchone()
        receipt = json.loads(row['receipt'])
        if (event and event['outcome'] == 'access_stop' and xn.valid_event_source(p, event)
                and receipt['original_event_sha256'] == event['sha256']
                and valid_archive(p, row['artifact'], row['sha256'])
                and valid_archive(p, row['observation_artifact'], row['observation_sha256'])):
            result.append((row['event_id'], receipt))
    return result


def native_project_stop_applies(p):
    """Exact matching native-only metadata can be scoped; API code still sees STOP."""
    stop = p.get_meta('stop')
    if not stop:
        return False
    if not isinstance(stop, dict):
        return True
    if (stop.get('reason') != 'shared_native_access_state_unknown'
            or str(stop.get('code')) != '300017' or stop.get('previous_stop')):
        return True
    digest = xp.digest(stop)
    return not any(r.get('project_stop_digest') == digest for _, r in valid_rows(p))


def attest(p, receipt_path):
    import xhs_native as xn
    r = xp.load(receipt_path)
    require = xn.require
    require(r.get('kind') == 'same_note_native_web_recovery', 'Explicit native-web scope required')
    event = p.db.execute('SELECT * FROM native_events WHERE id=?', (r.get('event_id'),)).fetchone()
    require(event is not None and event['outcome'] == 'access_stop', 'Original access_stop event required')
    require(xn.valid_event_source(p, event), 'Original stop archives missing or changed')
    require(r.get('original_event_sha256') == event['sha256'], 'Original stop source binding differs')
    original_note, original_account, original_url = original_context(p, event)
    require((r.get('note_id'), r.get('original_account_uid'), r.get('original_failed_url'))
            == (original_note, original_account, original_url), 'Receipt identity differs from archived original stop')
    require(xn.author_uid(r.get('note_id')), 'Observed note ID required')
    require(same_note(r.get('original_failed_url', ''), r['note_id'])
            and same_note(r.get('recovered_url', ''), r['note_id']), 'Same original note required')
    require(xn.author_uid(r.get('original_account_uid'))
            and r.get('account_uid') == r['original_account_uid'], 'Same original account required')
    require(all(r.get(k) is True for k in ['target_verified', 'content_ready', 'view_consistent',
                                          'no_access_warning', 'account_verified']), 'Actual UI verification required')
    require(r.get('entry_method') == 'observed_in_site_card'
            and r.get('authorization_basis') and r.get('positions'), 'Observed entry and review basis required')
    previous = json.loads(event['receipt'])
    try:
        current_at = datetime.fromisoformat(r['observed_at'])
        previous_at = datetime.fromisoformat(previous['observed_at'])
        newer = bool(current_at.tzinfo and previous_at.tzinfo and current_at > previous_at)
    except (KeyError, ValueError, TypeError):
        newer = False
    require(newer, 'Recovery observation must be later than the stop, with timezone')
    observation = Path(r.get('observation_file', ''))
    require(observation.is_absolute() and observation.is_file() and not observation.is_symlink(),
            'Explicit original recovery observation required')
    require(hashlib.sha256(observation.read_bytes()).hexdigest() == r.get('observation_sha256'),
            'Recovery observation changed')
    raw = xp.load(observation)
    recovery = raw.get('recovery', {})
    binding = {'note_id': raw.get('note', {}).get('id'), 'observed_at': raw.get('observed_at'),
               'original_account_uid': recovery.get('original_account_uid'),
               'account_uid': recovery.get('current_account_uid'),
               'original_failed_url': recovery.get('original_failed_url'),
               'recovered_url': recovery.get('actual_success_url')}
    require(all(r.get(k) == v and v for k, v in binding.items()), 'Receipt differs from original recovery observation')
    require(all(recovery.get(k) is True for k in ['target_verified', 'content_ready', 'view_consistent']),
            'Original recovery record lacks explicit UI verification')
    require(r.get('project_stop_digest') == xp.digest(p.get_meta('stop')), 'Project stop changed; reassess scope')
    stop = p.get_meta('stop')
    if isinstance(stop, dict) and stop.get('reason') == 'shared_native_access_state_unknown':
        require(stop.get('source') == previous.get('source_file'),
                'Project native STOP belongs to a different original observation')
        try:
            stopped_at = datetime.fromisoformat(stop['at'])
            later_than_meta = bool(stopped_at.tzinfo and current_at > stopped_at)
        except (KeyError, ValueError, TypeError):
            later_than_meta = False
        require(later_than_meta, 'Recovery predates current project STOP')
    setup(p)
    old = p.db.execute('SELECT * FROM native_web_recoveries WHERE event_id=?', (event['id'],)).fetchone()
    raw_sha = hashlib.sha256(Path(receipt_path).read_bytes()).hexdigest()
    if old:
        require(old['sha256'] == raw_sha and any(eid == event['id'] for eid, _ in valid_rows(p)),
                'Existing recovery differs or its evidence is invalid')
        return {'duplicate': True, 'event_id': event['id'], 'api_stop_unchanged': True}
    archive, sha = xn.archive(p, receipt_path)
    obs_archive, obs_sha = xn.archive(p, observation)
    with p.db:
        p.db.execute('INSERT INTO native_web_recoveries VALUES(?,?,?,?,?,?)',
                     (event['id'], xp.dumps(r), archive, sha, obs_archive, obs_sha))
    return {'event_id': event['id'], 'scope': 'native_ui_only', 'api_stop_unchanged': True,
            'historical_events_unchanged': True, 'current_ui_health_not_proven': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--receipt', required=True, type=Path)
    args = parser.parse_args()
    with xp.project_lock(args.project):
        p = xp.Project(args.project)
        try:
            print(xp.dumps(attest(p, args.receipt)))
        finally:
            p.close()


if __name__ == '__main__':
    main()
