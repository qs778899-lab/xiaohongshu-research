"""Local gap continuation never turns a hidden LIVE position into reviewed media."""
import base64
import copy
import hashlib
import json
from contextlib import ExitStack, nullcontext
from pathlib import Path
import unittest
from unittest.mock import patch

import xhs_bridge as bridge
import xhs_bridge_media as media
import test_bridge_media_readiness as fixtures

NOTE=fixtures.NOTE
URL=fixtures.URL


class MixedDriver:
    def __init__(self,change=None):
        self.current=0;self.count=3;self.shots={};self.actions=[];self.change=change;self.empty_reads=0
    def call(self,action,*args,**fields):
        self.actions.append((action,self.current,dict(fields)))
        if action=='activate':return {'activated':'p1','windowFocusRequested':False}
        if action=='media_select':
            if self.change=='unknown_selection' and fields['index']==1:
                raise bridge.BridgeError('COMMAND_OUTCOME_UNKNOWN')
            self.current=fields['index'];return {'selectionSubmitted':True}
        if action=='screenshot':
            self.shots[self.current]=self.shots.get(self.current,0)+1
            if self.current==1 and self.change=='access':raise bridge.BridgeError('ACCESS_STOP')
            if self.current==1 and self.change=='unknown':raise bridge.BridgeError('COMMAND_OUTCOME_UNKNOWN')
            if self.current==1 and self.change=='interrupt':raise KeyboardInterrupt()
            return {'url':URL,'pngBase64':base64.b64encode(b'formal or discarded synthetic pixels '+str(self.current).encode()).decode()}
        if action!='media_state':raise AssertionError(action)
        image={'src':f'https://sns-webpic-qc.xhscdn.com/observed/asset-{self.current}!webp',
               'visible':True,'loaded':True,'complete':True,'naturalWidth':891,'naturalHeight':1034}
        state={'index':self.current,'count':self.count,'kind':'image','screenshotReady':True,
               'mediaStatus':'rendered_decoded_media','mediaEvidence':{'root':{'visible':True},
               'activeSlide':{'visible':True},'images':[image,copy.deepcopy(image)],'videos':[],
               'backgrounds':[],'potentialKinds':['image']}}
        if self.current==1:
            empty=self.shots.get(1,0)>=1
            if self.change=='empty_selection':empty=True
            if self.change=='formal_empty':empty=self.shots.get(1,0)>=2
            video={'src':'blob:https://www.xiaohongshu.com/paired-live-source',
                   'loaded':not empty,'visible':not empty,'display':'none' if empty else 'block',
                   'readyState':4,'videoWidth':554,'videoHeight':642}
            e=state['mediaEvidence'];e.update(videos=[video],potentialKinds=['image','video'])
            state.update(kind='empty_or_unloaded' if empty else 'video',
                         screenshotReady=not empty,mediaStatus='empty_or_unloaded' if empty else 'rendered_decoded_media')
            if empty:
                self.empty_reads+=1
                if self.change=='image_asset':image['src']+='-changed'
                if self.change=='video_asset':video['src']+='-changed'
                if self.change=='fresh_asset' and self.empty_reads>=2:image['src']+='-fresh'
                if self.change=='transform':image['src']=image['src'].replace('!webp','!other')
                if self.change=='index':state['index']=2
                if self.change=='count':state['count']=4
                if self.change=='missing_image':e['images']=[]
                if self.change=='missing_video':e['videos']=[]
                if self.change=='missing_src':video.pop('src')
                if self.change=='root_hidden':e['root']['visible']=False
                if self.change=='slide_hidden':e['activeSlide']['visible']=False
                if self.change=='image_unloaded':image['loaded']=False;e['images'][1]['loaded']=False
                if self.change=='background':e['backgrounds']=[{'urls':['https://sns-webpic-qc.xhscdn.com/bg']}]
                if self.change=='video_still_visible':video.update(visible=True,loaded=True,display='block')
                if self.empty_reads>=3:
                    if self.change=='between_index':state['index']=2
                    if self.change=='between_count':state['count']=4
                    if self.change=='between_asset':image['src']+='-between'
        return copy.deepcopy(state)


