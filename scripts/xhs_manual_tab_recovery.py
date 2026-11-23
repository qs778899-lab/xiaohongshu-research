"""Bounded inspection of a user-restored public web tab; never clears STOP.

inspect reads the already loaded search/note. probe-note opens one link actually
present in that observation. Neither operation permits ordinary collection.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlparse, parse_qs

import xhs_bridge as bridge
import xhs_note_recovery as nr
import xhs_project as xp

KIND = 'manual_public_web_tab_probe_v1'
GRANT_KIND = 'manual_restored_current_public_web_v1'
AUTHORITY = ('format','project','binding_name','profile_label','context_id','account_uid',
             'profile_guard_key','session','runtime','extension_version','authorization',
             'transport_surface','manual_single_tab')

def pointer(path):
    path=Path(path).resolve()
    return {'file':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

def evidence(path, data):
    nr.immutable(path,data)
    return pointer(path)

def check_pointer(ref):
    return nr.read_json(ref['file'],ref['sha256'])[0]

def note_id(url):
    bridge.safe_url(url)
    m=re.fullmatch(r'/(?:explore|search_result)/([a-f0-9]{24})/?',urlparse(url).path)
    return m[1] if m else None

def excluded_notes(parent):
    inherited=parent.get('manual_excluded_note_ids',[])
    nr.require(isinstance(inherited,list) and all(isinstance(x,str) and re.fullmatch(r'[a-f0-9]{24}',x)
               for x in inherited),'MANUAL_EXCLUSIONS_INVALID')
    original=parent.get('tabs',{}).get('detail',{}).get('navigation_intent',{}).get('url')
    current=note_id(original) if original else None
    return sorted(set(inherited+([current] if current else [])))

def active_metadata(value):
    meta=value.get('metadata',value)
    rows=meta.get('tabs',[meta])
    active=[x for x in rows if x.get('active') is True]
    nr.require(len(active)==1,'MANUAL_UNIQUE_ACTIVE_TAB_REQUIRED')
    t=active[0]
    nr.require(type(t.get('tabId')) is int and type(t.get('windowId')) is int,'MANUAL_NATIVE_IDS_REQUIRED')
    bridge.safe_url(t.get('url',''))
    return t

def proof_args(url):
    parsed=urlparse(url)
    if parsed.path.rstrip('/')=='/explore':
        return {'stage':'home','backgroundText':True}
    if note_id(url):
        return {'stage':'body','expectedPath':'/explore/'+note_id(url),'backgroundText':True}
    query=parse_qs(parsed.query).get('keyword',[])
    nr.require(parsed.path.rstrip('/')=='/search_result' and len(query)==1,'MANUAL_NOTE_OR_SEARCH_REQUIRED')
    return {'stage':'search','expectedPath':parsed.path.rstrip('/'),'query':query[0],'backgroundText':True}

def require_capture(data,b,args):
    nr.require(isinstance(data,dict) and not data.get('error'),'MANUAL_CAPTURE_FAILED')
    nr.require(data.get('uid')==b['account_uid'],'ACCOUNT_MISMATCH')
    actual=proof_args(data.get('url',''))
    nr.require(actual==args,'MANUAL_CAPTURE_TARGET_CHANGED')
    if args['stage']=='home':
        return
    if args['stage']=='search':
        nr.require(bool(data.get('cards')),'MANUAL_SEARCH_CONTENT_REQUIRED')
    else:
        nr.require(not any(data.get(k) for k in ('loginWall','securityBlock','notFound','loading')),
                   'MANUAL_CAPTURE_FAILED')
        title,body=data.get('title'),data.get('body')
        nr.require(isinstance(title,str) and isinstance(body,str) and bool(body.strip()),
                   'MANUAL_NOTE_CONTENT_REQUIRED')
        nr.require(bool(data.get('authors')),'MANUAL_NOTE_AUTHOR_REQUIRED')
        if not title.strip():
            # An observed empty title is valid; an absent title or loading shell is not.
            authors=data['authors']
            nr.require(data.get('stage')=='body'
                       and body.strip() not in {'[图片]','[图片评论]','加载中','加载中...','加载中…','正在加载...', 'Loading...','Loading…'}
                       and isinstance(authors,list)
                       and any(valid_public_author(a) for a in authors),'MANUAL_NOTE_AUTHOR_REQUIRED')

def valid_public_author(author):
    if not isinstance(author,dict) or not isinstance(author.get('name'),str) or not author['name'].strip():
        return False
    url=author.get('url')
    if not isinstance(url,str):return False
    try:parsed=urlparse(url)
    except ValueError:return False
    return (parsed.scheme=='https' and parsed.netloc=='www.xiaohongshu.com' and not parsed.fragment
            and re.fullmatch(r'/user/profile/[a-f0-9]{24}/?',parsed.path) is not None)

def inspect(project,parent_name,metadata_file,user_file,parent_project=None):
    project=Path(project).resolve()
    xp.legacy.research_path(project)
    source_project=Path(parent_project).resolve() if parent_project is not None else project
    parent=bridge.load_binding(source_project,parent_name)
    metadata,_=nr.read_json(metadata_file)
    target=active_metadata(metadata)
    nr.require(note_id(target['url']) not in excluded_notes(parent),'ORIGINAL_RESTRICTED_NOTE_EXCLUDED')
    user,_=nr.read_json(user_file)
    nr.require(user.get('user_statement') and user.get('normal_other_posts_statement'),'MANUAL_USER_RESTORATION_REQUIRED')
    when=nr.stamp(user.get('processed_at'))
    args=proof_args(target['url'])
    with nr.locked_stops(parent['profile_guard_key'],parent['account_uid']) as keys:
        stops=nr.current_stops(keys)
        nr.require(all(when>nr.stamp(s['at']) for s in stops.values()),'MANUAL_CONFIRMATION_PREDATES_STOP')
        attempt_id=xp.digest({'stops':stops,'context':parent['context_id'],'tabId':target['tabId']})
        root=project/'bridge'/'manual-tab-recoveries'/attempt_id
        root.mkdir(parents=True,exist_ok=True)
        nr.require(not (root/'attempt.json').exists(),'MANUAL_INSPECT_ALREADY_ATTEMPTED')
        old_attempt=source_project/'bridge'/'manual-tab-recoveries'/attempt_id/'attempt.json'
        nr.require(not old_attempt.exists(),'MANUAL_INSPECT_ALREADY_ATTEMPTED')
        reservation=xp.legacy.account_root()/'manual-tab-attempts'/(attempt_id+'.json')
        nr.require(not reservation.exists(),'MANUAL_INSPECT_ALREADY_ATTEMPTED')
        reservation_ref=evidence(reservation,{'kind':'manual_tab_attempt_reservation_v1',
            'project':str(project),'parent_project':str(source_project),'context_id':parent['context_id'],
            'tab_id':target['tabId'],'stops':stops,'at':xp.legacy.now()})
        slot={'session':'xhs-manual-'+attempt_id[:24],'owned':False,'state':'user_bound',
              'window_id':target['windowId'],'chrome_tab_id':target['tabId'],'last_url':target['url']}
        b={k:parent[k] for k in ('format','profile_label','context_id','account_uid','profile_guard_key','runtime')}
        b['project']=str(project)
        b.update(binding_name='manual-'+attempt_id[:24],session=slot['session'],
                 extension_version=bridge.effective_extension_version(parent),
                 authorization=user['user_statement'],transport_surface='browser',manual_single_tab=True,
                 tabs={'search':slot,'detail':slot},state='probe_only',background_text_collection=True)
        frozen_stops={k:evidence(root/('stop-'+k+'.json'),nr.read_json(s['file'],s['sha256'])[0]) for k,s in stops.items()}
        attempt={'kind':KIND,'at':xp.legacy.now(),'project':str(project),'parent_project':str(source_project),
                 'parent':evidence(root/'parent.json',parent),'account_attempt':reservation_ref,
                 'metadata':evidence(root/'user-tab-metadata.json',metadata),'user':evidence(root/'user-request.json',user),
                 'target':target,'stops':stops,'frozen_stops':frozen_stops,'candidate':b,'args':args}
        evidence(root/'attempt.json',attempt)
        driver=bridge.Driver(b['runtime'])
        try:
            adopted=driver.call('adopt_user_tab',b,chromeTabId=target['tabId'],windowId=target['windowId'],expectedUrl=target['url'])
            evidence(root/'adopted.json',adopted)
            slot['page']=adopted['page']
            ident=driver.call('identity',b,'detail')
            evidence(root/'identity.json',ident)
            nr.require(ident.get('uid')==b['account_uid'] and ident.get('url')==target['url'],'MANUAL_IDENTITY_CHANGED')
            capture=ident if args['stage']=='home' else driver.call('capture',b,'detail',**args)
            capture_ref=evidence(root/'loaded-page.json',capture)
            require_capture(capture,b,args)
            nr.require(nr.current_stops(keys)==stops,'MANUAL_STOP_CHANGED')
            nr.require(bridge.load_binding(source_project,parent_name)==parent,'MANUAL_PARENT_CHANGED')
            result={'kind':KIND,'at':xp.legacy.now(),'attempt':pointer(root/'attempt.json'),'binding':b,
                    'capture':capture_ref,'collection_permitted':False,'original_binding_unchanged':True}
            evidence(root/'inspection.json',result)
            return {'inspection':str(root/'inspection.json'),'capture':capture_ref,'collection_permitted':False}
        except Exception as exc:
            evidence(root/'inspection-failure.json',{'at':xp.legacy.now(),'error':str(exc),'collection_permitted':False})
            raise
        finally:driver.close()

def finish_inspection(attempt_file, verify_focus=False):
    """One continuation after a proven pre-bind focus failure; no unknown retries."""
    attempt_file=Path(attempt_file).resolve();attempt,_=nr.read_json(attempt_file)
    root=attempt_file.parent
    failure,_=nr.read_json(root/'inspection-failure.json')
    nr.require(failure.get('error')=='USER_TAB_NOT_FOCUSED','MANUAL_PREBIND_FOCUS_FAILURE_REQUIRED')
    nr.require(not any((root/name).exists() for name in ('adopted.json','identity.json','loaded-page.json','inspection.json')),
               'MANUAL_INSPECTION_MAY_HAVE_PROGRESS')
    marker='finish-inspection-attempt.json'
    failure_name='finish-inspection-failure.json'
    previous=None
    if verify_focus:
        previous=pointer(root/marker)
        prior=check_pointer(previous)
        nr.require(prior.get('attempt')==pointer(attempt_file)
                   and prior.get('failure')==pointer(root/'inspection-failure.json'),'MANUAL_ATTEMPT_CHANGED')
        prior_failure,_=nr.read_json(root/failure_name)
        nr.require(prior_failure.get('error')=='USER_TAB_NOT_FOCUSED','MANUAL_PREBIND_FOCUS_FAILURE_REQUIRED')
        marker='verified-focus-attempt.json'
        failure_name='verified-focus-failure.json'
    nr.require(not (root/marker).exists(),'MANUAL_FINISH_INSPECTION_ALREADY_ATTEMPTED')
    project=Path(attempt['project']).resolve();xp.legacy.research_path(project)
    nr.require(root.parent==project/'bridge'/'manual-tab-recoveries','MANUAL_PROJECT_CHANGED')
    parent=check_pointer(attempt['parent']);metadata=check_pointer(attempt['metadata']);user=check_pointer(attempt['user'])
    target=active_metadata(metadata)
    nr.require(attempt['target']==target and attempt['args']==proof_args(target['url']),'MANUAL_ATTEMPT_CHANGED')
    expected_id=xp.digest({'stops':attempt['stops'],'context':parent['context_id'],'tabId':target['tabId']})
    nr.require(root.name==expected_id and attempt_file.name=='attempt.json','MANUAL_ATTEMPT_LOCATION_CHANGED')
    expected_reservation=(xp.legacy.account_root()/'manual-tab-attempts'/(expected_id+'.json')).resolve()
    nr.require(Path(attempt['account_attempt']['file']).resolve()==expected_reservation,'MANUAL_RESERVATION_LOCATION_CHANGED')
    slot={'session':'xhs-manual-'+expected_id[:24],'owned':False,'state':'user_bound',
          'window_id':target['windowId'],'chrome_tab_id':target['tabId'],'last_url':target['url']}
    expected={k:parent[k] for k in ('format','profile_label','context_id','account_uid','profile_guard_key','runtime')}
    expected['project']=str(project)
    expected.update(binding_name='manual-'+expected_id[:24],session=slot['session'],
                    extension_version=bridge.effective_extension_version(parent),authorization=user['user_statement'],
                    transport_surface='browser',manual_single_tab=True,tabs={'search':slot,'detail':slot},
                    state='probe_only',background_text_collection=True)
    nr.require(attempt['candidate']==expected,'MANUAL_CANDIDATE_CHANGED')
    nr.require(note_id(target['url']) not in excluded_notes(parent),'ORIGINAL_RESTRICTED_NOTE_EXCLUDED')
    nr.require(all(nr.stamp(user.get('processed_at'))>nr.stamp(v['at']) for v in attempt['stops'].values()),
               'MANUAL_CONFIRMATION_PREDATES_STOP')
    reservation=check_pointer(attempt['account_attempt'])
    nr.require(reservation.get('kind')=='manual_tab_attempt_reservation_v1'
               and reservation.get('project')==str(project) and reservation.get('tab_id')==target['tabId']
               and reservation.get('parent_project')==attempt.get('parent_project',attempt['project'])
               and reservation.get('context_id')==parent['context_id'] and reservation.get('stops')==attempt['stops'],
               'MANUAL_ATTEMPT_CHANGED')
    source_project=Path(attempt.get('parent_project',attempt['project']))
    nr.require(bridge.load_binding(source_project,parent['binding_name'])==parent,'MANUAL_PARENT_CHANGED')
    b=attempt['candidate'];target=attempt['target'];args=attempt['args']
    with nr.locked_stops(parent['profile_guard_key'],parent['account_uid']) as keys:
        nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
        driver=bridge.Driver(b['runtime'])
        try:
            focus=None
            if verify_focus:
                # Metadata only; a failed check cannot bind or navigate any page.
                focus=driver.call('active_user_tab',b)
                nr.require(all(focus.get(k)==target[k] for k in ('tabId','windowId','url','active')),
                           'MANUAL_FOCUSED_TARGET_CHANGED')
                nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
            # Exclusive marker is written before the original adoption call, including its focus recheck.
            with (root/marker).open('x') as f:
                json.dump({'at':xp.legacy.now(),'attempt':pointer(attempt_file),
                           'failure':pointer(root/'inspection-failure.json'),
                           'previous':previous,'focus_metadata':focus},f)
            # The original driver still enforces exact tab, URL, active/focused window and pinned versions.
            adopted=driver.call('adopt_user_tab',b,chromeTabId=target['tabId'],windowId=target['windowId'],expectedUrl=target['url'])
            evidence(root/'adopted.json',adopted)
            for slot in b['tabs'].values():slot['page']=adopted['page']
            ident=driver.call('identity',b,'detail');evidence(root/'identity.json',ident)
            nr.require(ident.get('uid')==b['account_uid'] and ident.get('url')==target['url'],'MANUAL_IDENTITY_CHANGED')
            capture=ident if args['stage']=='home' else driver.call('capture',b,'detail',**args)
            capture_ref=evidence(root/'loaded-page.json',capture);require_capture(capture,b,args)
            nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
            nr.require(bridge.load_binding(source_project,parent['binding_name'])==parent,'MANUAL_PARENT_CHANGED')
            result={'kind':KIND,'at':xp.legacy.now(),'attempt':pointer(attempt_file),'binding':b,
                    'capture':capture_ref,'collection_permitted':False,'original_binding_unchanged':True,
                    'finish_inspection':pointer(root/marker)}
            evidence(root/'inspection.json',result)
            return {'inspection':str(root/'inspection.json'),'capture':capture_ref,'collection_permitted':False}
        except Exception as exc:
            failure_data={'at':xp.legacy.now(),'error':str(exc),'collection_permitted':False}
            if verify_focus and not (root/marker).exists():
                # Read-only diagnostics must not occupy the final adoption outcome file.
                diagnostic=root/'focus-diagnostics'/(xp.digest(failure_data)+'.json')
                evidence(diagnostic,failure_data)
            else:
                evidence(root/failure_name,failure_data)
            raise
        finally:driver.close()

def inspect_active(project,parent_name,user_file,parent_project=None):
    """Observe only the active XHS tab in the original profile's focused window."""
    project=Path(project).resolve();xp.legacy.research_path(project)
    source_project=Path(parent_project).resolve() if parent_project is not None else project
    parent=bridge.load_binding(source_project,parent_name)
    user,_=nr.read_json(user_file)
    nr.require(user.get('user_statement') and user.get('normal_other_posts_statement'),'MANUAL_USER_RESTORATION_REQUIRED')
    driver=bridge.Driver(parent['runtime'])
    try:
        with nr.locked_stops(parent['profile_guard_key'],parent['account_uid']) as keys:
            stops=nr.current_stops(keys)
            meta=driver.call('active_user_tab',parent)
            nr.require(nr.current_stops(keys)==stops,'MANUAL_STOP_CHANGED')
    finally:driver.close()
    folder=project/'bridge/manual-active-observations';folder.mkdir(parents=True,exist_ok=True)
    path=folder/(xp.digest({'metadata':meta,'user':pointer(user_file),'stops':stops})+'.json')
    evidence(path,{'metadata':meta,'observed_at':xp.legacy.now(),'scope':'active_xhs_url_metadata_only'})
    return inspect(project,parent_name,path,user_file,parent_project=source_project)

