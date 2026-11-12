"""Single-use, same-search-page revalidation after an explicit user normal-page confirmation.

No navigation, sorting, reload, API/CDN access or STOP deletion. This proves only
that the exact failed search page currently passes the existing guarded DOM read.
"""
import argparse
import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import xhs_bridge as bridge
import xhs_current_page_recovery as current
import xhs_note_recovery as nr
import xhs_project as xp

KIND = 'same-target-current-search-revalidation_v1'
MANUAL_KIND = 'same-target-search-manual-login-restoration_v1'
require = nr.require


def search_url(url):
    bridge.safe_url(url)
    parsed = urlparse(url)
    values = parse_qs(parsed.query, keep_blank_values=True)
    require(parsed.path.rstrip('/') == '/search_result' and not parsed.fragment
            and len(values.get('keyword', [])) == 1 and values['keyword'][0].strip()
            and all(len(v) == 1 for v in values.values()), 'SEARCH_EXACT_QUERY_URL_REQUIRED')
    return values


def same_url(url, expected):
    require(search_url(url) == search_url(expected), 'SEARCH_ORIGINAL_QUERY_CHANGED')


def context(p, b, snapshot, task_id, failures=None):
    require(snapshot.get('state') == 'blocked' and snapshot.get('last_error', {}).get('code') == 'ACCESS_STOP',
            'SEARCH_BLOCKED_ACCESS_STOP_REQUIRED')
    require(snapshot.get('project') == str(p.path.resolve()) and snapshot.get('binding_name') == b.get('binding_name')
            and current.projection(snapshot) == current.projection(b), 'SEARCH_BINDING_AUTHORITY_CHANGED')
    require(snapshot.get('authorization') and snapshot.get('context_id') and snapshot.get('account_uid'),
            'SEARCH_EXISTING_AUTHORIZATION_REQUIRED')
    slot = snapshot.get('tabs', {}).get('search', {})
    require(slot.get('owned') is True and slot.get('page') and slot.get('session') == snapshot['session'] + '-search',
            'SEARCH_OWNED_ORIGINAL_ROLE_REQUIRED')
    intent = slot.get('navigation_intent', {})
    values = search_url(intent.get('url'))
    task = p.task(task_id)
    require(task.get('kind') == 'native' and task['payload'].get('stage') == 'search'
            and task['payload'].get('target') == values['keyword'][0]
            and task['payload'].get('sort') in xp.SORTS, 'SEARCH_FAILED_TASK_REQUIRED')
    when = nr.stamp(snapshot['last_error']['at'])
    require(nr.stamp(intent.get('at')) <= when, 'SEARCH_FAILURE_INTENT_TIME_REQUIRED')
    if failures is None:
        files = {'driver_error': p.path / 'bridge/last-driver-error.json', 'handoff': p.path / 'bridge/handoff.json'}
        failures = {}
        for key, file in files.items():
            ref, _ = nr.freeze(p, file)
            # Runtime diagnostic filenames are mutable; authority consumes their frozen bytes.
            ref['original_file'] = ref['file']
            ref['file'] = str(p.path / ref['archive'])
            failures[key] = ref
    require(set(failures) == {'driver_error', 'handoff'}, 'SEARCH_FAILURE_SOURCES_REQUIRED')
    error = nr.check_ref(p, failures['driver_error'])
    handoff = nr.check_ref(p, failures['handoff'])
    require(error.get('role') == 'search' and error.get('action') == 'identity'
            and error.get('error', {}).get('code') == 'ACCESS_STOP'
            and 0 <= (when - nr.stamp(error.get('at'))).total_seconds() <= 5,
            'SEARCH_ORIGINAL_IDENTITY_FAILURE_REQUIRED')
    require(handoff.get('event') == 'blocked' and handoff.get('reason') == 'ACCESS_STOP'
            and handoff.get('task_id') == task_id and handoff.get('processed_units') == 0,
            'SEARCH_ORIGINAL_FAILED_HANDOFF_REQUIRED')
    return {'url': intent['url'], 'query': values['keyword'][0], 'requested_sort': task['payload']['sort'],
            'failure_at': snapshot['last_error']['at'], 'failure_sources': failures}, when


