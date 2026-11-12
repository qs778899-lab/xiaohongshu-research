"""A decoded DOM image is not proof of painted screenshot content."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import xhs_bridge as bridge
import xhs_bridge_media as media
import test_bridge_media_readiness as fixtures
NOTE=fixtures.NOTE
URL=fixtures.URL


class PaintedDriver:
    def __init__(self,count=2):self.count=count;self.current=0;self.actions=[];self.activated=False
    def call(self,action,*args,**fields):
        self.actions.append(action)
        if action=='media_state':
            return {'index':self.current,'count':self.count,'kind':'image','mediaStatus':'rendered_decoded_media',
                    'screenshotReady':True,'mediaEvidence':{'notAVisualReview':True,'images':[{'loaded':True,'visible':True,
                        'complete':True,'naturalWidth':1080,'naturalHeight':1440,'src':f'https://sns-webpic-qc.xhscdn.com/observed/asset-{self.current}.webp'}]}}
        if action=='activate':
            self.activated=True;return {'activated':'p1','windowFocusRequested':False}
        if action=='media_select':self.current=fields['index'];return {'selectionSubmitted':True}
        if action=='screenshot':
            assert self.activated,'Screenshot before target activation'
            return {'url':URL,'pngBase64':base64.b64encode(b'painted synthetic image '+str(self.current).encode()).decode()}
        raise AssertionError(action)


class ActivationTests(unittest.TestCase):
    setUp=fixtures.MediaReadinessTests.setUp
    tearDown=fixtures.MediaReadinessTests.tearDown
    capture_with=fixtures.MediaReadinessTests.capture_with
    def state(self):return {'index':0,'count':1,'kind':'image','mediaStatus':'rendered_decoded_media',
                           'verified_target':{'account_uid':self.binding['account_uid'],'context_id':self.binding['context_id'],'note_id':NOTE,'page':'p1'}}
    def old_record(self):return json.loads(self.p.db.execute('SELECT body FROM media_inventory WHERE note_id=? AND media_key=?',(NOTE,'body:1')).fetchone()[0])
    def fresh_record(self):
        file=self.root/'fresh-painted.png';file.write_bytes(b'new painted pixels')
        return {'position':1,'count':1,'kind':'image','file':str(file),'url':URL,'reviewed':False,
                'capture_revision':media.CAPTURE_REVISION,'activation':{
                    'capture_revision':media.CAPTURE_REVISION,'action':'activate','response':{'activated':'p1','windowFocusRequested':False},
                    'page':'p1','target_note_id':NOTE,'account_uid':self.binding['account_uid'],'context_id':self.binding['context_id'],
                    'verified_url':URL,'activated_at':'2026-11-05T09:50:00+00:00','verified_at':'2026-11-05T09:50:00+00:00',
                    'visual_verification_required':True},
                'paint_preparation':{'discarded_warmup':True,'wait_seconds':1,'prime_sha256':'f'*64},
                'stable_media_identity':{'index':0,'count':1,'kind':'image','assets':[{'path':'/observed/asset.webp'}]}}
    def run_inventory(self,record=None,state=None):
        fresh=record or self.fresh_record()
        with patch.object(media,'capture',side_effect=[state or self.state(),{'captures':[fresh]}]) as capture, \
             patch.object(bridge,'store_capture',return_value={'awaiting_review':True}) as store:
            result=media.collect_inventory(self.p,'test',self.p.task(self.tid),None)
        return result,capture,store
    def test_two_images_use_one_exact_tab_activation_before_all_screenshots(self):
        driver=PaintedDriver();result=self.capture_with(driver,[1,2])
        self.assertEqual(driver.actions.count('activate'),1);self.assertEqual(driver.actions.count('screenshot'),4)
        self.assertLess(driver.actions.index('activate'),driver.actions.index('screenshot'))
        self.assertEqual(len(result['captures']),2)
        for record in result['captures']:
            self.assertEqual(record['capture_revision'],media.CAPTURE_REVISION)
            self.assertEqual(record['activation']['response'],{'activated':'p1','windowFocusRequested':False})
            self.assertEqual(record['activation']['account_uid'],self.binding['account_uid'])
            self.assertEqual(record['activation']['target_note_id'],NOTE)
            self.assertEqual(record['activation']['verified_url'],URL)
            self.assertTrue(record['visual_verification_required']);self.assertFalse(record['reviewed'])
            self.assertTrue(Path(record['file']).is_file())
        self.assertEqual(result['captures'][0]['activation'],result['captures'][1]['activation'])
    def test_no_positions_diagnoses_without_activation_capture_or_claiming_review(self):
        driver=PaintedDriver(count=1);result=self.capture_with(driver,[])
        self.assertEqual(driver.actions,['media_state']);self.assertNotIn('captures',result)
        self.assertNotIn('reviewed',result);self.assertNotIn('capture_revision',result)
        self.assertEqual(result['verified_target']['account_uid'],self.binding['account_uid'])
    def test_unreviewed_cache_without_new_proof_recaptures_and_preserves_old_png(self):
        before=self.old.read_bytes();old=self.old_record();result,capture,store=self.run_inventory()
        self.assertEqual(capture.call_count,2);self.assertEqual(capture.call_args_list[1].args[3],[1])
        current=self.old_record();self.assertNotEqual(current['file'],old['file']);self.assertEqual(self.old.read_bytes(),before)
        self.assertEqual(current['previous_capture']['file'],old['file']);self.assertEqual(current['previous_capture']['sha256'],old['sha256'])
        self.assertEqual(current['capture_revision'],media.CAPTURE_REVISION);store.assert_called_once()
        self.assertTrue(result['awaiting_review'])
    def test_valid_old_visually_reviewed_capture_is_not_forced_to_recapture(self):
        old=self.old_record();self.p.db.execute("UPDATE media_inventory SET state='reviewed' WHERE note_id=?",(NOTE,));self.p.db.commit()
        with patch.object(media,'capture',return_value=self.state()) as capture, \
             patch.object(bridge,'store_capture',side_effect=AssertionError('No already reviewed capture reimport')) as store:
            result=media.collect_inventory(self.p,'test',self.p.task(self.tid),None)
        self.assertEqual(capture.call_count,1);self.assertEqual(capture.call_args.args[3],[]);store.assert_not_called()
        self.assertEqual(result['local_gap'],'MEDIA_CONTEXT_REVIEW_REQUIRED');self.assertEqual(self.old_record(),old)
        self.assertEqual(self.p.db.execute('SELECT state FROM media_inventory WHERE note_id=?',(NOTE,)).fetchone()[0],'reviewed')
    def test_current_activation_proof_unreviewed_cache_can_be_reused(self):
        record=self.fresh_record();record['sha256']=hashlib.sha256(Path(record['file']).read_bytes()).hexdigest()
        self.p.db.execute('UPDATE media_inventory SET body=? WHERE note_id=?',(json.dumps(record),NOTE));self.p.db.commit()
        with patch.object(media,'capture',return_value=self.state()) as capture,patch.object(bridge,'store_capture',return_value={'awaiting_review':True}) as store:
            media.collect_inventory(self.p,'test',self.p.task(self.tid),None)
        self.assertEqual(capture.call_count,1);self.assertEqual(store.call_args.args[2]['media_captures'][0]['file'],record['file'])
    def test_old_revision_or_different_account_proof_requires_new_capture(self):
        for mismatch in ['revision','account']:
            with self.subTest(mismatch=mismatch):
                record=self.fresh_record();record['sha256']=hashlib.sha256(Path(record['file']).read_bytes()).hexdigest()
                if mismatch=='revision':record['capture_revision']='older-revision'
                else:record['activation']['account_uid']='c'*24
                self.p.db.execute("UPDATE media_inventory SET body=?,state='captured_unreviewed' WHERE note_id=?",(json.dumps(record),NOTE));self.p.db.commit()
                _,capture,_=self.run_inventory();self.assertEqual(capture.call_count,2)
    def test_reviewed_but_changed_capture_file_is_not_silently_accepted(self):
        self.p.db.execute("UPDATE media_inventory SET state='reviewed' WHERE note_id=?",(NOTE,));self.p.db.commit()
        self.old.write_bytes(b'externally changed image bytes')
        _,capture,_=self.run_inventory();self.assertEqual(capture.call_count,2)
    def test_wrong_activation_target_is_not_a_screenshot(self):
        driver=PaintedDriver();original=driver.call
        def call(action,*args,**fields):
            result=original(action,*args,**fields)
            return {'activated':'other-page','windowFocusRequested':False} if action=='activate' else result
        driver.call=call
        with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_ACTIVATION_UNCONFIRMED'):self.capture_with(driver,[1])
        self.assertNotIn('screenshot',driver.actions)

if __name__=='__main__':unittest.main()