def probe_search(inspection_file,task_id):
    inspected,_=nr.read_json(inspection_file)
    attempt=check_pointer(inspected['attempt']);loaded=check_pointer(inspected['capture'])
    loaded_args=proof_args(loaded['url'])
    nr.require(loaded_args['stage'] in {'home','body'},'MANUAL_HOME_OR_BODY_INSPECTION_REQUIRED')
    require_capture(loaded,inspected['binding'],loaded_args)
    b=inspected['binding'];root=Path(inspection_file).parent
    db=xp.Project(b['project'])
    try:task=db.task(task_id)
    finally:db.close()
    nr.require(task.get('kind')=='native' and task['payload'].get('stage')=='search','EXISTING_SEARCH_TASK_REQUIRED')
    url=bridge.observed_url(b['project'],task);args=proof_args(url)
    with nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
        nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
        nr.require(not (root/'search-attempt.json').exists(),'MANUAL_SEARCH_ALREADY_ATTEMPTED')
        evidence(root/'search-attempt.json',{'at':xp.legacy.now(),'task':task_id,'url':url,
                 'inspection':pointer(inspection_file),'scope':'one_existing_query_health_check_only'})
        driver=bridge.Driver(b['runtime'])
        try:
            meta=driver.call('metadata',b,'detail');evidence(root/'search-before.json',meta)
            nr.require(meta.get('url')==loaded['url'],'MANUAL_PAGE_CHANGED')
            ident=driver.call('identity',b,'detail')
            nr.require(ident.get('uid')==b['account_uid'],'MANUAL_IDENTITY_CHANGED')
            evidence(root/'search-navigation.json',driver.call('navigate',b,'detail',url=url))
            time.sleep(1)
            for index in range(3):
                meta=driver.call('metadata',b,'detail');evidence(root/f'search-metadata-{index}.json',meta)
                nr.require(meta.get('page')==b['tabs']['detail']['page'],'MANUAL_PAGE_CHANGED')
                nr.require(proof_args(meta['url'])==args,'MANUAL_QUERY_CHANGED')
                try:
                    captured=driver.call('capture',b,'detail',**args)
                    evidence(root/f'search-capture-{index}.json',captured)
                    require_capture(captured,b,args)
                    break
                except bridge.BridgeError as exc:
                    if exc.code not in {'CONTENT_LOADING','BACKGROUND_LAYOUT_UNSETTLED'} or index==2:raise
                    time.sleep(1)
            nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
            b['tabs']['detail']['last_url']=captured['url'];b['tabs']['search']=b['tabs']['detail']
            result=dict(inspected,binding=b,capture=pointer(root/f'search-capture-{index}.json'),
                        initial_inspection=pointer(inspection_file),search_task=task_id,at=xp.legacy.now())
            evidence(root/'search-probe.json',result)
            return {'inspection':str(root/'search-probe.json'),'capture':result['capture'],'collection_permitted':False}
        except Exception as exc:
            evidence(root/'search-failure.json',{'at':xp.legacy.now(),'error':str(exc),'collection_permitted':False})
            raise
        finally:driver.close()

