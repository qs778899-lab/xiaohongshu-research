"""Prepare one new project home tab under a currently valid parent web lease.

Fresh manual-tab search, body and review attestation remain mandatory. Unknown
creation or inspection outcomes are never retried by changing project names.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

import xhs_bridge as bridge
import xhs_manual_tab_recovery as manual
import xhs_note_recovery as nr
import xhs_project as xp

HOME = 'https://www.xiaohongshu.com/explore'


def historical_account_record(ref, expected):
    """Read a consumed record at its original or registered migrated location.

    This resolver is for immutable historical preparation validation only. It
    does not refresh grants, change STOPs or permit a new browser operation.
    """
    import xhs_account_state as state
    expected = Path(expected).absolute()
    root = Path(xp.legacy.account_root()).absolute()
    # Validate the lexical location before pointer() resolves paths. A symlink
    # must never turn an old reference into an apparent exact-current match.
    nr.require(not root.is_symlink() and root == root.resolve()
               and expected.parent.parent == root
               and expected.parent.name in {'project-tab-preparations', 'manual-tab-attempts'}
               and expected.suffix == '.json' and expected.is_file()
               and not expected.is_symlink() and not expected.parent.is_symlink(),
               'PROJECT_TAB_MIGRATION_RECORD_CHANGED')
    current = manual.pointer(expected)
    if ref == current:
        return manual.check_pointer(ref)
    root = state.secure_directory(root)
    manifest = root / 'state-migration.json'
    nr.require(not manifest.is_symlink() and manifest.is_file(), 'PROJECT_TAB_MIGRATION_REQUIRED')
    value, _ = nr.read_json(manifest)
    nr.require(value.get('format') == 1 and value.get('destination') == str(root)
               and value.get('collection_permitted') is False
               and value.get('fresh_web_validation_required') is True
               and value.get('source_unchanged') is True
               and value.get('api_and_media_restrictions_preserved') is True,
               'PROJECT_TAB_MIGRATION_CHANGED')
    relative = expected.relative_to(root)
    source = Path(value['source'])
    nr.require(source.is_absolute() and source.resolve() != root
               and Path(ref.get('file', '')).resolve() == (source / relative).resolve()
               and not Path(ref['file']).is_symlink(), 'PROJECT_TAB_MIGRATION_SOURCE_CHANGED')
    digest = current['sha256']
    nr.require(ref.get('sha256') == digest
               and value.get('files', {}).get(str(relative)) == digest
               and value.get('source_files', {}).get(str(relative)) == digest,
               'PROJECT_TAB_MIGRATION_RECORD_CHANGED')
    # A still-present original must also retain the exact registered bytes.
    if Path(ref['file']).exists(): manual.check_pointer(ref)
    return manual.check_pointer(current)


def archived_probe(probe_ref):
    """Validate durable history after a temporary reservation was removed.

    This proof never authorizes collection. Current bindings continue to use
    manual.validated_probe, which requires the original reservation as before.
    """
    probe = manual.check_pointer(probe_ref)
    inspected = manual.check_pointer(probe['inspection'])
    attempt = manual.check_pointer(inspected['attempt'])
    reservation = attempt.get('account_attempt', {})
    if not reservation:
        return manual.validated_probe(probe_ref)
    parent = manual.check_pointer(attempt['parent'])
    key = xp.digest({'stops': attempt['stops'], 'context': parent['context_id'],
                     'tabId': attempt['target']['tabId']})
    expected = xp.legacy.account_root() / 'manual-tab-attempts' / (key + '.json')
    migrated = Path(reservation['file']).resolve() != expected.resolve()
    if migrated:
        reserved = historical_account_record(reservation, expected)
        nr.require(reserved.get('project') == attempt['project']
                   and reserved.get('parent_project') == attempt.get('parent_project', attempt['project'])
                   and reserved.get('context_id') == parent['context_id']
                   and reserved.get('tab_id') == attempt['target']['tabId']
                   and reserved.get('stops') == attempt['stops'], 'MANUAL_ATTEMPT_RESERVATION_CHANGED')
    elif Path(reservation['file']).exists():
        return manual.validated_probe(probe_ref)
    nr.require((migrated or Path(reservation['file']).resolve() == expected.resolve())
               and not Path(reservation['file']).is_symlink()
               and isinstance(reservation.get('sha256'), str)
               and len(reservation['sha256']) == 64
               and set(reservation['sha256']) <= set('0123456789abcdef'),
               'PROJECT_TAB_ARCHIVED_RESERVATION_INVALID')
    # Continuations have additional proofs; do not silently broaden this path.
    nr.require(inspected.get('initial_inspection') and not inspected.get('verified_search_continuation'),
               'PROJECT_TAB_ARCHIVED_DIRECT_SEARCH_REQUIRED')
    initial = manual.check_pointer(inspected['initial_inspection'])
    nr.require(not (Path(inspected['initial_inspection']['file']).parent /
                    'verified-search-continuation.json').exists(), 'PROJECT_TAB_ARCHIVED_DIRECT_SEARCH_REQUIRED')
    nr.require(probe.get('kind') == manual.KIND and initial['attempt'] == inspected['attempt'],
               'MANUAL_ATTEMPT_CHANGED')
    user = manual.check_pointer(attempt['user'])
    nr.require(probe['binding'].get('project') == attempt['project'] == initial['binding'].get('project')
               and attempt.get('parent_project', attempt['project']) == parent.get('project'),
               'MANUAL_DESTINATION_PROJECT_CHANGED')
    for field in ('context_id', 'account_uid', 'profile_guard_key', 'profile_label', 'runtime'):
        nr.require(probe['binding'].get(field) == parent.get(field), 'MANUAL_PARENT_IDENTITY_CHANGED')
    nr.require(user.get('user_statement') and user.get('normal_other_posts_statement'),
               'MANUAL_USER_RESTORATION_REQUIRED')
    nr.require(all(nr.stamp(user['processed_at']) > nr.stamp(s['at']) for s in attempt['stops'].values()),
               'MANUAL_CONFIRMATION_PREDATES_STOP')
    search = manual.check_pointer(inspected['capture'])
    nr.require(manual.proof_args(search['url'])['stage'] == 'search', 'MANUAL_ACTUAL_SEARCH_REQUIRED')
    manual.require_capture(search, probe['binding'], manual.proof_args(search['url']))
    capture = manual.check_pointer(probe['capture'])
    nr.require(manual.note_id(capture['url']), 'MANUAL_NOTE_CONTENT_REQUIRED')
    manual.require_capture(capture, probe['binding'], manual.proof_args(capture['url']))
    nr.require(manual.note_id(capture['url']) in {manual.note_id(c['url']) for c in search['cards']},
               'MANUAL_NOTE_NOT_IN_SEARCH')
    original = parent.get('tabs', {}).get('detail', {}).get('navigation_intent', {}).get('url')
    nr.require(probe.get('excluded_note_id') == (manual.note_id(original) if original else None)
               and manual.note_id(capture['url']) not in manual.excluded_notes(parent),
               'ORIGINAL_RESTRICTED_NOTE_EXCLUDED')
    for ref in attempt['frozen_stops'].values(): manual.check_pointer(ref)
    return probe, attempt, capture


def create_home(parent, meta):
    # Driver construction verifies the pinned runtime before this entry is used.
    runtime = parent['runtime']
    env = {k:v for k,v in os.environ.items() if not k.startswith('OPENCLI_')}
    env.update(OPENCLI_CONFIG_DIR=runtime['config_dir'], OPENCLI_CACHE_DIR=runtime['cache_dir'])
    cfg = {'packageRoot':runtime['package_root'], 'contextId':parent['context_id'],
           'extensionVersion':bridge.effective_extension_version(parent),
           'chromeTabId':parent['tabs']['detail']['chrome_tab_id'],
           'windowId':parent['tabs']['detail']['window_id'], 'expectedUrl':meta['url']}
    try:
        result = subprocess.run([runtime['node'],str(Path(__file__).with_suffix('.mjs'))],
            input=json.dumps(cfg),text=True,capture_output=True,env=env,timeout=45)
        packet=json.loads(result.stdout)
    except (subprocess.TimeoutExpired,ValueError,OSError):
        raise bridge.BridgeError('PROJECT_TAB_CREATE_OUTCOME_UNKNOWN') from None
    if not packet.get('ok'):
        raise bridge.BridgeError(packet.get('error','PROJECT_TAB_CREATE_OUTCOME_UNKNOWN'))
    return packet['data']


def validate_preparation_archive(project, b, grant, allow_missing_temporary_reservation=False):
    """Verify an archived preparation, without granting current web access.

    A completed preparation remains a historical fact after collector code
    changes. Its immutable search, note and review chain must still validate,
    and all other authority and STOP checks remain in force. Current access is
    checked separately by prepare's parent lease; never use this function as a
    substitute for manual.web_exceptions.
    """
    nr.require(grant.get('kind') == manual.GRANT_KIND
               and grant.get('scope') == 'rendered_public_web_only',
               'MANUAL_GRANT_SCOPE_CHANGED')
    nr.require(Path(project).resolve() == Path(b['project']).resolve(), 'MANUAL_PROJECT_CHANGED')
    nr.require(grant.get('authority') == manual.authority(b), 'MANUAL_AUTHORITY_CHANGED')
    fingerprint = grant.get('code_fingerprint')
    nr.require(isinstance(fingerprint, dict) and set(fingerprint) == {
        'xhs_manual_tab_recovery.py', 'xhs_bridge.py',
        'xhs_bridge_driver.mjs', 'xhs_note_recovery.py'}
        and all(isinstance(value, str) and len(value) == 64
                and set(value) <= set('0123456789abcdef') for value in fingerprint.values()),
        'PROJECT_TAB_HISTORICAL_FINGERPRINT_INVALID')
    nr.require(b.get('transport_surface') == 'browser' and b.get('manual_single_tab') is True,
               'MANUAL_TRANSPORT_CHANGED')
    nr.require(set(b.get('tabs', {})) == {'search', 'detail'}
               and all(s.get('owned') is False for s in b['tabs'].values()),
               'MANUAL_USER_TAB_OWNERSHIP_CHANGED')
    reader = archived_probe if allow_missing_temporary_reservation else manual.validated_probe
    probe, attempt, _ = reader(grant['probe'])
    manual.validate_review(manual.check_pointer(grant['review']), probe)
    nr.require(grant['stops'] == attempt['stops'], 'MANUAL_STOP_CHANGED')
    expected = dict(probe['binding'], binding_name=b['binding_name'],
                    manual_excluded_note_ids=b['manual_excluded_note_ids'])
    nr.require(manual.authority(expected) == manual.authority(b), 'MANUAL_PROBE_BINDING_CHANGED')
    return probe


def validate_historical_preparation(project, b, grant, keys):
    probe = validate_preparation_archive(project, b, grant)
    nr.require(grant['stops'] == nr.current_stops(keys), 'MANUAL_STOP_CHANGED')
    return probe


def renewal_archive(project, inspection_file, parent):
    """Completed old access is history only; a current parent lease is mandatory."""
    inspection_file = Path(inspection_file).resolve()
    nr.require(inspection_file.parent.parent == project / 'bridge' / 'manual-tab-recoveries'
               and inspection_file.name == 'inspection.json', 'PROJECT_TAB_RENEWAL_LOCATION_CHANGED')
    inspected = manual.check_pointer(manual.pointer(inspection_file))
    grant_file = inspection_file.parent / 'grant.json'
    nr.require(grant_file.is_file(), 'PROJECT_TAB_FRESH_ATTESTATION_REQUIRED')
    grant = manual.check_pointer(manual.pointer(grant_file))
    b = bridge.load_binding(project, grant['authority']['binding_name'])
    nr.require(b.get('manual_tab_revalidation') == manual.pointer(grant_file), 'PROJECT_TAB_GRANT_CHANGED')
    probe = validate_preparation_archive(project, b, grant, allow_missing_temporary_reservation=True)
    search = manual.check_pointer(probe['inspection'])
    nr.require(search.get('initial_inspection') == manual.pointer(inspection_file),
               'PROJECT_TAB_INSPECTION_CHANGED')
    nr.require(b['tabs']['detail']['page'] == inspected['binding']['tabs']['detail']['page'],
               'PROJECT_TAB_ATTESTATION_TARGET_CHANGED')
    nr.require(b['account_uid'] == parent['account_uid'] and b['context_id'] == parent['context_id'],
               'PROJECT_TAB_RENEWAL_ACCOUNT_CHANGED')
    return {'inspection': manual.pointer(inspection_file), 'grant': manual.pointer(grant_file)}


def completed_preparation(record, keys, historical_only=False):
    """Require an intact historical search, note and review for the old tab."""
    done = record.with_suffix('.done.json')
    nr.require(done.exists(), 'PROJECT_TAB_PRIOR_OUTCOME_REQUIRES_DIAGNOSIS')
    result, _ = nr.read_json(done)
    inspected = manual.check_pointer(result['inspection'])
    grant_file = Path(result['inspection']['file']).parent / 'grant.json'
    if not grant_file.exists() and record.with_suffix('.replacement.json').exists():
        marker,_=nr.read_json(record.with_suffix('.replacement.json'))
        replacement=manual.check_pointer(marker['intent'])
        nr.require(replacement.get('inspection')==result['inspection'], 'PROJECT_TAB_REPLACEMENT_CHANGED')
        if historical_only:
            historical_account_record(replacement.get('original_record', {}), record)
        else:
            nr.require(replacement.get('original_record')==manual.pointer(record), 'PROJECT_TAB_REPLACEMENT_CHANGED')
        _,attempt,_,checked_record,folder=missing_presearch_archive(
            inspected['binding']['project'],result['inspection']['file'],historical_only=historical_only)
        nr.require(checked_record==record and replacement['stops']==attempt['stops']
                   and (historical_only or replacement['stops']==nr.current_stops(keys)),
                   'PROJECT_TAB_REPLACEMENT_CHANGED')
        replacement_folder=folder/'presearch-home-replacement'
        nr.require(Path(marker['intent']['file'])==replacement_folder/'intent.json', 'PROJECT_TAB_REPLACEMENT_CHANGED')
        result,_=nr.read_json(replacement_folder/'done.json')
        nr.require(result.get('intent')==marker['intent'], 'PROJECT_TAB_REPLACEMENT_CHANGED')
        inspected=manual.check_pointer(result['inspection'])
        new_attempt=manual.check_pointer(inspected['attempt'])
        new_user=manual.check_pointer(new_attempt['user'])
        nr.require(new_user.get('presearch_replacement')==marker['intent'], 'PROJECT_TAB_REPLACEMENT_CHANGED')
        grant_file=Path(result['inspection']['file']).parent/'grant.json'
    nr.require(grant_file.exists(), 'PROJECT_TAB_FRESH_ATTESTATION_REQUIRED')
    grant, _ = nr.read_json(grant_file)
    b = bridge.load_binding(inspected['binding']['project'], grant['authority']['binding_name'])
    nr.require(b.get('manual_tab_revalidation') == manual.pointer(grant_file), 'PROJECT_TAB_GRANT_CHANGED')
    nr.require(b['tabs']['detail']['page'] == inspected['binding']['tabs']['detail']['page'],
               'PROJECT_TAB_ATTESTATION_TARGET_CHANGED')
    probe = (validate_preparation_archive(inspected['binding']['project'], b, grant,
                allow_missing_temporary_reservation=True) if historical_only else
             validate_historical_preparation(inspected['binding']['project'], b, grant, keys))
    search = manual.check_pointer(probe['inspection'])
    nr.require(search.get('initial_inspection') == result['inspection'], 'PROJECT_TAB_INSPECTION_CHANGED')


def prepare(project, parent_project, binding, authorization, creator=create_home, renew_from_inspection=None):
    project=Path(project).resolve(); parent_project=Path(parent_project).resolve()
    xp.legacy.research_path(project)
    nr.require(project!=parent_project,'DISTINCT_PROJECT_REQUIRED')
    nr.require(isinstance(authorization,str) and authorization.strip(),'AUTHORIZATION_BASIS_REQUIRED')
    parent=bridge.load_binding(parent_project,binding)
    slot=parent.get('tabs',{}).get('detail',{})
    nr.require(type(slot.get('chrome_tab_id')) is int and type(slot.get('window_id')) is int,
               'REGISTERED_NATIVE_TAB_REQUIRED')
    # A new project is not an access-recovery mechanism. Parent authorization,
    # STOP bytes and live identity must all remain valid before creating a tab.
    with bridge.lease(parent_project,parent) as keys:
        stops=nr.current_stops(keys)
        renewal = renewal_archive(project, renew_from_inspection, parent) if renew_from_inspection else None
        root=xp.legacy.account_root()/'project-tab-preparations'
        root.mkdir(parents=True,exist_ok=True)
        identity = {'context':parent['context_id'],'project':str(project)}
        if renewal: identity['renewal'] = renewal
        key=xp.digest(identity)
        record=root/(key+'.json')
        nr.require(not record.exists(),'PROJECT_TAB_ALREADY_ATTEMPTED')
        folder=(project/'bridge'/'project-tab-renewals'/key if renewal
                else project/'bridge'/'project-tab-preparation')
        if renewal:
            nr.require(not (folder/'intent.json').exists(), 'PROJECT_TAB_ALREADY_ATTEMPTED')
        for old in root.glob('*.json'):
            if old.name.endswith(('.done.json', '.replacement.json')):continue
            value,_=nr.read_json(old)
            prior_parent = manual.check_pointer(value['parent'])
            if (value.get('context_id')==parent['context_id']
                    or prior_parent.get('account_uid')==parent['account_uid']):
                completed_preparation(old, keys, historical_only=True)
        driver=bridge.Driver(parent['runtime'])
        try:
            meta=driver.call('metadata',parent,'detail')
            ident=driver.call('identity',parent,'detail')
            nr.require(meta.get('page')==slot['page'] and ident.get('page')==slot['page']
                       and ident.get('uid')==parent['account_uid'] and ident.get('url')==meta.get('url'),
                       'PROJECT_TAB_PARENT_CHANGED')
            bridge.safe_url(meta['url'])
            nr.require(nr.current_stops(keys)==stops,'PROJECT_TAB_STOP_CHANGED')
            folder.mkdir(parents=True,exist_ok=True)
            intent={'context_id':parent['context_id'],'account_uid':parent['account_uid'],'project':str(project),'parent_project':str(parent_project),
                    'parent_binding':binding,'authorization':authorization,'at':xp.legacy.now(),
                    'stops':stops,'parent':manual.evidence(folder/'parent.json',parent),
                    'identity':manual.evidence(folder/'parent-identity.json',ident)}
            if renewal: intent['renewal'] = renewal
            manual.evidence(record,intent)
            manual.evidence(folder/'intent.json',intent)
            created=creator(parent,meta)
            manual.evidence(folder/'created.json',created)
            nr.require(created.get('url')==HOME and created.get('active') is True
                       and type(created.get('tabId')) is int and created['tabId']!=slot['chrome_tab_id']
                       and created.get('windowId')==slot['window_id'],'PROJECT_TAB_CREATE_MISMATCH')
            nr.require(nr.current_stops(keys)==stops,'PROJECT_TAB_STOP_CHANGED')
            nr.require(bridge.load_binding(parent_project,binding)==parent,'PROJECT_TAB_PARENT_CHANGED')
            user={'user_statement':authorization,'processed_at':xp.legacy.now(),
                  'normal_other_posts_statement':'Tool verified the original public page identity before creating this home tab; new research access remains unverified.',
                  'statement_origin':'automated_observation_not_user_restoration_claim',
                  'preparation':manual.pointer(folder/'intent.json')}
            manual.evidence(folder/'request.json',user)
        except bridge.BridgeError as exc:
            if exc.code == 'ACCESS_STOP':
                bridge.identity.stop_all(keys, 'Observed browser login/verification/access restriction')
            raise
        finally:driver.close()
    # inspect obtains its own account lock and does not inherit parent access.
    try:
        result=manual.inspect(project,binding,folder/'created.json',folder/'request.json',parent_project)
    except bridge.BridgeError as exc:
        if exc.code == 'ACCESS_STOP':
            with nr.locked_stops(parent['profile_guard_key'], parent['account_uid']) as keys:
                bridge.identity.stop_all(keys, 'Observed browser login/verification/access restriction')
        raise
    manual.evidence(record.with_suffix('.done.json'),{'inspection':manual.pointer(result['inspection']),
                    'collection_permitted':False,'at':xp.legacy.now()})
    return dict(result,new_project_tab=True,fresh_search_and_body_required=True)


def missing_presearch_archive(project, inspection_file, historical_only=False):
    """Validate a completed home inspection whose first metadata read failed.

    No search action may have reached the page. This is not permission to
    retry a navigation, verification failure, or unknown creation result.
    """
    project = Path(project).resolve(); inspection_file = Path(inspection_file).resolve()
    root = inspection_file.parent
    nr.require(root.parent == project / 'bridge/manual-tab-recoveries'
               and inspection_file.name == 'inspection.json', 'PROJECT_TAB_REPLACEMENT_LOCATION_CHANGED')
    inspected = manual.check_pointer(manual.pointer(inspection_file))
    attempt = manual.check_pointer(inspected['attempt'])
    parent = manual.check_pointer(attempt['parent'])
    user = manual.check_pointer(attempt['user'])
    target = manual.active_metadata(manual.check_pointer(attempt['metadata']))
    candidate = inspected['binding']; loaded = manual.check_pointer(inspected['capture'])
    nr.require(inspected.get('kind') == manual.KIND and not inspected.get('initial_inspection')
               and inspected.get('collection_permitted') is False
               and attempt['project'] == str(project) == candidate['project']
               and target == attempt['target'] and target['url'] == HOME
               and attempt['args'] == manual.proof_args(HOME), 'PROJECT_TAB_ORIGINAL_HOME_REQUIRED')
    manual.require_capture(loaded, candidate, manual.proof_args(HOME))
    nr.require(loaded.get('url') == HOME, 'PROJECT_TAB_ORIGINAL_HOME_REQUIRED')
    for field in ('context_id', 'account_uid', 'profile_guard_key', 'profile_label', 'runtime'):
        nr.require(candidate.get(field) == parent.get(field), 'PROJECT_TAB_PARENT_CHANGED')
    key = xp.digest({'stops': attempt['stops'], 'context': parent['context_id'], 'tabId': target['tabId']})
    nr.require(root.name == key, 'MANUAL_ATTEMPT_LOCATION_CHANGED')
    reservation_file = xp.legacy.account_root() / 'manual-tab-attempts' / (key + '.json')
    if historical_only:
        reservation = historical_account_record(attempt['account_attempt'], reservation_file)
    else:
        nr.require(Path(attempt['account_attempt']['file']).resolve() == reservation_file.resolve(),
                   'MANUAL_RESERVATION_LOCATION_CHANGED')
        reservation = manual.check_pointer(attempt['account_attempt'])
    nr.require(reservation.get('project') == str(project) and reservation.get('stops') == attempt['stops']
               and reservation.get('tab_id') == target['tabId']
               and reservation.get('context_id') == parent['context_id'], 'MANUAL_ATTEMPT_CHANGED')
    for ref in attempt['frozen_stops'].values(): manual.check_pointer(ref)
    adopted, _ = nr.read_json(root / 'adopted.json')
    expected=json.loads(json.dumps(attempt['candidate']))
    for slot in expected['tabs'].values(): slot['page']=adopted.get('page')
    nr.require(candidate==expected and candidate['session']=='xhs-manual-'+key[:24]
               and candidate['binding_name']=='manual-'+key[:24], 'MANUAL_CANDIDATE_CHANGED')
    nr.require(set(candidate.get('tabs', {})) == {'search', 'detail'}
               and candidate.get('transport_surface') == 'browser' and candidate.get('manual_single_tab') is True,
               'MANUAL_TRANSPORT_CHANGED')
    for slot in candidate['tabs'].values():
        nr.require(slot.get('chrome_tab_id') == target['tabId'] and slot.get('window_id') == target['windowId']
                   and slot.get('page') == adopted.get('page') == loaded.get('page')
                   and slot.get('last_url') == HOME and slot.get('owned') is False,
                   'PROJECT_TAB_ATTESTATION_TARGET_CHANGED')
    intent_ref = user.get('preparation', {})
    intent = manual.check_pointer(intent_ref)
    folder = Path(intent_ref['file']).parent
    nr.require(Path(intent_ref['file']).name == 'intent.json'
               and (folder == project / 'bridge/project-tab-preparation'
                    or folder.parent == project / 'bridge/project-tab-renewals'),
               'PROJECT_TAB_ORIGINAL_PREPARATION_REQUIRED')
    nr.require(intent['project'] == str(project) and intent['stops'] == attempt['stops']
               and manual.check_pointer(intent['parent']) == parent
               and intent['authorization'] == user.get('user_statement'), 'PROJECT_TAB_PREPARATION_CHANGED')
    created, _ = nr.read_json(folder / 'created.json')
    nr.require(created == target, 'PROJECT_TAB_PREPARATION_CHANGED')
    record_key = {'context': parent['context_id'], 'project': str(project)}
    if intent.get('renewal'): record_key['renewal'] = intent['renewal']
    record = xp.legacy.account_root() / 'project-tab-preparations' / (xp.digest(record_key) + '.json')
    registered, _ = nr.read_json(record)
    done, _ = nr.read_json(record.with_suffix('.done.json'))
    nr.require(registered == intent and done.get('inspection') == manual.pointer(inspection_file),
               'PROJECT_TAB_PREPARATION_CHANGED')
    failure, _ = nr.read_json(root / 'search-failure.json')
    search_attempt, _ = nr.read_json(root / 'search-attempt.json')
    nr.require(failure.get('error') == 'TARGET_MISSING' and failure.get('collection_permitted') is False
               and search_attempt.get('inspection') == manual.pointer(inspection_file)
               and search_attempt.get('scope') == 'one_existing_query_health_check_only'
               and nr.stamp(failure['at']) >= nr.stamp(search_attempt['at']) >= nr.stamp(inspected['at']),
               'PROJECT_TAB_PRESEARCH_MISSING_REQUIRED')
    forbidden = ('search-before.json', 'search-navigation.json', 'search-probe.json', 'note-attempt.json', 'grant.json')
    nr.require(not any((root / n).exists() for n in forbidden)
               and not list(root.glob('search-metadata-*.json'))
               and not list(root.glob('search-capture-*.json'))
               and not list(root.glob('*continuation*.json')), 'PROJECT_TAB_SEARCH_MAY_HAVE_PROGRESS')
    return inspected, attempt, parent, record, folder


def replace_missing_presearch_home(project, parent_project, binding, authorization, inspection_file, creator=create_home):
    """One replacement after exact absence, retaining the original reservation."""
    project = Path(project).resolve(); parent_project = Path(parent_project).resolve()
    xp.legacy.research_path(project)
    nr.require(project != parent_project, 'DISTINCT_PROJECT_REQUIRED')
    nr.require(isinstance(authorization, str) and authorization.strip(), 'AUTHORIZATION_BASIS_REQUIRED')
    inspected, attempt, original_parent, record, original_folder = missing_presearch_archive(project, inspection_file)
    parent = bridge.load_binding(parent_project, binding)
    for field in ('context_id', 'account_uid', 'profile_guard_key', 'profile_label', 'runtime'):
        nr.require(parent.get(field) == original_parent.get(field), 'PROJECT_TAB_PARENT_CHANGED')
    nr.require(str(parent_project) == attempt['parent_project'], 'PROJECT_TAB_PARENT_CHANGED')
    folder = original_folder / 'presearch-home-replacement'
    marker = record.with_suffix('.replacement.json')
    nr.require(not marker.exists() and not (folder / 'intent.json').exists(), 'PROJECT_TAB_REPLACEMENT_ALREADY_ATTEMPTED')
    with bridge.lease(parent_project, parent) as keys:
        nr.require(nr.current_stops(keys) == attempt['stops'], 'PROJECT_TAB_STOP_CHANGED')
        # No other unfinished preparation may be escaped by this continuation.
        for old in record.parent.glob('*.json'):
            if old == record or old.name.endswith(('.done.json', '.replacement.json')): continue
            value, _ = nr.read_json(old)
            old_parent = manual.check_pointer(value['parent'])
            if value.get('context_id') == parent['context_id'] or old_parent.get('account_uid') == parent['account_uid']:
                completed_preparation(old, keys, historical_only=True)
        driver = bridge.Driver(parent['runtime'])
        try:
            missing = inspected['binding']
            lease = driver.call('lease_status', missing, 'detail')
            nr.require(lease.get('matching') == 0 and lease.get('leaseTabCount') == 0,
                       'PROJECT_TAB_EXACT_ABSENCE_REQUIRED')
            absent = driver.call('probe_registered_target', missing, 'detail')
            nr.require(absent == {'state': 'registered_target_absent', 'page': missing['tabs']['detail']['page']},
                       'PROJECT_TAB_EXACT_ABSENCE_REQUIRED')
            meta = driver.call('metadata', parent, 'detail'); ident = driver.call('identity', parent, 'detail')
            slot = parent['tabs']['detail']
            nr.require(meta.get('page') == slot['page'] == ident.get('page')
                       and ident.get('uid') == parent['account_uid'] and ident.get('url') == meta.get('url'),
                       'PROJECT_TAB_PARENT_CHANGED')
            bridge.safe_url(meta['url'])
            nr.require(nr.current_stops(keys) == attempt['stops'], 'PROJECT_TAB_STOP_CHANGED')
            folder.mkdir(parents=True, exist_ok=True)
            intent = {'kind': 'project_tab_presearch_replacement_v1', 'at': xp.legacy.now(),
                      'inspection': manual.pointer(inspection_file), 'original_record': manual.pointer(record),
                      'stops': attempt['stops'], 'authorization': authorization,
                      'parent': manual.evidence(folder / 'parent.json', parent),
                      'exact_absence': absent, 'lease_status': lease, 'parent_identity': ident}
            # Both durable and shared-account records prevent a second creation.
            manual.evidence(folder / 'intent.json', intent)
            manual.evidence(marker, {'intent': manual.pointer(folder / 'intent.json')})
            created = creator(parent, meta)
            manual.evidence(folder / 'created.json', created)
            nr.require(created.get('url') == HOME and created.get('active') is True
                       and type(created.get('tabId')) is int
                       and created['tabId'] not in (slot['chrome_tab_id'], missing['tabs']['detail']['chrome_tab_id'])
                       and created.get('windowId') == slot['window_id'], 'PROJECT_TAB_CREATE_MISMATCH')
            nr.require(nr.current_stops(keys) == attempt['stops'], 'PROJECT_TAB_STOP_CHANGED')
            nr.require(bridge.load_binding(parent_project, binding) == parent, 'PROJECT_TAB_PARENT_CHANGED')
            request = {'user_statement': authorization, 'processed_at': xp.legacy.now(),
                       'normal_other_posts_statement': 'The current authorized parent page was actually verified; replacement access still needs search and body review.',
                       'statement_origin': 'automated_observation_not_user_restoration_claim',
                       'presearch_replacement': manual.pointer(folder / 'intent.json')}
            manual.evidence(folder / 'request.json', request)
        except bridge.BridgeError as exc:
            if exc.code == 'ACCESS_STOP': bridge.identity.stop_all(keys, 'Observed browser login/verification/access restriction')
            raise
        finally: driver.close()
    try:
        result = manual.inspect(project, binding, folder / 'created.json', folder / 'request.json', parent_project)
    except bridge.BridgeError as exc:
        if exc.code == 'ACCESS_STOP':
            with nr.locked_stops(parent['profile_guard_key'], parent['account_uid']) as keys:
                bridge.identity.stop_all(keys, 'Observed browser login/verification/access restriction')
        raise
    manual.evidence(folder / 'done.json', {'inspection': manual.pointer(result['inspection']),
                    'intent': manual.pointer(folder / 'intent.json'), 'collection_permitted': False, 'at': xp.legacy.now()})
    return dict(result, replacement_home=True, fresh_search_and_body_required=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,required=True);p.add_argument('--parent-project',type=Path,required=True)
    p.add_argument('--binding',required=True);p.add_argument('--authorization',required=True)
    p.add_argument('--renew-from-inspection',type=Path,
                   help='Completed archived home inspection of this destination; current parent access remains required.')
    p.add_argument('--replace-missing-before-search',type=Path,
                   help='Original completed home inspection whose first search metadata failed with exact target absence.')
    a=p.parse_args()
    if a.replace_missing_before_search:
        nr.require(not a.renew_from_inspection, 'PROJECT_TAB_REPLACEMENT_MODE_CONFLICT')
        result=replace_missing_presearch_home(a.project,a.parent_project,a.binding,a.authorization,a.replace_missing_before_search)
        print(json.dumps(result,ensure_ascii=False));return
    print(json.dumps(prepare(a.project,a.parent_project,a.binding,a.authorization,
                            renew_from_inspection=a.renew_from_inspection),ensure_ascii=False))

if __name__=='__main__':main()
