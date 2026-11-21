"""One return from an exact observed note-link error; never retry that failed URL."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlparse,parse_qs
import xhs_bridge as bridge
import xhs_manual_tab_recovery as manual
import xhs_note_recovery as nr
import xhs_project as xp


def validate_error(error,failed,target):
    bridge.safe_url(failed);bridge.safe_url(target)
    e=urlparse(error);f=urlparse(failed);q=parse_qs(e.query,keep_blank_values=True)
    nr.require(e.scheme=='https' and e.netloc=='www.xiaohongshu.com' and e.path=='/404' and not e.fragment,
               'EXACT_NOTE_LINK_ERROR_REQUIRED')
    nr.require(set(q)<= {'source','noteId','errorCode','xsec_token','xsec_source'} and all(len(v)==1 for v in q.values()),'EXACT_NOTE_LINK_ERROR_REQUIRED')
    nr.require(q.get('source')==['note'] and q.get('errorCode') in (['-510001'],['-510000']) and q.get('noteId')==[manual.note_id(failed)] and manual.note_id(failed) and manual.note_id(target) and manual.note_id(target)!=manual.note_id(failed),'EXACT_NOTE_LINK_ERROR_REQUIRED')
    for key in ['xsec_token','xsec_source']:
        nr.require(q.get(key)==parse_qs(f.query,keep_blank_values=True).get(key),'FAILED_LINK_PARAMETERS_CHANGED')


def navigate_error(b,error,failed,target):
    slot=b['tabs']['detail'];runtime=b['runtime']
    cfg=dict(packageRoot=runtime['package_root'],contextId=b['context_id'],extensionVersion=bridge.effective_extension_version(b),
             chromeTabId=slot['chrome_tab_id'],windowId=slot['window_id'],page=slot['page'],session=slot['session'],uid=b['account_uid'],
             errorUrl=error,failedUrl=failed,targetUrl=target)
    env={k:v for k,v in os.environ.items() if not k.startswith('OPENCLI_')}
    env.update(OPENCLI_CONFIG_DIR=runtime['config_dir'],OPENCLI_CACHE_DIR=runtime['cache_dir'])
    try:
        r=subprocess.run([runtime['node'],str(Path(__file__).with_suffix('.mjs'))],input=json.dumps(cfg),text=True,capture_output=True,timeout=45,env=env)
        packet=json.loads(r.stdout)
    except (subprocess.TimeoutExpired,ValueError,OSError):raise bridge.BridgeError('NOTE_RETURN_OUTCOME_UNKNOWN') from None
    if not packet.get('ok'):raise bridge.BridgeError(packet.get('error','NOTE_RETURN_OUTCOME_UNKNOWN'))
    return packet['data']


def restore(project,name,observation,authorization,navigator=navigate_error,driver=None,resume_home=False):
    nr.require(bool(authorization and authorization.strip()),'AUTHORIZATION_REQUIRED')
    project=Path(project).resolve();b=bridge.load_binding(project,name);original=copy.deepcopy(b);slot=b['tabs']['detail']
    saved=manual.check_pointer(manual.pointer(observation))
    error=saved.get('metadata',{});failed=slot.get('navigation_intent',{}).get('url');target=slot.get('last_url')
    expected_error='CURRENT_ERROR_CHANGED' if resume_home else 'SCOPE_CHANGED'
    nr.require(b.get('state')=='blocked' and b.get('last_error',{}).get('code')==expected_error and b.get('manual_tab_revalidation') and b.get('manual_single_tab') is True,'NOTE_SCOPE_FAILURE_REQUIRED')
    nr.require(saved.get('status')=='failed' and saved.get('requested_url')==failed and error.get('page')==slot['page'],'ERROR_OBSERVATION_MISMATCH')
    validate_error(error['url'],failed,target)
    grant=manual.check_pointer(b['manual_tab_revalidation']);capture=manual.check_pointer(manual.check_pointer(grant['probe'])['capture'])
    if capture.get('url')!=target:
        for file in sorted((project/'bridge/raw').glob('*.json')):
            raw=file.read_bytes()
            if file.is_symlink() or hashlib.sha256(raw).hexdigest()!=file.stem:continue
            try: value=json.loads(raw)
            except (ValueError,UnicodeError):continue
            if isinstance(value,dict) and value.get('url')==target and value.get('uid')==b['account_uid'] and value.get('stage')=='body':
                manual.require_capture(value,b,manual.proof_args(target));capture=value;break
    nr.require(capture.get('url')==target and capture.get('uid')==b['account_uid'],'ORIGINAL_HEALTH_NOTE_REQUIRED')
    key=xp.digest(dict(binding=name,page=slot['page'],failed=failed,target=target))
    root=project/'bridge/note-link-returns'/key
    if resume_home:
        attempt=manual.check_pointer(manual.pointer(root/'attempt.json'))
        failure=manual.check_pointer(manual.pointer(root/'failure.json'))
        nr.require(failure.get('error')=='CURRENT_ERROR_CHANGED' and not (root/'navigation.json').exists() and not (root/'navigation-intent.json').exists() and
                   attempt['observation']==manual.pointer(observation) and
                   manual.authority(attempt['binding'])==manual.authority(b),'PRE_NAVIGATION_HOME_CONTINUATION_REQUIRED')
        root=root/'home-continuation'
    nr.require(not (root/'attempt.json').exists(),'NOTE_RETURN_ALREADY_ATTEMPTED')
    owned=driver is None
    with xp.project_lock(project),bridge.lease(project,b) as keys:
        stops=nr.current_stops(keys)
        nr.immutable(root/'attempt.json',dict(binding=original,observation=manual.pointer(observation),authorization=authorization,stops=stops,at=xp.legacy.now()))
        try:
            driver=driver or bridge.Driver(b['runtime'])
            meta=driver.call('metadata',b,'detail')
            nr.immutable(root/'metadata.json',dict(at=xp.legacy.now(),result=meta))
            home=meta.get('url')=='https://www.xiaohongshu.com/explore?source=404'
            nr.require(meta.get('page')==slot['page'] and (home or (not resume_home and meta.get('url')==error['url'])),'CURRENT_ERROR_CHANGED')
            # A dedicated bounded helper reads error text, then returns once to the
            # original health note. It does not relax normal URL/identity guards.
            # The platform may redirect the observed error to its exact 404 home.
            # Reverify identity there using the unchanged ordinary guard.
            nr.immutable(root/'navigation-intent.json',dict(target=target,at=xp.legacy.now()))
            if home:
                identity=driver.call('identity',b,'detail')
                nr.require(identity.get('page')==slot['page'] and identity.get('url')==meta['url'] and identity.get('uid')==b['account_uid'],'HOME_IDENTITY_CHANGED')
                result=driver.call('navigate',b,'detail',url=target)
            else:
                result=navigator(b,error['url'],failed,target)
            nr.immutable(root/'navigation.json',dict(at=xp.legacy.now(),result=result))
            slot['navigation_intent']={'url':target,'at':xp.legacy.now(),'note_link_return':key}
            bridge.save_binding(project,name,b);time.sleep(1)
            meta=bridge.verify_tab(driver,b,'detail')
            actual=driver.call('capture',b,'detail',**manual.proof_args(target))
            manual.require_capture(actual,b,manual.proof_args(target))
            nr.require(nr.current_stops(keys)==stops,'STOP_CHANGED')
            source=manual.evidence(root/'restored-body.json',actual)
            b.setdefault('resolved_errors',[]).append(original['last_error']);b.pop('last_error',None);b['state']='ready'
            b.setdefault('failed_note_links',{})[failed]={'at':xp.legacy.now(),'observation':manual.pointer(observation),'return_attempt':str(root/'attempt.json'),'cause':'unknown'}
            bridge.save_binding(project,name,b)
            final=dict(at=xp.legacy.now(),capture=source,body_read=False,no_tasks_completed=True,failed_url_isolated=failed,stops_unchanged=True)
            nr.immutable(root/'success.json',final);return final
        except BaseException as exc:
            nr.immutable(root/'failure.json',dict(at=xp.legacy.now(),error=getattr(exc,'code',type(exc).__name__),no_retry=True))
            bridge.save_failure(project,name,b,exc,keys);raise
        finally:
            if owned and driver:driver.close()


if __name__=='__main__':
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument('--project',type=Path,required=True);a.add_argument('--binding',required=True)
    a.add_argument('--observation',type=Path,required=True);a.add_argument('--authorization',required=True)
    a.add_argument('--resume-home',action='store_true')
    args=a.parse_args();print(json.dumps(restore(args.project,args.binding,args.observation,args.authorization,resume_home=args.resume_home),ensure_ascii=False))
