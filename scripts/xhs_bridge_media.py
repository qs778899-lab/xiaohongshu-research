"""Read-only rendered carousel capture; never marks images reviewed."""
import argparse
import base64
import json
import hashlib
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse,parse_qsl

import xhs_bridge as bridge
import xhs_project as project_module

CAPTURE_REVISION = 'rendered-carousel-v4-activation-prime-stable-identity'


def prepare_media_target(project, binding, driver, task):
    """Keep normal target guards; a user-tab activation failure is a local gap."""
    slot = binding['tabs']['detail']
    if (binding.get('manual_single_tab') is True
            and binding.get('transport_surface') == 'browser'
            and slot.get('owned') is False):
        class NoUserTabActivation:
            def call(self, action, *args, **kwargs):
                if action == 'activate':
                    # prepare_target can observe a background page after navigation.
                    # Do not submit an unsupported selection or attempt a fallback.
                    raise bridge.BridgeError('MEDIA_ACTIVATION_UNCONFIRMED')
                return driver.call(action, *args, **kwargs)
        return bridge.prepare_target(project, binding, NoUserTabActivation(), task)
    return bridge.prepare_target(project, binding, driver, task)


def prepare_carousel_view(driver, binding, expected):
    """Use a verified already-visible user tab without forbidden tab selection."""
    slot=binding['tabs']['detail']
    before=bridge.verify_tab(driver,binding,'detail')
    started=bridge.legacy.now()
    if slot.get('owned') is True:
        response=driver.call('activate',binding,'detail')
        if response.get('activated')!=slot['page'] or response.get('windowFocusRequested') is not False:
            raise bridge.BridgeError('MEDIA_ACTIVATION_UNCONFIRMED')
        action='activate'
    elif (binding.get('manual_single_tab') is True and binding.get('transport_surface')=='browser'
          and slot.get('owned') is False and slot.get('chrome_tab_id') and slot.get('window_id')
          and before.get('visible') is True):
        action='verify_visible_user_tab'
        response={'page':slot['page'],'visible_before':True,'windowFocusRequested':False,'selectionSubmitted':False}
    else:
        raise bridge.BridgeError('MEDIA_ACTIVATION_UNCONFIRMED')
    verified=bridge.verify_tab(driver,binding,'detail')
    if (verified.get('page')!=slot['page'] or bridge.target_path(urlparse(verified.get('url','')).path)!=expected
        or (action=='verify_visible_user_tab' and verified.get('visible') is not True)):
        raise bridge.BridgeError('MEDIA_ACTIVATION_UNCONFIRMED')
    if action=='verify_visible_user_tab':response['visible_after']=True
    return {'capture_revision':CAPTURE_REVISION,'action':action,'response':response,
            'page':verified['page'],'account_uid':binding['account_uid'],'context_id':binding['context_id'],
            'target_note_id':expected.rsplit('/',1)[-1],'verified_url':verified['url'],
            'activated_at':started if action=='activate' else None,'preparation_started_at':started,
            'verified_at':bridge.legacy.now(),'visual_verification_required':True}


def confirmed_view_preparation(proof):
    response=proof.get('response',{})
    common=proof.get('page') and response.get('windowFocusRequested') is False
    if proof.get('action')=='activate':
        return bool(common and response.get('activated')==proof['page'] and proof.get('activated_at'))
    return bool(common and proof.get('action')=='verify_visible_user_tab'
                and response.get('page')==proof['page'] and response.get('selectionSubmitted') is False
                and response.get('visible_before') is True and response.get('visible_after') is True
                and proof.get('preparation_started_at'))