def confirmation(value, b, task_id, stops):
    # Preserve the actual user wording, while binding this input to the original task and binding.
    statement = value.get('user_confirmation')
    require(isinstance(statement, str) and statement.strip() in {'页面均正常', '搜索页、正文和评论均正常', '搜索页正常'},
            'SEARCH_EXPLICIT_USER_CONFIRMATION_REQUIRED')
    require(value.get('known_failed_task') == task_id and value.get('binding') == b['binding_name']
            and value.get('profile') == b['profile_label'] and value.get('confirmation_reference'),
            'SEARCH_USER_CONFIRMATION_SCOPE_REQUIRED')
    when = nr.stamp(value.get('processed_at'))
    require(all(when > nr.stamp(stop['at']) for stop in stops.values()), 'SEARCH_USER_CONFIRMATION_NOT_NEWER')
    return when


def validate_capture(b, scope, capture):
    require(not capture.get('error') and not any(capture.get(key) for key in ('loginWall', 'securityBlock', 'notFound')),
            'SEARCH_CAPTURE_ACCESS_WARNING')
    require(capture.get('uid') == b['account_uid'] and capture.get('stage') == 'search',
            'SEARCH_GUARDED_ACCOUNT_OR_STAGE_CHANGED')
    same_url(capture.get('url'), scope['url'])
    cards = capture.get('cards')
    require(isinstance(cards, list) and any(isinstance(card, dict) and isinstance(card.get('text'), str)
            and card['text'].strip() and re.fullmatch(r'https://www\.xiaohongshu\.com/(?:explore|search_result|user/profile/[a-f0-9]{24})/[a-f0-9]{24}(?:\?[^#]*)?', str(card.get('url') or ''))
            for card in cards), 'SEARCH_RENDERED_NOTE_CARD_REQUIRED')
    position = capture.get('position') or {}
    require(all(isinstance(position.get(key), (int, float)) and not isinstance(position.get(key), bool)
                and position[key] > 0 for key in ('scrollHeight', 'viewportHeight'))
            and position.get('loading') is False, 'SEARCH_RENDERED_RESULT_GEOMETRY_REQUIRED')


