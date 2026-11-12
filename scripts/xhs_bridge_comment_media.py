"""Capture one already observed comment attachment on the current owned note tab.

No navigation, reply expansion, viewer click, URL refresh, CDN fetch or review receipt.
Each attachment has one durable display transaction; uncertain mutations are not replayed.
"""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
from urllib.parse import urlparse

import xhs_bridge as bridge
import xhs_native as native

REVISION='rendered-comment-attachment-v1'
PROPERTIES=('width','height','max-width','max-height','object-fit','overflow')
ROLES=('image','imgBox','commentPicture')


class LocalGap(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)


def require(test,code):
    if not test:raise bridge.BridgeError(code)


def digest(raw):return hashlib.sha256(raw).hexdigest()


def packed(value):return json.dumps(value,ensure_ascii=False,sort_keys=True).encode()


def safe_file(path,root):
    # Inspect the serialized value before pathlib can erase '.' or repeated '/'.
    # Relative archives are rooted here; absolute cached files use the same gate.
    try:raw=os.fspath(path)
    except TypeError:raise bridge.BridgeError('COMMENT_SOURCE_PATH_UNSAFE') from None
    require(isinstance(raw,str) and bool(raw) and '\0' not in raw and
            not any(part in {'.','..'} for part in raw.split('/')) and str(Path(raw))==raw,
            'COMMENT_SOURCE_PATH_UNSAFE')
    root=Path(root).resolve(strict=True)
    path=Path(raw) if Path(raw).is_absolute() else root/raw
    require(path.is_relative_to(root) and all(not p.is_symlink() for p in (path,*path.parents)),
            'COMMENT_SOURCE_PATH_UNSAFE')
    require(path.is_file(),'COMMENT_SOURCE_MISSING')
    try:resolved=path.resolve(strict=True)
    except (OSError,RuntimeError):raise bridge.BridgeError('COMMENT_SOURCE_PATH_UNSAFE') from None
    require(resolved.is_relative_to(root),'COMMENT_SOURCE_PATH_UNSAFE')
    return resolved


def note_url(value,note):
    bridge.safe_url(value)
    require(bridge.target_path(urlparse(value).path)=='/explore/'+note,'TARGET_MISMATCH')


