"""Offline, typed page-presence attestation for one fully captured manual failure.

This never probes, marks body read, or changes the consumed attempt/failure.
"""
import hashlib
from pathlib import Path
import re
import shlex
from urllib.parse import urlparse

import xhs_current_page_recovery as current
import xhs_note_recovery as nr
import xhs_bridge as bridge
import xhs_project as xp

KIND='same-target-rendered-page-presence_v1'
require=nr.require
CODE_FILES=('xhs_page_presence.py','xhs_current_page_recovery.py','xhs_note_recovery.py',
            'xhs_bridge.py','xhs_bridge_driver.mjs','xhs_account_identity.py','xhs_project.py','xhs_native.py')
EXECUTED_FILES={'xhs_current_page_recovery.py','xhs_note_recovery.py','xhs_bridge.py','xhs_bridge_driver.mjs'}


def code_hashes():
    return {name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in CODE_FILES}


def pointer(ref):return {k:ref[k] for k in ('file','sha256')}


def execution(p,attempt,attempt_ref,failure_ref,ref):
    value=nr.check_ref(p,ref)
    require(value.get('kind')=='manual-page-probe-execution_v1' and value.get('reader')=='codex'
            and value.get('actual_tool_call') is True and value.get('binding_name')==attempt['binding_name']
            and value.get('attempt')==pointer(attempt_ref) and value.get('failure')==pointer(failure_ref),
            'PRESENCE_ACTUAL_EXECUTION_REQUIRED')
    require(value.get('tool_name') and value.get('extra_browser_calls_after_probe') is False
            and value.get('tool_result',{}).get('exit_code')==1
            and value['tool_result'].get('stdout_code')=='CURRENT_ACTUAL_BODY_AND_TITLE_REQUIRED',
            'PRESENCE_EXECUTION_RESULT_REQUIRED')
    failure=nr.check_ref(p,failure_ref)
    require(nr.stamp(value.get('recorded_at'))>=nr.stamp(failure['at']),'PRESENCE_EXECUTION_TIME_REQUIRED')
    manifest_ref=value.get('executed_code_manifest',{})
    require(re.fullmatch('[a-f0-9]{64}',str(manifest_ref.get('sha256') or '')),'PRESENCE_CODE_MANIFEST_HASH_REQUIRED')
    manifest,_=nr.read_json(manifest_ref.get('file'),manifest_ref['sha256'])
    require(manifest.get('kind')=='captured_manual_probe_code_archive_v1','PRESENCE_CODE_ARCHIVE_REQUIRED')
    rows=manifest.get('files',[])
    require(len(rows)==4 and {Path(row.get('original_file','')).name for row in rows}==EXECUTED_FILES,
            'PRESENCE_EXECUTED_CODE_SET_REQUIRED')
    originals={}
    for row in rows:
        current.check_authority_refs([pointer(row)])
        original=Path(row['original_file']);name=original.name
        require(original.is_absolute() and Path(row['file']).name==name,'PRESENCE_CODE_PATH_REQUIRED')
        originals[name]=original
        if name in {'xhs_bridge.py','xhs_bridge_driver.mjs'}:
            require(hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()==row['sha256'],
                    'PRESENCE_EXECUTED_GUARD_CODE_CHANGED')
    require(len({str(v.parent) for v in originals.values()})==1,'PRESENCE_CODE_DIRECTORY_CHANGED')
    try:argv=shlex.split(value.get('command',''))
    except ValueError:raise bridge.BridgeError('PRESENCE_COMMAND_REQUIRED')
    require(len(argv)>2 and argv[1]==str(originals['xhs_current_page_recovery.py']) and 'probe' in argv,
            'PRESENCE_COMMAND_REQUIRED')
    for flag,expected in {'--project':attempt['binding_snapshot']['project'],'--binding':attempt['binding_name'],
            '--task':attempt['task_id'],'--confirmation':attempt['confirmation']['file'],
            '--manual-restoration':attempt['manual_restoration']['authorization']['file'],
            '--authorization':attempt['authorization']}.items():
        require(argv.count(flag)==1 and argv.index(flag)+1<len(argv) and argv[argv.index(flag)+1]==expected,
                'PRESENCE_COMMAND_CONTEXT_CHANGED')
    return value


def title_source(p,task,title,target,expected=None):
    for row in task['payload'].get('provenance',[]):
        ref={'file':row.get('original_path'),'sha256':row.get('sha256'),'archive':row.get('archive')}
        if expected and ref!=expected['source']:continue
        value=nr.check_ref(p,ref)
        for i,card in enumerate(value.get('cards',[])):
            try:found=current.note(card.get('url'))==target
            except bridge.BridgeError:continue
            if found and card.get('text')==title:
                selected={'source':ref,'position':'/cards/'+str(i),'title':title}
                require(not expected or selected==expected,'PRESENCE_OBSERVED_TITLE_CHANGED')
                return selected
    raise bridge.BridgeError('PRESENCE_TITLE_NOT_IN_OBSERVED_SOURCE')


