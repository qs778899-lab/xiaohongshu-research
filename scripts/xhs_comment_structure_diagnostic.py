"""One diagnostic of a held original note, never a comment retry or repair.

Reads structure/UI labels only; optional navigation uses an already observed
same-note body source. STOP and held task bytes remain authoritative.
"""
import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlparse

import xhs_bridge as bridge
import xhs_current_page_recovery as current
import xhs_native as native
import xhs_note_recovery as nr
import xhs_project as xp

KIND='held-note-comment-structure-diagnostic_v1'
require=nr.require


def note(url):
    bridge.safe_url(url)
    match=re.fullmatch('/explore/([a-f0-9]{24})',bridge.target_path(urlparse(url).path))
    require(match is not None,'DIAGNOSTIC_EXACT_NOTE_REQUIRED')
    return match[1]


def source_context(p,b,task_id,source_sha256):
    require(re.fullmatch('[a-f0-9]{64}',str(source_sha256 or '')),'DIAGNOSTIC_SOURCE_HASH_REQUIRED')
    row=p.db.execute('SELECT kind,status,payload FROM tasks WHERE id=?',(task_id,)).fetchone()
    require(row is not None,'DIAGNOSTIC_ORIGINAL_TASK_REQUIRED')
    payload=json.loads(row['payload']);fault=payload.get('bridge',{}).get('fault',{})
    require(row['kind']=='native' and row['status']=='pending' and payload.get('stage')=='roots'
            and fault.get('code')=='COMMENT_PAGINATION_STALLED' and fault.get('hold') is True,
            'DIAGNOSTIC_ORIGINAL_HELD_ROOT_REQUIRED')
    target=payload['target'];slot=b.get('tabs',{}).get('detail',{})
    require(b.get('state')=='ready' and slot.get('owned') is True and slot.get('page')
            and isinstance(slot.get('session'),str) and slot['session'],'DIAGNOSTIC_READY_ORIGINAL_DETAIL_REQUIRED')
    require(re.fullmatch('[a-f0-9]{64}',str(fault.get('source_sha256') or '')),'DIAGNOSTIC_FAULT_HASH_REQUIRED')
    original=Path(fault.get('source_snapshot',''))
    require(original==p.path/'bridge/raw'/(fault.get('source_sha256','')+'.json'),'DIAGNOSTIC_FAULT_SOURCE_REQUIRED')
    old,_=nr.read_json(original,fault.get('source_sha256'))
    require(note(old.get('url'))==target,'DIAGNOSTIC_FAULT_NOTE_CHANGED')
    file=p.path/'bridge/raw'/(source_sha256+'.json');source,_=nr.read_json(file,source_sha256)
    require(source.get('stage')=='body' and source.get('uid')==b['account_uid'] and note(source.get('url'))==target,
            'DIAGNOSTIC_OBSERVED_BODY_ACCOUNT_OR_NOTE_REQUIRED')
    found=False
    for r in p.db.execute("SELECT payload FROM tasks WHERE kind='native' AND json_extract(payload,'$.stage')='body' AND json_extract(payload,'$.target')=?",(target,)):
        found=found or source_sha256 in json.loads(r[0]).get('bridge',{}).get('hashes',[])
    require(found,'DIAGNOSTIC_REGISTERED_BODY_SOURCE_REQUIRED')
    return {'task_id':task_id,'target':target,'task_row':dict(row),'fault':fault,
            'source':{'file':str(file),'sha256':source_sha256},'url':source['url']}


def stop_fingerprint(keys):
    result={}
    for key in keys:
        file=bridge.identity.guard_directory(key)/'STOP.json'
        if file.exists():result[key]=hashlib.sha256(file.read_bytes()).hexdigest()
    return result