def probe_note(inspection_file,url):
    inspected,_=nr.read_json(inspection_file)
    attempt=check_pointer(inspected['attempt'])
    loaded=check_pointer(inspected['capture'])
    b=inspected['binding'];root=Path(inspection_file).parent
    target=note_id(url)
    nr.require(target,'MANUAL_NOTE_LINK_REQUIRED')
    nr.require(url in {c.get('url') for c in loaded.get('cards',[])},'MANUAL_ACTUALLY_OBSERVED_CARD_REQUIRED')
    parent=check_pointer(attempt['parent'])
    original=parent.get('tabs',{}).get('detail',{}).get('navigation_intent',{}).get('url')
    nr.require(target not in excluded_notes(parent),'ORIGINAL_RESTRICTED_NOTE_EXCLUDED')
    with nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
        nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
        nr.require(not (root/'note-attempt.json').exists(),'MANUAL_NOTE_ALREADY_ATTEMPTED')
        evidence(root/'note-attempt.json',{'at':xp.legacy.now(),'url':url,'note_id':target,
                 'inspection':pointer(inspection_file),'scope':'one_observed_public_note_health_check_only'})
        driver=bridge.Driver(b['runtime'])
        try:
            before=driver.call('metadata',b,'detail')
            evidence(root/'note-before.json',before)
            nr.require(before.get('url')==loaded['url'],'MANUAL_PAGE_CHANGED')
            driver.call('identity',b,'detail')
            navigation=driver.call('navigate',b,'detail',url=url)
            evidence(root/'note-navigation.json',navigation)
            time.sleep(1)
            args=proof_args(url)
            capture=None
            for index in range(3):
                meta=driver.call('metadata',b,'detail')
                evidence(root/f'note-metadata-{index}.json',meta)
                nr.require(meta.get('page')==b['tabs']['detail']['page'],'MANUAL_PAGE_CHANGED')
                nr.require(note_id(meta['url'])==target,'MANUAL_NOTE_TARGET_CHANGED')
                try:
                    capture=driver.call('capture',b,'detail',**args)
                    evidence(root/f'note-capture-{index}.json',capture)
                    require_capture(capture,b,args)
                    break
                except bridge.BridgeError as exc:
                    if exc.code not in {'CONTENT_LOADING','BACKGROUND_LAYOUT_UNSETTLED'} or index==2:raise
                    time.sleep(1)
            nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
            b['tabs']['detail']['last_url']=capture['url']
            b['tabs']['search']=b['tabs']['detail']
            result={'kind':KIND,'at':xp.legacy.now(),'inspection':pointer(inspection_file),'binding':b,
                    'capture':pointer(root/f'note-capture-{index}.json'),'excluded_note_id':note_id(original) if original else None,
                    'collection_permitted':False,'stops_unchanged':True}
            evidence(root/'note-probe.json',result)
            return {'probe':str(root/'note-probe.json'),'capture':result['capture'],'collection_permitted':False}
        except Exception as exc:
            evidence(root/'note-failure.json',{'at':xp.legacy.now(),'error':str(exc),'collection_permitted':False})
            raise
        finally:driver.close()