def failed_capture(p,b,refs,keys,expected_title=None):
    attempt=nr.check_ref(p,refs['attempt']);failure=nr.check_ref(p,refs['failure'])
    require(attempt.get('kind')==current.KIND and attempt.get('state')=='started' and attempt.get('manual_restoration'),
            'PRESENCE_MANUAL_FAILED_ATTEMPT_REQUIRED')
    target,_=current.context(p,b,attempt['binding_snapshot'],attempt['task_id'])
    require(attempt.get('authority')==current.projection(b) and attempt.get('target_note_id')==target,
            'PRESENCE_AUTHORITY_CHANGED')
    stops=current.same_stops(attempt['stops'],keys)
    for stop in attempt['stops'].values():nr.read_json(p.path/stop['archive'],stop['sha256'])
    current.check_authority_refs(attempt['authority_files']);current.historical_authority(p,b,keys)
    original_user=nr.check_ref(p,attempt['confirmation']);current.confirmation(original_user,target,stops)
    manual_proof=attempt['manual_restoration'];manual=nr.check_ref(p,manual_proof['authorization'])
    for field in ('original_attempt','original_failure'):
        nr.check_ref(p,manual_proof[field])
        require(pointer(manual_proof[field])==manual.get(field),'CURRENT_MANUAL_SOURCE_CHANGED')
    token,confirmed=current.manual_context(p,attempt['binding_snapshot'],attempt['task_id'],target,stops,manual)
    directory=p.path/'bridge/current-page-recoveries'/token
    require(attempt.get('probe_id')==token and refs['attempt']['file']==str(directory/'attempt.json')
            and refs['failure']['file']==str(directory/'failure.json')
            and refs['preflight']['file']==str(directory/'manual-preflight.json')
            and nr.check_ref(p,refs['preflight'])==attempt
            and not (directory/'probe.json').exists() and not (directory/'current-observation.json').exists(),
            'PRESENCE_CONSUMED_CHILD_CONTEXT_REQUIRED')
    require(failure.get('code')=='CURRENT_ACTUAL_BODY_AND_TITLE_REQUIRED' and failure.get('failed_stage')=='capture'
            and failure.get('attempt_consumed') is True and failure.get('no_automatic_retry') is True
            and set(failure.get('observations',{}))=={'metadata','identity','capture'},'PRESENCE_EXACT_EMPTY_BODY_FAILURE_REQUIRED')
    stages={}
    previous=max(confirmed,nr.stamp(attempt['started_at']))
    for field in ('metadata','identity','capture'):
        ref=refs[field]
        require(ref==failure['observations'][field] and ref['file']==str(directory/(field+'.json')),
                'PRESENCE_STAGE_SOURCE_CHANGED')
        raw=nr.check_ref(p,ref);at=nr.stamp(raw.get('observed_at'))
        require(raw.get('stage')==field and previous<=at<=nr.stamp(failure['at']),'PRESENCE_STAGE_TIME_CHANGED')
        previous=at;stages[field]=raw['result']
        require(not stages[field].get('error') and current.note(stages[field].get('url'))==target,
                'PRESENCE_STAGE_ACCESS_OR_TARGET_CHANGED')
    meta,ident,cap=(stages[k] for k in ('metadata','identity','capture'))
    require(meta.get('page')==ident.get('page')==b['tabs']['detail']['page']
            and ident.get('uid')==cap.get('uid')==b['account_uid']
            and ident.get('visible') is True and cap.get('visible') is True,'PRESENCE_PAGE_OR_ACCOUNT_CHANGED')
    require(cap.get('stage')=='body' and not any(cap.get(k) for k in ('loginWall','securityBlock','notFound'))
            and isinstance(cap.get('body'),str) and not cap['body'].strip()
            and isinstance(cap.get('title'),str) and cap['title'].strip(),'PRESENCE_ONLY_EMPTY_BODY_WITH_TITLE_REQUIRED')
    authors=[]
    for author in cap.get('authors',[]):
        u=urlparse(author.get('url',''));match=re.fullmatch('/user/profile/([a-f0-9]{24})/?',u.path)
        require(u.scheme=='https' and u.netloc=='www.xiaohongshu.com' and match,'PRESENCE_VALID_AUTHOR_REQUIRED')
        authors.append(match[1])
    require(len(set(authors))==1,'PRESENCE_UNIQUE_AUTHOR_REQUIRED')
    source=title_source(p,p.task(attempt['task_id']),cap['title'],target,expected_title)
    execution(p,attempt,refs['attempt'],refs['failure'],refs['execution'])
    return attempt,cap,source