def validate_structure(b,context,value):
    require(value.get('uid')==b['account_uid'] and note(value.get('url'))==context['target']
            and not value.get('error'),'DIAGNOSTIC_GUARD_OR_TARGET_CHANGED')
    require(value.get('diagnosticOnly') is True and value.get('collectionPermitted') is False
            and value.get('repairVerified') is False,'DIAGNOSTIC_CLOSED_RESULT_REQUIRED')
    require(set(value)<=set(['url','uid','diagnosticOnly','pageVisible','commentContainer','rootNodeCount','rootIds',
        'loadingVisible','countHeading','interactionCount','messages','unrecognizedUiText','endSignalVisible',
        'noteScroller','documentTimeOrigin','collectionPermitted','repairVerified']), 'DIAGNOSTIC_UNEXPECTED_CONTENT_FIELD')
    require(type(value.get('rootNodeCount')) is int and value['rootNodeCount']>=0
            and isinstance(value.get('rootIds'),list) and len(value['rootIds'])<=3
            and all(re.fullmatch('[a-fA-F0-9]{16,40}',str(cid)) for cid in value['rootIds']),
            'DIAGNOSTIC_ROOT_ID_BOUNDS_REQUIRED')
    def geometry(box):
        require(box is None or isinstance(box,dict) and set(box)<=set(['clientHeight','scrollHeight','scrollTop','width','height'])
            and all(type(v) in (int,float) and math.isfinite(v) for v in box.values()), 'DIAGNOSTIC_GEOMETRY_BOUNDS_REQUIRED')
    container=value.get('commentContainer')
    require(isinstance(container,dict) and set(container)=={'exists','visible','geometry'}
        and type(container['exists']) is bool and type(container['visible']) is bool,'DIAGNOSTIC_CONTAINER_SCHEMA_REQUIRED')
    geometry(container['geometry']);geometry(value.get('noteScroller'))
    for field in ['pageVisible','loadingVisible','unrecognizedUiText','endSignalVisible']:
        if field in value:require(type(value[field]) is bool,'DIAGNOSTIC_BOOLEAN_FIELD_REQUIRED')
    for field in ['countHeading','interactionCount']:
        header=value.get(field)
        require(isinstance(header,dict) and set(header)=={'exists','visible','text','textWithheld'}
            and all(type(header[key]) is bool for key in ['exists','visible','textWithheld']), 'DIAGNOSTIC_COUNT_SCHEMA_REQUIRED')
        text=header['text']
        numeral=r'(?:[0-9]{1,7}|[0-9]{1,3}(?:,[0-9]{3}){1,2}|[0-9]{1,3}(?:\.[0-9]{1,2})?[万千wWkK])\+?'
        require(text is None or isinstance(text,str) and len(text)<=40 and len(re.findall('[0-9]',text))<=7
            and re.fullmatch(r'(?:评论|共\s*'+numeral+r'\s*条?评论|'+numeral+r'(?:\s*条评论)?)',text),
            'DIAGNOSTIC_SAFE_COUNT_TEXT_REQUIRED')
    labels=value.get('messages')
    require(isinstance(labels,list) and len(labels)<=6,'DIAGNOSTIC_UI_MESSAGE_BOUNDS_REQUIRED')
    patterns={'empty':r'(?:暂无评论|暂时没有评论|还没有评论|没有评论)(?:[哦呀呢吧~～！!。,.，\s]*|[，,！!。]?快来(?:抢沙发|评论)(?:吧)?[~～！!。]*)',
        'closed':r'(?:(?:作者|博主)已?关闭(?:了)?评论|评论(?:区)?已关闭|已关闭评论)[。！!\s]*',
        'app_required':r'(?:请)?(?:打开|前往)(?:小红书)?\s*APP(?:内)?(?:查看|查看更多)(?:全部)?评论[。！!\s]*'}
    selectors={'.total','.empty','.empty-text','.empty-state','.empty-container','.no-comment','.no-comments',
        '.comment-empty','.empty-comment','.comment-disabled','.comment-error','.open-app'}
    for label in labels:
        require(isinstance(label,dict) and set(label)=={'selector','type','text'} and label['selector'] in selectors
            and label['type'] in patterns and isinstance(label['text'],str) and len(label['text'])<=80
            and re.fullmatch(patterns[label['type']],label['text'],re.I), 'DIAGNOSTIC_SAFE_UI_MESSAGE_REQUIRED')
    require(value.get('documentTimeOrigin') is None or type(value['documentTimeOrigin']) in (int,float)
        and math.isfinite(value['documentTimeOrigin']),'DIAGNOSTIC_DOCUMENT_TIME_REQUIRED')