class PartialMediaTests(unittest.TestCase):
    setUp=fixtures.MediaReadinessTests.setUp
    tearDown=fixtures.MediaReadinessTests.tearDown
    def run_capture(self,driver,positions=(1,2,3),enabled=True,load_change=None,verify_error=None,stop_error=None):
        self.binding.update(binding_name='test',project=str(self.p.path),profile_guard_key='profile',
                            session='binding-session',extension_version='known')
        self.binding['tabs']['detail'].update(session='detail-session')
        loaded=0
        def load(*args):
            nonlocal loaded
            loaded+=1;b=copy.deepcopy(self.binding)
            if loaded>1 and load_change:load_change(b)
            return b
        def verify(*args):
            driver.actions.append(('uid_target_guard',driver.current,{}))
            if verify_error and driver.empty_reads:raise bridge.BridgeError(verify_error)
            return {'page':'p1','url':URL,'visible':True}
        with ExitStack() as stack:
            stack.enter_context(patch.object(bridge,'load_binding',side_effect=load))
            stack.enter_context(patch.object(bridge,'lease',return_value=nullcontext(['guard-key'])))
            stack.enter_context(patch.object(bridge,'prepare_target'))
            stack.enter_context(patch.object(bridge,'save_binding'))
            stack.enter_context(patch.object(bridge,'verify_tab',side_effect=verify))
            self.failure=stack.enter_context(patch.object(bridge,'save_failure'))
            self.project_access=stack.enter_context(patch.object(bridge,'assert_web_project_access'))
            self.auth=stack.enter_context(patch.object(bridge,'web_authorization_exceptions',return_value={'preserved':'original-sha'}))
            self.check_stops=stack.enter_context(patch.object(bridge.identity,'check_stops',side_effect=stop_error))
            stack.enter_context(patch.object(media.time,'sleep'))
            return media.capture(self.p.path,'test',self.tid,list(positions),driver,continue_local_empty=enabled)
    def journal(self):
        files=list((self.p.path/'bridge/media'/NOTE/'batches').glob('*.jsonl'))
        self.assertEqual(len(files),1)
        return [json.loads(line) for line in files[0].read_text().splitlines()]
    def selected(self,driver):return [fields['index']+1 for action,_,fields in driver.actions if action=='media_select']
    def test_same_mixed_resources_gap_preserves_early_capture_and_advances_explicit_later_position(self):
        before_tasks=[tuple(r) for r in self.p.db.execute('SELECT * FROM tasks')]
        before_inventory=[tuple(r) for r in self.p.db.execute('SELECT * FROM media_inventory')]
        driver=MixedDriver();result=self.run_capture(driver)
        self.assertEqual([r['position'] for r in result['captures']],[1,3]);self.assertEqual(self.selected(driver),[1,2,3])
        self.assertEqual(driver.shots,{0:2,1:1,2:2});self.assertEqual(result['position_gaps'][0]['position'],2)
        self.assertTrue(result['partial']);self.assertFalse(result['complete']);self.assertFalse(result['reviewed'])
        self.assertTrue(result['video_requires_segments']);self.assertFalse(result['comment_media_scan_complete'])
        for r in result['captures']:
            self.assertTrue(Path(r['file']).is_file());self.assertFalse(r['reviewed']);self.assertTrue(r['visual_verification_required'])
        gap=result['position_gaps'][0];self.assertFalse(gap['reviewed']);self.assertFalse(gap['automatic_retry'])
        self.assertTrue(gap['static_image_unread']);self.assertTrue(gap['paired_live_video_unread'])
        diag=Path(gap['diagnostic_file']);self.assertEqual(hashlib.sha256(diag.read_bytes()).hexdigest(),gap['diagnostic_sha256'])
        self.assertEqual(json.loads(diag.read_text())['code'],'MEDIA_EMPTY_OR_UNLOADED')
        self.assertEqual([e['event'] for e in self.journal()],['requested','position_started','position_captured',
            'position_started','position_gap','position_started','position_captured','handoff'])
        self.failure.assert_not_called();self.project_access.assert_called_once();self.auth.assert_called_once()
        self.check_stops.assert_called_once_with(['guard-key'],web_stop_exceptions={'preserved':'original-sha'},web_note_recovery=None)
        self.assertEqual(before_tasks,[tuple(r) for r in self.p.db.execute('SELECT * FROM tasks')])
        self.assertEqual(before_inventory,[tuple(r) for r in self.p.db.execute('SELECT * FROM media_inventory')])
    def test_default_still_aborts_same_failure_and_keeps_original_success(self):
        driver=MixedDriver()
        with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_EMPTY_OR_UNLOADED'):self.run_capture(driver,enabled=False)
        self.assertEqual(self.selected(driver),[1,2]);self.failure.assert_called_once()
        self.assertEqual(len(list((self.p.path/'bridge/media'/NOTE).glob('*.png'))),1)
        self.assertFalse((self.p.path/'bridge/media'/NOTE/'batches').exists());self.check_stops.assert_not_called()
    def test_all_explicit_positions_may_be_gaps_without_claiming_complete_or_read(self):
        driver=MixedDriver();result=self.run_capture(driver,positions=(2,))
        self.assertEqual(result['captures'],[]);self.assertEqual(len(result['position_gaps']),1)
        self.assertFalse(result['complete']);self.assertFalse(result['reviewed']);self.assertEqual(self.selected(driver),[2])
    def test_nonascending_or_repeated_positions_are_rejected_before_any_browser_action(self):
        for positions in [(2,1),(1,2,2),(True,2)]:
            with self.subTest(positions=positions):
                driver=MixedDriver()
                with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_PARTIAL_ORDER_REQUIRED'):self.run_capture(driver,positions)
                self.assertEqual(driver.actions,[])
    def test_only_explicit_positions_advance_not_unspecified_successor(self):
        driver=MixedDriver();result=self.run_capture(driver,positions=(1,2))
        self.assertEqual(self.selected(driver),[1,2]);self.assertEqual([r['position'] for r in result['captures']],[1])
        self.assertEqual(result['position_gaps'][0]['position'],2)
    @unittest.skipUnless((Path(__file__).with_name('fixtures') / 'x017-hidden-live-warmup.json').is_file(),
                         'Private original research fixture is retained in the project, outside the reusable skill')
    def test_exact_observed_warmup_fixture_has_unchanged_mixed_resources_and_can_only_be_a_gap(self):
        fixture=Path(__file__).with_name('fixtures')/'x017-hidden-live-warmup.json'
        data=json.loads(fixture.read_text());provenance=json.loads(fixture.with_name('provenance.json').read_text())
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(),provenance['sha256'])
        before,after=data['before'],data['after'];self.assertEqual(data['phase'],'warmup')
        self.assertEqual(before['kind'],'video');self.assertEqual(after['kind'],'empty_or_unloaded')
        self.assertEqual(media.mixed_slide_resources(before),media.mixed_slide_resources(after))
        with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_EMPTY_OR_UNLOADED'):media.stable_media_identity(after)
        b=copy.deepcopy(self.binding);b.update(binding_name='test',session='s',profile_guard_key='p')
        b['tabs']['detail'].update(session='detail-session')
        class ObservedDriver:
            def call(self,action,*args,**fields):
                assert action=='media_state'
                assert fields['expectedPath']=='/explore/'+data['note_id']
                return copy.deepcopy(after)
        with patch.object(bridge,'load_binding',return_value=copy.deepcopy(b)), \
             patch.object(bridge,'assert_web_project_access'),patch.object(bridge,'web_authorization_exceptions',return_value={}), \
             patch.object(bridge.identity,'check_stops'), \
             patch.object(bridge,'verify_tab',return_value={'page':'p1','url':'https://www.xiaohongshu.com/explore/'+data['note_id']}):
            proof=media.confirm_local_warmup_gap(self.p.path,'test',b,ObservedDriver(),[],media.partial_scope(b),
                                                data['note_id'],data['position'],before,after)
        self.assertTrue(proof['still_unread']);self.assertTrue(proof['static_image_unread']);self.assertTrue(proof['paired_live_video_unread'])
    def test_position_count_or_resource_change_after_gap_recheck_stops_before_next_selection(self):
        for change in ['between_index','between_count','between_asset']:
            with self.subTest(change=change):
                driver=MixedDriver(change)
                with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_POSITION_CHANGED'):self.run_capture(driver)
                self.assertEqual(self.selected(driver),[1,2]);self.failure.assert_called_once()
    def test_changed_assets_geometry_identity_or_not_observed_failure_shape_stop(self):
        for change in ['image_asset','video_asset','fresh_asset','transform','index','count','missing_image',
                       'missing_video','missing_src','root_hidden','slide_hidden','image_unloaded','background','video_still_visible']:
            with self.subTest(change=change):
                driver=MixedDriver(change)
                with self.assertRaises(bridge.BridgeError):self.run_capture(driver)
                self.assertEqual(self.selected(driver),[1,2]);self.failure.assert_called_once()
    def test_empty_before_warmup_and_formal_capture_empty_are_not_swallowed(self):
        for change in ['empty_selection','formal_empty']:
            with self.subTest(change=change):
                driver=MixedDriver(change)
                with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_EMPTY_OR_UNLOADED'):self.run_capture(driver)
                self.assertEqual(self.selected(driver),[1,2]);self.failure.assert_called_once()
    def test_already_empty_initial_position_is_not_automatically_bypassed(self):
        driver=MixedDriver();driver.current=1;driver.shots[1]=1
        with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_EMPTY_OR_UNLOADED'):self.run_capture(driver,positions=(3,))
        self.assertEqual(self.selected(driver),[]);self.assertNotIn(2,driver.shots);self.failure.assert_called_once()
    def test_state_only_diagnosis_still_does_not_activate_capture_or_create_a_progress_journal(self):
        driver=MixedDriver();result=self.run_capture(driver,positions=())
        self.assertEqual([a for a,_,_ in driver.actions],['media_state']);self.assertNotIn('captures',result)
        self.assertFalse((self.p.path/'bridge/media'/NOTE/'batches').exists())
    def test_access_and_unknown_command_outcomes_stop_without_next_position(self):
        for change,code in [('access','ACCESS_STOP'),('unknown','COMMAND_OUTCOME_UNKNOWN'),('unknown_selection','COMMAND_OUTCOME_UNKNOWN')]:
            with self.subTest(change=change):
                driver=MixedDriver(change)
                with self.assertRaisesRegex(bridge.BridgeError,code):self.run_capture(driver)
                self.assertEqual(self.selected(driver),[1,2]);self.failure.assert_called_once()
                self.assertEqual(self.failure.call_args.args[3].code,code)
    def test_fresh_uid_target_and_restriction_guard_errors_remain_fatal(self):
        for code in ['ACCOUNT_MISMATCH','TARGET_MISMATCH','ACCESS_STOP','UNEXPECTED_TARGET_CHANGE','IDENTITY_UNCONFIRMED']:
            with self.subTest(code=code):
                driver=MixedDriver()
                with self.assertRaisesRegex(bridge.BridgeError,code):self.run_capture(driver,verify_error=code)
                self.assertEqual(self.selected(driver),[1,2]);self.failure.assert_called_once()
    def test_changed_binding_session_page_account_authority_or_runtime_are_not_skipped(self):
        changes=[lambda b:b.update(account_uid='c'*24),lambda b:b.update(context_id='changed'),
                 lambda b:b.update(session='other-session'),lambda b:b.update(web_parent_authorization={'changed':True}),
                 lambda b:b.update(runtime={'changed':True}),lambda b:b['tabs']['detail'].update(page='p2'),
                 lambda b:b['tabs']['detail'].update(session='other-role'),lambda b:b['tabs']['detail'].update(owned=False)]
        for change in changes:
            with self.subTest(change=change):
                driver=MixedDriver()
                with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_PARTIAL_SCOPE_CHANGED'):self.run_capture(driver,load_change=change)
                self.assertEqual(self.selected(driver),[1,2]);self.failure.assert_called_once()
    def test_new_shared_stop_is_not_consumed_or_cleared(self):
        stop=self.root/'STOP.json';stop.write_bytes(b'{"reason":"new stop remains"}');original=stop.read_bytes()
        driver=MixedDriver()
        with self.assertRaisesRegex(bridge.legacy.StopRun,'Shared account STOP remains active'):
            self.run_capture(driver,stop_error=bridge.legacy.StopRun('Shared account STOP remains active'))
        self.assertEqual(stop.read_bytes(),original);self.assertEqual(self.selected(driver),[1,2]);self.failure.assert_called_once()
    def test_pause_after_local_gap_stops_before_later_position(self):
        pause=self.p.path/'bridge/PAUSE';pause.parent.mkdir(exist_ok=True);pause.write_text('user pause')
        driver=MixedDriver()
        with self.assertRaisesRegex(bridge.BridgeError,'USER_PAUSED'):self.run_capture(driver)
        self.assertEqual(pause.read_text(),'user pause');self.assertEqual(self.selected(driver),[1,2])
    def test_interrupt_leaves_started_position_and_previous_success_persisted_without_retry(self):
        driver=MixedDriver('interrupt')
        with self.assertRaises(KeyboardInterrupt):self.run_capture(driver)
        events=self.journal();self.assertEqual(events[-1]['event'],'position_started');self.assertEqual(events[-1]['position'],2)
        self.assertEqual(self.selected(driver),[1,2]);self.assertEqual(len(list((self.p.path/'bridge/media'/NOTE).glob('*.png'))),1)
    def test_journal_io_error_does_not_hide_original_access_stop_handler(self):
        original=media.partial_event
        def log(path,event,**fields):
            if event=='stopped':raise OSError('disk unavailable')
            return original(path,event,**fields)
        driver=MixedDriver('access')
        with patch.object(media,'partial_event',side_effect=log):
            with self.assertRaisesRegex(bridge.BridgeError,'ACCESS_STOP'):self.run_capture(driver)
        self.failure.assert_called_once();self.assertEqual(self.failure.call_args.args[3].code,'ACCESS_STOP')


if __name__=='__main__':unittest.main()