def manual_context(p, b, task_id, stops, value):
    """One child of the exact failed root after a new explicit manual-login report."""
    require(value.get('kind') == MANUAL_KIND and value.get('project') == str(p.path.resolve())
            and value.get('binding_name') == b['binding_name'] and value.get('task_id') == task_id,
            'SEARCH_MANUAL_SCOPE_REQUIRED')
    require(value.get('user_statement') in {'我已重新登陆好了，你继续', '我已重新登录好了，你继续'}
            and value.get('confirmation_reference'), 'SEARCH_MANUAL_EXPLICIT_LOGIN_CONFIRMATION_REQUIRED')
    parent = current.root_token(p, b, stops)
    directory = p.path / 'bridge/current-page-recoveries' / parent
    refs = {}
    for field, filename in [('original_attempt', 'attempt.json'), ('original_failure', 'failure.json')]:
        ref = value.get(field, {})
        require(ref.get('file') == str(directory / filename)
                and re.fullmatch('[a-f0-9]{64}', str(ref.get('sha256') or '')),
                'SEARCH_MANUAL_ROOT_REFERENCE_REQUIRED')
        refs[field] = nr.read_json(ref['file'], ref['sha256'])[0]
    attempt, failure = refs['original_attempt'], refs['original_failure']
    require(attempt.get('kind') == KIND and attempt.get('state') == 'started'
            and attempt.get('probe_id') == parent and not attempt.get('manual_restoration')
            and not (directory / 'probe.json').exists(), 'SEARCH_MANUAL_FAILED_ROOT_REQUIRED')
    require(attempt.get('binding_name') == b['binding_name'] and attempt.get('task_id') == task_id
            and attempt.get('authority') == current.projection(b)
            and attempt['binding_snapshot'].get('last_error') == b.get('last_error'),
            'SEARCH_MANUAL_ORIGINAL_CONTEXT_CHANGED')
    scope, failure_at = context(p, b, attempt['binding_snapshot'], task_id,
                                attempt['search_scope']['failure_sources'])
    require(scope == attempt.get('search_scope'), 'SEARCH_MANUAL_ORIGINAL_SCOPE_CHANGED')
    require(set(attempt['stops']) == set(stops)
            and all(attempt['stops'][k]['sha256'] == stop['sha256'] for k, stop in stops.items()),
            'SEARCH_MANUAL_STOP_CHANGED')
    require(failure.get('attempt_consumed') is True and failure.get('no_automatic_retry') is True
            and failure.get('code') == 'ACCESS_STOP' and failure.get('failed_stage') == 'identity'
            and set(failure.get('observations', {})) == {'metadata'},
            'SEARCH_MANUAL_IDENTITY_ACCESS_FAILURE_REQUIRED')
    meta_ref = failure['observations']['metadata']
    require(meta_ref.get('file') == str(directory / 'metadata.json'), 'SEARCH_MANUAL_ROOT_METADATA_REQUIRED')
    metadata = nr.check_ref(p, meta_ref)
    require(metadata.get('stage') == 'metadata'
            and metadata.get('result', {}).get('page') == b['tabs']['search']['page'],
            'SEARCH_MANUAL_ORIGINAL_METADATA_CHANGED')
    same_url(metadata['result'].get('url'), scope['url'])
    require(nr.stamp(attempt.get('started_at')) <= nr.stamp(metadata.get('observed_at'))
            <= nr.stamp(failure.get('at')), 'SEARCH_MANUAL_ROOT_METADATA_TIME_CHANGED')
    when = nr.stamp(value.get('processed_at'))
    require(when > nr.stamp(failure.get('at')) >= nr.stamp(attempt.get('started_at'))
            and all(when > nr.stamp(stop['at']) for stop in stops.values()),
            'SEARCH_MANUAL_CONFIRMATION_NOT_NEWER')
    token = xp.digest({'kind': MANUAL_KIND, 'original_probe_id': parent})
    return scope, failure_at, token, when, attempt