def review(p,ref,evidence,sha):
    value=nr.check_ref(p,ref)
    require(value.get('kind')==KIND and value.get('reader')=='codex' and value.get('reviewed') is True
            and value.get('probe_id')==evidence['probe_id'] and value.get('evidence_sha256')==sha
            and all(value.get(k) is True for k in ('title_read','author_read','media_metadata_read'))
            and all(value.get(k) is False for k in ('body_read','comment_read','media_read'))
            and value.get('summary') and value.get('positions'),'PRESENCE_ACTUAL_METADATA_ONLY_REVIEW_REQUIRED')


def prepare_evidence(p,b,attempt_file,execution_file,keys):
    # Only immutable local copies; never invokes Driver or edits an old record.
    attempt,unused=nr.read_json(attempt_file);directory=Path(attempt_file).parent
    failure,_=nr.read_json(directory/'failure.json')
    refs={name:nr.freeze(p,file)[0] for name,file in {'attempt':attempt_file,'failure':directory/'failure.json',
        'preflight':directory/'manual-preflight.json','execution':execution_file}.items()}
    refs.update(failure.get('observations',{}))
    attempt,cap,source=failed_capture(p,b,refs,keys)
    evidence={'kind':KIND,'scope':'rendered_public_web_only','probe_id':attempt['probe_id'],
        'binding_name':b['binding_name'],'authority':current.projection(b),'sources':refs,
        'observed_title':cap['title'],'title_source':source,'validator_sha256':code_hashes(),
        'body_read':False,'comment_read':False,'media_read':False}
    file=directory/'page-presence-evidence.json';sha=nr.immutable(file,evidence)
    return file,sha,evidence


def attest(project,name,attempt_file,execution_file,review_file=None):
    p=xp.Project(project)
    try:
        with xp.project_lock(project):
            b=bridge.load_binding(project,name)
            with nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
                bridge.assert_web_project_access(project,b['account_uid'])
                require(not (p.path/'bridge/PAUSE').exists(),'USER_PAUSED')
                file,sha,evidence=prepare_evidence(p,b,attempt_file,execution_file,keys)
                if review_file is None:
                    return {'evidence_file':str(file),'evidence_sha256':sha,'probe_id':evidence['probe_id'],
                            'review_required':True,'collection_permitted':False,'no_browser_calls':True}
                review_ref,_=nr.freeze(p,review_file);review(p,review_ref,evidence,sha)
                grant_file=file.with_name('page-presence-grant.json')
                created=nr.read_json(grant_file)[0]['created_at'] if grant_file.exists() else xp.legacy.now()
                grant={'kind':KIND,'scope':'rendered_public_web_only','evidence':nr.freeze(p,file)[0],
                       'review':review_ref,'created_at':created}
                pointer={'file':str(grant_file),'sha256':nr.immutable(grant_file,grant)}
                duplicate=b.get(current.POINTER)==pointer
                if not duplicate:
                    attempt=nr.check_ref(p,evidence['sources']['attempt'])
                    require(b.get('state')=='blocked' and b.get('last_error')==attempt['binding_snapshot']['last_error'],
                            'PRESENCE_ORIGINAL_STOP_STATE_CHANGED')
                    b[current.POINTER]=pointer;b['state']='ready'
                    b.setdefault('resolved_errors',[]).append(b.pop('last_error'))
                    b['last_recovery']={'kind':KIND,'probe_id':evidence['probe_id'],'at':created}
                    bridge.save_binding(project,name,b)
                return {'duplicate':duplicate,'grant_file':str(grant_file),'scope':'rendered_public_web_only',
                        'body_read':False,'comment_read':False,'media_read':False,'no_tasks_completed':True,
                        'stop_bytes_unchanged':True,'api_and_cdn_still_stopped':True,'no_browser_calls':True}
    finally:p.close()


def web_exceptions(p,b,keys,grant):
    require(grant.get('kind')==KIND and grant.get('scope')=='rendered_public_web_only','PRESENCE_TYPED_GRANT_REQUIRED')
    evidence=nr.check_ref(p,grant['evidence'])
    require(evidence.get('kind')==KIND and evidence.get('scope')=='rendered_public_web_only'
            and evidence.get('binding_name')==b['binding_name'] and evidence.get('authority')==current.projection(b)
            and evidence.get('validator_sha256')==code_hashes()
            and all(evidence.get(k) is False for k in ('body_read','comment_read','media_read')),
            'PRESENCE_AUTHORITY_OR_VALIDATOR_CHANGED')
    attempt,cap,source=failed_capture(p,b,evidence['sources'],keys,evidence['title_source'])
    require(evidence.get('probe_id')==attempt['probe_id'] and evidence.get('observed_title')==cap['title']
            and grant['evidence']['file']==str(Path(evidence['sources']['attempt']['file']).with_name('page-presence-evidence.json')),
            'PRESENCE_EVIDENCE_CONTEXT_CHANGED')
    review(p,grant['review'],evidence,grant['evidence']['sha256'])
    return {k:s['sha256'] for k,s in attempt['stops'].items()}
