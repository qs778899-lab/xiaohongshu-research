"""Blank or unverified carousel state must not recycle a previous image capture."""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_bridge as bridge
import xhs_bridge_media as media
import xhs_native as native
import xhs_project as xp
import xhs_rounds as rounds

NOTE='a'*24
URL='https://www.xiaohongshu.com/explore/'+NOTE+'?xsec_token=actually-observed'
EMPTY={'index':0,'count':1,'kind':'empty_or_unloaded','mediaStatus':'empty_or_unloaded','screenshotReady':False,
       'mediaEvidence':{'images':[],'backgrounds':[],'notANoMediaProof':True}}
LOADED={'index':0,'count':2,'kind':'image','mediaStatus':'rendered_decoded_media','screenshotReady':True}


class FakeMediaDriver:
    def __init__(self,states):self.states=list(states);self.actions=[];self.metrics={'calls':0,'process_starts':1}
    def call(self,action,*args,**kwargs):
        self.actions.append(action)
        if action=='media_state':return self.states.pop(0)
        if action=='media_select':return {'selectionSubmitted':True}
        if action=='activate':return {'activated':'p1','windowFocusRequested':False}
        raise AssertionError('Unexpected screenshot or navigation: '+action)
    def close(self):pass


class MediaReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.p=xp.Project(self.root/'project',create=True)
        source=self.root/'source.json';source.write_text(json.dumps({'url':URL}))
        manifest=self.root/'manifest.json';manifest.write_text(json.dumps({'units':[{'stage':'images','target':NOTE,'next':'read','sources':[str(source)]}]}))
        self.tid=native.import_manifest(self.p,manifest)['task_ids'][0];rounds.schema(self.p)
        self.old=self.root/'old-white-capture.png';self.old.write_bytes(b'old screenshot remains immutable')
        old={'position':1,'count':1,'kind':'image','file':str(self.old),'sha256':hashlib.sha256(self.old.read_bytes()).hexdigest(),
             'url':URL,'observed_at':'2026-11-05T09:00:00+00:00'}
        self.p.db.execute('INSERT INTO media_inventory VALUES(?,?,?,?,?)',(NOTE,'body:1','image',rounds.packed(old),'captured_unreviewed'));self.p.db.commit()
        self.binding={'binding_name':'test','runtime':{},'account_uid':'b'*24,'context_id':'context','tabs':{'detail':{'page':'p1','owned':True}},'state':'ready'}
    def tearDown(self):self.p.close();self.tmp.cleanup()
    def capture_with(self,driver,positions):
        with patch.object(bridge,'load_binding',return_value=self.binding),patch.object(bridge,'lease',return_value=nullcontext([])), \
             patch.object(bridge,'prepare_target'),patch.object(bridge,'save_binding'),patch.object(bridge,'verify_tab',return_value={'page':'p1','url':URL}), \
             patch.object(bridge,'save_failure'),patch.object(media.time,'sleep'):
            return media.capture(self.p.path,'test',self.tid,positions,driver)
    def test_existing_false_image_cache_is_not_reused_under_empty_state(self):
        before=[tuple(r) for r in self.p.db.execute('SELECT * FROM media_inventory')];raw=self.old.read_bytes()
        for state in [EMPTY,dict(EMPTY,kind='image'),dict(EMPTY,mediaStatus=None)]:
            with self.subTest(state=state):
                with patch.object(media,'capture',return_value=dict(state,note=NOTE)) as capture, \
                     patch.object(bridge,'store_capture',side_effect=AssertionError('No old cache accepted')) as store:
                    with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_EMPTY_OR_UNLOADED'):
                        media.collect_inventory(self.p,'test',self.p.task(self.tid),None)
                    self.assertEqual(capture.call_count,1);self.assertEqual(capture.call_args.args[3],[]);store.assert_not_called()
                self.assertEqual(before,[tuple(r) for r in self.p.db.execute('SELECT * FROM media_inventory')]);self.assertEqual(raw,self.old.read_bytes())
    def test_state_only_keeps_diagnostic_evidence_for_codex(self):
        driver=FakeMediaDriver([EMPTY]);result=self.capture_with(driver,[])
        self.assertEqual(result['kind'],'empty_or_unloaded');self.assertEqual(result['mediaEvidence'],EMPTY['mediaEvidence'])
        self.assertEqual(driver.actions,['media_state']);self.assertEqual(result['position'],1)
    def test_empty_before_selection_prevents_screenshot(self):
        driver=FakeMediaDriver([EMPTY,EMPTY])
        with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_EMPTY_OR_UNLOADED'):self.capture_with(driver,[1])
        self.assertEqual(driver.actions,['media_state','activate','media_state'])
    def test_empty_after_selection_prevents_screenshot_of_next_slide(self):
        driver=FakeMediaDriver([LOADED,LOADED,LOADED,dict(EMPTY,index=1,count=2)])
        with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_EMPTY_OR_UNLOADED'):self.capture_with(driver,[2])
        self.assertEqual(driver.actions,['media_state','activate','media_state','media_state','media_select','media_state'])
        self.assertFalse((self.p.path/'bridge/media').exists())
    def test_media_empty_is_a_local_task_gap_and_worker_can_finish_handoff(self):
        fake=FakeMediaDriver([])
        with patch.object(bridge,'load_binding',return_value=self.binding),patch.object(bridge,'Driver',return_value=fake), \
             patch.object(media,'collect_inventory',side_effect=bridge.BridgeError('MEDIA_EMPTY_OR_UNLOADED')), \
             patch.object(bridge.identity,'stop_all',side_effect=AssertionError('Not an account access stop')) as stop:
            result=bridge.work(self.p.path,'test',units=2,max_seconds=20,interval=3,task_ids=[self.tid])
        self.assertEqual(result['event'],'handoff');self.assertEqual(result['processed_units'],1);stop.assert_not_called()
        task=self.p.task(self.tid);self.assertEqual(task['status'],'pending')
        self.assertEqual(task['payload']['bridge']['fault']['code'],'MEDIA_EMPTY_OR_UNLOADED')
        self.assertTrue(task['payload']['bridge']['fault']['hold'])

if __name__=='__main__':unittest.main()