def finish_search(inspection_file,layout_observation=None):
    """One same-page read after equivalent redirect or observed layout recovery."""
    inspected,_=nr.read_json(inspection_file);root=Path(inspection_file).parent
    attempt=check_pointer(inspected['attempt']);b=inspected['binding']
    failure,_=nr.read_json(root/'search-failure.json')
    nav,_=nr.read_json(root/'search-navigation.json')
    previous,_=nr.read_json(root/'search-metadata-0.json')
    started,_=nr.read_json(root/'search-attempt.json')
    local_layout=failure.get('error') in {'BACKGROUND_LAYOUT_UNSETTLED','CONTENT_LOADING'}
    nr.require((failure.get('error')=='MANUAL_QUERY_CHANGED' or (local_layout and layout_observation))
               and nav.get('navigationSubmitted') is True,
               'MANUAL_READ_CONTINUATION_NOT_APPLICABLE')
    args=proof_args(started['url'])
    nr.require(proof_args(previous['url'])==args and previous.get('page')==b['tabs']['detail']['page'],
               'MANUAL_REDIRECT_NOT_EQUIVALENT')
    observation_ref=None
    if local_layout:
        observed,_=nr.read_json(layout_observation)
        nr.require(observed.get('reader')=='codex' and observed.get('account_uid')==b['account_uid']
                   and observed.get('normal_search_visible') is True and observed.get('no_access_warning') is True
                   and observed.get('window_restored') is True and bool(observed.get('summary')),
                   'MANUAL_LAYOUT_OBSERVATION_REQUIRED')
        nr.require(proof_args(observed.get('url',''))==args
                   and nr.stamp(observed.get('observed_at'))>nr.stamp(failure['at']),
                   'MANUAL_LAYOUT_OBSERVATION_SCOPE_CHANGED')
        observation_ref=pointer(layout_observation)
    with nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
        nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
        nr.require(not (root/'search-read-continuation.json').exists(),'MANUAL_READ_ALREADY_CONTINUED')
        evidence(root/'search-read-continuation.json',{'at':xp.legacy.now(),'failure':pointer(root/'search-failure.json'),
                'previous_metadata':pointer(root/'search-metadata-0.json'),'navigation':False,
                **({'layout_observation':observation_ref} if observation_ref else {})})
        driver=bridge.Driver(b['runtime'])
        try:
            meta=driver.call('metadata',b,'detail');evidence(root/'search-resumed-metadata.json',meta)
            nr.require(meta.get('page')==previous['page'] and proof_args(meta['url'])==args,'MANUAL_PAGE_CHANGED')
            captured=driver.call('capture',b,'detail',**args)
            capture_ref=evidence(root/'search-resumed-capture.json',captured)
            require_capture(captured,b,args)
            nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
            b['tabs']['detail']['last_url']=captured['url'];b['tabs']['search']=b['tabs']['detail']
            result=dict(inspected,binding=b,capture=capture_ref,initial_inspection=pointer(inspection_file),
                        search_task=started['task'],at=xp.legacy.now())
            evidence(root/'search-probe.json',result)
            return {'inspection':str(root/'search-probe.json'),'capture':capture_ref,'collection_permitted':False}
        finally:driver.close()

