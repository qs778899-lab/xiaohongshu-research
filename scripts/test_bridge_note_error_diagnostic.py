"""Offline error diagnosis must never grant account recovery or edit a STOP."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import xhs_bridge as bridge

NOTE='d'*24
OTHER='e'*24
NOTE_URL='https://www.xiaohongshu.com/explore/'+NOTE
INTENT={'url':NOTE_URL+'?xsec_token=observed','at':'2026-11-05T01:00:00+08:00'}


def error_url(updates=None,remove=(),nested=None):
    source='/404/sec_WMiaUGpu?'+urlencode({'redirectPath':nested or NOTE_URL})
    query={'source':source,'error_code':'300031','error_msg':'当前笔记暂时无法浏览','uuid':'observed','verifyMsg':''}
    query.update(updates or {})
    for key in remove:query.pop(key,None)
    return 'https://www.xiaohongshu.com/404?'+urlencode(query,doseq=True)


class NoteErrorClassificationTests(unittest.TestCase):
    def assert_blocked(self,result,classification='ACCESS_STOP'):
        self.assertEqual(result['classification'],classification)
        self.assertFalse(result['collection_permitted']);self.assertFalse(result['retry_allowed'])
        self.assertFalse(result['identity_verified']);self.assertFalse(result['stop_changed'])
        self.assertTrue(result['fresh_identity_verification_required_before_recovery'])
    def test_exact_example_is_note_unavailable_but_not_recovery(self):
        result=bridge.classify_note_error(error_url(),INTENT)
        self.assert_blocked(result,'NOTE_UNAVAILABLE');self.assertEqual(result['note_id'],NOTE)
    def test_absent_verification_field_is_allowed(self):
        self.assert_blocked(bridge.classify_note_error(error_url(remove=['verifyMsg']),INTENT),'NOTE_UNAVAILABLE')
    def test_error_code_message_and_verification_must_be_exact(self):
        for updates in [{'error_code':'300032'},{'error_code':' 300031'},
                        {'error_msg':'当前笔记暂时无法浏览 '},{'error_msg':'请先登录'},
                        {'verifyMsg':'captcha'},{'verifyMsg':' '},{'verifyMsg':['','']},
                        {'error_code':['300031','300031']},{'error_msg':['当前笔记暂时无法浏览']*2}]:
            with self.subTest(updates=updates):self.assert_blocked(bridge.classify_note_error(error_url(updates),INTENT))
    def test_unknown_or_duplicate_source_parameters_still_block(self):
        for updates in [{'captcha':'1'},{'verifymsg':'captcha'},
                        {'source':['/404/sec_x?redirectPath='+NOTE_URL]*2}]:
            with self.subTest(updates=updates):self.assert_blocked(bridge.classify_note_error(error_url(updates),INTENT))
    def test_same_message_on_other_origin_path_or_fragment_is_not_enough(self):
        url=error_url()
        for changed in [url.replace('https:','http:',1),url.replace('www.xiaohongshu.com','www.xiaohongshu.com.evil',1),
                        url.replace('www.xiaohongshu.com','user@www.xiaohongshu.com',1),
                        url.replace('www.xiaohongshu.com','www.xiaohongshu.com:443',1),
                        url.replace('/404?','/404/?'),url+'#fragment']:
            with self.subTest(changed=changed):self.assert_blocked(bridge.classify_note_error(changed,INTENT))
    def test_current_timed_note_intent_required(self):
        for intent in [None,{}, {'url':NOTE_URL},{'at':'time'},
                       {'url':'https://www.xiaohongshu.com/search_result?keyword=doctor','at':'time'},
                       {'url':'https://www.xiaohongshu.com/user/profile/'+NOTE,'at':'time'},
                       {'url':'https://example.com/explore/'+NOTE,'at':'time'}]:
            with self.subTest(intent=intent):self.assert_blocked(bridge.classify_note_error(error_url(),intent))
    def test_other_note_redirect_is_not_accepted(self):
        for nested in ['https://www.xiaohongshu.com/explore/'+OTHER,
                       'https://example.com/explore/'+NOTE,
                       'https://www.xiaohongshu.com/user/profile/'+NOTE,
                       NOTE_URL+'?error_code=300031']:
            with self.subTest(nested=nested):self.assert_blocked(bridge.classify_note_error(error_url(nested=nested),INTENT))
    def test_redirect_must_be_inside_expected_source(self):
        sources=['https://evil.example/404/sec_x?'+urlencode({'redirectPath':NOTE_URL}),
                 '/404?'+urlencode({'redirectPath':NOTE_URL}),
                 '/404/sec_x?redirectPath='+NOTE_URL+'&redirectPath='+NOTE_URL,
                 '/404/sec_x?'+urlencode({'redirectPath':NOTE_URL,'captcha':'1'}),
                 '/404/sec_x']
        for source in sources:
            with self.subTest(source=source):self.assert_blocked(bridge.classify_note_error(error_url({'source':source}),INTENT))
        self.assert_blocked(bridge.classify_note_error(error_url({'redirectPath':NOTE_URL},remove=['source']),INTENT))
    def test_guard_itself_is_not_relaxed(self):
        with self.assertRaisesRegex(bridge.BridgeError,'ACCESS_STOP'):bridge.safe_url(error_url())
        self.assertNotIn('NOTE_UNAVAILABLE',bridge.LOCAL_FAULTS)


class ExistingMetadataDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.project=Path(self.tmp.name)/'project'
        self.project.mkdir();(self.project/'bridge/bindings').mkdir(parents=True)
        self.binding={'format':bridge.VERSION,'project':str(self.project.resolve()),'state':'blocked',
                      'last_error':{'code':'ACCESS_STOP'},'account_uid':'a'*24,
                      'tabs':{'detail':{'page':'exact-owned-page','navigation_intent':dict(INTENT)}}}
        self.binding_file=self.project/'bridge/bindings/test.json'
        self.binding_file.write_text(json.dumps(self.binding))
        self.stop=self.project/'STOP';self.stop.write_text('original STOP must remain byte-identical')
        self.source=self.project/'observed-metadata.json'
        self.source.write_text(json.dumps({'page':'exact-owned-page','url':error_url()}))
    def tearDown(self):self.tmp.cleanup()
    def snapshot(self):return {str(f.relative_to(self.project)):f.read_bytes() for f in self.project.rglob('*') if f.is_file()}
    def test_diagnostic_reads_existing_metadata_without_network_or_mutations(self):
        before=self.snapshot()
        with patch.object(bridge,'Driver',side_effect=AssertionError('No network')) as driver, \
             patch.object(bridge.identity,'stop_all',side_effect=AssertionError('No stop changes')) as stop, \
             patch.object(bridge,'save_binding',side_effect=AssertionError('No state changes')) as save:
            result=bridge.diagnose_error(self.project,'test','detail',self.source)
        self.assertEqual(result['classification'],'NOTE_UNAVAILABLE')
        self.assertFalse(result['collection_permitted']);self.assertTrue(result['binding_state_unchanged'])
        self.assertEqual(result['source_sha256'],hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(before,self.snapshot());driver.assert_not_called();stop.assert_not_called();save.assert_not_called()
    def test_other_page_cannot_borrow_intent(self):
        self.source.write_text(json.dumps({'page':'some-other-page','url':error_url()}))
        before=self.snapshot();result=bridge.diagnose_error(self.project,'test','detail',self.source)
        self.assertEqual(result['classification'],'ACCESS_STOP');self.assertEqual(before,self.snapshot())
    def test_missing_or_other_role_has_no_recovery(self):
        result=bridge.diagnose_error(self.project,'test','search',self.source)
        self.assertEqual(result['classification'],'ACCESS_STOP');self.assertFalse(result['collection_permitted'])
    def test_existing_stop_blocks_collection_after_diagnosis(self):
        result=bridge.diagnose_error(self.project,'test','detail',self.source)
        self.assertEqual(result['classification'],'NOTE_UNAVAILABLE')
        self.assertEqual(json.loads(self.binding_file.read_text())['last_error']['code'],'ACCESS_STOP')
        with self.assertRaisesRegex(bridge.BridgeError,'ACCESS_STOP'):bridge.safe_url(error_url())
        self.assertTrue(self.stop.exists())

if __name__=='__main__':unittest.main()
