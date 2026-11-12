"""One-shot same-target rendered-page revalidation; never deletes STOP or navigates.

This is independent of the exact-300031/API/native recovery procedures. A probe
is not a research observation or grant until Codex reads and attests its capture.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlparse

import xhs_bridge as bridge
import xhs_note_recovery as nr
import xhs_native as native
import xhs_project as xp

KIND='same-target-current-web-revalidation_v1'
POINTER='current_page_revalidation'
MANUAL_KIND='same-target-manual-restoration_v1'
require=nr.require


def projection(b):
    fields=nr.fork_projection(b)
    fields.update({k:b.get(k) for k in ('binding_kind','web_parent_authorization','fork_home_verification','note_unavailable_recovery')})
    fields['tabs']={r:{k:s.get(k) for k in ('page','session','owned')} for r,s in b.get('tabs',{}).items()}
    return fields


def note(url):
    bridge.safe_url(url)
    path=bridge.target_path(urlparse(url).path)
    m=re.fullmatch('/explore/([a-f0-9]{24})',path)
    require(m is not None,'CURRENT_EXACT_NOTE_REQUIRED')
    return m[1]


def historical_direct_authority(p,b,keys):
    """The original direct proof, with only its old active STOP read archived.

    Keep the original source/context/normal-page/review semantics. A kind/scope
    marker alone is not authority. Current STOPs are independently bound by the
    new proof and never substituted into this historical proof.
    """
    require(not b.get('web_parent_authorization') and b.get('binding_kind')!=nr.FORK_KIND,
            'FORK_PARENT_MUST_HAVE_DIRECT_GRANT')
    pointer=b.get('note_unavailable_recovery',{})
    require(re.fullmatch(r'[a-f0-9]{64}',pointer.get('sha256','')),'RECOVERY_GRANT_HASH_REQUIRED')
    grant,_=nr.read_json(pointer.get('file',''),pointer.get('sha256'))
    require(grant.get('kind')==nr.KIND and grant.get('scope')=='rendered_public_web_only','RECOVERY_TYPED_GRANT_REQUIRED')
    require(re.fullmatch(r'[a-f0-9]{64}',str(grant.get('probe_sha256') or '')),'RECOVERY_PROBE_HASH_REQUIRED')
    record,_=nr.read_json(grant.get('probe_file'),grant['probe_sha256'])
    require(record.get('kind')==nr.KIND and record.get('state')=='captured','RECOVERY_CAPTURED_PROBE_REQUIRED')
    require(record.get('binding_name')==b['binding_name'],'RECOVERY_BINDING_MISMATCH')
    for key in ['context_id','account_uid','profile_guard_key']:
        require(record.get(key)==b.get(key),'RECOVERY_CURRENT_ACCOUNT_CHANGED')
    refs=record['sources'];sources={k:nr.check_ref(p,v) for k,v in refs.items()}
    ctx=nr.original_context(p,b,refs['original']['file']);target=nr.normal_target(b,refs['normal_target']['file'])
    require(record['failed_note']==ctx['note_id'] and target['target_note_id']!=ctx['note_id'],'RECOVERY_NOTE_SCOPE_MISMATCH')
    require(set(record['stops'])==set(keys),'RECOVERY_STOP_SET_CHANGED')
    historical={}
    for key,stop in record['stops'].items():
        archive=Path(stop['archive']);archive=archive if archive.is_absolute() else p.path/archive
        require(archive.resolve().is_relative_to(p.path.resolve()),'CURRENT_AUTHORITY_ARCHIVE_OUTSIDE_PROJECT')
        value,_=nr.read_json(archive,stop['sha256'])
        require(value.get('reason')==nr.STOP_REASON and value.get('channel')=='bridge' and value.get('credential_free') is True,
                'RECOVERY_UNRELATED_ACCOUNT_STOP')
        require(value.get('at')==stop.get('at'),'CURRENT_HISTORICAL_STOP_TIME_CHANGED')
        historical[key]={'file':stop['file'],'sha256':stop['sha256'],'at':value.get('at')}
    nr.stop_binding(historical,ctx);nr.validate_normal(b,target,sources['normal'],historical)
    review=nr.check_ref(p,grant['review'])
    require(review.get('probe_id')==record['probe_id'] and review.get('reviewed') is True
            and review.get('normal_sha256')==record['sources']['normal']['sha256'],'RECOVERY_REVIEW_BINDING_CHANGED')
    bridge.assert_web_project_access(p.path,b['account_uid'])
    return {key:stop['sha256'] for key,stop in historical.items()}


def historical_authority(p,b,keys):
    """Recheck the existing delegation graph on every new-proof use, offline."""
    if not (b.get('web_parent_authorization') or b.get('binding_kind')==nr.FORK_KIND
            or nr.fork_path(p.path,'names',b['binding_name']).exists()):
        return historical_direct_authority(p,b,keys)
    # Same semantics as fork_authority; only the parent's STOP is historical.
    require(b.get('binding_kind')==nr.FORK_KIND and not b.get('note_unavailable_recovery'),'FORK_TYPED_CHILD_REQUIRED')
    pointer=b.get('web_parent_authorization',{})
    record,sha=nr.read_json(pointer.get('file',''),pointer.get('sha256'))
    require(pointer.get('sha256')==sha and re.fullmatch(r'[a-f0-9]{64}',sha),'FORK_RECORD_HASH_REQUIRED')
    require(record.get('kind')==nr.FORK_KIND and record.get('scope')=='rendered_public_web_only'
            and record.get('project')==str(p.path.resolve()) and record.get('child_name')==b['binding_name'],'FORK_SCOPE_CHANGED')
    expected=nr.fork_path(p.path,'intents',record.get('intent_id'))
    require(Path(pointer['file'])==expected,'FORK_RECORD_PATH_CHANGED')
    index,_=nr.read_json(nr.fork_path(p.path,'names',b['binding_name']))
    require(index==pointer,'FORK_NAME_RECORD_CHANGED')
    require(nr.fork_projection(b)==record.get('child_projection'),'FORK_CHILD_AUTHORITY_CHANGED')
    parent=bridge.load_binding(p.path,record.get('parent_name'))
    require(not nr.fork_path(p.path,'names',parent['binding_name']).exists(),'FORK_RECURSION_FORBIDDEN')
    require(nr.fork_projection(parent)==record.get('parent_projection'),'FORK_PARENT_AUTHORITY_CHANGED')
    require(parent.get('note_unavailable_recovery')==record.get('parent_grant'),'FORK_PARENT_GRANT_CHANGED')
    require(all(b.get(k)==parent.get(k) for k in ('format','project','profile_label','context_id','account_uid',
                'profile_guard_key','runtime','extension_version','web_channel_authorization'))
            and b.get('binding_name')!=parent.get('binding_name') and b.get('session')!=parent.get('session'),
            'FORK_INHERITED_AUTHORITY_CHANGED')
    require(record.get('authorization')==b.get('authorization') and isinstance(b.get('authorization'),str)
            and b['authorization'].strip(),'FORK_AUTHORIZATION_BASIS_REQUIRED')
    bridge.web_authorization_exceptions(p.path,parent)
    stops=historical_direct_authority(p,parent,keys)
    require(stops==record.get('stop_hashes'),'FORK_STOP_SET_CHANGED')
    home,home_pointer=nr.completed_fork_home(p.path,expected,record,sha)
    require(b.get('fork_home_verification')==home_pointer,'FORK_HOME_RECORD_CHANGED')
    for role,proof in home['roles'].items():
        slot=b.get('tabs',{}).get(role,{})
        require(slot.get('owned') is True and slot.get('page')==proof.get('page') and slot.get('session')==proof.get('session')
                and proof.get('session')==b['session']+'-'+role,'FORK_HOME_TARGET_CHANGED')
    return stops


def authority_files(p,b):
    """Freeze the existing authorization graph, validating historical STOP archives.

    Old grants intentionally reference older STOPs: their immutable archive is
    checked, while the active STOP set is checked separately against this probe.
    """
    found={};seen=set()
    def add(file,sha):
        path=Path(file)
        require(path.is_absolute() and not path.is_symlink() and path.is_file(),'CURRENT_AUTHORITY_FILE_REQUIRED')
        raw=path.read_bytes();actual=hashlib.sha256(raw).hexdigest()
        require(actual==sha,'CURRENT_AUTHORITY_FILE_CHANGED')
        key=str(path)
        if key in found:require(found[key]==sha,'CURRENT_AUTHORITY_REFERENCE_CONFLICT')
        found[key]=sha
        if key in seen:return
        seen.add(key)
        try:walk(json.loads(raw))
        except (ValueError,UnicodeError):pass
    def walk(value):
        if isinstance(value,list):
            for v in value:walk(v)
        if not isinstance(value,dict):return
        archive=value.get('archive');sha=value.get('sha256')
        archived_stop=bool(archive and isinstance(value.get('file'),str) and Path(value['file']).name=='STOP.json')
        if archive and re.fullmatch('[a-f0-9]{64}',str(sha or '')):
            path=Path(archive);path=path if path.is_absolute() else p.path/path
            require(path.resolve().is_relative_to(p.path.resolve()),'CURRENT_AUTHORITY_ARCHIVE_OUTSIDE_PROJECT')
            add(path,sha)
        for fk,hk in [('file','sha256'),('source_file','source_sha256'),('probe_file','probe_sha256')]:
            if archived_stop and fk=='file':continue
            if value.get(fk) and re.fullmatch('[a-f0-9]{64}',str(value.get(hk) or '')):add(value[fk],value[hk])
        for v in value.values():
            if isinstance(v,(dict,list)):walk(v)
    authority=projection(b)
    # The existing rendered-web authority must be real, not a generic STOP exception.
    pointers=[b.get(k) for k in ('web_parent_authorization','note_unavailable_recovery') if b.get(k)]
    require(pointers,'CURRENT_EXISTING_RENDERED_AUTHORITY_REQUIRED')
    for pointer in pointers:
        require(re.fullmatch('[a-f0-9]{64}',str(pointer.get('sha256') or '')),'CURRENT_AUTHORITY_HASH_REQUIRED')
        value,_=nr.read_json(pointer.get('file'),pointer.get('sha256'))
        require(value.get('kind') in {nr.KIND,nr.FORK_KIND} and value.get('scope')=='rendered_public_web_only',
                'CURRENT_EXISTING_RENDERED_AUTHORITY_REQUIRED')
    walk(authority)
    if b.get('web_channel_authorization'):
        add(p.path/'bridge/web-channel-authorization.json',b['web_channel_authorization'])
    # Previous same-target grants are history, never reused for a different STOP.
    if b.get(POINTER):walk(b[POINTER])
    return [{'file':f,'sha256':h} for f,h in sorted(found.items())]


def check_authority_refs(refs):
    require(isinstance(refs,list) and refs,'CURRENT_AUTHORITY_REFERENCES_REQUIRED')
    for ref in refs:
        path=Path(ref.get('file',''))
        require(path.is_absolute() and path.is_file() and not path.is_symlink()
                and re.fullmatch('[a-f0-9]{64}',str(ref.get('sha256') or '')),'CURRENT_AUTHORITY_FILE_REQUIRED')
        require(hashlib.sha256(path.read_bytes()).hexdigest()==ref['sha256'],'CURRENT_AUTHORITY_FILE_CHANGED')


def context(p,b,snapshot,task_id):
    require(snapshot.get('state')=='blocked' and snapshot.get('last_error',{}).get('code')=='ACCESS_STOP',
            'CURRENT_BLOCKED_ACCESS_STOP_REQUIRED')
    require(snapshot.get('project')==str(p.path.resolve()) and snapshot.get('binding_name')==b.get('binding_name')
            and projection(snapshot)==projection(b),'CURRENT_BINDING_AUTHORITY_CHANGED')
    require(snapshot.get('authorization') and snapshot.get('context_id') and snapshot.get('account_uid'),
            'CURRENT_EXISTING_AUTHORIZATION_REQUIRED')
    slot=snapshot.get('tabs',{}).get('detail',{});intent=slot.get('navigation_intent',{})
    require(slot.get('owned') is True and slot.get('page') and slot.get('session'),'CURRENT_OWNED_DETAIL_REQUIRED')
    target=note(intent.get('url'))
    when=nr.stamp(snapshot['last_error']['at'])
    require(nr.stamp(intent.get('at'))<=when,'CURRENT_FAILURE_INTENT_TIME_REQUIRED')
    task=p.task(task_id)
    require(task.get('kind')=='native' and task['payload'].get('stage')=='body' and task['payload'].get('target')==target,
            'CURRENT_FAILED_BODY_TASK_REQUIRED')
    return target,when


def confirmation(value,target,stops):
    # Deliberately finite affirmative phrases: negation, hearsay and questions do not authorize a probe.
    statement=value.get('user_statement')
    affirmative={'正文和评论均正常','正文和评论都正常','正文、评论均正常','正文和评论正常'}
    require(value.get('target_note_id')==target and isinstance(statement,str)
            and statement.strip() in affirmative,'CURRENT_USER_NORMAL_PAGE_CONFIRMATION_REQUIRED')
    when=nr.stamp(value.get('processed_at'))
    require(all(when>nr.stamp(s['at']) for s in stops.values()),'CURRENT_USER_CONFIRMATION_NOT_NEWER')
    return when


def same_stops(expected,keys):
    current=nr.current_stops(keys)
    require(set(current)==set(expected) and all(current[k]['sha256']==expected[k]['sha256'] for k in current),
            'CURRENT_NEW_ACCOUNT_STOP')
    return current


def validate_capture(b,target,capture):
    require(not any(capture.get(k) for k in ('loginWall','securityBlock','notFound')),'CURRENT_CAPTURE_ACCESS_WARNING')
    require(capture.get('uid')==b['account_uid'] and not capture.get('error') and note(capture.get('url'))==target,
            'CURRENT_GUARDED_BODY_ACCOUNT_OR_TARGET_MISMATCH')
    require(isinstance(capture.get('title'),str) and capture['title'].strip()
            and isinstance(capture.get('body'),str) and capture['body'].strip(),'CURRENT_ACTUAL_BODY_AND_TITLE_REQUIRED')


def root_token(p,b,stops):
    return xp.digest({'binding':b['binding_name'],'project':str(p.path),'failure_at':b['last_error']['at'],
                      'stops':{k:s['sha256'] for k,s in stops.items()}})


def manual_context(p,b,task_id,target,stops,value):
    """One explicitly authorized child of a failed original probe, never a reset."""
    require(value.get('kind')==MANUAL_KIND and value.get('project')==str(p.path.resolve())
            and value.get('binding_name')==b['binding_name'] and value.get('target_note_id')==target,
            'CURRENT_MANUAL_SCOPE_REQUIRED')
    require(value.get('user_statement')=='已恢复原帖，正文评论正常，允许再次复核',
            'CURRENT_MANUAL_EXPLICIT_USER_CONFIRMATION_REQUIRED')
    parent=root_token(p,b,stops);directory=p.path/'bridge/current-page-recoveries'/parent
    refs={}
    for field,filename in [('original_attempt','attempt.json'),('original_failure','failure.json')]:
        ref=value.get(field,{})
        require(ref.get('file')==str(directory/filename) and re.fullmatch('[a-f0-9]{64}',str(ref.get('sha256') or '')),
                'CURRENT_MANUAL_ORIGINAL_REFERENCE_REQUIRED')
        refs[field]=nr.read_json(ref['file'],ref['sha256'])[0]
    attempt=refs['original_attempt'];failure=refs['original_failure']
    require(attempt.get('kind')==KIND and attempt.get('state')=='started' and attempt.get('probe_id')==parent
            and not attempt.get('manual_restoration') and not (directory/'probe.json').exists(),
            'CURRENT_MANUAL_FAILED_ORIGINAL_PROBE_REQUIRED')
    old_target,_=context(p,b,attempt['binding_snapshot'],task_id)
    require(attempt.get('binding_name')==b['binding_name'] and attempt.get('task_id')==task_id
            and old_target==target and attempt.get('target_note_id')==target
            and attempt.get('authority')==projection(b),'CURRENT_MANUAL_ORIGINAL_CONTEXT_CHANGED')
    require(set(attempt['stops'])==set(stops) and all(attempt['stops'][k]['sha256']==s['sha256'] for k,s in stops.items()),
            'CURRENT_MANUAL_STOP_CHANGED')
    require(failure.get('attempt_consumed') is True and failure.get('no_automatic_retry') is True,
            'CURRENT_MANUAL_ORIGINAL_FAILURE_REQUIRED')
    when=nr.stamp(value.get('processed_at'))
    require(when>nr.stamp(failure.get('at'))>=nr.stamp(attempt.get('started_at'))
            and all(when>nr.stamp(s['at']) for s in stops.values()),'CURRENT_MANUAL_CONFIRMATION_NOT_NEWER')
    # One child slot belongs to the original attempt, regardless of input hashes.
    # Changed evidence must never manufacture another preflight opportunity.
    token=xp.digest({'kind':MANUAL_KIND,'original_probe_id':parent})
    return token,when


def probe(project,name,task_id,confirmation_file,authorization,driver=None,manual_restoration=None):
    require(isinstance(authorization,str) and authorization.strip(),'CURRENT_RESEARCH_AUTHORIZATION_REQUIRED')
    p=xp.Project(project);owned=driver is None
    try:
        with xp.project_lock(project):
            b=bridge.load_binding(project,name)
            with nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
                bridge.assert_web_project_access(project,b['account_uid'])
                require(not (p.path/'bridge/PAUSE').exists(),'USER_PAUSED')
                target,failure=context(p,b,b,task_id);stops=nr.current_stops(keys)
                require(all(0<=(nr.stamp(s['at'])-failure).total_seconds()<=5 for s in stops.values()),
                        'CURRENT_STOP_NOT_FROM_THIS_FAILURE')
                user,user_sha=nr.read_json(confirmation_file);confirmed=confirmation(user,target,stops)
                historical_authority(p,b,keys);refs=authority_files(p,b)
                # STOP fingerprint and binding identity consume one attempt, independent of changing input files.
                token=root_token(p,b,stops);manual=None
                if manual_restoration:
                    manual,_=nr.read_json(manual_restoration)
                    token,confirmed_manual=manual_context(p,b,task_id,target,stops,manual)
                    confirmed=max(confirmed,confirmed_manual)
                directory=p.path/'bridge/current-page-recoveries'/token;attempt=directory/'attempt.json'
                require(not attempt.exists() and not (directory/'manual-preflight.json').exists(),
                        'CURRENT_PROBE_ALREADY_ATTEMPTED')
                user_ref,_=nr.freeze(p,confirmation_file)
                for stop in stops.values():stop['archive']=native.archive(p,stop['file'])[0]
                journal={'kind':KIND,'probe_id':token,'state':'started','binding_name':name,'binding_snapshot':b,
                    'authority':projection(b),'authority_files':refs,'task_id':task_id,'target_note_id':target,
                    'confirmation':user_ref,'authorization':authorization,'stops':stops,'started_at':xp.legacy.now()}
                if manual:
                    manual_ref,_=nr.freeze(p,manual_restoration)
                    prior={field:nr.freeze(p,manual[field]['file'])[0] for field in ('original_attempt','original_failure')}
                    journal['manual_restoration']={'authorization':manual_ref,**prior}
                    # Preflight itself is single use even if metadata fails or is interrupted.
                    nr.immutable(directory/'manual-preflight.json',journal)
                else:nr.immutable(attempt,journal)
                observations={};stage='driver_initialization'
                try:
                    driver=driver or bridge.Driver(b['runtime'])
                    def observed(action,**kwargs):
                        nonlocal stage
                        stage=action;result=driver.call(action,b,'detail',**kwargs)
                        file=directory/(action+'.json')
                        nr.immutable(file,{'stage':action,'observed_at':xp.legacy.now(),'result':result})
                        observations[action]=nr.freeze(p,file)[0]
                        return result
                    meta=observed('metadata')
                    require(meta.get('page')==b['tabs']['detail']['page'] and note(meta.get('url'))==target,
                            'CURRENT_SAME_EXISTING_TARGET_REQUIRED')
                    if manual:nr.immutable(attempt,journal) # Original target verified before consuming the child capture attempt.
                    ident=observed('identity')
                    require(ident.get('page')==meta['page'] and ident.get('uid')==b['account_uid'] and not ident.get('error') and note(ident.get('url'))==target,
                            'CURRENT_SAME_ACCOUNT_REQUIRED')
                    capture=observed('capture',stage='body',expectedPath='/explore/'+target)
                    validate_capture(b,target,capture)
                    same_stops(stops,keys)
                    require(projection(bridge.load_binding(project,name))==journal['authority'],'CURRENT_BINDING_AUTHORITY_CHANGED')
                    observation={'observed_at':xp.legacy.now(),'method':'same_existing_target_guarded_identity_body_no_navigation',
                        'page':meta['page'],'metadata':meta,'identity':ident,'capture':capture,'stage_observations':observations}
                    if manual:observation['manual_preflight']=nr.freeze(p,directory/'manual-preflight.json')[0]
                    require(nr.stamp(observation['observed_at'])>confirmed,'CURRENT_OBSERVATION_NOT_NEWER')
                    normal=directory/'current-observation.json';normal_sha=nr.immutable(normal,observation)
                    normal_ref,_=nr.freeze(p,normal)
                    record=dict(journal,state='captured',observation=normal_ref)
                    final=directory/'probe.json';sha=nr.immutable(final,record)
                    # Recording a probe does not grant or modify the binding.
                    return {'probe_id':token,'probe_file':str(final),'probe_sha256':sha,'normal_file':str(normal),
                        'normal_sha256':normal_sha,'review_required':True,'collection_permitted':False,
                        'stop_bytes_unchanged':True}
                except BaseException as exc:
                    nr.immutable(directory/'failure.json',{'at':xp.legacy.now(),'code':getattr(exc,'code',type(exc).__name__),
                        'attempt_consumed':True,'no_automatic_retry':True,'failed_stage':stage,'observations':observations})
                    raise
    finally:
        p.close()
        if owned and driver:driver.close()


def validated(p,b,probe_file,probe_sha,keys):
    require(re.fullmatch(r'[a-f0-9]{64}',str(probe_sha or '')),'CURRENT_PROBE_HASH_REQUIRED')
    record,_=nr.read_json(probe_file,probe_sha)
    require(record.get('kind')==KIND and record.get('state')=='captured','CURRENT_CAPTURED_PROBE_REQUIRED')
    require(record.get('authority')==projection(b),'CURRENT_BINDING_AUTHORITY_CHANGED')
    target,_=context(p,b,record['binding_snapshot'],record['task_id'])
    require(record['target_note_id']==target,'CURRENT_PROBE_TARGET_CHANGED')
    attempt_path=Path(probe_file).with_name('attempt.json');attempt,_=nr.read_json(attempt_path)
    require(attempt.get('probe_id')==record.get('probe_id') and attempt.get('kind')==KIND
            and {k:v for k,v in record.items() if k not in {'state','observation'}}=={k:v for k,v in attempt.items() if k!='state'},
            'CURRENT_ATTEMPT_RECORD_CHANGED')
    token=root_token(p,record['binding_snapshot'],record['stops'])
    manual_time=None
    if record.get('manual_restoration'):
        proof=record['manual_restoration'];manual=nr.check_ref(p,proof['authorization'])
        for field in ('original_attempt','original_failure'):
            nr.check_ref(p,proof[field])
            require({k:proof[field][k] for k in ('file','sha256')}==manual.get(field),'CURRENT_MANUAL_SOURCE_CHANGED')
        token,manual_time=manual_context(p,record['binding_snapshot'],record['task_id'],target,record['stops'],manual)
    require(record['probe_id']==token,'CURRENT_PROBE_ID_CHANGED')
    expected=p.path/'bridge/current-page-recoveries'/token/'probe.json'
    require(Path(probe_file)==expected and not expected.with_name('failure.json').exists(),'CURRENT_PROBE_NOT_BOUND')
    current=same_stops(record['stops'],keys)
    for stop in record['stops'].values():nr.read_json(p.path/stop['archive'],stop['sha256'])
    user=nr.check_ref(p,record['confirmation']);confirmed=confirmation(user,target,current)
    if manual_time:confirmed=max(confirmed,manual_time)
    check_authority_refs(record['authority_files'])
    historical_authority(p,b,keys)
    normal=nr.check_ref(p,record['observation'])
    stages=normal.get('stage_observations',{})
    require(set(stages)=={'metadata','identity','capture'},'CURRENT_STAGE_OBSERVATIONS_REQUIRED')
    for field,ref in stages.items():
        observed=nr.check_ref(p,ref)
        require(observed.get('stage')==field and observed.get('result')==normal.get(field),'CURRENT_STAGE_OBSERVATION_CHANGED')
    if manual_time:
        require(nr.check_ref(p,normal['manual_preflight'])==attempt,'CURRENT_MANUAL_PREFLIGHT_CHANGED')
    require(normal.get('method')=='same_existing_target_guarded_identity_body_no_navigation'
            and normal.get('page')==record['authority']['tabs']['detail']['page'],'CURRENT_NORMAL_OBSERVATION_CHANGED')
    for field in ('metadata','identity','capture'):require(note(normal[field].get('url'))==target,'CURRENT_NORMAL_TARGET_CHANGED')
    require(normal['metadata'].get('page')==normal['page'] and normal['identity'].get('uid')==b['account_uid']
            and normal['identity'].get('page')==normal['page'] and not normal['identity'].get('error'),'CURRENT_NORMAL_ACCOUNT_CHANGED')
    validate_capture(b,target,normal['capture'])
    require(nr.stamp(normal['observed_at'])>confirmed,'CURRENT_OBSERVATION_NOT_NEWER')
    return record,normal


def attest(project,name,probe_file,review_file):
    p=xp.Project(project)
    try:
        with xp.project_lock(project):
            b=bridge.load_binding(project,name)
            with nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
                record,sha=nr.read_json(probe_file);record,normal=validated(p,b,probe_file,sha,keys)
                review_ref,review=nr.freeze(p,review_file)
                require(review.get('probe_id')==record['probe_id'] and review.get('normal_sha256')==record['observation']['sha256']
                    and review.get('reviewed') is True and review.get('body_read') is True and review.get('reader')=='codex'
                    and review.get('summary') and review.get('positions'),'CURRENT_ACTUAL_CODEX_BODY_REVIEW_REQUIRED')
                grant_path=Path(probe_file).with_name('grant.json')
                created=nr.read_json(grant_path)[0]['created_at'] if grant_path.exists() else xp.legacy.now()
                grant={'kind':KIND,'scope':'rendered_public_web_only','probe_file':str(probe_file),'probe_sha256':sha,
                    'probe_id':record['probe_id'],'review':review_ref,'created_at':created}
                grant_sha=nr.immutable(grant_path,grant);pointer={'file':str(grant_path),'sha256':grant_sha}
                duplicate=b.get(POINTER)==pointer
                if not duplicate:
                    require(b.get('state')=='blocked' and b.get('last_error')==record['binding_snapshot']['last_error'],
                            'CURRENT_FAILURE_STATE_CHANGED')
                    b[POINTER]=pointer;b['state']='ready'
                    b.setdefault('resolved_errors',[]).append(b['last_error']);b.pop('last_error',None)
                    b['last_recovery']={'kind':KIND,'probe_id':record['probe_id'],'at':created}
                    bridge.save_binding(project,name,b)
                return {'duplicate':duplicate,'scope':'rendered_public_web_only','grant_file':str(grant_path),
                    'stop_bytes_unchanged':True,'api_and_cdn_still_stopped':True,'no_tasks_completed':True,
                    'normal_file':record['observation']['file']}
    finally:p.close()


def web_exceptions(p,b,keys):
    from xhs_fork_role_recovery import grant_validation_binding
    b=grant_validation_binding(p.path,b)
    pointer=b.get(POINTER,{})
    require(re.fullmatch(r'[a-f0-9]{64}',str(pointer.get('sha256') or '')),'CURRENT_GRANT_HASH_REQUIRED')
    grant,_=nr.read_json(pointer.get('file'),pointer.get('sha256'))
    if grant.get('kind')=='same-target-current-search-revalidation_v1':
        from xhs_current_search_recovery import web_exceptions as search_exceptions
        return search_exceptions(p,b,keys,grant)
    if grant.get('kind')=='same-target-rendered-page-presence_v1':
        from xhs_page_presence import web_exceptions as presence_exceptions
        return presence_exceptions(p,b,keys,grant)
    require(grant.get('kind')==KIND and grant.get('scope')=='rendered_public_web_only','CURRENT_TYPED_GRANT_REQUIRED')
    record,_=validated(p,b,grant['probe_file'],grant['probe_sha256'],keys)
    require(grant.get('probe_id')==record['probe_id'],'CURRENT_GRANT_PROBE_CHANGED')
    review=nr.check_ref(p,grant['review'])
    require(review.get('probe_id')==record['probe_id'] and review.get('normal_sha256')==record['observation']['sha256']
        and review.get('reader')=='codex' and review.get('reviewed') is True and review.get('body_read') is True
        and review.get('summary') and review.get('positions'),'CURRENT_REVIEW_CHANGED')
    bridge.assert_web_project_access(p.path,b['account_uid'])
    return {k:s['sha256'] for k,s in record['stops'].items()}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--project',required=True,type=Path)
    sub=parser.add_subparsers(dest='command',required=True)
    q=sub.add_parser('probe');q.add_argument('--binding',required=True);q.add_argument('--task',required=True)
    q.add_argument('--confirmation',required=True,type=Path);q.add_argument('--authorization',required=True)
    q.add_argument('--manual-restoration',type=Path)
    q=sub.add_parser('attest');q.add_argument('--binding',required=True);q.add_argument('--probe',required=True,type=Path);q.add_argument('--review',required=True,type=Path)
    q=sub.add_parser('attest-presence');q.add_argument('--binding',required=True);q.add_argument('--attempt',required=True,type=Path)
    q.add_argument('--execution',required=True,type=Path);q.add_argument('--review',type=Path)
    a=parser.parse_args()
    if a.command=='attest-presence':
        from xhs_page_presence import attest as presence_attest
        result=presence_attest(a.project,a.binding,a.attempt,a.execution,a.review)
    elif a.command=='probe':result=probe(a.project,a.binding,a.task,a.confirmation,a.authorization,manual_restoration=a.manual_restoration)
    else:result=attest(a.project,a.binding,a.probe,a.review)
    print(xp.dumps({'ok':True,'data':result}))

if __name__=='__main__':
    try:main()
    except xp.legacy.ResearchError as exc:
        print(xp.dumps({'ok':False,'code':getattr(exc,'code','STOP_ACTIVE'),'reason':str(exc)}));raise SystemExit(1)
