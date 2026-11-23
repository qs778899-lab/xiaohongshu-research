"""Return once from an exact expired note link, without granting collection."""
import argparse
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import json
import hashlib
import xhs_bridge as bridge
import xhs_manual_tab_recovery as manual
import xhs_note_recovery as nr
import xhs_note_link_return as link
import xhs_project as xp


def validate_expired(error, failed, target):
    bridge.safe_url(failed); bridge.safe_url(target)
    e=urlparse(error); q=parse_qs(e.query,keep_blank_values=True)
    nr.require(e.scheme=='https' and e.netloc=='www.xiaohongshu.com'
        and e.path=='/website-login/error' and not e.fragment,
        'EXACT_EXPIRED_NOTE_LINK_REQUIRED')
    nr.require(set(q)=={'redirectPath','error_code','error_msg','uuid','verifyMsg'}
        and all(len(v)==1 for v in q.values())
        and q['redirectPath']==[failed] and q['error_code']==['300017']
        and q['error_msg']==['当前访问链接已失效'] and q['verifyMsg']==['']
        and bridge.re.fullmatch(r'[a-f0-9-]{36}',q['uuid'][0])
        and manual.note_id(failed) and manual.note_id(target)
        and manual.note_id(failed)!=manual.note_id(target),
        'EXACT_EXPIRED_NOTE_LINK_REQUIRED')


def restore(project,name,authorization,navigator=link.navigate_error,driver=None,normal_source=None):
    nr.require(isinstance(authorization,str) and authorization.strip(),'AUTHORIZATION_REQUIRED')
    project=Path(project).resolve();xp.legacy.research_path(project)
    b=bridge.load_binding(project,name);slot=b['tabs']['detail']
    nr.require(b.get('state')=='blocked' and b.get('last_error',{}).get('code')=='ACCESS_STOP'
        and b.get('manual_single_tab') is True and b.get('manual_tab_revalidation'),
        'EXPIRED_LINK_STOP_CONTEXT_REQUIRED')
    failed=slot.get('navigation_intent',{}).get('url');target=slot.get('last_url')
    grant=manual.check_pointer(b['manual_tab_revalidation'])
    nr.require(grant.get('authority')==manual.authority(b),'ORIGINAL_AUTHORITY_CHANGED')
    probe=manual.check_pointer(grant['probe']);capture=manual.check_pointer(probe['capture'])
    source_ref=None
    if normal_source is not None:
        source=Path(normal_source)
        validation=b.get('web_validation',{})
        nr.require(source.is_absolute() and source.parent==project/'bridge'/'raw'
            and not source.is_symlink() and source.is_file()
            and validation.get('snapshot')==str(source)
            and validation.get('context_id')==b['context_id']
            and validation.get('uid')==b['account_uid'],'LINKED_NORMAL_BODY_REQUIRED')
        raw=source.read_bytes();sha=hashlib.sha256(raw).hexdigest()
        nr.require(source.name==sha+'.json','NORMAL_BODY_HASH_CHANGED')
        capture,_=nr.read_json(source,sha)
        nr.require(capture.get('stage')=='body' and capture.get('uid')==b['account_uid']
            and validation.get('target')==manual.note_id(capture.get('url'))
            and nr.stamp(validation.get('at'))<nr.stamp(b['last_error']['at']),
            'PRE_FAILURE_NORMAL_BODY_REQUIRED')
        target=capture.get('url');source_ref={'file':str(source),'sha256':sha}
    else:
        nr.require(capture.get('url')==target,'ORIGINAL_HEALTH_NOTE_REQUIRED')
    manual.require_capture(capture,b,manual.proof_args(target))
    failure_at=nr.stamp(b['last_error']['at']);owned=driver is None
    with xp.project_lock(project),nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:
        stops=nr.current_stops(keys)
        nr.require(all(0<=(nr.stamp(s['at'])-failure_at).total_seconds()<=5 for s in stops.values()),'STOP_NOT_FROM_LINK_FAILURE')
        key=xp.digest(dict(stops=stops,context=b['context_id'],page=slot['page'],failed=failed))
        root=project/'bridge/expired-link-returns'/key
        reservation=bridge.identity.legacy.account_root()/'expired-link-return-attempts'/(key+'.json')
        nr.require(not reservation.exists() and not (root/'attempt.json').exists(),'EXPIRED_LINK_RETURN_ALREADY_ATTEMPTED')
        driver=driver or bridge.Driver(b['runtime'])
        try:
            meta=driver.call('metadata',b,'detail')
            nr.require(meta.get('page')==slot['page'],'REGISTERED_TARGET_CHANGED')
            validate_expired(meta.get('url',''),failed,target)
            attempt=dict(at=xp.legacy.now(),binding=b,metadata=meta,stops=stops,authorization=authorization,
                collection_permitted=False,failed_url=failed,target_url=target,normal_source=source_ref)
            nr.immutable(reservation,attempt);nr.immutable(root/'attempt.json',attempt)
            # This helper verifies the exact error text and rejects verification
            # prompts before submitting one navigation to the archived healthy URL.
            result=navigator(b,meta['url'],failed,target)
            nr.immutable(root/'navigation.json',result)
            actual=driver.call('capture',b,'detail',**manual.proof_args(target))
            manual.require_capture(actual,b,manual.proof_args(target))
            nr.require(nr.current_stops(keys)==stops,'STOP_CHANGED')
            ref=manual.evidence(root/'normal-body.json',actual)
            result=dict(at=xp.legacy.now(),capture=ref,collection_permitted=False,
                state='fresh_revalidation_required',original_binding_unchanged=True,
                stops_unchanged=True,failed_note_excluded=manual.note_id(failed))
            nr.immutable(root/'result.json',result);return result
        except BaseException as exc:
            if (root/'attempt.json').exists():
                nr.immutable(root/'failure.json',dict(at=xp.legacy.now(),error=getattr(exc,'code',type(exc).__name__),no_replay=True))
            if getattr(exc,'code',None) in {'ACCESS_STOP','LOGIN_REQUIRED','CAPTCHA'}:
                bridge.save_failure(project,name,b,exc,keys)
            raise
        finally:
            if owned:driver.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',required=True,type=Path);parser.add_argument('--binding',required=True)
    parser.add_argument('--authorization',required=True)
    parser.add_argument('--normal-source',type=Path,help='Exact pre-failure web_validation body source; never an arbitrary URL')
    a=parser.parse_args()
    print(json.dumps(restore(a.project,a.binding,a.authorization,normal_source=a.normal_source),ensure_ascii=False))