def verified_search_context(inspection_file, user, refs=None):
    """Validate the original captcha failure and a later, exact-scope user reply."""
    inspection_file=Path(inspection_file).resolve();root=inspection_file.parent
    inspected,_=nr.read_json(inspection_file)
    attempt=check_pointer(inspected['attempt']);parent=check_pointer(attempt['parent'])
    b=inspected['binding'];project=Path(attempt['project']).resolve()
    expected=xp.digest({'stops':attempt['stops'],'context':parent['context_id'],'tabId':attempt['target']['tabId']})
    nr.require(root==project/'bridge/manual-tab-recoveries'/expected and inspection_file.name=='inspection.json',
               'MANUAL_ATTEMPT_LOCATION_CHANGED')
    nr.require(inspected['attempt']==pointer(root/'attempt.json'),'MANUAL_ATTEMPT_CHANGED')
    nr.require(inspected.get('kind')==KIND and b.get('project')==str(project)
               and b.get('manual_single_tab') is True and b.get('transport_surface')=='browser',
               'MANUAL_ATTEMPT_CHANGED')
    for field in ('context_id','account_uid','profile_guard_key','profile_label','runtime'):
        nr.require(b.get(field)==parent.get(field),'MANUAL_PARENT_IDENTITY_CHANGED')
    nr.require(set(b.get('tabs',{}))=={'search','detail'} and b['tabs']['search']==b['tabs']['detail'],
               'MANUAL_PAGE_CHANGED')
    slot=b['tabs']['detail'];target=attempt['target']
    nr.require(slot.get('chrome_tab_id')==target['tabId'] and slot.get('window_id')==target['windowId']
               and slot.get('owned') is False,'MANUAL_PAGE_CHANGED')
    adopted,_=nr.read_json(root/'adopted.json')
    nr.require(slot.get('page')==adopted.get('page') and bool(slot.get('page')),'MANUAL_PAGE_CHANGED')
    candidate=json.loads(json.dumps(attempt['candidate']))
    for candidate_slot in candidate['tabs'].values():candidate_slot['page']=adopted['page']
    nr.require(authority(b)==authority(candidate),'MANUAL_AUTHORITY_CHANGED')
    reserved=check_pointer(attempt['account_attempt'])
    reservation=(xp.legacy.account_root()/'manual-tab-attempts'/(expected+'.json')).resolve()
    nr.require(Path(attempt['account_attempt']['file']).resolve()==reservation
               and reserved.get('project')==str(project) and reserved.get('context_id')==b['context_id']
               and reserved.get('tab_id')==target['tabId'] and reserved.get('stops')==attempt['stops']
               and reserved.get('parent_project')==attempt.get('parent_project',attempt['project']),
               'MANUAL_ATTEMPT_RESERVATION_CHANGED')
    loaded=check_pointer(inspected['capture']);args=proof_args(loaded['url'])
    nr.require(args['stage']=='home','MANUAL_HOME_INSPECTION_REQUIRED');require_capture(loaded,b,args)
    names={'failure':'search-failure.json','navigation':'search-navigation.json',
           'previous_metadata':'search-metadata-0.json','search_attempt':'search-attempt.json'}
    actual_refs={key:pointer(root/name) for key,name in names.items()}
    nr.require(refs is None or refs==actual_refs,'MANUAL_SEARCH_HISTORY_CHANGED')
    nr.require(user.get('history')==actual_refs,'MANUAL_CONFIRMATION_HISTORY_CHANGED')
    saved={key:check_pointer(ref) for key,ref in actual_refs.items()}
    failure,nav,previous,started=(saved[k] for k in ('failure','navigation','previous_metadata','search_attempt'))
    nr.require(failure.get('error')=='ACCESS_STOP' and nav.get('navigationSubmitted') is True,
               'MANUAL_VERIFIED_SEARCH_NOT_APPLICABLE')
    nr.require(started.get('inspection')==pointer(inspection_file)
               and started.get('scope')=='one_existing_query_health_check_only','MANUAL_SEARCH_HISTORY_CHANGED')
    args=proof_args(started['url']);nr.require(args['stage']=='search','MANUAL_ACTUAL_SEARCH_REQUIRED')
    db=nr.native.open_lookup_project(project)
    try:task=db.db.execute('SELECT kind,payload FROM tasks WHERE id=?',(started.get('task'),)).fetchone()
    finally:db.close()
    nr.require(task and task['kind']=='native','EXISTING_SEARCH_TASK_REQUIRED')
    payload=json.loads(task['payload'])
    nr.require(payload.get('stage')=='search' and payload.get('target')==args['query'],
               'MANUAL_SEARCH_TASK_CHANGED')
    parsed=urlparse(previous.get('url',''));redirects=parse_qs(parsed.query,keep_blank_values=True).get('redirectPath',[])
    nr.require(parsed.scheme=='https' and parsed.netloc=='www.xiaohongshu.com'
               and parsed.path=='/website-login/captcha' and not parsed.fragment and len(redirects)==1,
               'MANUAL_SEARCH_CAPTCHA_REDIRECT_REQUIRED')
    nr.require(proof_args(redirects[0])==args and previous.get('page')==slot['page'],
               'MANUAL_SEARCH_CAPTCHA_TARGET_CHANGED')
    nr.require(isinstance(user,dict) and isinstance(user.get('user_statement'),str)
               and user['user_statement'].strip() and isinstance(user.get('normal_other_posts_statement'),str)
               and user['normal_other_posts_statement'].strip(),'MANUAL_USER_RESTORATION_REQUIRED')
    nr.require(user.get('inspection')==pointer(inspection_file) and user.get('account_uid')==b['account_uid']
               and user.get('context_id')==b['context_id'] and user.get('tab_id')==slot['chrome_tab_id']
               and user.get('page')==slot['page'],'MANUAL_CONFIRMATION_SCOPE_CHANGED')
    when=nr.stamp(user.get('processed_at'))
    nr.require(when>nr.stamp(started.get('at')) and when>nr.stamp(failure.get('at'))
               and all(when>nr.stamp(s['at']) for s in attempt['stops'].values()),
               'MANUAL_CONFIRMATION_PREDATES_FAILURE')
    for ref in attempt['frozen_stops'].values():check_pointer(ref)
    return inspected,attempt,args,actual_refs

def resume_verified_search(inspection_file,user_file):
    """One read of a user-confirmed search after captcha; no navigation or solving."""
    user,_=nr.read_json(user_file)
    inspected,attempt,args,refs=verified_search_context(inspection_file,user)
    root=Path(inspection_file).resolve().parent;b=inspected['binding']
    marker=root/'verified-search-continuation.json';parent=check_pointer(attempt['parent'])
    source_project=Path(attempt.get('parent_project',attempt['project']))
    with nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
        nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
        nr.require(bridge.load_binding(source_project,parent['binding_name'])==parent,'MANUAL_PARENT_CHANGED')
        nr.require(not marker.exists() and not (root/'search-probe.json').exists(),
                   'MANUAL_VERIFIED_SEARCH_ALREADY_ATTEMPTED')
        user_ref=evidence(root/'verified-search-user.json',user)
        continuation=evidence(marker,{'kind':'user_verified_same_search_read_v1','at':xp.legacy.now(),
            'inspection':pointer(inspection_file),'user':user_ref,'history':refs,
            'navigation':False,'scope':'current_exact_search_after_explicit_user_verification'})
        driver=bridge.Driver(b['runtime'])
        try:
            meta=driver.call('metadata',b,'detail');meta_ref=evidence(root/'verified-search-metadata.json',meta)
            bridge.safe_url(meta.get('url',''))
            nr.require(meta.get('page')==b['tabs']['detail']['page'] and proof_args(meta.get('url',''))==args,
                       'MANUAL_PAGE_CHANGED')
            ident=driver.call('identity',b,'detail');ident_ref=evidence(root/'verified-search-identity.json',ident)
            bridge.safe_url(ident.get('url',''))
            nr.require(ident.get('page')==b['tabs']['detail']['page']
                       and ident.get('uid')==b['account_uid'] and proof_args(ident.get('url',''))==args,
                       'MANUAL_IDENTITY_CHANGED')
            captured=driver.call('capture',b,'detail',**args)
            capture_ref=evidence(root/'verified-search-capture.json',captured)
            bridge.safe_url(captured.get('url',''));require_capture(captured,b,args)
            nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
            nr.require(bridge.load_binding(source_project,parent['binding_name'])==parent,'MANUAL_PARENT_CHANGED')
            b['tabs']['detail']['last_url']=captured['url'];b['tabs']['search']=b['tabs']['detail']
            started=check_pointer(refs['search_attempt'])
            result=dict(inspected,binding=b,capture=capture_ref,initial_inspection=pointer(inspection_file),
                search_task=started['task'],at=xp.legacy.now(),verified_search_continuation=continuation,
                verified_search_metadata=meta_ref,verified_search_identity=ident_ref)
            evidence(root/'search-probe.json',result)
            return {'inspection':str(root/'search-probe.json'),'capture':capture_ref,'collection_permitted':False}
        except Exception as exc:
            evidence(root/'verified-search-failure.json',{'at':xp.legacy.now(),'error':str(exc),'collection_permitted':False})
            if getattr(exc,'code',None)=='ACCESS_STOP':nr.identity.stop_all(keys,nr.STOP_REASON)
            raise
        finally:driver.close()