def stable_asset_identity(url):
    """Compare observed resources; this never fetches, synthesizes or refreshes a URL."""
    if not isinstance(url,str) or not url:raise bridge.BridgeError('MEDIA_IDENTITY_UNCONFIRMED')
    parsed=urlparse(url);host=(parsed.hostname or '').lower()
    if parsed.scheme not in {'http','https'}:
        return {'opaque_url_sha256':hashlib.sha256(url.encode()).hexdigest()}
    try:port=parsed.port
    except ValueError:raise bridge.BridgeError('MEDIA_IDENTITY_UNCONFIRMED')
    if parsed.username or parsed.password:raise bridge.BridgeError('MEDIA_IDENTITY_UNCONFIRMED')
    path=parsed.path;query=parse_qsl(parsed.query,keep_blank_values=True)
    if host=='xhscdn.com' or host.endswith('.xhscdn.com'):
        # Known CDN timestamp/signature prefix; the complete asset path and
        # transform suffix remain identity-bearing. Unknown path formats stay exact.
        prefix=re.fullmatch(r'/[0-9]{12,14}/[a-fA-F0-9]{32}/(.+)',path)
        if prefix:path='/'+prefix[1]
        signatures={'signature','sign','token','xsec_token','expires','expiration','timestamp','ts',
                    'auth_key','authkey','key-pair-id','policy','ossaccesskeyid','accesskeyid',
                    'x-oss-signature','x-oss-expires','x-oss-date','x-oss-credential','x-oss-security-token',
                    'x-amz-signature','x-amz-expires','x-amz-date','x-amz-credential','x-amz-security-token','x-amz-algorithm'}
        query=[(k,v) for k,v in query if k.lower() not in signatures]
    return {'scheme':parsed.scheme,'host':host,'port':port,'path':path,'fragment':parsed.fragment,
            'query':sorted(query,key=lambda pair:pair[0])}


def stable_media_identity(state):
    if (state.get('kind') not in {'image','video'} or state.get('screenshotReady') is not True
            or state.get('mediaStatus')=='empty_or_unloaded'):
        raise bridge.BridgeError('MEDIA_EMPTY_OR_UNLOADED')
    evidence=state.get('mediaEvidence',{});kind=state['kind'];assets=[]
    for media in evidence.get('images' if kind=='image' else 'videos',[]):
        if media.get('loaded') is not True:continue
        valid=(media.get('visible') is True and media.get('src') and
               ((media.get('complete') is True and media.get('naturalWidth',0)>0 and media.get('naturalHeight',0)>0)
                if kind=='image' else (media.get('readyState',0)>=2 and media.get('videoWidth',0)>0 and media.get('videoHeight',0)>0)))
        if not valid:raise bridge.BridgeError('MEDIA_IDENTITY_UNCONFIRMED')
        assets.append(stable_asset_identity(media['src']))
    if not assets:raise bridge.BridgeError('MEDIA_IDENTITY_UNCONFIRMED')
    unique={json.dumps(asset,sort_keys=True):asset for asset in assets}
    return {'index':state['index'],'count':state['count'],'kind':kind,'assets':[unique[k] for k in sorted(unique)]}


def assert_media_stable(project,note,position,before,after,phase):
    left=right=None
    try:
        left=stable_media_identity(before);right=stable_media_identity(after)
        if left!=right:raise bridge.BridgeError('MEDIA_POSITION_CHANGED')
    except bridge.BridgeError as exc:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        path=Path(project)/'bridge'/'media'/note/'diagnostics'/f'{position:03d}-{phase}-{stamp}.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'phase':phase,'code':exc.code,'note_id':note,'position':position,
            'before':before,'after':after,'stable_before':left,'stable_after':right,
            'no_automatic_retry':True,'visual_review_not_completed':True},ensure_ascii=False,indent=2))
        exc.diagnostic_file=str(path);raise
    return right


def screenshot_target(shot,expected):
    bridge.safe_url(shot.get('url'))
    if bridge.target_path(urlparse(shot['url']).path)!=expected:raise bridge.BridgeError('TARGET_MISMATCH')


def partial_scope(binding):
    """Ignore only observation timestamps/window metadata, never authority or targets."""
    value=json.loads(json.dumps(binding))
    for slot in value.get('tabs',{}).values():
        for key in ('last_url','window_id','verified_at'):slot.pop(key,None)
    return value