def source_context(project,task_id,source_hash,comment_id,image_index,account_uid):
    require(bool(re.fullmatch(r'[a-f0-9]{64}',source_hash or '')),'COMMENT_SOURCE_SHA_REQUIRED')
    require(bool(re.fullmatch(r'[a-f0-9]{24}',comment_id or '')),'COMMENT_REAL_ID_REQUIRED')
    require(type(image_index) is int and image_index>=1,'COMMENT_IMAGE_INDEX_INVALID')
    db=native.open_lookup_project(project)
    try:
        task=db.task(task_id);p=task['payload'];note=p.get('target')
        require(task['kind']=='native' and p.get('stage')=='images' and
                bool(re.fullmatch(r'[a-f0-9]{24}',note or '')),'COMMENT_IMAGES_TASK_REQUIRED')
        refs=[]
        for entry in p.get('provenance',[]):
            if entry.get('sha256')==source_hash and entry.get('archive'):
                archive=safe_file(entry['archive'],project)
                require(digest(archive.read_bytes())==source_hash,'IMMUTABLE_SOURCE_CHANGED')
                refs.append({'task_id':task_id,'kind':'registered_images_source','archive':entry['archive']})
        for row in db.db.execute("SELECT id,payload FROM tasks WHERE kind='native'"):
            other=json.loads(row['payload'])
            if (other.get('target')!=note or other.get('stage') not in {'roots','replies'}
                    or source_hash not in other.get('bridge',{}).get('hashes',[])):continue
            for event in db.db.execute("SELECT artifact,sha256,receipt FROM native_events WHERE task_id=? AND action='read' AND outcome='ready' AND sha256=?",(row['id'],source_hash)):
                receipt=json.loads(event['receipt'])
                if receipt.get('target')!=note or receipt.get('target_verified') is not True:continue
                archive=safe_file(event['artifact'],project)
                require(digest(archive.read_bytes())==source_hash,'IMMUTABLE_SOURCE_CHANGED')
                refs.append({'task_id':row['id'],'kind':'registered_comment_capture','archive':event['artifact']})
        require(bool(refs),'COMMENT_SOURCE_NOT_REGISTERED_FOR_NOTE')
    finally:db.close()
    path=safe_file(Path(project)/'bridge'/'raw'/(source_hash+'.json'),project)
    raw=path.read_bytes();require(digest(raw)==source_hash,'IMMUTABLE_SOURCE_CHANGED')
    data=json.loads(raw);note_url(data.get('url'),note)
    if data.get('pageUrl'):note_url(data['pageUrl'],note)
    require(not any(data.get(k) for k in ('loginWall','securityBlock','notFound')),'COMMENT_SOURCE_RESTRICTED_OR_UNAVAILABLE')
    # A bare uid may be an author or legacy adapter field. Only explicit collector
    # fields establish historical account provenance; absence remains unknown.
    collectors=[data[k] for k in ('collector_account_uid','capture_account_uid','account_uid') if k in data]
    if isinstance(data.get('collection_identity'),dict) and 'account_uid' in data['collection_identity']:
        collectors.append(data['collection_identity']['account_uid'])
    require(all(isinstance(v,str) and re.fullmatch(r'[a-f0-9]{24}',v) for v in collectors),'COMMENT_SOURCE_ACCOUNT_UNCONFIRMED')
    require(all(v==account_uid for v in collectors),'COMMENT_SOURCE_ACCOUNT_MISMATCH')
    rows=[(i,r) for i,r in enumerate(data.get('results',[])) if r.get('commentId')==comment_id]
    require(len(rows)==1,'COMMENT_SOURCE_ID_AMBIGUOUS_OR_MISSING')
    index,row=rows[0]
    require(row.get('identityKind')=='observed_dom_id','COMMENT_REAL_ID_REQUIRED')
    images=row.get('images',[])
    require(isinstance(images,list) and image_index<=len(images),'COMMENT_IMAGE_INDEX_INVALID')
    image_url=images[image_index-1]
    require(isinstance(image_url,str) and images.count(image_url)==1,'COMMENT_IMAGE_SOURCE_AMBIGUOUS')
    parsed=urlparse(image_url)
    try:port=parsed.port
    except ValueError:raise bridge.BridgeError('OBSERVED_COMMENT_IMAGE_REQUIRED') from None
    require(parsed.scheme=='https' and bool(re.search(r'(?:^|\.)xhscdn\.com$',parsed.hostname or ''))
            and not port and not parsed.username and not parsed.password and '/comment/' in parsed.path,
            'OBSERVED_COMMENT_IMAGE_REQUIRED')
    require(type(row.get('is_reply')) is bool,'COMMENT_PARENT_CONTEXT_UNCONFIRMED')
    parent=row.get('parentCommentId') or ''
    require((row['is_reply'] and bool(re.fullmatch(r'[a-f0-9]{24}',parent))) or
            (not row['is_reply'] and not parent),'COMMENT_PARENT_CONTEXT_UNCONFIRMED')
    parents=[(i,r) for i,r in enumerate(data.get('results',[])) if parent and r.get('commentId')==parent]
    require(len(parents)<=1,'COMMENT_PARENT_CONTEXT_AMBIGUOUS')
    def context(r):
        href=r.get('authorHrefRaw','');uid=''
        if isinstance(href,str):
            url=urlparse(href)
            if not url.netloc or url.netloc=='www.xiaohongshu.com':
                match=re.fullmatch(r'/user/profile/([a-f0-9]{24})/?',url.path)
                if match:uid=match[1]
        text=r.get('text','')
        return {'comment_id':r.get('commentId'),'author':r.get('author'), 'author_uid':uid or 'unknown',
                'author_href_raw':href,'text':text[:4096] if isinstance(text,str) else '',
                'text_truncated':isinstance(text,str) and len(text)>4096,'time':r.get('time'),
                'parent_comment_id':r.get('parentCommentId') or '', 'reply_to_comment_id':r.get('replyToCommentId') or '',
                'reply_to':r.get('reply_to'),'is_reply':r.get('is_reply')}
    return {'task_id':task_id,'note_id':note,'source_file':str(path),'source_sha256':source_hash,
            'source_task_refs':refs,'source_account_uid':collectors[0] if collectors else 'unknown',
            'source_collector_uid_unavailable':not bool(collectors),'source_position':f'/results/{index}/images/{image_index-1}',
            'comment_position':f'/results/{index}','comment_id':comment_id,'image_index':image_index,
            'image_url':image_url,'comment_context':context(row),
            'parent_context':context(parents[0][1]) if parents else None,
            'parent_context_position':f'/results/{parents[0][0]}' if parents else None,
            'parent_context_gap':bool(parent and not parents)}