def validate_verified_search(inspected):
    """Keep the user's later confirmation and original failed attempt in the grant chain."""
    ref=inspected['verified_search_continuation'];record=check_pointer(ref)
    initial_ref=inspected['initial_inspection'];root=Path(initial_ref['file']).parent
    nr.require(ref==pointer(root/'verified-search-continuation.json')
               and record.get('kind')=='user_verified_same_search_read_v1'
               and record.get('inspection')==initial_ref and record.get('navigation') is False
               and record.get('scope')=='current_exact_search_after_explicit_user_verification',
               'MANUAL_VERIFIED_SEARCH_CHAIN_CHANGED')
    nr.require(record.get('user')==pointer(root/'verified-search-user.json'),'MANUAL_CONFIRMATION_SCOPE_CHANGED')
    initial,attempt,args,refs=verified_search_context(initial_ref['file'],check_pointer(record['user']),record['history'])
    nr.require(authority(inspected['binding'])==authority(initial['binding']),'MANUAL_AUTHORITY_CHANGED')
    for key,name in [('verified_search_metadata','verified-search-metadata.json'),
                     ('verified_search_identity','verified-search-identity.json'),('capture','verified-search-capture.json')]:
        nr.require(inspected.get(key)==pointer(root/name),'MANUAL_VERIFIED_SEARCH_CHAIN_CHANGED')
    meta=check_pointer(inspected['verified_search_metadata']);ident=check_pointer(inspected['verified_search_identity'])
    nr.require(meta.get('page')==initial['binding']['tabs']['detail']['page'] and proof_args(meta['url'])==args,
               'MANUAL_PAGE_CHANGED')
    nr.require(ident.get('page')==initial['binding']['tabs']['detail']['page']
               and ident.get('uid')==initial['binding']['account_uid'] and proof_args(ident['url'])==args,
               'MANUAL_IDENTITY_CHANGED')
    require_capture(check_pointer(inspected['capture']),initial['binding'],args)

def finish_note(inspection_file):
    """Validate saved normal content after a local URL or empty-title rejection."""
    inspected,_=nr.read_json(inspection_file);root=Path(inspection_file).parent
    attempt=check_pointer(inspected['attempt']);b=inspected['binding']
    failure,_=nr.read_json(root/'note-failure.json');started,_=nr.read_json(root/'note-attempt.json')
    meta,_=nr.read_json(root/'note-metadata-0.json');capture,_=nr.read_json(root/'note-capture-0.json')
    titleless=(failure.get('error')=='MANUAL_NOTE_CONTENT_REQUIRED'
               and isinstance(capture.get('title'),str) and not capture['title'].strip())
    nr.require(failure.get('error')=='MANUAL_CAPTURE_TARGET_CHANGED' or titleless,
               'MANUAL_SAVED_NOTE_NOT_APPLICABLE')
    nr.require(not (root/'note-probe.json').exists(),'MANUAL_NOTE_ALREADY_FINISHED')
    history=None
    if titleless:
        before,_=nr.read_json(root/'note-before.json');navigation,_=nr.read_json(root/'note-navigation.json')
        search=check_pointer(inspected['capture']);parent=check_pointer(attempt['parent'])
        nr.require(proof_args(search['url'])['stage']=='search','MANUAL_ACTUAL_SEARCH_REQUIRED')
        require_capture(search,b,proof_args(search['url']))
        nr.require(started.get('inspection')==pointer(inspection_file)
                   and started.get('scope')=='one_observed_public_note_health_check_only'
                   and started.get('note_id')==note_id(started['url']), 'MANUAL_NOTE_HISTORY_CHANGED')
        nr.require(not any(record.get(k) for record in (before,meta,navigation)
                   for k in ('error','loginWall','securityBlock','notFound','loading')),'MANUAL_CAPTURE_FAILED')
        nr.require(navigation.get('navigationSubmitted') is True
                   and before.get('page')==b['tabs']['detail']['page'] and before.get('url')==search['url']
                   and failure.get('collection_permitted') is False
                   and nr.stamp(failure.get('at'))>=nr.stamp(started.get('at')),
                   'MANUAL_NOTE_HISTORY_CHANGED')
        nr.require(started['url'] in {c.get('url') for c in search.get('cards',[])},
                   'MANUAL_ACTUALLY_OBSERVED_CARD_REQUIRED')
        nr.require(note_id(started['url']) not in excluded_notes(parent),'ORIGINAL_RESTRICTED_NOTE_EXCLUDED')
        nr.require(not list(root.glob('note-capture-[1-9]*.json'))
                   and not list(root.glob('note-metadata-[1-9]*.json')),'MANUAL_NOTE_HISTORY_CHANGED')
        history={name:pointer(root/name) for name in ('note-failure.json','note-attempt.json',
                 'note-before.json','note-navigation.json','note-metadata-0.json','note-capture-0.json')}
    nr.require(meta.get('page')==b['tabs']['detail']['page'],'MANUAL_PAGE_CHANGED')
    if titleless:
        nr.require(capture.get('page',meta['page'])==meta['page']
                   and all(v.get('windowId') in (None,b['tabs']['detail']['window_id']) for v in (before,meta)),
                   'MANUAL_PAGE_CHANGED')
    nr.require(note_id(meta['url'])==note_id(started['url'])==note_id(capture['url']),'MANUAL_NOTE_TARGET_CHANGED')
    require_capture(capture,b,proof_args(started['url']))
    with nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
        nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
        b['tabs']['detail']['last_url']=capture['url'];b['tabs']['search']=b['tabs']['detail']
        parent=check_pointer(attempt['parent'])
        original=parent.get('tabs',{}).get('detail',{}).get('navigation_intent',{}).get('url')
        result={'kind':KIND,'at':xp.legacy.now(),'inspection':pointer(inspection_file),'binding':b,
                'capture':pointer(root/'note-capture-0.json'),'excluded_note_id':note_id(original) if original else None,
                'collection_permitted':False,'stops_unchanged':True,
                'local_validation_of_existing_capture':True,'preserved_failure':pointer(root/'note-failure.json')}
        if history:result['saved_titleless_note_history']=history
        evidence(root/'note-probe.json',result)
        return {'probe':str(root/'note-probe.json'),'capture':result['capture'],'collection_permitted':False}

def code_fingerprint():
    base=Path(__file__).parent
    return {name:hashlib.sha256((base/name).read_bytes()).hexdigest() for name in
            ('xhs_manual_tab_recovery.py','xhs_bridge.py','xhs_bridge_driver.mjs','xhs_note_recovery.py')}

