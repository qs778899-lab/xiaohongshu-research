"""Append-only single-create recovery of exact proven-absent fork role tips.

V1 records remain immutable. Later generations link the previous proof by SHA;
only the locked recovery path may project a missing, exactly retired chain tip.
"""
from pathlib import Path
import argparse,copy,hashlib,json,re,time
import xhs_bridge as bridge
import xhs_note_recovery as nr
import xhs_project as xp

def require(value,code):
    if not value:raise bridge.BridgeError(code)

def _retirement(b,role,tip):
    matches=[r for r in b.get('retired_targets',[]) if r.get('role')==role
             and r.get('tab',{}).get('page')==tip.get('page')]
    require(len(matches)==1,'FORK_REPLACEMENT_ABSENCE_PROOF_REQUIRED')
    retired=matches[0];slot=retired.get('tab',{})
    require(slot.get('session')==tip.get('session') and slot.get('owned') is True
            and retired.get('proof')=={'state':'registered_target_absent','page':tip['page']},
            'FORK_REPLACEMENT_ABSENCE_PROOF_REQUIRED')
    return retired


def _stop_hashes(b):
    pointer=b.get('current_page_revalidation',{})
    require(isinstance(pointer,dict) and re.fullmatch(r'[a-f0-9]{64}',str(pointer.get('sha256') or '')),
            'FORK_REPLACEMENT_CURRENT_GRANT_CHANGED')
    grant,_=nr.read_json(pointer.get('file',''),pointer.get('sha256'))
    require(re.fullmatch(r'[a-f0-9]{64}',str(grant.get('probe_sha256') or '')),
            'FORK_REPLACEMENT_CURRENT_GRANT_CHANGED')
    probe,_=nr.read_json(grant.get('probe_file',''),grant.get('probe_sha256'))
    return {k:v['sha256'] for k,v in probe['stops'].items()}


