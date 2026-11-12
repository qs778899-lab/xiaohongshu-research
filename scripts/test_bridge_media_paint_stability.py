"""Warmup captures and stable media checks avoid compositor and diagnostic noise."""
import base64
import copy
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
import unittest
from unittest.mock import patch

import xhs_bridge as bridge
import xhs_bridge_media as media
import test_bridge_media_readiness as fixtures
NOTE=fixtures.NOTE
URL=fixtures.URL


class DynamicDriver:
    def __init__(self,change=None):self.actions=[];self.activated=False;self.shots=0;self.states=0;self.change=change
    def call(self,action,*args,**fields):
        self.actions.append(action)
        if action=='activate':self.activated=True;return {'activated':'p1','windowFocusRequested':False}
        if action=='media_select':return {'selectionSubmitted':True}
        if action=='screenshot':
            assert self.activated;self.shots+=1
            payload=b'discarded warmup pixels' if self.shots==1 else b'actual formal image pixels'
            return {'url':URL,'pngBase64':base64.b64encode(payload).decode()}
        if action=='media_state':
            self.states+=1;last=self.shots>=2
            asset='asset-B' if self.change=='asset' and last else 'asset-A'
            item='2' if self.change=='query' and last else '1'
            suffix='other' if self.change=='transform' and last else 'webp'
            # All these dimensions/styles and known signatures vary every observation.
            src=f'https://sns-webpic-qc.xhscdn.com/202611051759/{self.states:032x}/{asset}!{suffix}?signature={self.states}&expires={self.states}&id={item}'
            state={'index':1 if self.change=='index' and last else 0,
                   'count':2 if self.change=='count' and last else 1,'kind':'image','screenshotReady':True,
                   'mediaStatus':'rendered_decoded_media','mediaEvidence':{'activeSlide':{'rect':{'width':468+self.states}},
                       'images':[{'src':src,'loaded':True,'complete':True,'visible':True,
                           'naturalWidth':1080+self.states,'naturalHeight':1440+self.states,
                           'rect':{'width':400+self.states},'display':str(self.states)}]}}
            if self.change=='unloaded' and last:state.update(kind='empty_or_unloaded',screenshotReady=False,mediaStatus='empty_or_unloaded')
            if self.change=='missing_identity':state['mediaEvidence']['images']=[]
            if self.change=='kind' and last:
                state['kind']='video';state['mediaEvidence']['videos']=[{'src':'https://sns-video.xhscdn.com/video.mp4','loaded':True,
                    'visible':True,'readyState':2,'videoWidth':1280,'videoHeight':720}]
            return state
        raise AssertionError(action)