def authority(b):
    return {**{k:b.get(k) for k in AUTHORITY},
            'tabs':{r:{k:s.get(k) for k in ('page','session','owned','window_id','chrome_tab_id')}
                    for r,s in b.get('tabs',{}).items()},
            'manual_excluded_note_ids':b.get('manual_excluded_note_ids',[])}

def validated_probe(probe_ref):
    probe=check_pointer(probe_ref)
    nr.require(probe.get('kind')==KIND,'MANUAL_PROBE_KIND_REQUIRED')
    inspected=check_pointer(probe['inspection']);attempt=check_pointer(inspected['attempt'])
    if probe.get('saved_titleless_note_history'):
        root=Path(probe['inspection']['file']).parent
        history=probe['saved_titleless_note_history']
        nr.require(set(history)=={'note-failure.json','note-attempt.json','note-before.json',
                   'note-navigation.json','note-metadata-0.json','note-capture-0.json'},'MANUAL_NOTE_HISTORY_CHANGED')
        for name,ref in history.items():
            nr.require(ref.get('file')==str((root/name).resolve()),'MANUAL_NOTE_HISTORY_CHANGED')
            check_pointer(ref)
        nr.require(probe['capture']==history['note-capture-0.json']
                   and probe.get('preserved_failure')==history['note-failure.json'],'MANUAL_NOTE_HISTORY_CHANGED')
    if probe.get('prior_grant'):
        prior=check_pointer(probe['prior_grant'])
        nr.require(prior.get('kind')==GRANT_KIND and prior.get('scope')=='rendered_public_web_only'
                   and prior.get('authority')==authority(probe['binding'])
                   and prior.get('stops')==attempt['stops'],'MANUAL_PRIOR_GRANT_CHANGED')
        prior_probe,_,_=validated_probe(prior['probe'])
        validate_review(check_pointer(prior['review']),prior_probe)
    initial=check_pointer(inspected['initial_inspection']) if inspected.get('initial_inspection') else inspected
    nr.require(initial['attempt']==inspected['attempt'],'MANUAL_ATTEMPT_CHANGED')
    continuation_root=Path(inspected.get('initial_inspection',probe['inspection'])['file']).parent
    if (continuation_root/'verified-search-continuation.json').exists() or inspected.get('verified_search_continuation'):
        nr.require(bool(inspected.get('verified_search_continuation')),'MANUAL_VERIFIED_SEARCH_CHAIN_CHANGED')
        validate_verified_search(inspected)
    user=check_pointer(attempt['user']);parent=check_pointer(attempt['parent'])
    if attempt.get('account_attempt'):
        reserved=check_pointer(attempt['account_attempt'])
        nr.require(reserved.get('project')==attempt['project'] and
                   reserved.get('parent_project')==attempt.get('parent_project',attempt['project']) and
                   reserved.get('context_id')==parent['context_id'] and
                   reserved.get('tab_id')==attempt['target']['tabId'] and
                   reserved.get('stops')==attempt['stops'],'MANUAL_ATTEMPT_RESERVATION_CHANGED')
    nr.require(probe['binding'].get('project')==attempt.get('project')==initial['binding'].get('project'),
               'MANUAL_DESTINATION_PROJECT_CHANGED')
    nr.require(attempt.get('parent_project',attempt['project'])==parent.get('project'),
               'MANUAL_SOURCE_PROJECT_CHANGED')
    for field in ('context_id','account_uid','profile_guard_key','profile_label','runtime'):
        nr.require(probe['binding'].get(field)==parent.get(field),'MANUAL_PARENT_IDENTITY_CHANGED')
    nr.require(user.get('user_statement') and user.get('normal_other_posts_statement'),'MANUAL_USER_RESTORATION_REQUIRED')
    nr.require(all(nr.stamp(user['processed_at'])>nr.stamp(s['at']) for s in attempt['stops'].values()),'MANUAL_CONFIRMATION_PREDATES_STOP')
    search=check_pointer(inspected['capture'])
    nr.require(proof_args(search['url'])['stage']=='search','MANUAL_ACTUAL_SEARCH_REQUIRED')
    require_capture(search,probe['binding'],proof_args(search['url']))
    capture=check_pointer(probe['capture'])
    nr.require(note_id(capture['url']),'MANUAL_NOTE_CONTENT_REQUIRED')
    require_capture(capture,probe['binding'],proof_args(capture['url']))
    nr.require(note_id(capture['url']) in {note_id(c['url']) for c in search['cards']},'MANUAL_NOTE_NOT_IN_SEARCH')
    original=parent.get('tabs',{}).get('detail',{}).get('navigation_intent',{}).get('url')
    excluded=note_id(original) if original else None
    nr.require(probe.get('excluded_note_id')==excluded and note_id(capture['url']) not in excluded_notes(parent),
               'ORIGINAL_RESTRICTED_NOTE_EXCLUDED')
    for ref in attempt['frozen_stops'].values():check_pointer(ref)
    return probe,attempt,capture

def validate_review(review,probe):
    nr.require(review.get('reader')=='codex' and review.get('reviewed') is True and review.get('body_read') is True,
               'MANUAL_ACTUAL_CODEX_REVIEW_REQUIRED')
    nr.require(review.get('capture_sha256')==probe['capture']['sha256'] and bool(review.get('summary')),
               'MANUAL_REVIEW_SOURCE_CHANGED')
    nr.require({'/title','/body','/authors'}<=set(review.get('positions',[])),'MANUAL_REVIEW_POSITIONS_REQUIRED')
    nr.require(review.get('comments_read') is False and review.get('media_read') is False,'MANUAL_HEALTH_CHECK_SCOPE_ONLY')

def recheck(project,name):
    """One same-page read after a local validator fix; never navigate/retry access."""
    project=Path(project).resolve();b=bridge.load_binding(project,name)
    old_ref=b['manual_tab_revalidation'];old=check_pointer(old_ref)
    nr.require(old.get('kind')==GRANT_KIND and old.get('scope')=='rendered_public_web_only','MANUAL_GRANT_SCOPE_CHANGED')
    previous,attempt,capture=validated_probe(old['probe'])
    validate_review(check_pointer(old['review']),previous)
    # Only the observed local bug (an omitted window ID written as null) is
    # repairable here. Account, native/CDP IDs, session and exclusions stay fixed.
    for role,slot in b['tabs'].items():
        if slot.get('window_id') is None:
            slot['window_id']=old['authority']['tabs'][role]['window_id']
    nr.require(authority(b)==old['authority'],'MANUAL_AUTHORITY_CHANGED')
    nr.require(proof_args(b['tabs']['detail']['last_url'])==proof_args(capture['url']),'MANUAL_RECHECK_ORIGINAL_NORMAL_PAGE_REQUIRED')
    b['tabs']['search']=b['tabs']['detail']
    key=xp.digest({'prior':old_ref,'code':code_fingerprint()})
    root=Path(old_ref['file']).parent/'rechecks'/key;root.mkdir(parents=True,exist_ok=True)
    with nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
        nr.require(nr.current_stops(keys)==old['stops']==attempt['stops'],'MANUAL_STOP_CHANGED')
        nr.require(not (root/'attempt.json').exists(),'MANUAL_RECHECK_ALREADY_ATTEMPTED')
        evidence(root/'attempt.json',{'at':xp.legacy.now(),'prior_grant':old_ref,
                 'code_fingerprint':code_fingerprint(),'scope':'same_normal_note_read_only_no_navigation'})
        driver=bridge.Driver(b['runtime'])
        try:
            meta=bridge.verify_tab(driver,b,'detail');evidence(root/'metadata.json',meta)
            args=proof_args(capture['url'])
            actual=driver.call('capture',b,'detail',**args);require_capture(actual,b,args)
            ref=evidence(root/'actual-note.json',actual)
            nr.require(nr.current_stops(keys)==old['stops'],'MANUAL_STOP_CHANGED')
            probe={'kind':KIND,'at':xp.legacy.now(),'binding':b,'inspection':previous['inspection'],
                   'capture':ref,'excluded_note_id':previous.get('excluded_note_id'),
                   'collection_permitted':False,'prior_grant':old_ref,'stops_unchanged':True}
            evidence(root/'note-probe.json',probe)
            return {'probe':str(root/'note-probe.json'),'capture':ref,'collection_permitted':False}
        except Exception as exc:
            evidence(root/'failure.json',{'at':xp.legacy.now(),'error':str(exc),'collection_permitted':False})
            raise
        finally:driver.close()