def mixed_slide_resources(state):
    """Identity evidence for the observed hidden-LIVE/loaded-image case, not readiness."""
    evidence=state.get('mediaEvidence',{})
    if (set(evidence.get('potentialKinds',[]))!={'image','video'}
            or evidence.get('backgrounds')
            or any(evidence.get(k,{}).get('visible') is not True for k in ('root','activeSlide'))):
        raise bridge.BridgeError('MEDIA_PARTIAL_IDENTITY_UNCONFIRMED')
    assets={}
    for kind in ('images','videos'):
        items=evidence.get(kind,[])
        if not items or any(not item.get('src') for item in items):
            raise bridge.BridgeError('MEDIA_PARTIAL_IDENTITY_UNCONFIRMED')
        assets[kind]=sorted({json.dumps(stable_asset_identity(item['src']),sort_keys=True) for item in items})
    if not any(i.get('loaded') is True and i.get('visible') is True and i.get('complete') is True
               and i.get('naturalWidth',0)>2 and i.get('naturalHeight',0)>2 for i in evidence['images']):
        raise bridge.BridgeError('MEDIA_PARTIAL_IDENTITY_UNCONFIRMED')
    return {'index':state.get('index'),'count':state.get('count'),'assets':assets}


def confirm_local_warmup_gap(project,binding,b,driver,keys,scope,note,position,before,after):
    """Recheck the original guards; this never retries, captures or marks media read."""
    def hidden_live(state):
        videos=state.get('mediaEvidence',{}).get('videos',[])
        return (state.get('kind')=='empty_or_unloaded' and state.get('mediaStatus')=='empty_or_unloaded'
                and state.get('screenshotReady') is False and bool(videos)
                and all(v.get('visible') is False and v.get('loaded') is False and v.get('display')=='none'
                        and v.get('readyState',0)>=2 and v.get('videoWidth',0)>0 and v.get('videoHeight',0)>0 for v in videos))
    if before.get('kind')!='video' or not hidden_live(after):
        raise bridge.BridgeError('MEDIA_PARTIAL_IDENTITY_UNCONFIRMED')
    stable_media_identity(before)
    identity=mixed_slide_resources(before)
    if identity['index']!=position-1 or identity!=mixed_slide_resources(after):
        raise bridge.BridgeError('MEDIA_POSITION_CHANGED')
    current=bridge.load_binding(project,binding)
    if partial_scope(current)!=scope or partial_scope(b)!=scope:
        raise bridge.BridgeError('MEDIA_PARTIAL_SCOPE_CHANGED')
    # Use the same STOP/grant checks as the already held account lease, without
    # reacquiring locks or granting a new exception. New STOPs remain fatal.
    bridge.assert_web_project_access(project,b['account_uid'])
    exceptions=bridge.web_authorization_exceptions(project,b)
    from xhs_note_recovery import FORK_KIND,fork_path
    is_fork=bool(b.get('web_parent_authorization') or b.get('binding_kind')==FORK_KIND
                 or fork_path(project,'names',b['binding_name']).exists())
    recovery={'project':str(project),'binding':b['binding_name']} if b.get('note_unavailable_recovery') or is_fork else None
    bridge.identity.check_stops(keys,web_stop_exceptions=exceptions,web_note_recovery=recovery)
    if (Path(project)/'bridge'/'PAUSE').exists():raise bridge.BridgeError('USER_PAUSED')
    meta=bridge.verify_tab(driver,b,'detail')
    expected='/explore/'+note
    if (meta.get('page')!=b['tabs']['detail']['page']
            or bridge.target_path(urlparse(meta.get('url','')).path)!=expected):
        raise bridge.BridgeError('TARGET_MISMATCH')
    fresh=driver.call('media_state',b,'detail',expectedPath=expected)
    if not hidden_live(fresh) or identity!=mixed_slide_resources(fresh):
        raise bridge.BridgeError('MEDIA_PARTIAL_IDENTITY_UNCONFIRMED')
    return {'resources':identity,'verified_at':bridge.legacy.now(),'page':meta['page'],
            'note_id':note,'account_uid':b['account_uid'],'context_id':b['context_id'],
            'session':b['tabs']['detail'].get('session'),'still_unread':True,
            'static_image_unread':True,'paired_live_video_unread':True}


def partial_event(path,event,**fields):
    if path is None:return
    with path.open('a',encoding='utf-8') as stream:
        stream.write(json.dumps({'event':event,'at':bridge.legacy.now(),**fields},ensure_ascii=False)+'\n')
        stream.flush();os.fsync(stream.fileno())


