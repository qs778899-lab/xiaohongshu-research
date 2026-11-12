"""Synthetic/offline current-comment display transaction behavior tests."""
import base64
import copy
from contextlib import ExitStack, nullcontext
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

import xhs_bridge as bridge
import xhs_native as native
import xhs_project as xp
import xhs_bridge_comment_media as cm

UID='e'*24;NOTE='a'*24;CID='b'*24;PARENT='c'*24;AUTHOR='d'*24;PAGE='f'*32
URL='https://www.xiaohongshu.com/explore/'+NOTE+'?xsec_token=synthetic-note'
IMAGE='https://fixture.xhscdn.com/202601011200/'+'1'*32+'/comment/syntheticasset0000000000000001!nc_n_webp_mw_1'

def png_bytes():
    def chunk(kind,value):return struct.pack('>I',len(value))+kind+value+struct.pack('>I',zlib.crc32(kind+value)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1280,720,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\xff'*1280*3)*720))+chunk(b'IEND',b'')
PNG=png_bytes()


class DisplayDriver:
    def __init__(self,project,change=None):
        self.project=project;self.change=change;self.actions=[];self.enlarged=False;self.shot=False
        self.current_image=IMAGE;self.closed=False
    def close(self):self.closed=True
    def display(self):
        inline={p:{'value':'','priority':''} for p in cm.PROPERTIES}
        inline['height']={'value':'120px','priority':'important'}
        if self.enlarged:
            inline={p:{'value':('307px' if p in ('width','max-width') else '230.25px' if p in ('height','max-height') else 'contain' if p=='object-fit' else 'visible'),'priority':'important'} for p in cm.PROPERTIES}
        if self.change=='css_after_shot' and self.shot:inline['width']['value']='300px'
        w,h=(307,230.25) if self.enlarged else (160,120)
        rect={'left':20,'top':20,'right':20+w,'bottom':20+h,'x':20,'y':20,'width':w,'height':h}
        if self.change=='geometry_after_shot' and self.shot:rect.update(top=25,bottom=25+h)
        if self.change=='clipped' and self.enlarged:rect['bottom']=900
        nodes=[{'role':role,'tagName':'IMG' if i==0 else 'DIV','className':('inner','img-box','comment-picture')[i],
                'inline':copy.deepcopy(inline),'rect':copy.deepcopy(rect),'cssVisible':True,'hasRect':True,
                'intersectsViewport':True,'viewportWidth':1280,'viewportHeight':720} for i,role in enumerate(cm.ROLES)]
        return {'format':'comment-media-display-v1','binding':{'path':'/explore/'+NOTE,'uid':UID,'commentId':CID,'imageUrl':IMAGE},
                'naturalWidth':640,'naturalHeight':480,'loaded':True,'guardVisible':True,'allowedProperties':list(cm.PROPERTIES),'nodes':nodes}
    def base(self):
        value={'uid':UID,'url':URL,'commentId':CID,'observedImageUrl':IMAGE,'guardVisible':True,
               'matchState':'matched','gap':None,'matchingImages':1,'isReply':True,'parentCommentId':PARENT,
               'media':{'src':self.current_image,'complete':True,'loaded':True,'naturalWidth':640,'naturalHeight':480,
                        'intersectsViewport':True,'cssVisible':True,'hasRect':True}}
        if self.change=='parent':value['parentCommentId']='9'*24
        if self.change=='unloaded':value['gap']='COMMENT_MEDIA_UNLOADED';value['media']['loaded']=False
        if self.change=='missing':value.update(gap='COMMENT_NOT_RENDERED',matchState=None)
        if self.change=='state_uid':value['uid']='9'*24
        return value
    def intent_present(self,action):
        logs=list((self.project/'bridge/comment-media').rglob('events.jsonl'))
        assert len(logs)==1
        last=json.loads(logs[0].read_text().splitlines()[-1])
        assert last['event']=='mutation_intent' and last['action']==action, 'Mutation sent without durable preceding intent'
    def call(self,action,*args,**fields):
        self.actions.append(action)
        assert fields.get('expectedPath')=='/explore/'+NOTE
        if action=='screenshot':
            self.shot=True
            if self.change=='screenshot_access':raise bridge.BridgeError('ACCESS_STOP')
            if self.change=='screenshot_unknown':raise bridge.BridgeError('COMMAND_OUTCOME_UNKNOWN')
            result={'url':URL,'uid':UID,'page':PAGE,'targetVerified':True,'targetIdentitySpace':'cdp_target_id',
                    'pngBase64':base64.b64encode(PNG).decode()}
            if self.change=='wrong_screenshot_page':result['page']='1'*32
            if self.change=='wrong_screenshot_uid':result['uid']='1'*24
            if self.change=='bad_png':result['pngBase64']=base64.b64encode(b'not a PNG').decode()
            if self.change=='source_after_shot':self.current_image=IMAGE+'-changed'
            return result
        assert fields.get('commentId')==CID and fields.get('imageUrl')==IMAGE
        if action in {'comment_media_focus','comment_media_enlarge','comment_media_restore'}:
            self.intent_present(action)
            if self.change==action+'_unknown':
                if action=='comment_media_enlarge':self.enlarged=True
                raise bridge.BridgeError('COMMAND_OUTCOME_UNKNOWN')
            if self.change==action+'_interrupt':raise KeyboardInterrupt()
        if action=='comment_media_state':return self.base()
        if action=='comment_media_focus':
            if self.change=='viewer':return dict(self.base(),gap='COMMENT_VIEWER_ALREADY_OBSERVED',interactionSubmitted=False)
            return dict(self.base(),focusSubmitted=True,interactionSubmitted=True,openSubmitted=False)
        plan={'clippingBounds':{'left':8,'top':8,'right':1200,'bottom':680},'width':307,'height':230.25,'gap':None}
        if action=='comment_media_display_state':
            value=dict(self.base(),displayState=self.display(),displayPlan=plan,displayMutationSubmitted=False)
            if self.change=='long':value['gap']='COMMENT_DISPLAY_LONG_IMAGE_UNSUPPORTED'
            return value
        if action=='comment_media_enlarge':
            assert fields['expectedDisplayState']==self.display()
            original=self.display()
            if self.change=='rollback':return dict(self.base(),gap='COMMENT_DISPLAY_FULL_IMAGE_UNCONFIRMED',
                 displayMutationSubmitted=True,rolledBack=True,displayState=original,displayPlan=plan)
            if self.change=='uid_gap':return dict(self.base(),uid='9'*24,gap='COMMENT_DISPLAY_VIEWPORT_TOO_SMALL',displayMutationSubmitted=False)
            self.enlarged=True
            value=dict(self.base(),displayMutationSubmitted=True,enlarged=True,restored=False,
                       fullImageGeometryConfirmed=True,originalDisplayState=original,displayState=self.display(),displayPlan=plan)
            if self.change=='source_after_enlarge':self.current_image=IMAGE+'-changed'
            return value
        if action=='comment_media_restore':
            assert fields['expectedDisplayState']==self.display(), 'Restore did not CAS the last known displayed state'
            if self.change=='restore_gap':return dict(self.base(),gap='COMMENT_DISPLAY_STATE_CHANGED',displayMutationSubmitted=False)
            self.enlarged=False
            value=dict(self.base(),displayMutationSubmitted=True,restored=True,enlarged=False,
                       displayState=self.display(),displayPlan=plan)
            if self.change=='restore_wrong_style':value['displayState']['nodes'][0]['inline']['height']['value']='wrong'
            return value
        raise AssertionError('Forbidden or unexpected browser action '+action)


class CommentMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.p=xp.Project(self.root/'project',create=True)
        self.binding={'format':bridge.VERSION,'project':str(self.p.path),'binding_name':'test','state':'ready',
                      'profile_guard_key':'synthetic-profile','account_uid':UID,'context_id':'context','session':'session',
                      'extension_version':'synthetic','runtime':{},'tabs':{'detail':{'owned':True,'page':PAGE,'session':'detail-session','last_url':URL}}}
        self.raw={'url':URL,'uid':AUTHOR,'loginWall':False,'securityBlock':False,'notFound':False,'results':[
            {'commentId':PARENT,'identityKind':'observed_dom_id','is_reply':False,'parentCommentId':'','images':[],
             'author':'parent synthetic','authorHrefRaw':'/user/profile/'+'f'*24,'text':'parent context'},
            {'commentId':CID,'identityKind':'observed_dom_id','is_reply':True,'parentCommentId':PARENT,
             'replyToCommentId':PARENT,'reply_to':'parent synthetic','author':'synthetic commenter','authorHrefRaw':'/user/profile/'+AUTHOR,
             'text':'synthetic patient attachment; equipment unknown','time':'01-01','images':[IMAGE]}]}
        self.register_source(self.raw)
    def tearDown(self):self.p.close();self.tmp.cleanup()
    def register_source(self,raw):
        payload=cm.packed(raw);self.source=hashlib.sha256(payload).hexdigest();directory=self.p.path/'bridge/raw';directory.mkdir(parents=True,exist_ok=True)
        self.source_file=directory/(self.source+'.json');self.source_file.write_bytes(payload)
        manifest=self.root/'manifest.json';manifest.write_text(json.dumps({'units':[{'stage':'images','target':NOTE,'sources':[str(self.source_file)],'next':'read one observed attachment'}]}))
        self.task=native.import_manifest(self.p,manifest)['task_ids'][0]
    def run_capture(self,driver=None,source=None,comment_id=CID,image_index=1,verify_error=None,stop_error=None,authority_change=False):
        self.driver=driver or DisplayDriver(self.p.path)
        loads=0
        def load(*args):
            nonlocal loads
            loads+=1;value=copy.deepcopy(self.binding)
            if authority_change and self.driver.shot:value['tabs']['detail']['session']='new-session'
            return value
        def verify(*args):
            self.driver.actions.append('verify_tab')
            if verify_error and self.driver.shot:raise bridge.BridgeError(verify_error)
            return {'page':PAGE,'url':URL,'visible':True}
        def stops(*args,**kwargs):
            if stop_error and self.driver.shot:raise stop_error
        with ExitStack() as stack:
            stack.enter_context(patch.object(bridge,'load_binding',side_effect=load))
            stack.enter_context(patch.object(bridge,'lease',return_value=nullcontext(['test-key'])))
            self.project_guard=stack.enter_context(patch.object(bridge,'assert_web_project_access'))
            self.auth_guard=stack.enter_context(patch.object(bridge,'web_authorization_exceptions',return_value={'preserved':'unchanged'}))
            self.stop_guard=stack.enter_context(patch.object(bridge.identity,'check_stops',side_effect=stops))
            stack.enter_context(patch.object(bridge,'verify_tab',side_effect=verify))
            self.failure=stack.enter_context(patch.object(bridge,'save_failure'))
            self.prepare=stack.enter_context(patch.object(bridge,'prepare_target',side_effect=AssertionError('No navigation')))
            return cm.capture(self.p.path,'test',self.task,source or self.source,comment_id,image_index,self.driver)
    def result(self):
        paths=list((self.p.path/'bridge/comment-media').rglob('result.json'));self.assertEqual(len(paths),1)
        return json.loads(paths[0].read_bytes())
    def events(self):
        paths=list((self.p.path/'bridge/comment-media').rglob('events.jsonl'));self.assertEqual(len(paths),1)
        return [json.loads(line) for line in paths[0].read_text().splitlines()]
    def mutations(self,driver):return [x for x in driver.actions if x in {'comment_media_focus','comment_media_enlarge','comment_media_restore'}]
    def test_single_attachment_known_path_captures_restores_and_never_marks_read_or_whole_scope(self):
        before=[tuple(r) for r in self.p.db.execute('SELECT * FROM tasks')];driver=DisplayDriver(self.p.path)
        result=self.run_capture(driver)
        self.assertEqual(result['outcome'],'captured');self.assertTrue(result['display_restored']);self.assertFalse(driver.enlarged)
        self.assertEqual(self.mutations(driver),['comment_media_focus','comment_media_enlarge','comment_media_restore'])
        self.assertEqual(driver.actions.count('screenshot'),1);self.assertEqual(Path(result['file']).read_bytes(),PNG)
        self.assertEqual(hashlib.sha256(PNG).hexdigest(),result['sha256']);self.assertFalse(result['reviewed']);self.assertFalse(result['complete'])
        self.assertFalse(result['note_media_scope_complete']);self.assertFalse(result['comment_scope_complete']);self.assertFalse(result['body_or_video_read'])
        self.assertEqual(result['source_context']['source_account_uid'],'unknown');self.assertEqual(result['capture_account_uid'],UID)
        self.assertEqual(result['source_context']['comment_context']['author_uid'],AUTHOR)
        self.assertEqual(result['source_context']['parent_context']['text'],'parent context')
        self.assertEqual(result['source_context']['source_position'],'/results/1/images/0')
        sidecar=json.loads(Path(result['sidecar_file']).read_bytes());self.assertTrue(sidecar['visual_occlusion_check_required'])
        self.assertTrue(sidecar['geometry_is_not_pixel_read']);self.assertTrue(sidecar['original_resolution_not_claimed'])
        self.assertEqual(sidecar['natural_width'],640);self.assertEqual(sidecar['screenshot_width'],1280)
        self.assertEqual(hashlib.sha256(Path(result['sidecar_file']).read_bytes()).hexdigest(),result['sidecar_sha256'])
        intents=[e for e in self.events() if e['event']=='mutation_intent'];self.assertEqual(len(intents),3)
        self.assertTrue(all(e['no_automatic_retry'] for e in intents));self.failure.assert_not_called();self.prepare.assert_not_called()
        self.assertEqual(before,[tuple(r) for r in self.p.db.execute('SELECT * FROM tasks')])
    def test_cache_is_original_time_resource_and_no_new_mutation_or_screenshot(self):
        first=self.run_capture();second_driver=DisplayDriver(self.p.path);cached=self.run_capture(second_driver)
        self.assertTrue(cached['cached']);self.assertEqual(cached['observed_at'],first['observed_at'])
        self.assertFalse(cached['new_screenshot_performed']);self.assertFalse(cached['current_content_read'])
        self.assertEqual(self.mutations(second_driver),[]);self.assertNotIn('screenshot',second_driver.actions)
        self.assertEqual(cached['source_context']['source_sha256'],first['source_context']['source_sha256'])
    def test_new_source_same_image_can_only_return_old_capture_with_new_source_separately(self):
        first=self.run_capture();raw=copy.deepcopy(self.raw);raw['results'][1]['text']+=' changed context';self.register_source(raw)
        result=self.run_capture();self.assertTrue(result['cached']);self.assertEqual(result['observed_at'],first['observed_at'])
        self.assertNotEqual(result['source_context']['source_sha256'],result['requested_source_context']['source_sha256'])
    def test_cache_binary_and_sidecar_tampering_are_rejected_without_browser_mutations(self):
        first=self.run_capture();Path(first['file']).write_bytes(b'changed PNG');driver=DisplayDriver(self.p.path)
        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_CACHED_FILE_CHANGED'):self.run_capture(driver)
        self.assertEqual(self.mutations(driver),[]);self.assertNotIn('screenshot',driver.actions)
    def test_cache_source_resource_change_cannot_start_another_attempt(self):
        self.run_capture();raw=copy.deepcopy(self.raw);raw['results'][1]['images']=[IMAGE+'-other'];self.register_source(raw)
        driver=DisplayDriver(self.p.path)
        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_CACHED_RESOURCE_UNCONFIRMED'):self.run_capture(driver)
        self.assertEqual(self.mutations(driver),[])
    def test_explicit_collector_mismatch_is_rejected_before_browser_action(self):
        raw=copy.deepcopy(self.raw);raw['collector_account_uid']='1'*24;self.register_source(raw);driver=DisplayDriver(self.p.path)
        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_SOURCE_ACCOUNT_MISMATCH'):self.run_capture(driver)
        self.assertEqual(driver.actions,[])
    def test_explicit_matching_collector_is_kept_separate_from_comment_author(self):
        raw=copy.deepcopy(self.raw);raw['collector_account_uid']=UID;self.register_source(raw)
        result=self.run_capture();self.assertEqual(result['source_context']['source_account_uid'],UID)
        self.assertEqual(result['source_context']['comment_context']['author_uid'],AUTHOR)
    def test_source_sha_content_change_is_not_accepted(self):
        self.source_file.write_bytes(self.source_file.read_bytes()+b' ');driver=DisplayDriver(self.p.path)
        with self.assertRaisesRegex(bridge.BridgeError,'IMMUTABLE_SOURCE_CHANGED'):self.run_capture(driver)
        self.assertEqual(driver.actions,[])
    def test_missing_id_or_invalid_image_index_is_not_guessed(self):
        for cid,index in [('not-real',1),(CID,0),(CID,2),('1'*24,1)]:
            with self.subTest(cid=cid,index=index):
                driver=DisplayDriver(self.p.path)
                with self.assertRaises(bridge.BridgeError):self.run_capture(driver,comment_id=cid,image_index=index)
                self.assertEqual(driver.actions,[])
    def test_known_unavailable_viewer_long_or_unloaded_states_are_explicit_local_gaps(self):
        for change,code in [('missing','COMMENT_NOT_RENDERED'),('unloaded','COMMENT_MEDIA_UNLOADED'),
                            ('viewer','COMMENT_VIEWER_ALREADY_OBSERVED'),('long','COMMENT_DISPLAY_LONG_IMAGE_UNSUPPORTED')]:
            with self.subTest(change=change):
                # A fresh temporary project is needed because attempts intentionally cannot replay.
                case=CommentMediaTests();case.setUp()
                try:
                    driver=DisplayDriver(case.p.path,change);result=case.run_capture(driver)
                    self.assertEqual(result['outcome'],'gap');self.assertIn(code,result['gaps'])
                    self.assertNotIn('comment_media_enlarge',driver.actions);self.assertNotIn('comment_media_restore',driver.actions)
                    self.assertNotIn('screenshot',driver.actions);case.failure.assert_not_called()
                finally:case.tearDown()
    def test_unknown_mutations_never_retry_restore_or_replay_through_reencoded_source(self):
        for action in ['focus','enlarge','restore']:
            with self.subTest(action=action):
                case=CommentMediaTests();case.setUp()
                try:
                    driver=DisplayDriver(case.p.path,'comment_media_'+action+'_unknown')
                    with self.assertRaisesRegex(bridge.BridgeError,'COMMAND_OUTCOME_UNKNOWN'):case.run_capture(driver)
                    self.assertEqual(driver.actions.count('comment_media_'+action),1);case.failure.assert_called_once()
                    if action!='restore':self.assertNotIn('comment_media_restore',driver.actions)
                    raw=copy.deepcopy(case.raw);raw['results'][1]['time']='later';case.register_source(raw)
                    again=DisplayDriver(case.p.path)
                    with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_MEDIA_ALREADY_ATTEMPTED'):case.run_capture(again)
                    self.assertEqual(case.mutations(again),[]);self.assertNotIn('screenshot',again.actions)
                finally:case.tearDown()
    def test_new_stop_after_screenshot_preserves_png_and_does_not_restore(self):
        driver=DisplayDriver(self.p.path);stop=self.root/'STOP.json';stop.write_bytes(b'{"reason":"new stop must remain"}');old=stop.read_bytes()
        with self.assertRaises(bridge.legacy.StopRun):self.run_capture(driver,stop_error=bridge.legacy.StopRun('New STOP'))
        self.assertEqual(stop.read_bytes(),old);self.assertNotIn('comment_media_restore',driver.actions)
        partial=self.result();self.assertEqual(Path(partial['file']).read_bytes(),PNG);self.assertFalse(partial['display_restored'])
        self.assertIn('display_state_unrestored_or_uncertain',partial['gaps']);self.failure.assert_called_once()
    def test_account_scope_or_target_error_after_capture_prevents_restore(self):
        for code in ['ACCESS_STOP','ACCOUNT_MISMATCH','TARGET_MISMATCH','COMMAND_OUTCOME_UNKNOWN']:
            with self.subTest(code=code):
                case=CommentMediaTests();case.setUp()
                try:
                    driver=DisplayDriver(case.p.path)
                    with self.assertRaisesRegex(bridge.BridgeError,code):case.run_capture(driver,verify_error=code)
                    self.assertNotIn('comment_media_restore',driver.actions);self.assertTrue(Path(case.result()['file']).is_file())
                    self.assertEqual(case.failure.call_args.args[3].code,code)
                finally:case.tearDown()
    def test_session_authority_change_prevents_restore(self):
        driver=DisplayDriver(self.p.path)
        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_BINDING_AUTHORITY_CHANGED'):self.run_capture(driver,authority_change=True)
        self.assertNotIn('comment_media_restore',driver.actions);self.assertFalse(self.result()['display_restored'])
    def test_source_parent_cas_geometry_and_screenshot_proof_fail_closed(self):
        for change in ['parent','state_uid','source_after_enlarge','source_after_shot','css_after_shot','geometry_after_shot',
                       'clipped','wrong_screenshot_page','wrong_screenshot_uid','bad_png','uid_gap']:
            with self.subTest(change=change):
                case=CommentMediaTests();case.setUp()
                try:
                    driver=DisplayDriver(case.p.path,change)
                    with self.assertRaises(bridge.BridgeError):case.run_capture(driver)
                    self.assertNotIn('comment_media_restore',driver.actions);case.failure.assert_called_once()
                finally:case.tearDown()
    def test_access_stop_from_screenshot_is_not_a_local_gap(self):
        driver=DisplayDriver(self.p.path,'screenshot_access')
        with self.assertRaisesRegex(bridge.BridgeError,'ACCESS_STOP'):self.run_capture(driver)
        self.assertNotIn('comment_media_restore',driver.actions);self.assertEqual(self.failure.call_args.args[3].code,'ACCESS_STOP')
    def test_primitive_known_rollback_is_a_gap_without_second_style_mutation(self):
        driver=DisplayDriver(self.p.path,'rollback');result=self.run_capture(driver)
        self.assertEqual(result['outcome'],'gap');self.assertIn('COMMENT_DISPLAY_FULL_IMAGE_UNCONFIRMED',result['gaps'])
        self.assertNotIn('comment_media_restore',driver.actions);self.assertNotIn('screenshot',driver.actions)
    def test_restore_failure_never_claims_restored(self):
        for change in ['restore_gap','restore_wrong_style']:
            with self.subTest(change=change):
                case=CommentMediaTests();case.setUp()
                try:
                    driver=DisplayDriver(case.p.path,change)
                    if change=='restore_gap':
                        result=case.run_capture(driver);self.assertEqual(result['outcome'],'gap')
                    else:
                        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_RESTORE_OUTCOME_UNCONFIRMED'):case.run_capture(driver)
                        result=case.result()
                    self.assertFalse(result['display_restored']);self.assertEqual(driver.actions.count('comment_media_restore'),1)
                finally:case.tearDown()
    def test_interrupt_intent_is_persisted_and_is_not_replayed(self):
        driver=DisplayDriver(self.p.path,'comment_media_enlarge_interrupt')
        with self.assertRaises(KeyboardInterrupt):self.run_capture(driver)
        self.assertEqual(self.events()[-1]['event'],'stopped');self.assertEqual(self.result()['outcome'],'stopped')
        again=DisplayDriver(self.p.path)
        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_MEDIA_ALREADY_ATTEMPTED'):self.run_capture(again)
        self.assertEqual(self.mutations(again),[])
    def test_read_only_database_source_registration_is_required(self):
        row=self.p.db.execute('SELECT payload FROM tasks WHERE id=?',(self.task,)).fetchone();p=json.loads(row[0]);p['provenance']=[]
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(p),self.task));self.p.db.commit();driver=DisplayDriver(self.p.path)
        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_SOURCE_NOT_REGISTERED_FOR_NOTE'):self.run_capture(driver)
        self.assertEqual(driver.actions,[])
    def test_source_duplicates_fallback_identity_unknown_parent_and_non_comment_assets_fail_before_ui(self):
        def duplicate(raw):raw['results'].append(copy.deepcopy(raw['results'][1]))
        def fallback(raw):raw['results'][1]['identityKind']='synthetic_fingerprint'
        def no_parent(raw):raw['results'][1]['parentCommentId']=''
        def duplicate_image(raw):raw['results'][1]['images']=[IMAGE,IMAGE]
        def emoji(raw):raw['results'][1]['images']=['https://fixture.xhscdn.com/note-content-emoji/picasso.webp']
        def external(raw):raw['results'][1]['images']=['https://example.org/comment/asset']
        def wrong_note(raw):raw['url']='https://www.xiaohongshu.com/explore/'+'9'*24
        def restricted(raw):raw['securityBlock']=True
        for change in [duplicate,fallback,no_parent,duplicate_image,emoji,external,wrong_note,restricted]:
            with self.subTest(change=change):
                case=CommentMediaTests();case.setUp()
                try:
                    raw=copy.deepcopy(case.raw);change(raw);case.register_source(raw);driver=DisplayDriver(case.p.path)
                    with self.assertRaises(bridge.BridgeError):case.run_capture(driver)
                    self.assertEqual(driver.actions,[])
                finally:case.tearDown()
    def test_missing_parent_text_is_an_explicit_context_gap_not_a_guessed_parent(self):
        raw=copy.deepcopy(self.raw);raw['results']=raw['results'][1:];self.register_source(raw)
        result=self.run_capture();self.assertIsNone(result['source_context']['parent_context'])
        self.assertTrue(result['source_context']['parent_context_gap'])
        self.assertIn('parent_comment_text_not_in_source_snapshot',result['gaps'])
    def test_source_registered_by_actual_same_note_roots_capture_is_accepted_without_images_provenance(self):
        manifest=self.root/'root-manifest.json';manifest.write_text(json.dumps({'units':[{'stage':'roots','target':NOTE,
            'sources':[str(self.source_file)],'next':'read comment snapshot'}]}))
        task=native.import_manifest(self.p,manifest)['task_ids'][0]
        acquired=bridge.store_capture(self.p,self.p.task(task),self.raw)
        self.assertEqual(Path(acquired['snapshot']).stem,self.source)
        p=self.p.task(self.task)['payload'];p['provenance']=[]
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(p),self.task));self.p.db.commit()
        result=self.run_capture()
        self.assertTrue(any(r['task_id']==task and r['kind']=='registered_comment_capture' for r in result['source_context']['source_task_refs']))
    def test_intent_journal_failure_prevents_sending_the_mutation(self):
        original=cm.journal
        def log(path,event,**fields):
            if event=='mutation_intent':raise OSError('intent fsync failed')
            return original(path,event,**fields)
        driver=DisplayDriver(self.p.path)
        with patch.object(cm,'journal',side_effect=log):
            with self.assertRaises(OSError):self.run_capture(driver)
        self.assertEqual(self.mutations(driver),[]);self.failure.assert_called_once()
    def test_logging_failure_cannot_hide_original_access_stop_handler(self):
        original=cm.journal
        def log(path,event,**fields):
            if event=='stopped':raise OSError('stop log unavailable')
            return original(path,event,**fields)
        driver=DisplayDriver(self.p.path,'screenshot_access')
        with patch.object(cm,'journal',side_effect=log):
            with self.assertRaisesRegex(bridge.BridgeError,'ACCESS_STOP'):self.run_capture(driver)
        self.assertEqual(self.failure.call_args.args[3].code,'ACCESS_STOP');self.assertNotIn('comment_media_restore',driver.actions)
    def test_current_note_different_last_url_and_navigation_intent_are_not_automatically_fixed(self):
        for mode in ['url','intent','unowned']:
            with self.subTest(mode=mode):
                case=CommentMediaTests();case.setUp()
                try:
                    if mode=='url':case.binding['tabs']['detail']['last_url']='https://www.xiaohongshu.com/explore/'+'9'*24
                    elif mode=='intent':case.binding['tabs']['detail']['navigation_intent']={'url':URL}
                    else:case.binding['tabs']['detail']['owned']=False
                    driver=DisplayDriver(case.p.path)
                    with self.assertRaises(bridge.BridgeError):case.run_capture(driver)
                    self.assertEqual(driver.actions,[])
                finally:case.tearDown()
    def test_cached_path_access_stop_still_uses_original_stop_handler(self):
        self.run_capture();driver=DisplayDriver(self.p.path);original=driver.call
        def call(action,*args,**fields):
            if action=='comment_media_state':raise bridge.BridgeError('ACCESS_STOP')
            return original(action,*args,**fields)
        driver.call=call
        with self.assertRaisesRegex(bridge.BridgeError,'ACCESS_STOP'):self.run_capture(driver)
        self.assertEqual(self.failure.call_args.args[3].code,'ACCESS_STOP');self.assertEqual(self.mutations(driver),[])
    def test_archive_parent_traversal_cannot_validate_an_outside_copy_of_real_source(self):
        outside=self.root/'outside-source.json';outside.write_bytes(self.source_file.read_bytes())
        p=self.p.task(self.task)['payload'];p['provenance'][0]['archive']='../outside-source.json'
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(p),self.task));self.p.db.commit()
        driver=DisplayDriver(self.p.path)
        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_SOURCE_PATH_UNSAFE'):self.run_capture(driver)
        self.assertEqual(driver.actions,[]);self.assertEqual(outside.read_bytes(),self.source_file.read_bytes())
    def test_cached_png_parent_traversal_cannot_load_matching_outside_png(self):
        result=self.run_capture();outside=self.root/'outside.png';outside.write_bytes(PNG)
        result_file=Path(result['result_file']);stored=json.loads(result_file.read_bytes())
        stored['file']=str(self.p.path)+'/../outside.png';result_file.write_bytes(cm.packed(stored))
        driver=DisplayDriver(self.p.path)
        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_SOURCE_PATH_UNSAFE'):self.run_capture(driver)
        self.assertEqual(self.mutations(driver),[]);self.assertNotIn('screenshot',driver.actions)
        self.assertEqual(outside.read_bytes(),PNG)
    def test_cached_sidecar_parent_traversal_cannot_load_matching_outside_sidecar(self):
        result=self.run_capture();outside=self.root/'outside-sidecar.json';outside.write_bytes(Path(result['sidecar_file']).read_bytes())
        result_file=Path(result['result_file']);stored=json.loads(result_file.read_bytes())
        stored['sidecar_file']=str(self.p.path)+'/../outside-sidecar.json';result_file.write_bytes(cm.packed(stored))
        driver=DisplayDriver(self.p.path)
        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_SOURCE_PATH_UNSAFE'):self.run_capture(driver)
        self.assertEqual(self.mutations(driver),[])
    def test_safe_file_rejects_serialized_dot_double_slash_and_trailing_slash_paths(self):
        relative='bridge/raw/'+self.source+'.json';absolute=str(self.source_file)
        for value in ['bridge/./raw/'+self.source+'.json','bridge/raw/../raw/'+self.source+'.json',
                      'bridge//raw/'+self.source+'.json',relative+'/',str(self.p.path)+'/./'+relative,
                      str(self.p.path)+'//'+relative,'//'+absolute.lstrip('/')]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_SOURCE_PATH_UNSAFE'):cm.safe_file(value,self.p.path)
    def test_safe_file_accepts_canonical_relative_absolute_and_path_objects_within_real_project(self):
        for value in ['bridge/raw/'+self.source+'.json',str(self.source_file),self.source_file]:
            with self.subTest(value=value):
                self.assertEqual(cm.safe_file(value,self.p.path),self.source_file.resolve())
    def test_noncanonical_archive_is_rejected_before_pathlib_can_erase_dot_segment(self):
        p=self.p.task(self.task)['payload'];p['provenance'][0]['archive']=p['provenance'][0]['archive'].replace('/', '/./',1)
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(p),self.task));self.p.db.commit()
        driver=DisplayDriver(self.p.path)
        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_SOURCE_PATH_UNSAFE'):self.run_capture(driver)
        self.assertEqual(driver.actions,[])
    def test_source_and_archive_symlink_components_are_rejected_even_when_target_is_inside_project(self):
        target=self.p.path/'real';target.mkdir();(target/'source.json').write_bytes(self.source_file.read_bytes())
        link=self.p.path/'alias';link.symlink_to(target,target_is_directory=True)
        for value in [str(link/'source.json'),'alias/source.json']:
            with self.subTest(value=value):
                with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_SOURCE_PATH_UNSAFE'):cm.safe_file(value,self.p.path)
        raw_link=self.p.path/'raw-file-alias.json';raw_link.symlink_to(self.source_file)
        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_SOURCE_PATH_UNSAFE'):cm.safe_file(raw_link,self.p.path)
    def test_roots_event_archive_parent_traversal_is_rejected(self):
        manifest=self.root/'root-manifest.json';manifest.write_text(json.dumps({'units':[{'stage':'roots','target':NOTE,
            'sources':[str(self.source_file)],'next':'read'}]}))
        task=native.import_manifest(self.p,manifest)['task_ids'][0];bridge.store_capture(self.p,self.p.task(task),self.raw)
        p=self.p.task(self.task)['payload'];p['provenance']=[]
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(p),self.task))
        outside=self.root/'outside-event-source.json';outside.write_bytes(self.source_file.read_bytes())
        self.p.db.execute('UPDATE native_events SET artifact=? WHERE task_id=? AND sha256=?',('../outside-event-source.json',task,self.source));self.p.db.commit()
        driver=DisplayDriver(self.p.path)
        with self.assertRaisesRegex(bridge.BridgeError,'COMMENT_SOURCE_PATH_UNSAFE'):self.run_capture(driver)
        self.assertEqual(driver.actions,[])


if __name__=='__main__':unittest.main()