def probe(project, name, task_id, confirmation_file, authorization, driver=None, manual_restoration=None):
    require(isinstance(authorization, str) and authorization.strip(), 'SEARCH_RESEARCH_AUTHORIZATION_REQUIRED')
    p = xp.Project(project)
    owned = driver is None
    try:
        with xp.project_lock(project):
            b = bridge.load_binding(project, name)
            with nr.locked_stops(b['profile_guard_key'], b['account_uid']) as keys:
                bridge.assert_web_project_access(project, b['account_uid'])
                require(not (p.path / 'bridge/PAUSE').exists(), 'USER_PAUSED')
                stops = nr.current_stops(keys)
                manual = None
                if manual_restoration:
                    manual, _ = nr.read_json(manual_restoration)
                    scope, failure, token, manual_time, original = manual_context(p, b, task_id, stops, manual)
                else:
                    scope, failure = context(p, b, b, task_id)
                    token = current.root_token(p, b, stops)
                require(all(0 <= (nr.stamp(s['at']) - failure).total_seconds() <= 5 for s in stops.values()),
                        'SEARCH_STOP_NOT_FROM_THIS_FAILURE')
                user, _ = nr.read_json(confirmation_file)
                confirmed = confirmation(user, b, task_id, stops)
                if manual:
                    require(user == nr.check_ref(p, original['confirmation']), 'SEARCH_MANUAL_ORIGINAL_CONFIRMATION_CHANGED')
                    confirmed = max(confirmed, manual_time)
                current.historical_authority(p, b, keys)
                refs = current.authority_files(p, b)
                # Shared root_token means choosing another task/module/input cannot reset the opportunity.
                directory = p.path / 'bridge/current-page-recoveries' / token
                attempt = directory / 'attempt.json'
                require(not attempt.exists() and not (directory / 'manual-preflight.json').exists(),
                        'SEARCH_PROBE_ALREADY_ATTEMPTED')
                user_ref, _ = nr.freeze(p, confirmation_file)
                for stop in stops.values():
                    stop['archive'] = current.native.archive(p, stop['file'])[0]
                journal = {'kind': KIND, 'probe_id': token, 'state': 'started', 'binding_name': name,
                           'binding_snapshot': b, 'authority': current.projection(b), 'authority_files': refs,
                           'task_id': task_id, 'search_scope': scope, 'confirmation': user_ref,
                           'authorization': authorization, 'stops': stops, 'started_at': xp.legacy.now()}
                if manual:
                    manual_ref, _ = nr.freeze(p, manual_restoration)
                    journal['manual_restoration'] = {'authorization': manual_ref, **{
                        field: nr.freeze(p, manual[field]['file'])[0]
                        for field in ('original_attempt', 'original_failure')}}
                nr.immutable(attempt, journal)
                observations = {}
                stage = 'driver_initialization'
                try:
                    driver = driver or bridge.Driver(b['runtime'])
                    def observed(action, **kwargs):
                        nonlocal stage
                        stage = action
                        result = driver.call(action, b, 'search', **kwargs)
                        file = directory / (action + '.json')
                        nr.immutable(file, {'stage': action, 'observed_at': xp.legacy.now(), 'result': result})
                        observations[action] = nr.freeze(p, file)[0]
                        return result
                    meta = observed('metadata')
                    require(meta.get('page') == b['tabs']['search']['page'], 'SEARCH_SAME_EXISTING_PAGE_REQUIRED')
                    same_url(meta.get('url'), scope['url'])
                    ident = observed('identity')
                    require(ident.get('page') == meta['page'] and ident.get('uid') == b['account_uid']
                            and not ident.get('error'), 'SEARCH_SAME_ACCOUNT_REQUIRED')
                    same_url(ident.get('url'), scope['url'])
                    capture = observed('capture', stage='search', query=scope['query'], expectedPath='/search_result')
                    validate_capture(b, scope, capture)
                    current.same_stops(stops, keys)
                    require(current.projection(bridge.load_binding(project, name)) == journal['authority'],
                            'SEARCH_BINDING_AUTHORITY_CHANGED')
                    observation = {'observed_at': xp.legacy.now(), 'method': 'same_existing_search_guarded_read_no_navigation',
                                   'page': meta['page'], 'metadata': meta, 'identity': ident, 'capture': capture,
                                   'stage_observations': observations, 'sort_verified': False, 'pagination_verified': False}
                    require(nr.stamp(observation['observed_at']) > confirmed, 'SEARCH_OBSERVATION_NOT_NEWER')
                    normal = directory / 'current-observation.json'
                    normal_sha = nr.immutable(normal, observation)
                    normal_ref, _ = nr.freeze(p, normal)
                    record = dict(journal, state='captured', observation=normal_ref)
                    final = directory / 'probe.json'
                    sha = nr.immutable(final, record)
                    return {'probe_id': token, 'probe_file': str(final), 'probe_sha256': sha, 'normal_file': str(normal),
                            'normal_sha256': normal_sha, 'review_required': True, 'collection_permitted': False,
                            'sort_verified': False, 'pagination_verified': False, 'stop_bytes_unchanged': True}
                except BaseException as exc:
                    nr.immutable(directory / 'failure.json', {'at': xp.legacy.now(), 'code': getattr(exc, 'code', type(exc).__name__),
                                 'attempt_consumed': True, 'no_automatic_retry': True, 'failed_stage': stage,
                                 'observations': observations})
                    raise
    finally:
        p.close()
        if owned and driver:
            driver.close()