def attest(probe_file,review_file,name):
    probe_ref=pointer(probe_file);probe,attempt,capture=validated_probe(probe_ref)
    review,_=nr.read_json(review_file);validate_review(review,probe)
    b=probe['binding'];project=Path(b['project'])
    target=bridge.binding_path(project,name)
    with xp.project_lock(project):
      with nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
        nr.require(nr.current_stops(keys)==attempt['stops'],'MANUAL_STOP_CHANGED')
        nr.require(not target.exists(),'MANUAL_NEW_BINDING_ALREADY_EXISTS')
        b.update(binding_name=name,binding_kind=GRANT_KIND,state='ready',
                 manual_excluded_note_ids=excluded_notes(check_pointer(attempt['parent'])))
        b['tabs']['search']=b['tabs']['detail']
        root=Path(probe_file).parent
        review_ref=evidence(root/'codex-review.json',review)
        grant={'kind':GRANT_KIND,'scope':'rendered_public_web_only','at':xp.legacy.now(),
               'probe':probe_ref,'review':review_ref,'authority':authority(b),'stops':attempt['stops'],
               'code_fingerprint':code_fingerprint(),'original_binding_unchanged':True,
               'original_failed_note_restored':False,'api_and_media_restrictions_preserved':True}
        grant_ref=evidence(root/'grant.json',grant)
        b['manual_tab_revalidation']=grant_ref
        b['web_validation']={'at':grant['at'],'target':note_id(capture['url']),'snapshot':probe['capture']['file'],
                             'context_id':b['context_id'],'uid':b['account_uid']}
        web_exceptions(project,b,keys)
        bridge.save_binding(project,name,b)
        return {'binding':name,'state':'ready','grant':grant_ref,'scope':'rendered_public_web_only',
                'excluded_note_ids':b['manual_excluded_note_ids'],'stops_unchanged':True}

def web_exceptions(project,b,keys):
    grant=check_pointer(b['manual_tab_revalidation'])
    nr.require(grant.get('kind')==GRANT_KIND and grant.get('scope')=='rendered_public_web_only','MANUAL_GRANT_SCOPE_CHANGED')
    nr.require(Path(project).resolve()==Path(b['project']).resolve(),'MANUAL_PROJECT_CHANGED')
    nr.require(grant.get('authority')==authority(b),'MANUAL_AUTHORITY_CHANGED')
    nr.require(grant.get('code_fingerprint')==code_fingerprint(),'MANUAL_VALIDATOR_CHANGED')
    nr.require(b.get('transport_surface')=='browser' and b.get('manual_single_tab') is True,'MANUAL_TRANSPORT_CHANGED')
    nr.require(set(b.get('tabs',{}))=={'search','detail'} and
               all(s.get('owned') is False for s in b['tabs'].values()),'MANUAL_USER_TAB_OWNERSHIP_CHANGED')
    probe,attempt,_=validated_probe(grant['probe']);validate_review(check_pointer(grant['review']),probe)
    nr.require(grant['stops']==attempt['stops']==nr.current_stops(keys),'MANUAL_STOP_CHANGED')
    expected=dict(probe['binding'],binding_name=b['binding_name'],manual_excluded_note_ids=b['manual_excluded_note_ids'])
    nr.require(authority(expected)==authority(b),'MANUAL_PROBE_BINDING_CHANGED')
    return {k:v['sha256'] for k,v in grant['stops'].items()}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    s=sub.add_parser('inspect');s.add_argument('--project',type=Path,required=True);s.add_argument('--binding',required=True)
    s.add_argument('--metadata',type=Path,required=True);s.add_argument('--user',type=Path,required=True)
    s.add_argument('--parent-project',type=Path)
    s=sub.add_parser('inspect-active');s.add_argument('--project',type=Path,required=True);s.add_argument('--binding',required=True);s.add_argument('--user',type=Path,required=True)
    s.add_argument('--parent-project',type=Path)
    s=sub.add_parser('finish-inspection');s.add_argument('--attempt',type=Path,required=True);s.add_argument('--verify-focus',action='store_true')
    s=sub.add_parser('probe-note');s.add_argument('--inspection',type=Path,required=True);s.add_argument('--url',required=True)
    s=sub.add_parser('probe-search');s.add_argument('--inspection',type=Path,required=True);s.add_argument('--task',required=True)
    s=sub.add_parser('finish-search');s.add_argument('--inspection',type=Path,required=True);s.add_argument('--layout-observation',type=Path)
    s=sub.add_parser('resume-verified-search');s.add_argument('--inspection',type=Path,required=True);s.add_argument('--user',type=Path,required=True)
    s=sub.add_parser('finish-note');s.add_argument('--inspection',type=Path,required=True)
    s=sub.add_parser('attest');s.add_argument('--probe',type=Path,required=True);s.add_argument('--review',type=Path,required=True);s.add_argument('--binding',required=True)
    s=sub.add_parser('recheck');s.add_argument('--project',type=Path,required=True);s.add_argument('--binding',required=True)
    a=p.parse_args()
    if a.command=='inspect':result=inspect(a.project,a.binding,a.metadata,a.user,a.parent_project)
    elif a.command=='inspect-active':result=inspect_active(a.project,a.binding,a.user,a.parent_project)
    elif a.command=='finish-inspection':result=finish_inspection(a.attempt,a.verify_focus)
    elif a.command=='probe-search':result=probe_search(a.inspection,a.task)
    elif a.command=='finish-search':result=finish_search(a.inspection,a.layout_observation)
    elif a.command=='resume-verified-search':result=resume_verified_search(a.inspection,a.user)
    elif a.command=='finish-note':result=finish_note(a.inspection)
    elif a.command=='attest':result=attest(a.probe,a.review,a.binding)
    elif a.command=='recheck':result=recheck(a.project,a.binding)
    else:result=probe_note(a.inspection,a.url)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
