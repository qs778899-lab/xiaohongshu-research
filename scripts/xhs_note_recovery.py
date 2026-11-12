"""Bounded same-account health probe after an exactly attested 300031 note error.

Preserves STOP bytes. Only the bridge may consume a typed rendered-web grant;
API/media account leases never receive this exception. Not a generic recovery.
"""
import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlparse

import xhs_account_identity as identity
import xhs_bridge as bridge
import xhs_native as native
import xhs_project as xp

KIND='single_note_300031_rendered_web_recovery_v1'
FORK_KIND='single_note_300031_rendered_web_binding_fork_v1'
STOP_REASON='Observed browser login/verification/access restriction'


def require(value,message):
    if not value:raise bridge.BridgeError(message)


def stamp(value):
    try:parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    except (ValueError,TypeError,AttributeError):raise bridge.BridgeError('RECOVERY_TIMED_OBSERVATION_REQUIRED')
    require(parsed.tzinfo is not None,'RECOVERY_TIMEZONE_REQUIRED');return parsed


def read_json(path,sha=None):
    require(isinstance(path,(str,Path)) and str(path),'RECOVERY_ORIGINAL_FILE_REQUIRED')
    path=Path(path)
    require(path.is_absolute() and path.is_file() and not path.is_symlink(),'RECOVERY_ORIGINAL_FILE_REQUIRED')
    raw=path.read_bytes();actual=hashlib.sha256(raw).hexdigest()
    require(not sha or actual==sha,'RECOVERY_EVIDENCE_CHANGED')
    try:value=json.loads(raw)
    except (ValueError,UnicodeError):raise bridge.BridgeError('RECOVERY_JSON_REQUIRED')
    require(isinstance(value,dict),'RECOVERY_JSON_OBJECT_REQUIRED')
    return value,actual


def freeze(p,path):
    path=Path(path);value,sha=read_json(path)
    archive,stored=native.archive(p,path);require(stored==sha,'RECOVERY_EVIDENCE_CHANGED')
    return {'file':str(path),'sha256':sha,'archive':archive},value


def check_ref(p,ref):
    require(isinstance(ref,dict) and re.fullmatch(r'[a-f0-9]{64}',ref.get('sha256','')),'RECOVERY_EVIDENCE_HASH_REQUIRED')
    value,_=read_json(ref['file'],ref['sha256'])
    read_json(p.path/ref['archive'],ref['sha256']);return value