def validated(p, b, probe_file, probe_sha, keys):
    require(re.fullmatch('[a-f0-9]{64}', str(probe_sha or '')), 'SEARCH_PROBE_HASH_REQUIRED')
    record, _ = nr.read_json(probe_file, probe_sha)
    require(record.get('kind') == KIND and record.get('state') == 'captured', 'SEARCH_CAPTURED_PROBE_REQUIRED')
    require(record.get('authority') == current.projection(b), 'SEARCH_BINDING_AUTHORITY_CHANGED')
    scope, _ = context(p, b, record['binding_snapshot'], record['task_id'], record['search_scope']['failure_sources'])
    require(record.get('search_scope') == scope, 'SEARCH_PROBE_SCOPE_CHANGED')
    token = current.root_token(p, record['binding_snapshot'], record['stops'])
    manual_time = None
    if record.get('manual_restoration'):
        proof = record['manual_restoration']
        manual = nr.check_ref(p, proof['authorization'])
        for field in ('original_attempt', 'original_failure'):
            nr.check_ref(p, proof[field])
            require({key: proof[field][key] for key in ('file', 'sha256')} == manual.get(field),
                    'SEARCH_MANUAL_SOURCE_CHANGED')
        original_scope, _, token, manual_time, _ = manual_context(
            p, record['binding_snapshot'], record['task_id'], record['stops'], manual)
        require(original_scope == scope, 'SEARCH_MANUAL_CHILD_SCOPE_CHANGED')
    require(record.get('probe_id') == token, 'SEARCH_PROBE_ID_CHANGED')
    expected = p.path / 'bridge/current-page-recoveries' / token / 'probe.json'
    require(Path(probe_file) == expected and not expected.with_name('failure.json').exists(), 'SEARCH_PROBE_NOT_BOUND')
    attempt, _ = nr.read_json(expected.with_name('attempt.json'))
    require(attempt.get('kind') == KIND and attempt.get('state') == 'started'
            and {k: v for k, v in record.items() if k not in {'state', 'observation'}} == {k: v for k, v in attempt.items() if k != 'state'},
            'SEARCH_ATTEMPT_RECORD_CHANGED')
    current.same_stops(record['stops'], keys)
    for stop in record['stops'].values():
        nr.read_json(p.path / stop['archive'], stop['sha256'])
    user = nr.check_ref(p, record['confirmation'])
    confirmed = confirmation(user, b, record['task_id'], record['stops'])
    if manual_time:
        confirmed = max(confirmed, manual_time)
    current.check_authority_refs(record['authority_files'])
    current.historical_authority(p, b, keys)
    normal = nr.check_ref(p, record['observation'])
    stages = normal.get('stage_observations', {})
    require(set(stages) == {'metadata', 'identity', 'capture'}, 'SEARCH_STAGE_OBSERVATIONS_REQUIRED')
    for field, ref in stages.items():
        observed = nr.check_ref(p, ref)
        require(observed.get('stage') == field and observed.get('result') == normal.get(field), 'SEARCH_STAGE_OBSERVATION_CHANGED')
    require(normal.get('method') == 'same_existing_search_guarded_read_no_navigation'
            and normal.get('page') == record['authority']['tabs']['search']['page']
            and normal.get('sort_verified') is False and normal.get('pagination_verified') is False,
            'SEARCH_NORMAL_OBSERVATION_CHANGED')
    for field in ('metadata', 'identity', 'capture'):
        same_url(normal[field].get('url'), scope['url'])
    require(normal['metadata'].get('page') == normal['page'] and normal['identity'].get('uid') == b['account_uid']
            and normal['identity'].get('page') == normal['page'] and not normal['identity'].get('error'),
            'SEARCH_NORMAL_ACCOUNT_CHANGED')
    validate_capture(b, scope, normal['capture'])
    require(nr.stamp(normal['observed_at']) > confirmed, 'SEARCH_OBSERVATION_NOT_NEWER')
    return record, normal