class PaintStabilityTests(unittest.TestCase):
    setUp=fixtures.MediaReadinessTests.setUp
    tearDown=fixtures.MediaReadinessTests.tearDown
    def capture_with(self,driver):
        def verify(*args):
            driver.actions.append('target_uid_guard');return {'page':'p1','url':URL}
        with patch.object(bridge,'load_binding',return_value=self.binding),patch.object(bridge,'lease',return_value=nullcontext([])), \
             patch.object(bridge,'prepare_target'),patch.object(bridge,'save_binding'),patch.object(bridge,'verify_tab',side_effect=verify), \
             patch.object(bridge,'save_failure'),patch.object(media.time,'sleep'):
            return media.capture(self.p.path,'test',self.tid,[1],driver)
    def test_one_discarded_warmup_then_formal_capture_keeps_only_formal_pixels(self):
        driver=DynamicDriver();result=self.capture_with(driver);record=result['captures'][0]
        self.assertEqual(driver.actions.count('activate'),1);self.assertEqual(driver.actions.count('screenshot'),2)
        for i,action in enumerate(driver.actions):
            if action=='screenshot':self.assertEqual(driver.actions[i-1],'target_uid_guard')
        self.assertEqual(Path(record['file']).read_bytes(),b'actual formal image pixels')
        self.assertEqual(len(list(Path(record['file']).parent.glob('*.png'))),1)
        proof=record['paint_preparation'];self.assertTrue(proof['discarded_warmup']);self.assertEqual(proof['wait_seconds'],1)
        self.assertEqual(proof['prime_sha256'],hashlib.sha256(b'discarded warmup pixels').hexdigest())
        self.assertTrue(proof['does_not_prove_readability']);self.assertFalse(record['reviewed'])
        self.assertTrue(record['visual_verification_required'])
    def test_rect_intrinsic_size_style_and_known_signatures_are_not_position_identity(self):
        driver=DynamicDriver();result=self.capture_with(driver)
        identity=result['captures'][0]['stable_media_identity']
        self.assertEqual(identity['assets'][0]['path'],'/asset-A!webp')
        self.assertEqual(identity['assets'][0]['query'],[('id','1')])
    def test_real_asset_transform_query_index_count_and_kind_changes_are_blocked_with_diagnostics(self):
        for change in ['asset','transform','query','index','count','kind']:
            with self.subTest(change=change):
                driver=DynamicDriver(change)
                with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_POSITION_CHANGED') as raised:self.capture_with(driver)
                diagnostic=Path(raised.exception.diagnostic_file);data=json.loads(diagnostic.read_text())
                self.assertEqual(data['phase'],'formal-capture');self.assertNotEqual(data['stable_before'],data['stable_after'])
                self.assertEqual(data['code'],'MEDIA_POSITION_CHANGED');self.assertTrue(data['no_automatic_retry'])
                self.assertIn('before',data);self.assertIn('after',data)
        self.assertEqual(list((self.p.path/'bridge/media'/NOTE).glob('*.png')),[])
    def test_readiness_is_required_before_and_after_capture(self):
        driver=DynamicDriver('unloaded')
        with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_EMPTY_OR_UNLOADED') as raised:self.capture_with(driver)
        self.assertTrue(Path(raised.exception.diagnostic_file).is_file())
        self.assertEqual(list((self.p.path/'bridge/media'/NOTE).glob('*.png')),[])
    def test_missing_actual_asset_identity_stops_before_any_screenshot(self):
        driver=DynamicDriver('missing_identity')
        with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_IDENTITY_UNCONFIRMED') as raised:self.capture_with(driver)
        self.assertNotIn('screenshot',driver.actions);self.assertTrue(Path(raised.exception.diagnostic_file).is_file())
    def test_unknown_query_signature_and_unknown_path_are_not_broadly_ignored(self):
        pairs=[('https://sns-webpic-qc.xhscdn.com/a?asset=1','https://sns-webpic-qc.xhscdn.com/a?asset=2'),
               ('https://other.example/a?signature=1','https://other.example/a?signature=2'),
               ('https://sns-webpic-qc.xhscdn.com/folder-A/image.webp','https://sns-webpic-qc.xhscdn.com/folder-B/image.webp'),
               ('https://sns-webpic-qc.xhscdn.com/a#view-1','https://sns-webpic-qc.xhscdn.com/a#view-2')]
        for before,after in pairs:
            with self.subTest(before=before):self.assertNotEqual(media.stable_asset_identity(before),media.stable_asset_identity(after))
    def test_loaded_video_diagnostic_progress_is_not_asset_identity(self):
        state={'index':0,'count':1,'kind':'video','screenshotReady':True,'mediaEvidence':{'videos':[{
            'src':'https://sns-video.xhscdn.com/asset.mp4?sign=old','loaded':True,'visible':True,'readyState':2,'videoWidth':720,'videoHeight':1280}]}}
        later=copy.deepcopy(state);later['mediaEvidence']['videos'][0].update(src='https://sns-video.xhscdn.com/asset.mp4?sign=new',readyState=4,videoWidth=360)
        self.assertEqual(media.stable_media_identity(state),media.stable_media_identity(later))

if __name__=='__main__':unittest.main()