def authority(binding):
    result=json.loads(json.dumps(binding))
    for slot in result.get('tabs',{}).values():
        for key in ('last_url','window_id','verified_at'):slot.pop(key,None)
    return result


def journal(path,event,**fields):
    with path.open('a',encoding='utf-8') as stream:
        stream.write(json.dumps({'event':event,'at':bridge.legacy.now(),**fields},ensure_ascii=False)+'\n')
        stream.flush();os.fsync(stream.fileno())


def write_once(path,value):
    require(all(not p.is_symlink() for p in (path,*path.parents)),'COMMENT_OUTPUT_PATH_UNSAFE')
    with path.open('xb') as stream:
        stream.write(packed(value));stream.flush();os.fsync(stream.fileno())


def display_identity(state,context,uid):
    require(isinstance(state,dict) and state.get('format')=='comment-media-display-v1','COMMENT_DISPLAY_STATE_UNCONFIRMED')
    require(state.get('binding')=={'path':'/explore/'+context['note_id'],'uid':uid,
                'commentId':context['comment_id'],'imageUrl':context['image_url']},'COMMENT_DISPLAY_BINDING_CHANGED')
    require(state.get('loaded') is True and state.get('guardVisible') is True,'COMMENT_DISPLAY_UNLOADED_OR_HIDDEN')
    require(state.get('allowedProperties')==list(PROPERTIES),'COMMENT_DISPLAY_STATE_UNCONFIRMED')
    nodes=state.get('nodes');require(isinstance(nodes,list) and len(nodes)==3,'COMMENT_DISPLAY_STATE_UNCONFIRMED')
    for i,node in enumerate(nodes):
        require(node.get('role')==ROLES[i] and node.get('tagName')==('IMG' if i==0 else 'DIV') and
                isinstance(node.get('className'),str),'COMMENT_DISPLAY_STATE_UNCONFIRMED')
        inline=node.get('inline',{});require(set(inline)==set(PROPERTIES),'COMMENT_DISPLAY_STATE_UNCONFIRMED')
        for value in inline.values():
            require(isinstance(value,dict) and isinstance(value.get('value'),str) and len(value['value'])<=2048
                    and '\0' not in value['value'] and value.get('priority') in {'','important'}
                    and (value['value'] or not value['priority']),'COMMENT_DISPLAY_STATE_UNCONFIRMED')
    require(all(isinstance(state.get(k),(int,float)) and math.isfinite(state[k]) and state[k]>1
                for k in ('naturalWidth','naturalHeight')),'COMMENT_MEDIA_UNLOADED')
    return {k:state[k] for k in ('format','binding','naturalWidth','naturalHeight')} | {
        'nodes':[{k:n[k] for k in ('role','tagName','className','inline')} for n in nodes]}