def review_matches(review, record):
    require(review.get('probe_id') == record['probe_id'] and review.get('normal_sha256') == record['observation']['sha256']
            and review.get('reader') == 'codex' and review.get('reviewed') is True
            and review.get('query_read') is True and review.get('search_cards_read') is True
            and review.get('body_read') is False and review.get('sort_verified') is False
            and review.get('pagination_verified') is False and review.get('summary') and review.get('positions'),
            'SEARCH_ACTUAL_CODEX_QUERY_AND_CARDS_REVIEW_REQUIRED')


def attest(project, name, probe_file, review_file):
    p = xp.Project(project)
    try:
        with xp.project_lock(project):
            b = bridge.load_binding(project, name)
            with nr.locked_stops(b['profile_guard_key'], b['account_uid']) as keys:
                record, sha = nr.read_json(probe_file)
                record, normal = validated(p, b, probe_file, sha, keys)
                review_ref, review = nr.freeze(p, review_file)
                review_matches(review, record)
                grant_path = Path(probe_file).with_name('grant.json')
                created = nr.read_json(grant_path)[0]['created_at'] if grant_path.exists() else xp.legacy.now()
                grant = {'kind': KIND, 'scope': 'rendered_public_web_only', 'probe_file': str(probe_file),
                         'probe_sha256': sha, 'probe_id': record['probe_id'], 'review': review_ref, 'created_at': created}
                grant_sha = nr.immutable(grant_path, grant)
                pointer = {'file': str(grant_path), 'sha256': grant_sha}
                duplicate = b.get(current.POINTER) == pointer
                if not duplicate:
                    require(b.get('state') == 'blocked' and b.get('last_error') == record['binding_snapshot']['last_error'],
                            'SEARCH_FAILURE_STATE_CHANGED')
                    b[current.POINTER] = pointer
                    b['state'] = 'ready'
                    b.setdefault('resolved_errors', []).append(b['last_error'])
                    b.pop('last_error', None)
                    b['last_recovery'] = {'kind': KIND, 'probe_id': record['probe_id'], 'at': created}
                    bridge.save_binding(project, name, b)
                return {'duplicate': duplicate, 'scope': 'rendered_public_web_only', 'grant_file': str(grant_path),
                        'stop_bytes_unchanged': True, 'api_and_cdn_still_stopped': True, 'no_tasks_completed': True,
                        'sort_verified': False, 'pagination_verified': False, 'normal_file': record['observation']['file']}
    finally:
        p.close()


def web_exceptions(p, b, keys, grant):
    require(grant.get('kind') == KIND and grant.get('scope') == 'rendered_public_web_only', 'SEARCH_TYPED_GRANT_REQUIRED')
    record, _ = validated(p, b, grant['probe_file'], grant['probe_sha256'], keys)
    require(grant.get('probe_id') == record['probe_id'], 'SEARCH_GRANT_PROBE_CHANGED')
    review_matches(nr.check_ref(p, grant['review']), record)
    bridge.assert_web_project_access(p.path, b['account_uid'])
    return {key: stop['sha256'] for key, stop in record['stops'].items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    q = sub.add_parser('probe')
    q.add_argument('--binding', required=True); q.add_argument('--task', required=True)
    q.add_argument('--confirmation', required=True, type=Path); q.add_argument('--authorization', required=True)
    q.add_argument('--manual-restoration', type=Path)
    q = sub.add_parser('attest')
    q.add_argument('--binding', required=True); q.add_argument('--probe', required=True, type=Path)
    q.add_argument('--review', required=True, type=Path)
    a = parser.parse_args()
    result = probe(a.project, a.binding, a.task, a.confirmation, a.authorization, manual_restoration=a.manual_restoration) if a.command == 'probe' else attest(a.project, a.binding, a.probe, a.review)
    print(xp.dumps({'ok': True, 'data': result}))


if __name__ == '__main__':
    try:
        main()
    except xp.legacy.ResearchError as exc:
        print(xp.dumps({'ok': False, 'code': getattr(exc, 'code', 'STOP_ACTIVE'), 'reason': str(exc)}))
        raise SystemExit(1)