def run(project,name,task_id,source_sha256,*,navigate_original_target=False,screenshot=False,driver=None):
    from xhs_batch import batch_lock
    require(type(navigate_original_target) is bool and type(screenshot) is bool,'DIAGNOSTIC_EXPLICIT_FLAGS_REQUIRED')
    p=native.open_lookup_project(Path(project));owned=driver is None
    try:
        with batch_lock(p.path/'bridge/runner'),xp.project_lock(p.path):
            b=bridge.load_binding(project,name)
            context=source_context(p,b,task_id,source_sha256)
            token=xp.digest({'kind':KIND,'project':str(p.path),'task_id':task_id,'target':context['target'],'fault':context['fault']})
            directory=p.path/'bridge/comment-structure-diagnostics'/token
            intent_file=directory/'intent.json'
            require(not intent_file.exists(),'DIAGNOSTIC_ALREADY_ATTEMPTED')
            with bridge.lease(p.path,b) as keys:
                stops=stop_fingerprint(keys)
                journal={'kind':KIND,'diagnostic_id':token,'at':xp.legacy.now(),'binding_name':name,
                    'authority':current.projection(b),'context':context,'stops':stops,
                    'navigate_original_target':navigate_original_target,'screenshot_requested':screenshot,
                    'no_deep_no_scroll_no_expand':True,'collection_permitted':False,'repair_verified':False}
                nr.immutable(intent_file,journal)
                stage='driver_initialization';observations={}
                try:
                    driver=driver or bridge.Driver(b['runtime'])
                    # Existing target/UID verification never grants a new tab or broader surface.
                    stage='original_metadata_identity';meta=bridge.verify_tab(driver,b,'detail')
                    nr.immutable(directory/'initial-target.json',meta)
                    if bridge.target_path(urlparse(meta['url']).path)!='/explore/'+context['target']:
                        require(navigate_original_target,'DIAGNOSTIC_ORIGINAL_NOTE_NOT_CURRENT')
                        stage='navigate_original_observed_target'
                        b['tabs']['detail']['navigation_intent']={'url':context['url'],'at':xp.legacy.now()}
                        bridge.save_binding(p.path,name,b)
                        result=driver.call('navigate',b,'detail',url=context['url'])
                        nr.immutable(directory/'navigation.json',result)
                        time.sleep(1)
                        stage='original_target_identity';meta=bridge.verify_tab(driver,b,'detail')
                        nr.immutable(directory/'verified-original-target.json',meta)
                    require(note(meta['url'])==context['target'],'DIAGNOSTIC_EXACT_CURRENT_NOTE_REQUIRED')
                    bridge.save_binding(p.path,name,b)
                    args={'stage':'body','expectedPath':urlparse(context['url']).path}
                    stage='structure';value=driver.call('comment_diagnose',b,'detail',**args)
                    validate_structure(b,context,value)
                    nr.immutable(directory/'structure.json',value)
                    image=None;gaps=[]
                    if screenshot:
                        if value.get('pageVisible') is not True:gaps.append('SCREENSHOT_TARGET_NOT_VISIBLE')
                        else:
                            stage='screenshot'
                            try:shot=driver.call('screenshot',b,'detail',**args)
                            except bridge.BridgeError as exc:
                                if exc.code not in {'SCREENSHOT_TARGET_NOT_VISIBLE','SCREENSHOT_VISIBILITY_UNCONFIRMED'}:raise
                                gaps.append(exc.code)
                            else:
                                require(shot.get('targetVerified') is True and shot.get('page')==b['tabs']['detail']['page']
                                    and shot.get('uid')==b['account_uid'] and note(shot.get('url'))==context['target'],
                                    'DIAGNOSTIC_SCREENSHOT_TARGET_CHANGED')
                                raw=base64.b64decode(shot.get('pngBase64',''),validate=True)
                                require(raw.startswith(b'\x89PNG\r\n\x1a\n'),'DIAGNOSTIC_SCREENSHOT_PNG_REQUIRED')
                                file=directory/'diagnostic.png'
                                with file.open('xb') as output:output.write(raw)
                                image={'file':str(file),'sha256':hashlib.sha256(raw).hexdigest(),'diagnostic_only':True,
                                    'body_read':False,'comment_read':False,'media_read':False}
                                nr.immutable(directory/'screenshot.json',image)
                    require(stop_fingerprint(keys)==stops,'DIAGNOSTIC_STOP_CHANGED')
                    nr.read_json(context['source']['file'],context['source']['sha256'])
                    # End the read snapshot so the postcondition observes any intervening external writer.
                    p.db.rollback()
                    after=p.db.execute('SELECT kind,status,payload FROM tasks WHERE id=?',(task_id,)).fetchone()
                    require(dict(after)==context['task_row'],'DIAGNOSTIC_HELD_TASK_CHANGED')
                    current_binding=bridge.load_binding(p.path,name)
                    require(current.projection(current_binding)==journal['authority'],'DIAGNOSTIC_BINDING_AUTHORITY_CHANGED')
                    result={'kind':KIND,'diagnostic_id':token,'at':xp.legacy.now(),'structure_file':str(directory/'structure.json'),
                        'screenshot':image,'gaps':gaps,'task_bytes_unchanged':True,'stop_bytes_unchanged':True,
                        'collection_permitted':False,'repair_verified':False,'research_complete':False}
                    nr.immutable(directory/'result.json',result)
                    return result
                except BaseException as exc:
                    nr.immutable(directory/'failure.json',{'at':xp.legacy.now(),'stage':stage,'attempt_consumed':True,
                        'no_automatic_retry':True,'code':getattr(exc,'code',type(exc).__name__),
                        'collection_permitted':False,'repair_verified':False})
                    if isinstance(exc,bridge.BridgeError) and exc.code in {'ACCESS_STOP','ACCOUNT_MISMATCH'}:
                        bridge.save_failure(p.path,name,b,exc,keys)
                    raise
    finally:
        p.close()
        if owned and driver:driver.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--project',required=True,type=Path)
    parser.add_argument('--binding',required=True);parser.add_argument('--task',required=True)
    parser.add_argument('--source',required=True);parser.add_argument('--navigate-original-target',action='store_true')
    parser.add_argument('--screenshot',action='store_true');args=parser.parse_args()
    result=run(args.project,args.binding,args.task,args.source,navigate_original_target=args.navigate_original_target,screenshot=args.screenshot)
    print(xp.dumps({'ok':True,'data':result}))

if __name__=='__main__':
    try:main()
    except xp.legacy.ResearchError as exc:
        print(xp.dumps({'ok':False,'code':getattr(exc,'code','DIAGNOSTIC_FAILED'),'reason':str(exc)}));raise SystemExit(1)