def _resolve_roles(project,b,home,missing_role=None):
    """Validate complete chains. Missing-tip projection is private to recover()."""
    roles=copy.deepcopy(home['roles']);chains={}
    require(set(roles)=={'search','detail'},'FORK_REPLACEMENT_ROLE_INVALID')
    pointers=b.get('fork_role_replacements',{})
    require(isinstance(pointers,dict),'FORK_REPLACEMENT_POINTER_INVALID')
    folder=Path(project).resolve()/'bridge/binding-forks/replacements'/b['binding_name']
    pages=set()
    for role,original in roles.items():
        page=original.get('page','')
        require(isinstance(page,str) and re.fullmatch(r'[A-Fa-f0-9]{32}',page)
                and original.get('session')==b['session']+'-'+role,'FORK_REPLACEMENT_TARGET_INVALID')
        require(page.lower() not in pages,'FORK_REPLACEMENT_TARGET_COLLISION');pages.add(page.lower())

    def resolve(role,pointer,seen,depth=0):
        require(depth<128,'FORK_REPLACEMENT_CHAIN_TOO_DEEP')
        require(isinstance(pointer,dict) and isinstance(pointer.get('file'),str)
                and re.fullmatch(r'[a-f0-9]{64}',str(pointer.get('sha256') or '')),
                'FORK_REPLACEMENT_POINTER_INVALID')
        path=Path(pointer['file']);marker=str(path)
        require(marker not in seen,'FORK_REPLACEMENT_CHAIN_CYCLE')
        require(path.is_absolute() and path.parent==folder and not path.is_symlink(),
                'FORK_REPLACEMENT_PATH_INVALID')
        record,sha=nr.read_json(path,pointer['sha256'])
        require(pointer['sha256']==sha,'FORK_REPLACEMENT_PATH_INVALID')
        kind=record.get('kind')
        require(kind in ('guarded_fork_role_replacement_v1','guarded_fork_role_replacement_v2')
                and record.get('authority')==nr.fork_projection(b)
                and record.get('original_home')==b['fork_home_verification'] and record.get('role')==role,
                'FORK_REPLACEMENT_AUTHORITY_CHANGED')
        if kind.endswith('_v2'):
            prior=record.get('previous_replacement')
            predecessor,chain=resolve(role,prior,seen|{marker},depth+1)
        else:
            require('previous_replacement' not in record,'FORK_REPLACEMENT_CHAIN_INVALID')
            predecessor=home['roles'][role];chain=[]
        expected=folder/(role+'-'+predecessor['page']+'.proof.json')
        require(path==expected,'FORK_REPLACEMENT_PATH_INVALID')
        ip=record.get('intent',{})
        require(isinstance(ip,dict) and re.fullmatch(r'[a-f0-9]{64}',str(ip.get('sha256') or '')),
                'FORK_REPLACEMENT_INTENT_CHANGED')
        intent,ish=nr.read_json(ip.get('file',''),ip['sha256'])
        require(ip['sha256']==ish and Path(ip['file'])==expected.with_name(role+'-'+predecessor['page']+'.intent.json')
                and intent.get('kind')==kind.replace('guarded_fork_role_replacement','single_guarded_fork_role_create')
                and intent.get('authority')==record['authority'] and intent.get('role')==role
                and intent.get('original_home')==record['original_home'], 'FORK_REPLACEMENT_INTENT_CHANGED')
        require(intent.get('previous_replacement')==record.get('previous_replacement'),
                'FORK_REPLACEMENT_CHAIN_INVALID')
        retirement=_retirement(b,role,predecessor)
        require(record.get('retirement')==retirement and intent.get('retirement')==retirement,
                'FORK_REPLACEMENT_RETIREMENT_CHANGED')
        before_ref=intent.get('before_binding',{})
        require(isinstance(before_ref,dict)
                and Path(before_ref.get('file',''))==expected.with_name(role+'-'+predecessor['page']+'.binding-before.json')
                and re.fullmatch(r'[a-f0-9]{64}',str(before_ref.get('sha256') or '')),
                'FORK_REPLACEMENT_INTENT_CHANGED')
        before,_=nr.read_json(before_ref['file'],before_ref['sha256'])
        require(nr.fork_projection(before)==nr.fork_projection(b) and role not in before.get('tabs',{})
                and before.get('fork_role_replacements',{}).get(role)==record.get('previous_replacement')
                and before.get('fork_home_verification')==b['fork_home_verification']
                and before.get('current_page_revalidation')==b.get('current_page_revalidation')
                and _retirement(before,role,predecessor)==retirement,
                'FORK_REPLACEMENT_RESUME_CONTEXT_CHANGED')
        require(record.get('current_grant')==b.get('current_page_revalidation')
                and intent.get('current_grant')==record['current_grant'], 'FORK_REPLACEMENT_CURRENT_GRANT_CHANGED')
        require(record.get('stop_hashes')==_stop_hashes(b) and intent.get('stop_hashes')==record['stop_hashes'],
                'FORK_REPLACEMENT_STOP_SET_CHANGED')
        proof=record.get('proof',{});new=proof.get('page')
        require(isinstance(new,str) and re.fullmatch(r'[A-Fa-f0-9]{32}',new)
                and proof.get('session')==predecessor['session'],'FORK_REPLACEMENT_TARGET_INVALID')
        require(new.lower() not in pages,'FORK_REPLACEMENT_TARGET_COLLISION');pages.add(new.lower())
        for key in ('metadata','identity'):
            observed=proof.get(key,{})
            require(observed.get('page')==new and observed.get('url')=='https://www.xiaohongshu.com/explore'
                    and not observed.get('error') and not any(observed.get(k) for k in ('loginWall','securityBlock','notFound')),
                    'FORK_REPLACEMENT_ACTUAL_HOME_REQUIRED')
        require(proof['identity'].get('uid')==b['account_uid'],'FORK_REPLACEMENT_UID_REQUIRED')
        return proof,chain+[{'pointer':copy.deepcopy(pointer),'record':record}]

    for role,pointer in pointers.items():
        require(role in ('search','detail'),'FORK_REPLACEMENT_ROLE_INVALID')
        roles[role],chains[role]=resolve(role,pointer,set())
    for role,tip in roles.items():
        if role==missing_role:
            require(role not in b.get('tabs',{}),'FORK_REPLACEMENT_ROLE_NOT_EMPTY')
            _retirement(b,role,tip)
        else:
            slot=b.get('tabs',{}).get(role,{})
            require(slot.get('page')==tip['page'] and slot.get('session')==tip['session']
                    and slot.get('owned') is True,'FORK_REPLACEMENT_SLOT_CHANGED')
    return roles,chains


def effective_roles(project,b,home):
    """Public resolver: validate every link and require actual final owned slots."""
    return _resolve_roles(project,b,home)[0]

