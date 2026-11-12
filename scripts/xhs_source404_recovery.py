"""One owned-page source=404 redirect recovery under an existing web lease.

No new authorization, account proof, session, reload or core-code changes.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse

HOME='https://www.xiaohongshu.com/explore?source=404'
KIND='owned-source404-last-url-restore_v1'
HOLD='OBSERVED_SOURCE_REDIRECTED_TO_HOME_404'


def restore(project,name,task_id,driver=None):
    import xhs_bridge as bridge
    import xhs_note_recovery as nr
    import xhs_project as xp
    from xhs_batch import batch_lock
    require=nr.require;p=xp.Project(project);owned=driver is None
    try:
        with batch_lock(p.path/'bridge/runner'),xp.project_lock(project):
            b=bridge.load_binding(project,name);slot=b.get('tabs',{}).get('detail',{})
            original=copy.deepcopy(b);task=p.task(task_id)
            require(b.get('state')=='blocked' and b.get('last_error',{}).get('code')=='SCOPE_CHANGED',
                    'SOURCE404_PAGE_SCOPE_FAILURE_REQUIRED')
            require(slot.get('owned') is True and slot.get('page') and slot.get('session')
                    and slot.get('verified_at'),'SOURCE404_EXISTING_OWNED_DETAIL_REQUIRED')
            require(task.get('kind')=='native' and task.get('status')=='pending'
                    and task['payload'].get('stage') in {'body','roots','replies','images','video'},
                    'SOURCE404_PENDING_NOTE_TASK_REQUIRED')
            failed=slot.get('navigation_intent',{}).get('url');target=slot.get('last_url')
            bridge.safe_url(failed);bridge.safe_url(target)
            def note(url):
                path=bridge.target_path(urlparse(url).path)
                require(re.fullmatch('/explore/[a-f0-9]{24}',path),'SOURCE404_EXACT_NOTE_REQUIRED')
                return path.rsplit('/',1)[1]
            require(note(failed)==task['payload']['target'] and note(target)!=note(failed)
                    and bridge.observed_url(project,task)==failed,'SOURCE404_FAILED_OBSERVED_SOURCE_REQUIRED')
            key=xp.digest({'kind':KIND,'project':str(p.path.resolve()),'binding':name,
                'uid':b['account_uid'],'page':slot['page'],'session':slot['session'],
                'failed_url':failed,'restore_url':target})
            directory=p.path/'bridge/source404-recoveries'/key
            require(not (directory/'attempt.json').exists(),'SOURCE404_ALREADY_ATTEMPTED')
            # The same original grant is checked in full, before any browser call.
            with bridge.lease(project,b) as keys:
                stops=nr.current_stops(keys)
                source=None
                pointer=b.get('current_page_revalidation',{})
                grant,_=nr.read_json(pointer.get('file'),pointer.get('sha256'))
                require(grant.get('kind')=='same-target-rendered-page-presence_v1','SOURCE404_EXISTING_PAGE_PRESENCE_GRANT_REQUIRED')
                evidence=nr.check_ref(p,grant['evidence']);cap_ref=evidence['sources']['capture']
                if nr.check_ref(p,cap_ref).get('result',{}).get('url')==target:source=cap_ref
                if source is None:
                    for file in sorted((p.path/'bridge/raw').glob('*.json')):
                        if not re.fullmatch('[a-f0-9]{64}',file.stem) or file.is_symlink():continue
                        raw=file.read_bytes()
                        if hashlib.sha256(raw).hexdigest()!=file.stem:continue
                        value=json.loads(raw)
                        if value.get('url')==target and value.get('uid')==b['account_uid'] and value.get('stage') in {'body','roots','replies'}:
                            source={'file':str(file),'sha256':file.stem};break
                require(source,'SOURCE404_RESTORE_SOURCE_REQUIRED')
                affected=[]
                for row in p.db.execute("SELECT id,payload FROM tasks WHERE kind='native' AND status='pending'"):
                    payload=json.loads(row['payload'])
                    if payload.get('target')!=task['payload']['target'] or payload.get('stage')=='search':continue
                    candidate=p.task(row['id'])
                    try:same=bridge.observed_url(project,candidate)==failed
                    except bridge.BridgeError:same=False
                    if same:affected.append(candidate)
                require(task_id in {t['id'] for t in affected},'SOURCE404_FAILED_TASK_NOT_ISOLATED')
                journal={'kind':KIND,'at':xp.legacy.now(),'binding':original,'failed_task':task_id,
                    'failed_url':failed,'target':target,'target_source':source,'affected_tasks':affected,
                    'stops':stops,'reason':'Original observed link led to source=404 home; cause unknown, not evidence of deletion.'}
                # Consume the fixed opportunity before any external observation.
                nr.immutable(directory/'attempt.json',journal)
                observations={};stage='driver_initialization'
                try:
                    driver=driver or bridge.Driver(b['runtime'])
                    def call(action,**kwargs):
                        nonlocal stage
                        stage=action;value=driver.call(action,b,'detail',**kwargs)
                        file=directory/(action+'.json');nr.immutable(file,{'at':xp.legacy.now(),'result':value})
                        observations[action]={'file':str(file),'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
                        return value
                    meta=call('metadata')
                    bridge.safe_url(meta.get('url'))
                    require(meta.get('page')==slot['page'] and meta.get('url')==HOME,'SOURCE404_EXACT_HOME_REQUIRED')
                    ident=call('identity')
                    require(ident.get('page')==slot['page'] and ident.get('url')==HOME
                            and ident.get('uid')==b['account_uid'] and not ident.get('error')
                            and not any(ident.get(k) for k in ('loginWall','securityBlock','notFound')),
                            'SOURCE404_SAME_ACCOUNT_REQUIRED')
                    lease_state=call('lease_status')
                    require(lease_state.get('expectedPage')==slot['page'] and lease_state.get('matching')==1
                            and lease_state.get('leaseTabCount')==1 and lease_state.get('tabs')==[{'page':slot['page'],'url':HOME}],
                            'SOURCE404_OWN_SINGLE_PAGE_REQUIRED')
                    # Isolate only this exact failed source; save all old payloads first.
                    for item in affected:
                        bridge.hold_task(p,item['id'],HOLD)
                        held=p.task(item['id']);fault=held['payload']['bridge']['fault']
                        fault.update(source_url=failed,source404_recovery=key,cause='unknown',not_evidence_of_deletion=True,
                                     prior_fault=item['payload'].get('bridge',{}).get('fault'))
                        p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(held['payload']),item['id']))
                    p.db.commit()
                    slot['navigation_intent']={'url':target,'at':xp.legacy.now(),'source404_recovery':key}
                    bridge.save_binding(project,name,b)
                    call('navigate',url=target) # Once; unknown outcome is never resent.
                    time.sleep(1) # Same one-time render wait as the existing owned-home restorer.
                    stage='verify_tab';verified=bridge.verify_tab(driver,b,'detail')
                    require(verified.get('page')==slot['page'] and bridge.target_path(urlparse(verified['url']).path)
                            ==bridge.target_path(urlparse(target).path),'SOURCE404_RESTORE_TARGET_CHANGED')
                    # All original grant sources, code and STOP hashes still govern.
                    nr.web_exceptions(project,name,keys)
                    after=nr.current_stops(keys)
                    require(set(after)==set(stops) and all(after[k]['sha256']==s['sha256'] for k,s in stops.items()),
                            'SOURCE404_NEW_STOP')
                    b.setdefault('resolved_errors',[]).append(original['last_error']);b.pop('last_error',None);b['state']='ready'
                    b.setdefault('source404_recoveries',{})[key]={'at':xp.legacy.now(),'attempt':str(directory/'attempt.json')}
                    bridge.save_binding(project,name,b)
                    result={'at':xp.legacy.now(),'verified':verified,'held_tasks':[t['id'] for t in affected],
                        'original_source_cause':'unknown','stop_bytes_unchanged':True,'navigation_calls':1}
                    nr.immutable(directory/'success.json',result);return result
                except BaseException as exc:
                    nr.immutable(directory/'failure.json',{'at':xp.legacy.now(),'stage':stage,
                        'code':getattr(exc,'code',type(exc).__name__),'no_automatic_retry':True,'observations':observations})
                    bridge.save_failure(project,name,b,exc,keys)
                    raise
    finally:
        p.close()
        if owned and driver:driver.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill',type=Path,required=True);parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--binding',required=True);parser.add_argument('--task',required=True)
    a=parser.parse_args();sys.path.insert(0,str(a.skill/'scripts'))
    import xhs_project as xp
    try:print(xp.dumps({'ok':True,'data':restore(a.project,a.binding,a.task)}))
    except xp.legacy.ResearchError as exc:
        print(xp.dumps({'ok':False,'code':getattr(exc,'code','STOP_ACTIVE'),'reason':str(exc)}));raise SystemExit(1)

if __name__=='__main__':main()
