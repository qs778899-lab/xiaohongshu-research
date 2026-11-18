"""Return a verified manual tab from the exact normal homepage to its last note.

This uses the existing web grant. It cannot recover account access, create a
grant, replace a tab, retry an uncertain navigation, or complete research tasks.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import time

import xhs_bridge as bridge
import xhs_manual_tab_recovery as manual
import xhs_native as native
import xhs_note_recovery as nr
import xhs_project as xp
from xhs_batch import batch_lock

HOME='https://www.xiaohongshu.com/explore'
KIND='manual-normal-home-last-note-return_v1'


def normal_page(value):
    nr.require(isinstance(value,dict),'HOME_RETURN_NORMAL_PAGE_REQUIRED')
    bridge.safe_url(value.get('url'))
    if any(value.get(k) for k in ('loginWall','securityBlock')) or value.get('error')=='ACCESS_STOP':
        raise bridge.BridgeError('ACCESS_STOP')
    nr.require(not value.get('error') and not value.get('notFound'),'HOME_RETURN_NORMAL_PAGE_REQUIRED')


def task_snapshot(project,task_id):
    db=native.open_lookup_project(Path(project))
    try:task=db.task(task_id)
    finally:db.close()
    nr.require(task and task.get('kind')=='native' and task.get('status')=='pending'
               and task['payload'].get('stage')=='search','HOME_RETURN_PENDING_SEARCH_REQUIRED')
    return task


def failure_log(path,sha=None):
    path=Path(path).resolve()
    nr.require(path.is_file() and not path.is_symlink(),'HOME_RETURN_FAILURE_LOG_REQUIRED')
    raw=path.read_bytes();actual=hashlib.sha256(raw).hexdigest()
    nr.require(not sha or sha==actual,'RECOVERY_EVIDENCE_CHANGED')
    try:rows=[json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    except (ValueError,UnicodeError):raise bridge.BridgeError('HOME_RETURN_FAILURE_LOG_REQUIRED')
    nr.require(rows and isinstance(rows[-1],dict),'HOME_RETURN_FAILURE_LOG_REQUIRED')
    return rows[-1],{'file':str(path),'sha256':actual}


def context(project,name):
    b=bridge.load_binding(project,name);slot=b.get('tabs',{}).get('detail',{})
    nr.require(b.get('state')=='blocked' and b.get('last_error',{}).get('code')=='UNEXPECTED_TARGET_CHANGE',
               'HOME_RETURN_TARGET_CHANGE_REQUIRED')
    nr.require(b.get('manual_tab_revalidation') and b.get('manual_single_tab') is True
               and b.get('transport_surface')=='browser' and slot.get('owned') is False
               and slot.get('page') and slot.get('session') and slot.get('verified_at')
               and type(slot.get('chrome_tab_id')) is int and type(slot.get('window_id')) is int,
               'HOME_RETURN_VERIFIED_MANUAL_TAB_REQUIRED')
    nr.require(not slot.get('navigation_intent'),'HOME_RETURN_NO_INFLIGHT_NAVIGATION_REQUIRED')
    target=slot.get('last_url');note=manual.note_id(target)
    nr.require(note and note not in b.get('manual_excluded_note_ids',[]),'HOME_RETURN_NORMAL_NOTE_REQUIRED')
    key=xp.digest({'kind':KIND,'project':str(Path(project).resolve()),'binding':name,
        'uid':b['account_uid'],'page':slot['page'],'session':slot['session'],
        'note':note,'grant':b['manual_tab_revalidation']})
    return b,target,Path(project)/'bridge/manual-home-returns'/key


def restore_source(project,b,target):
    grant=manual.check_pointer(b['manual_tab_revalidation'])
    source=manual.check_pointer(grant['probe'])['capture']
    value=manual.check_pointer(source)
    if value.get('url')==target:
        normal_page(value)
        manual.require_capture(value,b,manual.proof_args(target));return source
    files=list((Path(project)/'bridge/raw').glob('*.json'))+list((Path(project)/'native-sources').glob('*'))
    for path in sorted(files):
        if not path.is_file() or path.is_symlink() or not re.fullmatch('[a-f0-9]{64}',path.stem):continue
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=path.stem:continue
        try:value=json.loads(raw)
        except (ValueError,UnicodeError):continue
        if not isinstance(value,dict) or value.get('url')!=target or value.get('uid')!=b['account_uid']:continue
        if value.get('stage') not in {'body','roots','replies'}:continue
        try:
            normal_page(value);manual.require_capture(value,b,manual.proof_args(target))
        except bridge.BridgeError:continue
        return manual.pointer(path)
    raise bridge.BridgeError('HOME_RETURN_OBSERVED_NORMAL_NOTE_REQUIRED')


def observed_failure(metadata,log,task,b):
    slot=b['tabs']['detail']
    bridge.safe_url(metadata.get('url'))
    nr.require(metadata.get('url')==HOME and metadata.get('page')==slot['page']
               and metadata.get('windowId') in (None,slot['window_id']), 'HOME_RETURN_EXACT_HOME_REQUIRED')
    nr.require(log.get('event')=='blocked' and log.get('reason')=='UNEXPECTED_TARGET_CHANGE'
               and log.get('processed_units')==0 and log.get('task_id')==task['id']
               and log.get('actor')=='codex' and log.get('metrics',{}).get('calls')==1,
               'HOME_RETURN_PRE_NAVIGATION_FAILURE_REQUIRED')


def observe(driver,b,expected,root,prefix,same_note=False):
    """Read only the already bound CDP/native tab and account; no selection."""
    slot=b['tabs']['detail'];refs={}
    def save(action,label,**kw):
        value=driver.call(action,b,'detail',**kw)
        refs[label]=manual.evidence(root/(prefix+'-'+label+'.json'),value);return value
    meta=save('metadata','metadata');normal_page(meta)
    target_ok=(manual.note_id(meta.get('url'))==manual.note_id(expected) if same_note else meta.get('url')==expected)
    nr.require(meta.get('page')==slot['page'] and target_ok
               and meta.get('windowId') in (None,slot['window_id']),'HOME_RETURN_PAGE_CHANGED')
    ident=save('identity','identity');normal_page(ident)
    nr.require(ident.get('page')==slot['page'] and ident.get('url')==meta['url']
               and ident.get('uid')==b['account_uid'] and not ident.get('error')
               and not ident.get('notFound'),'HOME_RETURN_IDENTITY_CHANGED')
    found=save('locate_registered_url','native',url=meta['url'])
    nr.require(sum(t.get('tabId')==slot['chrome_tab_id'] and t.get('windowId')==slot['window_id']
                   and t.get('url')==meta['url'] for t in found.get('matches',[]))==1,
               'HOME_RETURN_NATIVE_TAB_CHANGED')
    return refs


def restore(project,name,task_id,metadata_file,log_file,authorization,driver=None):
    project=Path(project).resolve();owned=driver is None
    nr.require(isinstance(authorization,str) and authorization.strip(),'HOME_RETURN_AUTHORIZATION_REQUIRED')
    with batch_lock(project/'bridge/runner'),xp.project_lock(project):
        b,target,root=context(project,name);original=copy.deepcopy(b)
        nr.require(not (root/'attempt.json').exists(),'HOME_RETURN_ALREADY_ATTEMPTED')
        task=task_snapshot(project,task_id);metadata,_=nr.read_json(metadata_file)
        log,log_ref=failure_log(log_file);observed_failure(metadata,log,task,b)
        with bridge.lease(project,b) as keys:
            stops=nr.current_stops(keys);source=restore_source(project,b,target)
            attempt={'kind':KIND,'at':xp.legacy.now(),'project':str(project),'binding':original,
                'target':target,'source':source,'task':task,'metadata':manual.pointer(metadata_file),
                'failure_log':log_ref,'stops':stops,'authorization':authorization,
                'scope':'same_manual_tab_normal_home_return_under_existing_grant'}
            attempt_ref=manual.evidence(root/'attempt.json',attempt)
            try:
                driver=driver or bridge.Driver(b['runtime'])
                before=observe(driver,b,HOME,root,'before')
                nr.require(nr.current_stops(keys)==stops,'HOME_RETURN_STOP_CHANGED')
                nr.require(bridge.load_binding(project,name)==original,'HOME_RETURN_BINDING_CHANGED')
                b['tabs']['detail']['navigation_intent']={'url':target,'at':xp.legacy.now(),
                                                         'manual_home_return':attempt_ref}
                bridge.save_binding(project,name,b)
                navigation=driver.call('navigate',b,'detail',url=target)
                manual.evidence(root/'navigation.json',navigation)
                nr.require(navigation.get('navigationSubmitted') is True,'HOME_RETURN_NAVIGATION_UNKNOWN')
                time.sleep(1)
                after=observe(driver,b,target,root,'after',same_note=True)
                capture=driver.call('capture',b,'detail',**manual.proof_args(target))
                capture_ref=manual.evidence(root/'capture.json',capture);normal_page(capture)
                manual.require_capture(capture,b,manual.proof_args(target))
                nr.web_exceptions(project,name,keys)
                nr.require(nr.current_stops(keys)==stops,'HOME_RETURN_STOP_CHANGED')
                b['tabs']['detail'].pop('navigation_intent');b['tabs']['detail']['verified_at']=xp.legacy.now()
                b['tabs']['detail']['last_url']=capture['url']
                probe={'kind':KIND,'attempt':attempt_ref,'before':before,'after':after,'capture':capture_ref,
                       'binding_pending':copy.deepcopy(b),'body_read':False,'comments_read':False,
                       'media_read':False,'collection_permitted':False,'navigation_calls':1}
                probe_ref=manual.evidence(root/'probe.json',probe)
                b['manual_home_return_pending']=probe_ref;bridge.save_binding(project,name,b)
                return {'probe':probe_ref,'capture':capture_ref,'collection_permitted':False,'state':'blocked'}
            except BaseException as exc:
                manual.evidence(root/'failure.json',{'at':xp.legacy.now(),'code':getattr(exc,'code',type(exc).__name__),
                    'no_automatic_retry':True,'collection_permitted':False})
                bridge.save_failure(project,name,b,exc,keys);raise
            finally:
                if owned and driver:driver.close()


def attest(project,name,probe_file,review_file,driver=None):
    project=Path(project).resolve();owned=driver is None
    with batch_lock(project/'bridge/runner'),xp.project_lock(project):
        b=bridge.load_binding(project,name);probe_ref=manual.pointer(probe_file)
        nr.require(b.get('manual_home_return_pending')==probe_ref,'HOME_RETURN_BOUND_PROBE_REQUIRED')
        probe=manual.check_pointer(probe_ref);attempt=manual.check_pointer(probe['attempt'])
        expected=copy.deepcopy(probe['binding_pending']);expected['manual_home_return_pending']=probe_ref
        nr.require(b==expected and probe.get('kind')==attempt.get('kind')==KIND,'HOME_RETURN_BINDING_CHANGED')
        original=attempt['binding'];root=Path(probe_file).resolve().parent
        # Re-derive the fixed location from the archived original binding, never caller input.
        slot=original['tabs']['detail']
        key=xp.digest({'kind':KIND,'project':str(project),'binding':name,'uid':original['account_uid'],
            'page':slot['page'],'session':slot['session'],'note':manual.note_id(attempt['target']),
            'grant':original['manual_tab_revalidation']})
        nr.require(root==project/'bridge/manual-home-returns'/key and probe_ref==manual.pointer(root/'probe.json')
                   and probe['attempt']==manual.pointer(root/'attempt.json'),'HOME_RETURN_LOCATION_CHANGED')
        nr.require(manual.authority(b)==manual.authority(original)
                   and b['manual_tab_revalidation']==original['manual_tab_revalidation'],'HOME_RETURN_AUTHORITY_CHANGED')
        nr.require(task_snapshot(project,attempt['task']['id'])==attempt['task'],'HOME_RETURN_TASK_CHANGED')
        log,_=failure_log(attempt['failure_log']['file'],attempt['failure_log']['sha256'])
        observed_failure(manual.check_pointer(attempt['metadata']),log,attempt['task'],original)
        old_source=manual.check_pointer(attempt['source'])
        nr.require(old_source.get('url')==attempt['target'],'HOME_RETURN_SOURCE_CHANGED')
        normal_page(old_source)
        manual.require_capture(old_source,original,manual.proof_args(attempt['target']))
        nr.require(probe['capture']==manual.pointer(root/'capture.json'),'HOME_RETURN_CAPTURE_CHANGED')
        capture=manual.check_pointer(probe['capture']);normal_page(capture)
        manual.require_capture(capture,b,manual.proof_args(attempt['target']))
        review,_=nr.read_json(review_file);manual.validate_review(review,{'capture':probe['capture']})
        with bridge.lease(project,b) as keys:
            nr.require(nr.current_stops(keys)==attempt['stops'],'HOME_RETURN_STOP_CHANGED')
            nr.require(not (root/'attest-attempt.json').exists(),'HOME_RETURN_ATTEST_ALREADY_ATTEMPTED')
            review_ref=manual.evidence(root/'review.json',review)
            manual.evidence(root/'attest-attempt.json',{'at':xp.legacy.now(),'probe':probe_ref,'review':review_ref})
            try:
                driver=driver or bridge.Driver(b['runtime'])
                live=observe(driver,b,capture['url'],root,'reviewed')
                nr.web_exceptions(project,name,keys)
                nr.require(nr.current_stops(keys)==attempt['stops'],'HOME_RETURN_STOP_CHANGED')
                nr.require(bridge.load_binding(project,name)==expected,'HOME_RETURN_BINDING_CHANGED')
                b.setdefault('resolved_errors',[]).append(copy.deepcopy(original['last_error']))
                b.pop('last_error',None);b.pop('manual_home_return_pending');b['state']='ready'
                b.setdefault('manual_home_returns',{})[key]={'probe':probe_ref,'review':review_ref,'at':xp.legacy.now()}
                bridge.save_binding(project,name,b)
                result={'state':'ready','new_grant':False,'original_grant':b['manual_tab_revalidation'],
                        'stop_bytes_unchanged':True,'tasks_completed':0,'live':live}
                manual.evidence(root/'success.json',result);return result
            except BaseException as exc:
                manual.evidence(root/'attest-failure.json',{'at':xp.legacy.now(),
                    'code':getattr(exc,'code',type(exc).__name__),'collection_permitted':False})
                bridge.save_failure(project,name,b,exc,keys);raise
            finally:
                if owned and driver:driver.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,required=True);parser.add_argument('--binding',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    r=sub.add_parser('return');r.add_argument('--task',required=True);r.add_argument('--metadata',type=Path,required=True)
    r.add_argument('--failure-log',type=Path,required=True);r.add_argument('--authorization',required=True)
    a=sub.add_parser('attest');a.add_argument('--probe',type=Path,required=True);a.add_argument('--review',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.command=='return':result=restore(args.project,args.binding,args.task,args.metadata,args.failure_log,args.authorization)
        else:result=attest(args.project,args.binding,args.probe,args.review)
        print(xp.dumps({'ok':True,'data':result}))
    except xp.legacy.ResearchError as exc:
        print(xp.dumps({'ok':False,'code':getattr(exc,'code','STOP_ACTIVE'),'reason':str(exc)}));raise SystemExit(1)


if __name__=='__main__':main()