def capture(project, binding, task_id, positions, driver=None, *, continue_local_empty=False):
    project = Path(project).resolve()
    db = project_module.Project(project)
    try:
        task = db.task(task_id)
        if task['payload']['stage'] not in {'body', 'images', 'video'}:
            raise bridge.BridgeError('MEDIA_TASK_REQUIRED')
        url = bridge.observed_url(project, task)
        note = task['payload']['target']
    finally:
        db.db.close()
    expected = '/explore/' + note
    if continue_local_empty and (any(type(i) is not int for i in positions)
                                 or any(a>=b for a,b in zip(positions,positions[1:]))):
        raise bridge.BridgeError('MEDIA_PARTIAL_ORDER_REQUIRED')
    b = bridge.load_binding(project, binding)
    driver = driver or bridge.Driver(b['runtime'])
    journal=None;records=[];gaps=[]
    if continue_local_empty and positions:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        journal=project/'bridge'/'media'/note/'batches'/f'{stamp}-{uuid.uuid4().hex}.jsonl'
        journal.parent.mkdir(parents=True,exist_ok=True)
        with journal.open('x',encoding='utf-8'):pass
        partial_event(journal,'requested',task_id=task_id,binding=binding,note_id=note,
                      requested_positions=positions,automatic_retry=False,reviewed=False)
    with bridge.lease(project, b) as keys:
        try:
            db = project_module.Project(project)
            try:
                prepare_media_target(db, b, driver, task)
            finally:
                db.close()
            bridge.save_binding(project, binding, b)
            scope=partial_scope(b) if continue_local_empty else None
            state = driver.call('media_state', b, 'detail', expectedPath=expected)
            if not positions:
                return dict(state, note=note, position=state['index'] + 1,
                            verified_target={'note_id':note,'account_uid':b['account_uid'],
                                             'context_id':b['context_id'],'page':b['tabs']['detail']['page']})
            if any(i < 1 or i > state['count'] for i in positions):
                raise bridge.BridgeError('MEDIA_INDEX_INVALID')
            # Owned tabs keep exact selection. User tabs need an already-visible
            # verified surface; never try alternate forbidden activation methods.
            # Both paths still require warmup, stable media identity and visual reading.
            activation=prepare_carousel_view(driver,b,expected)
            time.sleep(1)
            refreshed=driver.call('media_state',b,'detail',expectedPath=expected)
            if refreshed['count']!=state['count'] or refreshed['index']!=state['index']:
                raise bridge.BridgeError('MEDIA_POSITION_CHANGED')
            state=refreshed
            if state.get('kind')=='empty_or_unloaded' or state.get('mediaStatus')=='empty_or_unloaded':
                raise bridge.BridgeError('MEDIA_EMPTY_OR_UNLOADED')
            cursor_index=state['index'];carousel_count=state['count'];gap_anchor=None
            for position in positions:
                partial_event(journal,'position_started',position=position,reviewed=False)
                bridge.verify_tab(driver, b, 'detail')
                state = driver.call('media_state', b, 'detail', expectedPath=expected)
                if continue_local_empty:
                    if state['index']!=cursor_index or state['count']!=carousel_count:
                        raise bridge.BridgeError('MEDIA_POSITION_CHANGED')
                    if gap_anchor is not None and mixed_slide_resources(state)!=gap_anchor:
                        raise bridge.BridgeError('MEDIA_POSITION_CHANGED')
                driver.call('media_select', b, 'detail', expectedPath=expected, index=position - 1,
                            expectedIndex=state['index'], expectedCount=state['count'])
                time.sleep(1)
                after = driver.call('media_state', b, 'detail', expectedPath=expected)
                if after['index'] != position - 1 or after['count'] != state['count']:
                    raise bridge.BridgeError('MEDIA_SELECTION_UNCONFIRMED')
                if after.get('kind')=='empty_or_unloaded' or after.get('mediaStatus')=='empty_or_unloaded':
                    raise bridge.BridgeError('MEDIA_EMPTY_OR_UNLOADED')
                assert_media_stable(project,note,position,after,after,'pre-warmup')
                # A compositor may need one capture before it paints decoded media.
                # Warm up once per requested position; discarded pixels are never evidence read.
                bridge.verify_tab(driver,b,'detail')
                prime=driver.call('screenshot',b,'detail',expectedPath=expected)
                screenshot_target(prime,expected)
                try:prime_bytes=base64.b64decode(prime['pngBase64'],validate=True)
                except (ValueError,TypeError,KeyError):raise bridge.BridgeError('MEDIA_WARMUP_UNCONFIRMED')
                if not prime_bytes:raise bridge.BridgeError('MEDIA_WARMUP_UNCONFIRMED')
                preparation={'action':'discarded_guarded_warmup_screenshot','discarded_warmup':True,
                             'prime_sha256':hashlib.sha256(prime_bytes).hexdigest(),'observed_at':bridge.legacy.now(),
                             'wait_seconds':1,'does_not_prove_readability':True}
                del prime,prime_bytes
                time.sleep(1)
                warmed=driver.call('media_state',b,'detail',expectedPath=expected)
                try:
                    assert_media_stable(project,note,position,after,warmed,'warmup')
                except bridge.BridgeError as exc:
                    if not continue_local_empty or exc.code!='MEDIA_EMPTY_OR_UNLOADED':raise
                    proof=confirm_local_warmup_gap(project,binding,b,driver,keys,scope,note,position,after,warmed)
                    diagnostic=Path(exc.diagnostic_file)
                    gap={'position':position,'count':after['count'],'code':exc.code,'phase':'warmup',
                         'diagnostic_file':str(diagnostic),'diagnostic_sha256':hashlib.sha256(diagnostic.read_bytes()).hexdigest(),
                         'continuation_proof':proof,'reviewed':False,'automatic_retry':False,
                         'static_image_unread':True,'paired_live_video_unread':True}
                    gaps.append(gap);partial_event(journal,'position_gap',**gap)
                    cursor_index=position-1;gap_anchor=proof['resources']
                    continue
                bridge.verify_tab(driver,b,'detail')
                shot = driver.call('screenshot', b, 'detail', expectedPath=expected)
                screenshot_target(shot,expected)
                final = driver.call('media_state', b, 'detail', expectedPath=expected)
                identity=assert_media_stable(project,note,position,warmed,final,'formal-capture')
                stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                out = project / 'bridge' / 'media' / note / f'{position:03d}-{stamp}.png'
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(base64.b64decode(shot['pngBase64']))
                record = {'position': position, 'count': after['count'], 'kind': after['kind'],
                          'file': str(out), 'url': shot['url'], 'observed_at': bridge.legacy.now(),
                          'reviewed': False, 'capture_revision': CAPTURE_REVISION,
                          'activation': activation, 'paint_preparation': preparation, 'stable_media_identity': identity,
                          'visual_verification_required': True}
                out.with_suffix('.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
                records.append(record)
                partial_event(journal,'position_captured',**record)
                cursor_index=position-1;gap_anchor=None
            if continue_local_empty:
                result={'captures':records,'position_gaps':gaps,'analysis_required':True,
                        'partial':bool(gaps),'complete':False,'reviewed':False,'journal_file':str(journal),
                        'body_carousel_only':True,'comment_media_scan_complete':False,
                        'video_requires_segments':bool(gaps) or any(r['kind']=='video' for r in records)}
                partial_event(journal,'handoff',captured_positions=[r['position'] for r in records],
                              gap_positions=[g['position'] for g in gaps],complete=False,reviewed=False)
                return result
            return {'captures': records, 'analysis_required': True}
        except Exception as exc:
            try:
                partial_event(journal,'stopped',code=getattr(exc,'code',type(exc).__name__),
                              captured_positions=[r['position'] for r in records],
                              gap_positions=[g['position'] for g in gaps],automatic_retry=False,complete=False)
            except OSError:pass  # A journal failure must not prevent the original STOP handler.
            bridge.save_failure(project, binding, b, exc, keys)
            raise


def collect_inventory(db, binding, task, driver):
    """Discover actual carousel positions and hand visual review to Codex in small batches."""
    import xhs_rounds as rounds
    rounds.schema(db)
    note=task['payload']['target']
    state=capture(db.path,binding,task['id'],[],driver)
    if state.get('kind')=='empty_or_unloaded' or state.get('mediaStatus')=='empty_or_unloaded':
        raise bridge.BridgeError('MEDIA_EMPTY_OR_UNLOADED')
    existing={r[0]:json.loads(r[1]) for r in db.db.execute('SELECT media_key,body FROM media_inventory WHERE note_id=?',(note,))}
    states={r[0]:r[1] for r in db.db.execute('SELECT media_key,state FROM media_inventory WHERE note_id=?',(note,))}
    def usable(key,record):
        file=Path(record.get('file',''))
        valid=(record.get('count')==state['count'] and file.is_file()
               and record.get('sha256')==__import__('hashlib').sha256(file.read_bytes()).hexdigest())
        if not valid:return False
        # Actual prior visual review remains valid regardless of capture revision.
        if states.get(key)=='reviewed':return True
        proof=record.get('activation',{});target=state.get('verified_target',{})
        preparation=record.get('paint_preparation',{})
        return (record.get('capture_revision')==CAPTURE_REVISION and proof.get('capture_revision')==CAPTURE_REVISION
                and preparation.get('discarded_warmup') is True and preparation.get('wait_seconds')==1
                and re.fullmatch(r'[a-f0-9]{64}',preparation.get('prime_sha256','')) and record.get('stable_media_identity')
                and confirmed_view_preparation(proof)
                and proof.get('target_note_id')==note and target.get('account_uid') and target.get('context_id')
                and proof.get('account_uid')==target.get('account_uid')
                and proof.get('context_id')==target.get('context_id') and proof.get('verified_at')
                and bridge.target_path(urlparse(proof.get('verified_url','')).path)=='/explore/'+note)
    missing=[i for i in range(1,state['count']+1) if not usable(f'body:{i}',existing.get(f'body:{i}',{}))]
    records=[]
    if missing:
        records=capture(db.path,binding,task['id'],missing[:3],driver)['captures']
        for r in records:
            previous=existing.get(f"body:{r['position']}")
            if previous:
                r['previous_capture']={k:previous.get(k) for k in ('file','sha256','capture_revision')}
                r['previous_capture']['reason']='unreviewed_capture_missing_current_activation_proof_or_invalid_file'
            r['sha256']=__import__('hashlib').sha256(Path(r['file']).read_bytes()).hexdigest()
            db.db.execute('INSERT OR REPLACE INTO media_inventory VALUES(?,?,?,?,?)',(note,f"body:{r['position']}",r['kind'],rounds.packed(r),'captured_unreviewed'))
        db.db.commit()
    else:
        records=[v for k,v in existing.items() if k.startswith('body:') and states.get(k)!='reviewed'][:3]
        if not records:
            pending=[{'key':k,**v} for k,v in existing.items() if k.startswith('comment:') and states.get(k)!='reviewed']
            bridge.hold_task(db,task['id'],'MEDIA_CONTEXT_REVIEW_REQUIRED')
            return {'local_gap':'MEDIA_CONTEXT_REVIEW_REQUIRED','comment_attachments':pending,'next':'Codex checks comment attachment context and video/caption coverage; do not recapture the already reviewed carousel.'}
    # Video frames are never passed off as transcription or verified content.
    video=any(r.get('kind')=='video' for r in records)
    snap={'url':records[0]['url'] if records else bridge.observed_url(db.path,task),
          'media_captures':records,'carousel_count':state['count'],'captured_positions':[r['position'] for r in records],
          'video_requires_segments':video,'body_carousel_only':True,'comment_media_scan_complete':False}
    result=bridge.store_capture(db,task,snap)
    return dict(result,media_files=[r['file'] for r in records],video_requires_segments=video,
                next='Codex visually reads images; video needs existing caption/transcript workflow' if video else 'Codex reads media and submits exact position receipts')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--binding', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--positions', nargs='*', type=int, default=[])
    parser.add_argument('--continue-local-empty', action='store_true',
                        help='Keep a verified mixed LIVE/image warmup gap and continue only later explicitly requested positions; never marks media read.')
    args = parser.parse_args()
    try:
        print(json.dumps(capture(args.project, args.binding, args.task, args.positions,
                                 continue_local_empty=args.continue_local_empty), ensure_ascii=False))
    except bridge.BridgeError as exc:
        print(json.dumps({'error': exc.code, 'diagnostic_file': getattr(exc,'diagnostic_file',None)}))
        raise SystemExit(1)