def immutable(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    require(not path.is_symlink(),'RECOVERY_UNSAFE_ARCHIVE')
    data=xp.dumps(value).encode()
    if path.exists():require(path.read_bytes()==data,'RECOVERY_IMMUTABLE_RECORD_CHANGED')
    else:
        with path.open('xb') as out:out.write(data)
    return hashlib.sha256(data).hexdigest()


@contextmanager
def locked_stops(profile,uid):
    """Offline/probe synchronization only; it does not permit normal collection."""
    with identity.lock_file(identity.legacy.account_root()/'identity-registry.lock'):
        keys=identity.related_keys(profile,uid)
        with ExitStack() as stack:
            acquired=[]
            try:
                for key in keys:
                    lock=identity.guard_directory(key)/'collection.lock'
                    require(str(lock) not in identity.legacy._ACCOUNT_LOCKS,'RECOVERY_NOT_REENTRANT')
                    stack.enter_context(identity.lock_file(lock));identity.legacy._ACCOUNT_LOCKS[str(lock)]=True;acquired.append(str(lock))
                yield keys
            finally:
                for key in acquired:identity.legacy._ACCOUNT_LOCKS.pop(key,None)


def current_stops(keys):
    result={}
    for key in keys:
        path=identity.guard_directory(key)/'STOP.json'
        value,sha=read_json(path)
        require(value.get('reason')==STOP_REASON and value.get('channel')=='bridge' and value.get('credential_free') is True,
                'RECOVERY_UNRELATED_ACCOUNT_STOP')
        result[key]={'file':str(path),'sha256':sha,'at':value.get('at')}
    require(result,'RECOVERY_EXACT_STOP_SET_REQUIRED');return result


def original_context(p,b,original):
    raw,_=read_json(original);old=raw.get('binding_snapshot',{});meta=raw.get('metadata',{})
    for key in ['project','context_id','account_uid','profile_guard_key','format']:
        require(old.get(key)==b.get(key) and old.get(key),'RECOVERY_ORIGINAL_ACCOUNT_CONTEXT_MISMATCH')
    slot=old.get('tabs',{}).get('detail',{});intent=slot.get('navigation_intent')
    require(slot.get('owned') is True and meta.get('page')==slot.get('page') and slot.get('page'),'RECOVERY_ORIGINAL_TAB_MISMATCH')
    diagnosis=bridge.classify_note_error(meta.get('url'),intent)
    require(diagnosis['classification']=='NOTE_UNAVAILABLE','RECOVERY_EXACT_300031_REQUIRED')
    require(old.get('last_error',{}).get('code')=='ACCESS_STOP','RECOVERY_ORIGINAL_STOP_CONTEXT_REQUIRED')
    failure_at=stamp(old['last_error'].get('at'));observed=stamp(raw.get('observed_at'))
    require(stamp(intent.get('at'))<=failure_at<=observed,'RECOVERY_ORIGINAL_TIMES_INCONSISTENT')
    # Metadata references bind the original recorded URL, rather than a new summary.
    source=meta.get('source_file') or raw.get('original_error_source')
    require(source and meta.get('source_sha256'),'RECOVERY_ORIGINAL_METADATA_SOURCE_REQUIRED')
    data,_=read_json(source,meta['source_sha256'])
    def found(value):
        if isinstance(value,dict):
            if value.get('page')==meta['page'] and value.get('url')==meta['url']:return True
            return any(found(v) for v in value.values())
        return isinstance(value,list) and any(found(v) for v in value)
    require(found(data),'RECOVERY_METADATA_NOT_IN_ORIGINAL_SOURCE')
    return {'note_id':diagnosis['note_id'],'failure_at':failure_at,'observed_at':observed,'binding':old,
            'metadata_source':source,'metadata_sha256':meta['source_sha256']}


def normal_target(b,path):
    value,_=read_json(path)
    require(value.get('scope')=='one_normal_public_research_note_health_check_only','RECOVERY_PROBE_SCOPE_REQUIRED')
    for key in ['context_id','account_uid','profile_label']:
        require(value.get(key)==b.get(key),'RECOVERY_NORMAL_ACCOUNT_CONTEXT_MISMATCH')
    request=value.get('user_request',{})
    require(request.get('text') and request.get('processed_at'),'RECOVERY_EXPLICIT_USER_AUTHORIZATION_REQUIRED')
    stamp(request['processed_at'])
    bridge.safe_url(value.get('url'))
    note=value.get('target_note_id')
    require(re.fullmatch(r'[a-f0-9]{24}',note or '') and bridge.target_path(urlparse(value['url']).path)=='/explore/'+note,
            'RECOVERY_NORMAL_EXACT_NOTE_REQUIRED')
    require(re.fullmatch(r'[a-f0-9]{64}',value.get('source_sha256','')),'RECOVERY_CARD_HASH_REQUIRED')
    source,_=read_json(value.get('source_file'),value['source_sha256'])
    position=value.get('source_position','')
    require(position.startswith('/'),'RECOVERY_OBSERVED_CARD_POSITION_REQUIRED')
    try:
        selected=source
        for part in position.split('/')[1:]:
            key=part.replace('~1','/').replace('~0','~');selected=selected[int(key)] if isinstance(selected,list) else selected[key]
    except (KeyError,IndexError,ValueError,TypeError):raise bridge.BridgeError('RECOVERY_OBSERVED_CARD_POSITION_INVALID')
    require(isinstance(selected,dict) and selected.get('url')==value['url'],'RECOVERY_NORMAL_URL_NOT_ACTUALLY_OBSERVED')
    return value


def stop_binding(stops,ctx):
    for stop in stops.values():
        delta=(stamp(stop['at'])-ctx['failure_at']).total_seconds()
        require(0<=delta<=5 and stamp(stop['at'])<=ctx['observed_at'],'RECOVERY_STOP_NOT_FROM_ORIGINAL_FAILURE')


def validate_normal(b,target,observation,stops):
    require(observation.get('context_id')==b['context_id'] and observation.get('account_uid')==b['account_uid'],
            'RECOVERY_OBSERVED_CONTEXT_MISMATCH')
    meta=observation.get('metadata',{});ident=observation.get('identity',{});capture=observation.get('capture',{})
    require(meta.get('page') and meta.get('page')==observation.get('page'),'RECOVERY_NORMAL_TAB_MISMATCH')
    require(ident.get('uid')==b['account_uid'] and capture.get('uid')==b['account_uid'],'RECOVERY_ACTUAL_SAME_UID_REQUIRED')
    require(not ident.get('error') and not capture.get('error'),'RECOVERY_ACCESS_WARNING')
    for data in [meta,ident,capture]:
        url=data.get('url');bridge.safe_url(url)
        require(bridge.target_path(urlparse(url).path)=='/explore/'+target['target_note_id'],
                'RECOVERY_NORMAL_TARGET_CHANGED')
    require(isinstance(capture.get('body'),str) and capture['body'].strip(),'RECOVERY_ACTUAL_BODY_REQUIRED')
    # Pinned identity/capture guard succeeds only after login/captcha warning checks.
    require(observation.get('method')=='pinned_bridge_guarded_identity_and_body_capture','RECOVERY_RAW_GUARD_OBSERVATIONS_REQUIRED')
    at=stamp(observation.get('observed_at'))
    require(all(at>stamp(s['at']) for s in stops.values()) and at>stamp(target['user_request']['processed_at']),
            'RECOVERY_NORMAL_OBSERVATION_NOT_NEWER')
    return meta


def probe(project,name,original_file,target_file,driver=None):
    """One ordinary public page only; no research pagination or broad browsing."""
    p=xp.Project(project);b=bridge.load_binding(project,name);owned_driver=driver is None
    try:
        with xp.project_lock(project),locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
            bridge.assert_web_project_access(project,b['account_uid'])
            require(not (p.path/'bridge'/'PAUSE').exists(),'USER_PAUSED')
            ctx=original_context(p,b,original_file);target=normal_target(b,target_file);stops=current_stops(keys);stop_binding(stops,ctx)
            require(target['target_note_id']!=ctx['note_id'],'RECOVERY_INDEPENDENT_NORMAL_NOTE_REQUIRED')
            token=xp.digest({'context':b['context_id'],'uid':b['account_uid'],'note':ctx['note_id'],
                             'intent':ctx['binding']['tabs']['detail']['navigation_intent'],'stops':{k:s['sha256'] for k,s in stops.items()}})
            directory=p.path/'bridge'/'note-recoveries'/token;attempt=directory/'attempt.json'
            require(not attempt.exists(),'RECOVERY_PROBE_ALREADY_ATTEMPTED')
            refs={}
            for key,path in [('original',original_file),('normal_target',target_file),('error_metadata',ctx['metadata_source']),('normal_card',target['source_file'])]:
                refs[key],_=freeze(p,path)
            for key,stop in stops.items():stop['archive']=native.archive(p,stop['file'])[0]
            role='note_recovery_'+token[:12]
            journal={'kind':KIND,'probe_id':token,'state':'started','binding_name':name,'context_id':b['context_id'],
                     'account_uid':b['account_uid'],'profile_guard_key':b['profile_guard_key'],'failed_note':ctx['note_id'],
                     'role':role,'sources':refs,'stops':stops,'started_at':xp.legacy.now()}
            immutable(attempt,journal) # Written before any possible external action.
            driver=driver or bridge.Driver(b['runtime'])
            try:
                profiles=driver.call('profiles').get('profiles',[])
                require(sum(v.get('contextId')==b['context_id'] and v.get('extensionVersion')==b['extension_version'] for v in profiles)==1,
                        'RECOVERY_SPECIFIED_PROFILE_UNCONFIRMED')
                require(role not in b['tabs'],'RECOVERY_ROLE_ALREADY_EXISTS')
                bridge.provision_role(project,name,b,driver,role)
                b['tabs'][role]['navigation_intent']={'url':target['url'],'at':xp.legacy.now(),'recovery_probe':token}
                bridge.save_binding(project,name,b)
                driver.call('navigate',b,role,url=target['url'])
                # Retry observations only, never navigation or creation.
                last=None
                for attempt_number in range(4):
                    try:
                        meta=bridge.verify_tab(driver,b,role)
                        ident=driver.call('identity',b,role)
                        capture=driver.call('capture',b,role,stage='body',expectedPath='/explore/'+target['target_note_id'])
                        last=None;break
                    except bridge.BridgeError as exc:
                        last=exc
                        if exc.code not in {'CONTENT_LOADING','IDENTITY_UNCONFIRMED'} or attempt_number==3:raise
                        time.sleep(1)
                if last:raise last
                observation={'observed_at':xp.legacy.now(),'context_id':b['context_id'],'account_uid':b['account_uid'],
                             'role':role,'page':meta['page'],'metadata':meta,'identity':ident,'capture':capture,
                             'method':'pinned_bridge_guarded_identity_and_body_capture'}
                validate_normal(b,target,observation,stops)
                normal_path=directory/'normal-observation.json';immutable(normal_path,observation)
                refs['normal'],_=freeze(p,normal_path)
                journal.update(state='captured',sources=refs,captured_at=observation['observed_at'])
                final=directory/'probe.json';sha=immutable(final,journal)
                b.setdefault('note_recovery_probes',{})[token]={'file':str(final),'sha256':sha}
                bridge.save_binding(project,name,b)
                return {'probe_id':token,'probe_file':str(final),'normal_file':str(normal_path),
                        'normal_sha256':refs['normal']['sha256'],'review_required':True,'stop_bytes_unchanged':True,
                        'collection_permitted':False}
            except Exception as exc:
                immutable(directory/'failure.json',{'at':xp.legacy.now(),'code':getattr(exc,'code',type(exc).__name__),
                                                  'unknown_outcome_must_not_retry':True})
                bridge.save_binding(project,name,b)
                if getattr(exc,'code',None)=='ACCESS_STOP':identity.stop_all(keys,STOP_REASON)
                raise
    finally:
        p.close()
        if owned_driver and driver:driver.close()


def validate_probe(p,b,probe_file,expected_sha,keys):
    require(re.fullmatch(r'[a-f0-9]{64}',expected_sha or ''),'RECOVERY_PROBE_HASH_REQUIRED')
    record,_=read_json(probe_file,expected_sha)
    require(record.get('kind')==KIND and record.get('state')=='captured','RECOVERY_CAPTURED_PROBE_REQUIRED')
    require(record.get('binding_name')==b['binding_name'],'RECOVERY_BINDING_MISMATCH')
    for key in ['context_id','account_uid','profile_guard_key']:
        require(record.get(key)==b.get(key),'RECOVERY_CURRENT_ACCOUNT_CHANGED')
    refs=record['sources'];sources={k:check_ref(p,v) for k,v in refs.items()}
    ctx=original_context(p,b,refs['original']['file']);target=normal_target(b,refs['normal_target']['file'])
    require(record['failed_note']==ctx['note_id'] and target['target_note_id']!=ctx['note_id'],'RECOVERY_NOTE_SCOPE_MISMATCH')
    current=current_stops(keys)
    require(set(record['stops'])==set(current),'RECOVERY_STOP_SET_CHANGED')
    for key,stop in current.items():
        old=record['stops'][key];require(stop['sha256']==old['sha256'],'RECOVERY_NEW_ACCOUNT_STOP')
        read_json(p.path/old['archive'],old['sha256'])
    stop_binding(current,ctx);validate_normal(b,target,sources['normal'],current)
    return record,sources


def attest(project,name,probe_file,review_file):
    p=xp.Project(project)
    try:
        with xp.project_lock(project):
            b=bridge.load_binding(project,name)
            with locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
                bridge.assert_web_project_access(project,b['account_uid'])
                probe_raw,probe_sha=read_json(probe_file);token=probe_raw.get('probe_id')
                pointer=b.get('note_recovery_probes',{}).get(token,{})
                require(pointer.get('file')==str(Path(probe_file)) and pointer.get('sha256')==probe_sha,'RECOVERY_BOUND_PROBE_REQUIRED')
                record,sources=validate_probe(p,b,probe_file,probe_sha,keys)
                review_ref,review=freeze(p,review_file)
                require(review.get('probe_id')==token and review.get('normal_sha256')==record['sources']['normal']['sha256']
                        and review.get('reviewed') is True and review.get('summary') and review.get('positions'),
                        'RECOVERY_ACTUAL_CODEX_REVIEW_REQUIRED')
                old=sources['original']['binding_snapshot']['tabs']['detail'];role=record['role']
                normal=sources['normal'];path=Path(probe_file).parent/'grant.json'
                created_at=read_json(path)[0].get('created_at') if path.exists() else xp.legacy.now()
                require(created_at,'RECOVERY_GRANT_TIME_REQUIRED')
                grant={'kind':KIND,'probe_file':str(Path(probe_file)),'probe_sha256':probe_sha,'probe_id':token,
                       'review':review_ref,'scope':'rendered_public_web_only','created_at':created_at}
                sha=immutable(path,grant);grant_pointer={'file':str(path),'sha256':sha}
                already_promoted=(b.get('note_unavailable_recovery')==grant_pointer
                    and b.get('tabs',{}).get('detail',{}).get('page')==normal['page']
                    and any(q.get('probe_id')==token and q.get('tab',{}).get('page')==old['page']
                            for q in b.get('quarantined_tabs',[])))
                if not already_promoted:
                    require(b.get('tabs',{}).get('detail',{}).get('page')==old['page']
                            and b['tabs']['detail'].get('navigation_intent')==old.get('navigation_intent'),
                            'RECOVERY_FAILED_ROLE_CHANGED')
                    slot=b.get('tabs',{}).get(role,{})
                    require(slot.get('owned') is True and slot.get('page')==normal['page']
                            and bridge.target_path(urlparse(slot.get('last_url','')).path)
                            ==bridge.target_path(urlparse(normal['metadata']['url']).path),
                            'RECOVERY_NORMAL_ROLE_CHANGED')
                    b.setdefault('quarantined_tabs',[]).append({'role':'detail','tab':dict(b['tabs']['detail']),
                                                              'note_id':record['failed_note'],'probe_id':token,'grant_sha256':sha})
                    b['tabs']['detail']=b['tabs'].pop(role)
                    b['note_unavailable_recovery']=grant_pointer
                    b['state']='ready';b['last_recovery']={'kind':KIND,'probe_id':token,'at':grant['created_at']}
                    if b.get('last_error'):b.setdefault('resolved_errors',[]).append(b['last_error']);b.pop('last_error',None)
                for row in p.db.execute("SELECT id,payload FROM tasks WHERE kind='native' AND status='pending'").fetchall():
                    payload=json.loads(row['payload'])
                    if payload.get('target')!=record['failed_note'] or payload.get('stage') not in {'body','roots','replies','images','video'}:continue
                    progress=payload.setdefault('bridge',{});prior=progress.get('fault')
                    if prior and prior.get('code')=='NOTE_UNAVAILABLE' and prior.get('probe_id')==token:continue
                    progress['fault']={'code':'NOTE_UNAVAILABLE','hold':True,'at':grant['created_at'],
                                       'probe_id':token,'grant_sha256':sha,'prior_fault':prior,
                                       'next':'Original note remains unavailable; wait for a newly observed valid source, do not retry this source.'}
                    p.db.execute("UPDATE tasks SET payload=?,reason='NOTE_UNAVAILABLE',updated=? WHERE id=?",(xp.dumps(payload),grant['created_at'],row['id']))
                p.db.commit()
                if not already_promoted:bridge.save_binding(project,name,b)
                return {'duplicate':already_promoted,'scope':'rendered_public_web_only','probe_id':token,'grant_file':str(path),
                        'failed_note_still_isolated':True,'stop_bytes_unchanged':True,'api_and_cdn_still_stopped':True}
    finally:p.close()


def direct_web_exceptions(p,b,keys):
    """Validate the original grant under the caller's already held account locks."""
    require(not b.get('web_parent_authorization') and b.get('binding_kind')!=FORK_KIND,
            'FORK_PARENT_MUST_HAVE_DIRECT_GRANT')
    pointer=b.get('note_unavailable_recovery',{})
    require(re.fullmatch(r'[a-f0-9]{64}',pointer.get('sha256','')),'RECOVERY_GRANT_HASH_REQUIRED')
    grant,_=read_json(pointer.get('file',''),pointer.get('sha256'))
    require(grant.get('kind')==KIND and grant.get('scope')=='rendered_public_web_only','RECOVERY_TYPED_GRANT_REQUIRED')
    record,_=validate_probe(p,b,grant['probe_file'],grant['probe_sha256'],keys)
    review=check_ref(p,grant['review'])
    require(review.get('probe_id')==record['probe_id'] and review.get('reviewed') is True
            and review.get('normal_sha256')==record['sources']['normal']['sha256'],'RECOVERY_REVIEW_BINDING_CHANGED')
    bridge.assert_web_project_access(p.path,b['account_uid'])
    return {key:stop['sha256'] for key,stop in record['stops'].items()}


def fork_path(project,category,name):
    require(category in {'intents','names'},'FORK_RECORD_PATH_INVALID')
    require(isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,60}',name),'FORK_RECORD_NAME_INVALID')
    path=Path(project)/'bridge'/'binding-forks'/category/(name+'.json')
    require(not any(x.is_symlink() for x in (path,path.parent,path.parent.parent,path.parent.parent.parent)),
            'FORK_UNSAFE_RECORD_PATH')
    return path


def fork_projection(b):
    # Ordinary tab URLs/last_error/state are deliberately mutable. Authority is not.
    return {k:b.get(k) for k in ('format','project','binding_name','profile_label','context_id','account_uid',
        'profile_guard_key','session','runtime','extension_version','web_channel_authorization','authorization')}


def completed_fork_home(project,record_file,record,sha):
    """Immutable creation result, independent of later mutable page/task faults."""
    expected=fork_path(project,'intents',record.get('intent_id'));b=record.get('child_projection',{})
    require(Path(record_file)==expected and record.get('kind')==FORK_KIND
            and record.get('scope')=='rendered_public_web_only' and record.get('project')==str(Path(project).resolve())
            and b.get('binding_name')==record.get('child_name'),'FORK_SCOPE_CHANGED')
    pointer={'file':str(expected),'sha256':sha}
    ready,_=read_json(expected.with_suffix('.ready.json'));home_pointer=ready.get('home',{})
    require(ready.get('authorization')==pointer and set(ready)=={'authorization','home'},'FORK_READY_RECORD_CHANGED')
    home,home_sha=read_json(home_pointer.get('file',''),home_pointer.get('sha256'))
    require(home_pointer.get('sha256')==home_sha and Path(home_pointer['file'])==expected.with_suffix('.home.json'),
            'FORK_HOME_RECORD_CHANGED')
    require(home.get('kind')=='guarded_new_binding_home_identity_v1' and home.get('authorization')==pointer
            and home.get('context_id')==b.get('context_id') and home.get('account_uid')==b.get('account_uid')
            and home.get('session')==b.get('session') and home.get('binding_name')==b.get('binding_name'),
            'FORK_HOME_CONTEXT_CHANGED')
    require(stamp(home.get('observed_at'))>=stamp(record.get('created_at')),'FORK_HOME_NOT_NEWER')
    roles=home.get('roles',{});require(set(roles)=={'search','detail'},'FORK_HOME_ROLES_REQUIRED')
    pages=set()
    for role,proof in roles.items():
        require(proof.get('session')==b['session']+'-'+role,'FORK_HOME_TARGET_CHANGED')
        require(re.fullmatch(r'[A-Fa-f0-9]{32}',proof.get('page','')) and proof['page'].lower() not in pages,'FORK_HOME_TARGET_INVALID')
        pages.add(proof['page'].lower())
        for observed in (proof.get('metadata',{}),proof.get('identity',{})):
            bridge.safe_url(observed.get('url'))
            require(observed.get('page')==proof['page'] and urlparse(observed['url']).path.rstrip('/')=='/explore'
                    and not observed.get('error'),'FORK_ACTUAL_HOME_TARGET_REQUIRED')
        require(proof['identity'].get('uid')==b['account_uid'],'FORK_ACTUAL_HOME_UID_REQUIRED')
    return home,home_pointer


def fork_authority(p,b,keys,home_required=True):
    """Nonrecursive delegation, checked afresh on every rendered-web lease."""
    require(b.get('binding_kind')==FORK_KIND and not b.get('note_unavailable_recovery'),'FORK_TYPED_CHILD_REQUIRED')
    pointer=b.get('web_parent_authorization',{})
    record,sha=read_json(pointer.get('file',''),pointer.get('sha256'))
    require(pointer.get('sha256')==sha and re.fullmatch(r'[a-f0-9]{64}',sha),'FORK_RECORD_HASH_REQUIRED')
    require(record.get('kind')==FORK_KIND and record.get('scope')=='rendered_public_web_only'
            and record.get('project')==str(p.path.resolve()) and record.get('child_name')==b['binding_name'],
            'FORK_SCOPE_CHANGED')
    expected=fork_path(p.path,'intents',record.get('intent_id'))
    require(Path(pointer['file'])==expected,'FORK_RECORD_PATH_CHANGED')
    index,_=read_json(fork_path(p.path,'names',b['binding_name']))
    require(index==pointer,'FORK_NAME_RECORD_CHANGED')
    require(fork_projection(b)==record.get('child_projection'),'FORK_CHILD_AUTHORITY_CHANGED')
    parent=bridge.load_binding(p.path,record.get('parent_name'))
    require(not fork_path(p.path,'names',parent['binding_name']).exists(),'FORK_RECURSION_FORBIDDEN')
    require(fork_projection(parent)==record.get('parent_projection'),'FORK_PARENT_AUTHORITY_CHANGED')
    require(parent.get('note_unavailable_recovery')==record.get('parent_grant'),'FORK_PARENT_GRANT_CHANGED')
    require(all(b.get(k)==parent.get(k) for k in ('format','project','profile_label','context_id','account_uid',
                'profile_guard_key','runtime','extension_version','web_channel_authorization'))
            and b.get('binding_name')!=parent.get('binding_name') and b.get('session')!=parent.get('session'),
            'FORK_INHERITED_AUTHORITY_CHANGED')
    require(record.get('authorization')==b.get('authorization') and isinstance(b.get('authorization'),str)
            and b['authorization'].strip(),'FORK_AUTHORIZATION_BASIS_REQUIRED')
    bridge.web_authorization_exceptions(p.path,parent)
    stops=direct_web_exceptions(p,parent,keys)
    require(stops==record.get('stop_hashes'),'FORK_STOP_SET_CHANGED')
    if not home_required:return stops
    home,home_pointer=completed_fork_home(p.path,expected,record,sha)
    require(b.get('fork_home_verification')==home_pointer,'FORK_HOME_RECORD_CHANGED')
    from xhs_fork_role_recovery import effective_roles
    for role,proof in effective_roles(p.path,b,home).items():
        slot=b.get('tabs',{}).get(role,{})
        require(slot.get('owned') is True and slot.get('page')==proof.get('page') and slot.get('session')==proof.get('session')
                and proof.get('session')==b['session']+'-'+role,'FORK_HOME_TARGET_CHANGED')
    return stops


def web_exceptions(project,name,keys):
    """Called under all account locks, only by the rendered bridge lease."""
    p=native.open_lookup_project(Path(project))
    try:
        b=bridge.load_binding(project,name)
        if b.get('manual_tab_revalidation'):
            from xhs_manual_tab_recovery import web_exceptions as manual_tab_exceptions
            return manual_tab_exceptions(project,b,keys)
        if b.get('current_page_revalidation'):
            from xhs_current_page_recovery import web_exceptions as current_page_exceptions
            return current_page_exceptions(p,b,keys)
        if b.get('web_parent_authorization') or b.get('binding_kind')==FORK_KIND or fork_path(project,'names',name).exists():
            return fork_authority(p,b,keys)
        return direct_web_exceptions(p,b,keys)
    except (KeyError,TypeError,OSError,bridge.BridgeError,xp.legacy.ResearchError):
        raise xp.legacy.StopRun('Typed 300031 rendered-web recovery is missing, changed, or no longer valid') from None
    finally:p.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--project',required=True,type=Path)
    sub=parser.add_subparsers(dest='command',required=True)
    q=sub.add_parser('probe');q.add_argument('--binding',required=True);q.add_argument('--original-error',required=True,type=Path);q.add_argument('--normal-source',required=True,type=Path)
    q=sub.add_parser('attest');q.add_argument('--binding',required=True);q.add_argument('--probe',required=True,type=Path);q.add_argument('--review',required=True,type=Path)
    a=parser.parse_args()
    if a.command=='probe':result=probe(a.project,a.binding,a.original_error,a.normal_source)
    else:result=attest(a.project,a.binding,a.probe,a.review)
    print(xp.dumps({'ok':True,'data':result}))


if __name__=='__main__':
    try:main()
    except xp.legacy.ResearchError as exc:
        print(xp.dumps({'ok':False,'code':getattr(exc,'code','STOP_ACTIVE'),'reason':str(exc)}));raise SystemExit(1)