def full_geometry(state,plan):
    image=state['nodes'][0];rect=image.get('rect') or {};bounds=plan.get('clippingBounds') or {}
    values=[rect.get(k) for k in ('left','top','right','bottom','width','height')]+[bounds.get(k) for k in ('left','top','right','bottom')]
    require(all(type(v) in (int,float) and math.isfinite(v) for v in values),'COMMENT_IMAGE_GEOMETRY_UNCONFIRMED')
    require(image.get('cssVisible') is True and image.get('hasRect') is True and image.get('intersectsViewport') is True
            and rect['width']>0 and rect['height']>0 and rect['left']>=bounds['left'] and rect['top']>=bounds['top']
            and rect['right']<=bounds['right'] and rect['bottom']<=bounds['bottom']
            and abs(rect['width']/rect['height']-state['naturalWidth']/state['naturalHeight'])<.02,
            'COMMENT_IMAGE_GEOMETRY_UNCONFIRMED')
    return rect


def capture(project,binding,task_id,source_hash,comment_id,image_index,driver=None):
    project=Path(project).resolve();b=bridge.load_binding(project,binding)
    require(b.get('state')=='ready','COMMENT_BINDING_NOT_READY')
    slot=b.get('tabs',{}).get('detail',{})
    require(slot.get('owned') is True and bool(re.fullmatch(r'[A-Fa-f0-9]{32}',slot.get('page','')))
            and slot.get('session') and not slot.get('navigation_intent'),'COMMENT_CURRENT_OWNED_NOTE_REQUIRED')
    context=source_context(project,task_id,source_hash,comment_id,image_index,b['account_uid'])
    expected='/explore/'+context['note_id'];note_url(slot.get('last_url'),context['note_id'])
    scope=authority(b);own_driver=driver is None;driver=driver or bridge.Driver(b['runtime'])
    # The key excludes source hash/URL, so re-encoding an old source or rotating a
    # signed URL cannot create a second mutation opportunity for this attachment.
    token=digest(packed({'note_id':context['note_id'],'comment_id':comment_id,'image_index':image_index}))
    directory=project/'bridge'/'comment-media'/context['note_id']/comment_id/token
    require(all(not p.is_symlink() for p in (directory,*directory.parents)),'COMMENT_OUTPUT_PATH_UNSAFE')
    log=directory/'events.jsonl';result_file=directory/'result.json';record={};enlarged=False;restored=False
    def guard(keys):
        current=bridge.load_binding(project,binding)
        require(authority(current)==scope and authority(b)==scope,'COMMENT_BINDING_AUTHORITY_CHANGED')
        bridge.assert_web_project_access(project,b['account_uid'])
        exceptions=bridge.web_authorization_exceptions(project,b)
        from xhs_note_recovery import FORK_KIND,fork_path
        is_fork=bool(b.get('web_parent_authorization') or b.get('binding_kind')==FORK_KIND or fork_path(project,'names',binding).exists())
        recovery={'project':str(project),'binding':binding} if b.get('note_unavailable_recovery') or is_fork else None
        bridge.identity.check_stops(keys,web_stop_exceptions=exceptions,web_note_recovery=recovery)
        if (project/'bridge'/'PAUSE').exists():raise bridge.BridgeError('USER_PAUSED')
        meta=bridge.verify_tab(driver,b,'detail');require(meta.get('page')==slot['page'],'TARGET_MISMATCH')
        note_url(meta.get('url'),context['note_id'])
        require(meta.get('visible') is True,'COMMENT_TARGET_VISIBILITY_UNCONFIRMED')
    def call(keys,action,**fields):
        guard(keys)
        return driver.call(action,b,'detail',expectedPath=expected,commentId=comment_id,imageUrl=context['image_url'],**fields)
    def bound(value):
        require(isinstance(value,dict),'COMMENT_MEDIA_STATE_UNCONFIRMED')
        require(value.get('uid')==b['account_uid'],'ACCOUNT_MISMATCH');note_url(value.get('url'),context['note_id'])
        require(value.get('commentId')==comment_id and value.get('observedImageUrl')==context['image_url'], 'COMMENT_MEDIA_BINDING_CHANGED')
    def matched(value,allow_gap=False):
        bound(value)
        if value.get('gap') and not allow_gap:raise LocalGap(value['gap'])
        require(value.get('matchState')=='matched' and value.get('matchingImages')==1,'COMMENT_MEDIA_MATCH_UNCONFIRMED')
        media=value.get('media',{})
        require(media.get('src')==context['image_url'],'COMMENT_MEDIA_SOURCE_CHANGED')
        require(value.get('isReply')==context['comment_context']['is_reply']
                and (value.get('parentCommentId') or '')==context['comment_context']['parent_comment_id'],'COMMENT_PARENT_CONTEXT_CHANGED')
        require(value.get('guardVisible') is True,'COMMENT_TARGET_VISIBILITY_UNCONFIRMED')
        if media.get('loaded') is not True or media.get('complete') is not True:raise LocalGap('COMMENT_MEDIA_UNLOADED')
        return value
    def mutation(keys,action,**fields):
        guard(keys)
        intent_id='comment-'+token[:48]+'-'+action.removeprefix('comment_media_')
        journal(log,'mutation_intent',action=action,display_intent_id=intent_id,parameters=fields,no_automatic_retry=True)
        value=driver.call(action,b,'detail',expectedPath=expected,commentId=comment_id,imageUrl=context['image_url'],
                          **({'displayIntentId':intent_id} if action!='comment_media_focus' else {}),**fields)
        journal(log,'mutation_result',action=action,response=value)
        return value
    def finish(**fields):
        if context['parent_context_gap']:
            fields['gaps']=list(fields.get('gaps',[]))+['parent_comment_text_not_in_source_snapshot']
        result={'revision':REVISION,**record,**fields,'complete':False,'reviewed':False,
                'comment_scope_complete':False,'note_media_scope_complete':False,'journal_file':str(log),
                'result_file':str(result_file),'source_context':context,'body_or_video_read':False,
                'display_enlarged':enlarged,'display_restored':restored}
        write_once(result_file,result);return result
    def run(keys):
        nonlocal record,enlarged,restored
        guard(keys)
        if directory.exists():
            # Existing failed/uncertain attempts are not resumable through this CLI.
            try:
                old=json.loads(safe_file(result_file,project).read_bytes())
                require(old.get('outcome')=='captured' and old.get('display_restored') is True,'COMMENT_MEDIA_ALREADY_ATTEMPTED')
                old_context=old['source_context']
                require(all(old_context.get(k)==context[k] for k in ('note_id','comment_id','image_index','image_url')),
                        'COMMENT_CACHED_RESOURCE_UNCONFIRMED')
                require(old.get('capture_account_uid')==b['account_uid'],'ACCOUNT_MISMATCH')
                for field,sha in [('file','sha256'),('sidecar_file','sidecar_sha256')]:
                    file=safe_file(old[field],project);require(digest(file.read_bytes())==old[sha],'COMMENT_CACHED_FILE_CHANGED')
                sidecar=json.loads(Path(old['sidecar_file']).read_bytes())
                require(sidecar.get('image_url')==context['image_url'] and sidecar.get('sha256')==old['sha256']
                        and sidecar.get('capture_account_uid')==b['account_uid'],'COMMENT_CACHED_RESOURCE_UNCONFIRMED')
                matched(call(keys,'comment_media_state'))
                return {**old,'cached':True,'requested_source_context':context,'current_content_read':False,
                        'new_screenshot_performed':False,'current_page_pixels_checked':False}
            except LocalGap as exc:
                return {'outcome':'gap','gaps':[exc.code],'complete':False,'reviewed':False,'cached':False,
                        'result_file':str(result_file),'source_context':context,'no_automatic_retry':True}
            except (KeyError,ValueError):raise bridge.BridgeError('COMMENT_MEDIA_ALREADY_ATTEMPTED') from None
        directory.mkdir(parents=True,exist_ok=False)
        write_once(directory/'request.json',{'context':context,'binding':binding,'authority':scope,'revision':REVISION})
        journal(log,'started',requested_context=context,reviewed=False)
        try:
            matched(call(keys,'comment_media_state'))
            focused=matched(mutation(keys,'comment_media_focus'))
            require(focused.get('focusSubmitted') is True and focused.get('interactionSubmitted') is True,
                    'COMMENT_FOCUS_OUTCOME_UNCONFIRMED')
            before=matched(call(keys,'comment_media_display_state'))
            original=before.get('displayState');original_id=display_identity(original,context,b['account_uid'])
            journal(log,'original_display_state',display_state=original,display_state_sha256=digest(packed(original)))
            grown=mutation(keys,'comment_media_enlarge',expectedDisplayState=original)
            bound(grown)
            if grown.get('gap'):
                if grown.get('displayMutationSubmitted') is False:raise LocalGap(grown['gap'])
                if grown.get('rolledBack') is True:
                    matched(grown,allow_gap=True)
                    require(display_identity(grown.get('displayState'),context,b['account_uid'])==original_id,
                            'COMMENT_ROLLBACK_UNCONFIRMED')
                    raise LocalGap(grown['gap'])
                raise bridge.BridgeError('COMMENT_ENLARGE_OUTCOME_UNCONFIRMED')
            matched(grown)
            require(grown.get('displayMutationSubmitted') is True and grown.get('enlarged') is True
                    and grown.get('fullImageGeometryConfirmed') is True,'COMMENT_ENLARGE_OUTCOME_UNCONFIRMED')
            enlarged=True
            require(display_identity(grown.get('originalDisplayState'),context,b['account_uid'])==original_id,
                    'COMMENT_ORIGINAL_DISPLAY_CHANGED')
            grown_id=display_identity(grown.get('displayState'),context,b['account_uid'])
            full_geometry(grown['displayState'],grown.get('displayPlan',{}))
            pre=matched(call(keys,'comment_media_display_state'))
            require(display_identity(pre.get('displayState'),context,b['account_uid'])==grown_id,'COMMENT_DISPLAY_STATE_CHANGED')
            pre_rect=full_geometry(pre['displayState'],pre.get('displayPlan',{}))
            shot=call(keys,'screenshot')
            note_url(shot.get('url'),context['note_id'])
            require(shot.get('uid')==b['account_uid'] and shot.get('page')==slot['page']
                    and shot.get('targetVerified') is True and shot.get('targetIdentitySpace')=='cdp_target_id',
                    'COMMENT_SCREENSHOT_IDENTITY_UNCONFIRMED')
            try:png=base64.b64decode(shot['pngBase64'],validate=True)
            except (KeyError,ValueError,TypeError):raise bridge.BridgeError('COMMENT_SCREENSHOT_INVALID') from None
            require(len(png)>=33 and png[:8]==b'\x89PNG\r\n\x1a\n' and png[12:16]==b'IHDR', 'COMMENT_SCREENSHOT_INVALID')
            width,height=struct.unpack('>II',png[16:24]);require(width>0 and height>0,'COMMENT_SCREENSHOT_INVALID')
            image_file=directory/'capture.png'
            require(all(not p.is_symlink() for p in (image_file,*image_file.parents)),'COMMENT_OUTPUT_PATH_UNSAFE')
            with image_file.open('xb') as stream:stream.write(png);stream.flush();os.fsync(stream.fileno())
            sidecar={'revision':REVISION,'file':str(image_file),'sha256':digest(png),'observed_at':bridge.legacy.now(),
                     'image_url':context['image_url'],'capture_account_uid':b['account_uid'],'source_context':context,
                     'current_target':{'page':slot['page'],'session':slot['session'],'context_id':b['context_id']},
                     'natural_width':pre['displayState']['naturalWidth'],'natural_height':pre['displayState']['naturalHeight'],
                     'screenshot_width':width,'screenshot_height':height,'image_css_rect':pre_rect,
                     'full_image_geometry_confirmed':True,'original_resolution_not_claimed':True,
                     'visual_occlusion_check_required':True,'geometry_is_not_pixel_read':True,'reviewed':False,
                     'restore_status':'not_yet_verified','body_or_video_read':False}
            sidecar_file=directory/'capture.json';write_once(sidecar_file,sidecar)
            record={k:sidecar[k] for k in ('file','sha256','observed_at','capture_account_uid')}
            record.update(sidecar_file=str(sidecar_file),sidecar_sha256=digest(sidecar_file.read_bytes()),
                          visual_occlusion_check_required=True,original_resolution_not_claimed=True,
                          current_content_read=False,cached=False,new_screenshot_performed=True)
            journal(log,'screenshot_saved',**record)
            after=matched(call(keys,'comment_media_display_state'))
            require(display_identity(after.get('displayState'),context,b['account_uid'])==grown_id,'COMMENT_DISPLAY_STATE_CHANGED')
            after_rect=full_geometry(after['displayState'],after.get('displayPlan',{}))
            require(all(abs(pre_rect[k]-after_rect[k])<1 for k in ('left','top','width','height')),
                    'COMMENT_SCREENSHOT_GEOMETRY_CHANGED')
            # Restoration is reached only on a completely known path. A STOP,
            # changed resource/CAS or uncertain command never enters a finally retry.
            restored_response=matched(mutation(keys,'comment_media_restore',expectedDisplayState=after['displayState'],
                                                originalDisplayState=original))
            require(restored_response.get('displayMutationSubmitted') is True and restored_response.get('restored') is True
                    and display_identity(restored_response.get('displayState'),context,b['account_uid'])==original_id,
                    'COMMENT_RESTORE_OUTCOME_UNCONFIRMED')
            restored=True;journal(log,'restored',restore_confirmed=True,reviewed=False)
            return finish(outcome='captured',gaps=['visual_read_and_occlusion_check_pending'],
                          current_page_pixels_checked=False)
        except LocalGap as exc:
            gap=exc.code
            journal(log,'gap',code=gap,display_enlarged=enlarged,display_restored=restored,no_automatic_retry=True)
            return finish(outcome='gap',gaps=[gap]+(['display_not_restored'] if enlarged and not restored else []))
        except BaseException as exc:
            # Persist known partial success before the existing STOP handler;
            # no mutation is sent here, including on Ctrl-C.
            try:
                journal(log,'stopped',code=getattr(exc,'code',type(exc).__name__),display_enlarged=enlarged,
                        display_restored=restored,no_automatic_retry=True)
                if not result_file.exists():finish(outcome='stopped',gaps=[getattr(exc,'code',type(exc).__name__)]+(
                    ['display_state_unrestored_or_uncertain'] if not restored else []))
            except OSError:pass
            raise
    try:
        with bridge.lease(project,b) as keys:
            try:return run(keys)
            except Exception as exc:
                bridge.save_failure(project,binding,b,exc,keys)
                raise
    finally:
        if own_driver:driver.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('project','binding','task','source','comment-id'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--image-index',type=int,required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(capture(args.project,args.binding,args.task,args.source,args.comment_id,args.image_index),ensure_ascii=False))
    except (bridge.BridgeError,bridge.legacy.StopRun) as exc:
        print(json.dumps({'error':getattr(exc,'code','SHARED_STOP'),'no_automatic_retry':True,'reviewed':False,'complete':False},ensure_ascii=False))
        raise SystemExit(1)


if __name__=='__main__':main()
