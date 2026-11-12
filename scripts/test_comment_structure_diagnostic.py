"""Offline held-root diagnostic contract: no live browser/account/network."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import xhs_bridge as bridge
import xhs_comment_structure_diagnostic as diag
import xhs_native as native
import xhs_project as xp
import test_current_page_recovery as fixture

URL=fixture.URL

class Fake:
    def __init__(self,url=URL,fail=None,visible=True):self.url=url;self.fail=fail;self.visible=visible;self.calls=[]
    def call(self,action,b,role,**kw):
        self.calls.append((action,role,kw))
        assert role=='detail'
        assert action in {'metadata','identity','navigate','comment_diagnose','screenshot'}
        if action==self.fail:raise bridge.BridgeError('CONTENT_LOADING')
        if self.fail=='interrupt' and action=='comment_diagnose':raise KeyboardInterrupt()
        page=b['tabs']['detail']['page']
        if action=='metadata':return {'page':page,'url':self.url}
        if action=='identity':return {'page':page,'url':self.url,'uid':'f'*24 if self.fail=='uid' else b['account_uid'],'visible':self.visible}
        if action=='navigate':assert kw=={'url':URL};self.url=URL;return {'navigationSubmitted':True}
        if action=='comment_diagnose':
            assert kw=={'stage':'body','expectedPath':'/explore/'+fixture.NOTE}
            return {'uid':b['account_uid'],'url':self.url,'diagnosticOnly':True,'pageVisible':self.visible,
                'rootNodeCount':0,'rootIds':[],'commentContainer':{'exists':True,'visible':True,'geometry':{'height':100}},
                'loadingVisible':False,'countHeading':{'exists':False,'visible':False,'text':None,'textWithheld':False},
                'interactionCount':{'exists':True,'visible':True,'text':'评论','textWithheld':False},
                'messages':[{'selector':'.empty','type':'empty','text':'暂无评论'}],
                'collectionPermitted':False,'repairVerified':False}
        raw=b'\x89PNG\r\n\x1a\nsynthetic'
        return {'uid':b['account_uid'],'url':self.url,'page':page,'targetVerified':True,'pngBase64':base64.b64encode(raw).decode()}
    def close(self):pass

class Tests(unittest.TestCase):
    def setUp(self):
        self.base=fixture.Tests();self.base.setUp();self.base.attest();self.p=self.base.p;self.name=self.base.name
        body={'stage':'body','uid':fixture.UID,'url':URL,'title':'Synthetic current note','body':'Synthetic body'}
        b=bridge.store_capture(self.p,self.p.task(self.base.task),body);self.sha=Path(b['snapshot']).stem
        manifest=self.base.root/'root-scope.json';manifest.write_text(json.dumps({'units':[{'stage':'roots','target':fixture.NOTE,'sources':[b['snapshot']],'next':'diagnose without retry'}]}))
        self.tid=native.import_manifest(self.p,manifest)['task_ids'][0]
        raw={'url':URL,'results':[],'deepCoverage':{'rootPaginationState':'stalled','explicitEnd':False}}
        data=xp.dumps(raw).encode();sha=hashlib.sha256(data).hexdigest();f=self.p.path/'bridge/raw'/f'{sha}.json';f.write_bytes(data)
        t=self.p.task(self.tid);pl=t['payload'];pl['bridge']={'fault':{'code':'COMMENT_PAGINATION_STALLED','hold':True,'source_snapshot':str(f),'source_sha256':sha,'at':'2026-01-01T00:00:00+00:00'},'stalled_count':3,'cursor':{'scrollTop':100},'hashes':[sha],'collection_end':False}
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(pl),self.tid));self.p.db.commit();self.before=self.row()
    def tearDown(self):self.base.tearDown()
    def row(self):return self.p.db.execute('SELECT payload,status,reason FROM tasks WHERE id=?',(self.tid,)).fetchone()
    def run_diag(self,driver=None,**kw):
        with patch.object(diag.time,'sleep'):
            return diag.run(self.p.path,self.name,self.tid,self.sha,driver=driver or Fake(),**kw)
    def test_same_note_structure_only_preserves_held_task_and_stops(self):
        driver=Fake();result=self.run_diag(driver);self.assertEqual([a[0] for a in driver.calls],['metadata','identity','comment_diagnose'])
        self.assertEqual(tuple(self.before),tuple(self.row()));self.base.unchanged();self.assertFalse(result['collection_permitted']);self.assertFalse(result['repair_verified']);self.assertTrue(result['task_bytes_unchanged'])
    def test_one_intent_cannot_be_reset_by_source_or_flags(self):
        self.run_diag();driver=Fake()
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.run_diag(driver,screenshot=True,navigate_original_target=True)
        self.assertEqual(driver.calls,[])
    def test_no_navigation_without_explicit_flag_and_failure_consumed(self):
        b=bridge.load_binding(self.p.path,self.name);other='https://www.xiaohongshu.com/user/profile/'+'6'*24;b['tabs']['detail'].pop('navigation_intent',None);b['tabs']['detail']['last_url']=other;bridge.save_binding(self.p.path,self.name,b)
        driver=Fake(other)
        with self.assertRaisesRegex(bridge.BridgeError,'NOT_CURRENT'):self.run_diag(driver)
        self.assertNotIn('navigate',[a[0] for a in driver.calls])
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.run_diag(Fake(other),navigate_original_target=True)
        self.assertEqual(tuple(self.before),tuple(self.row()))
    def test_explicit_navigation_uses_only_actual_same_note_source_once(self):
        b=bridge.load_binding(self.p.path,self.name);other='https://www.xiaohongshu.com/user/profile/'+'6'*24;b['tabs']['detail'].pop('navigation_intent',None);b['tabs']['detail']['last_url']=other;bridge.save_binding(self.p.path,self.name,b)
        driver=Fake(other);self.run_diag(driver,navigate_original_target=True)
        self.assertEqual([x[0] for x in driver.calls],['metadata','identity','navigate','metadata','identity','comment_diagnose'])
        self.assertEqual([x[2]['url'] for x in driver.calls if x[0]=='navigate'],[URL]);self.assertEqual(tuple(self.before),tuple(self.row()));self.base.unchanged()
    def test_other_unregistered_source_wrong_uid_or_note_rejected(self):
        f=self.p.path/'bridge/raw'/f'{self.sha}.json';original=f.read_bytes();value=json.loads(original);value['uid']='f'*24;raw=xp.dumps(value).encode();sha=hashlib.sha256(raw).hexdigest();(f.parent/f'{sha}.json').write_bytes(raw)
        driver=Fake()
        with self.assertRaises(bridge.BridgeError):diag.run(self.p.path,self.name,self.tid,sha,driver=driver)
        self.assertEqual(driver.calls,[])
    def test_visible_screenshot_once_not_media_review(self):
        driver=Fake();r=self.run_diag(driver,screenshot=True);self.assertEqual([x[0] for x in driver.calls].count('screenshot'),1);self.assertTrue(Path(r['screenshot']['file']).exists());self.assertFalse(r['screenshot']['comment_read']);self.assertEqual(tuple(self.before),tuple(self.row()))
    def test_hidden_target_does_not_activate_or_request_screenshot(self):
        driver=Fake(visible=False);r=self.run_diag(driver,screenshot=True);self.assertIn('SCREENSHOT_TARGET_NOT_VISIBLE',r['gaps']);self.assertNotIn('screenshot',[x[0] for x in driver.calls]);self.assertEqual(tuple(self.before),tuple(self.row()))
    def test_failures_and_interrupt_do_not_allow_retry(self):
        for fail in ['metadata','identity','comment_diagnose','interrupt']:
            t=Tests();t.setUp()
            try:
                driver=Fake(fail=fail)
                with self.assertRaises(BaseException):t.run_diag(driver)
                old=len(driver.calls)
                with self.assertRaises(bridge.BridgeError):t.run_diag(driver)
                self.assertEqual(len(driver.calls),old);self.assertEqual(tuple(t.before),tuple(t.row()))
            finally:t.tearDown()
    def test_unexpected_returned_content_not_accepted(self):
        class Bad(Fake):
            def call(self,action,b,role,**kw):
                value=super().call(action,b,role,**kw)
                if action=='comment_diagnose':value['comments']=['Should never be returned by fixed DOM diagnostic']
                return value
        with self.assertRaisesRegex(bridge.BridgeError,'UNEXPECTED_CONTENT'):self.run_diag(Bad())
        self.assertEqual(tuple(self.before),tuple(self.row()))

    def test_private_nested_text_rejected_without_saving_structure(self):
        class Private(Fake):
            def call(self,action,b,role,**kw):
                value=super().call(action,b,role,**kw)
                if action=='comment_diagnose':value['countHeading']['text']='患者张三的电话13800138000'
                return value
        with self.assertRaisesRegex(bridge.BridgeError,'SAFE_COUNT'):self.run_diag(Private())
        self.assertFalse(list((self.p.path/'bridge/comment-structure-diagnostics').glob('*/structure.json')))
        self.assertEqual(tuple(self.before),tuple(self.row()))
    def test_account_mismatch_stops_before_structure_or_navigation(self):
        driver=Fake(fail='uid')
        with self.assertRaisesRegex(bridge.BridgeError,'ACCOUNT_MISMATCH'):self.run_diag(driver,navigate_original_target=True)
        self.assertEqual([a[0] for a in driver.calls],['metadata','identity']);self.assertEqual(tuple(self.before),tuple(self.row()))
    def test_screenshot_visibility_failure_is_gap_without_repeat(self):
        class HiddenShot(Fake):
            def call(self,action,b,role,**kw):
                if action=='screenshot':self.calls.append((action,role,kw));raise bridge.BridgeError('SCREENSHOT_TARGET_NOT_VISIBLE')
                return super().call(action,b,role,**kw)
        driver=HiddenShot();r=self.run_diag(driver,screenshot=True)
        self.assertEqual(r['gaps'],['SCREENSHOT_TARGET_NOT_VISIBLE']);self.assertIsNone(r['screenshot']);self.assertEqual([a[0] for a in driver.calls].count('screenshot'),1)
        self.assertNotIn('activate',[a[0] for a in driver.calls]);self.assertEqual(tuple(self.before),tuple(self.row()))
    def test_count_format_rejects_grouped_phone_and_accepts_real_count_forms(self):
        b=bridge.load_binding(self.p.path,self.name);context=diag.source_context(self.p,b,self.tid,self.sha)
        value=Fake().call('comment_diagnose',b,'detail',stage='body',expectedPath='/explore/'+fixture.NOTE)
        for text in ['138.0013.8000','138,0013,8000','138 0013 8000','共 138.0013.8000 条评论','138.00.13','1,23']:
            value['countHeading']['text']=text
            with self.subTest(text=text),self.assertRaisesRegex(bridge.BridgeError,'SAFE_COUNT'):
                diag.validate_structure(b,context,value)
        for text in ['共 17 条评论','1,234','共1,234条评论','1.2万','0','评论']:
            value['countHeading']['text']=text;diag.validate_structure(b,context,value)

    def test_unheld_or_different_stage_not_diagnostic_authority(self):
        t=self.p.task(self.tid);payload=t['payload'];payload['bridge']['fault']['hold']=False
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(payload),self.tid));self.p.db.commit();driver=Fake()
        with self.assertRaisesRegex(bridge.BridgeError,'ORIGINAL_HELD_ROOT'):self.run_diag(driver)
        self.assertEqual(driver.calls,[])

if __name__=='__main__':unittest.main()