def grant_validation_binding(project,b):
    """Preserve the original grant projection only for verified replacement roles."""
    if not b.get('fork_role_replacements'):return b
    home,_=nr.read_json(b['fork_home_verification']['file'],b['fork_home_verification']['sha256'])
    effective_roles(project,b,home)
    historical=copy.deepcopy(b)
    for role in b['fork_role_replacements']:
        old=home['roles'][role]
        historical['tabs'][role]=copy.deepcopy(_retirement(b,role,old)['tab'])
    return historical


def recover(project,name,role,driver=None,resume_known_created=None):
    project=Path(project).resolve()
    with xp.project_lock(project):
        b=bridge.load_binding(project,name)
        authority,sha=nr.read_json(b['web_parent_authorization']['file'],b['web_parent_authorization']['sha256'])
        # Existing account registry + every related collection lock; no browser call
        # is allowed until the complete current child grant validates below.
        with nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
            p=nr.native.open_lookup_project(project)
            try:
                import xhs_current_page_recovery as current
                require(role in ('search','detail'),'FORK_REPLACEMENT_ROLE_INVALID')
                # Only under project/account locks: reconstitute an exactly retired
                # verified chain tip in memory. It is never persisted or read.
                historical=copy.deepcopy(b)
                old_home,_=nr.read_json(b['fork_home_verification']['file'],b['fork_home_verification']['sha256'])
                tips,_=_resolve_roles(project,b,old_home,missing_role=role)
                original=tips[role];retirement=_retirement(b,role,original)
                historical['tabs'][role]=copy.deepcopy(retirement['tab'])
                stops=current.web_exceptions(p,historical,keys)
                home,_=nr.completed_fork_home(project,Path(b['web_parent_authorization']['file']),authority,sha)
                require(home==old_home,'FORK_REPLACEMENT_AUTHORITY_CHANGED')
                require(stops==_stop_hashes(b),'FORK_REPLACEMENT_STOP_SET_CHANGED')
                require(role not in b.get('tabs',{}),'FORK_REPLACEMENT_ROLE_NOT_EMPTY')
                previous=copy.deepcopy(b.get('fork_role_replacements',{}).get(role))
                version='v2' if previous else 'v1'
                folder=project/'bridge/binding-forks/replacements'/name;folder.mkdir(parents=True,exist_ok=True)
                base=folder/(role+'-'+original['page']);intent_path=base.with_suffix('.intent.json');proof_path=base.with_suffix('.proof.json')
                candidate=copy.deepcopy(b);candidate['tabs'][role]={'session':original['session'],'owned':False,'state':'creation_pending'}
                driver=driver or bridge.Driver(b['runtime'])
                if resume_known_created:
                    require(re.fullmatch(r'[A-Fa-f0-9]{32}',resume_known_created) and intent_path.exists()
                            and not proof_path.exists(),'FORK_REPLACEMENT_KNOWN_CREATE_REQUIRED')
                    intent,intent_sha=nr.read_json(intent_path)
                    before,_=nr.read_json(intent.get('before_binding',{}).get('file',''),intent.get('before_binding',{}).get('sha256'))
                    require(before==b and intent.get('authority')==nr.fork_projection(b) and intent.get('role')==role
                            and intent.get('retirement')==retirement and intent.get('original_home')==b['fork_home_verification']
                            and intent.get('current_grant')==b['current_page_revalidation'] and intent.get('stop_hashes')==stops
                            and intent.get('kind')=='single_guarded_fork_role_create_'+version
                            and intent.get('previous_replacement')==previous,
                            'FORK_REPLACEMENT_RESUME_CONTEXT_CHANGED')
                    created,created_sha=nr.read_json(base.with_suffix('.created.json'))
                    require(created.get('page')==resume_known_created,'FORK_REPLACEMENT_KNOWN_CREATE_CHANGED')
                    resume_intent=base.with_suffix('.resume-intent.json')
                    require(not resume_intent.exists(),'FORK_REPLACEMENT_RESUME_ALREADY_CONSUMED')
                    nr.immutable(resume_intent,{'kind':'bounded_known_created_home_resume_v1','page':resume_known_created,
                                 'intent':{'file':str(intent_path),'sha256':intent_sha},'created_sha256':created_sha,
                                 'at':bridge.legacy.now(),'max_metadata_reads':3,'no_create_or_navigation':True})
                else:
                    require(not intent_path.exists() and not proof_path.exists(),'FORK_REPLACEMENT_ATTEMPT_ALREADY_CONSUMED')
                    before=base.with_suffix('.binding-before.json');nr.immutable(before,b)
                    intent={'kind':'single_guarded_fork_role_create_'+version,'authority':nr.fork_projection(b),'role':role,'retirement':retirement,
                            'original_home':b['fork_home_verification'],'stop_hashes':stops,'current_grant':b['current_page_revalidation'],'before_binding':{'file':str(before),'sha256':hashlib.sha256(before.read_bytes()).hexdigest()},'created_at':bridge.legacy.now()}
                    if previous:intent['previous_replacement']=previous
                    intent_sha=nr.immutable(intent_path,intent)
                    created=driver.call('create',candidate,role);nr.immutable(base.with_suffix('.created.json'),created)
                page=created.get('page')
                require(isinstance(page,str) and re.fullmatch(r'[A-Fa-f0-9]{32}',page) and page.lower()!=original['page'].lower(),'FORK_REPLACEMENT_TARGET_INVALID')
                for f in (project/'bridge/bindings').glob('*.json'):
                    known=json.loads(f.read_text());require(all(str(s.get('page','')).lower()!=page.lower() for s in known.get('tabs',{}).values()),'FORK_REPLACEMENT_TARGET_COLLISION')
                slot=candidate['tabs'][role];slot.update(page=page,owned=True,state='created',last_url='https://www.xiaohongshu.com/explore')
                for attempt in range(3 if resume_known_created else 1):
                    metadata=driver.call('metadata',candidate,role)
                    suffix='.resume-metadata-'+str(attempt)+'.json' if resume_known_created else '.metadata.json'
                    nr.immutable(base.with_suffix(suffix),metadata)
                    require(metadata.get('page')==page and not metadata.get('error'),'FORK_REPLACEMENT_ACTUAL_HOME_REQUIRED')
                    if metadata.get('url') not in ('','about:blank'):bridge.safe_url(metadata['url'])
                    if metadata.get('url')=='https://www.xiaohongshu.com/explore':break
                    require(resume_known_created and metadata.get('url') in ('','about:blank') and attempt<2,
                            'FORK_REPLACEMENT_ACTUAL_HOME_REQUIRED')
                    time.sleep(1)
                identity=driver.call('identity',candidate,role)
                nr.immutable(base.with_suffix('.resume-identity.json' if resume_known_created else '.identity.json'),identity)
                proof={'page':page,'session':original['session'],'metadata':metadata,'identity':identity}
                record={'kind':'guarded_fork_role_replacement_'+version,'authority':nr.fork_projection(b),'original_home':b['fork_home_verification'],
                        'role':role,'retirement':retirement,'stop_hashes':stops,'current_grant':b['current_page_revalidation'],'intent':{'file':str(intent_path),'sha256':intent_sha},'proof':proof,'observed_at':bridge.legacy.now()}
                if previous:record['previous_replacement']=previous
                proof_sha=nr.immutable(proof_path,record)
                candidate.setdefault('fork_role_replacements',{})[role]={'file':str(proof_path),'sha256':proof_sha}
                effective_roles(project,candidate,home)
                require(bridge.load_binding(project,name)==b,'FORK_REPLACEMENT_BINDING_CHANGED')
                slot['verified_at']=bridge.legacy.now();candidate['state']='blocked'
                candidate['last_error']={'code':'FORK_REPLACEMENT_HOME_VERIFIED_RECOVER_REQUIRED','at':bridge.legacy.now()}
                bridge.save_binding(project,name,candidate)
                return {'role':role,'page':page,'home_verified':True,'proof_file':str(proof_path),'next':'normal recover then body validation; no search yet'}
            except Exception as exc:
                if getattr(exc,'code',None)=='ACCESS_STOP':
                    bridge.save_failure(project,name,b,exc,keys)
                raise
            finally:p.close()

if __name__=='__main__':
    q=argparse.ArgumentParser();q.add_argument('--project',required=True);q.add_argument('--binding',required=True);q.add_argument('--role',choices=['search','detail'],required=True);q.add_argument('--resume-known-created');a=q.parse_args()
    try:print(json.dumps({'ok':True,'data':recover(a.project,a.binding,a.role,resume_known_created=a.resume_known_created)},ensure_ascii=False))
    except Exception as e:print(json.dumps({'ok':False,'error':getattr(e,'code',str(e))},ensure_ascii=False));raise SystemExit(1)
