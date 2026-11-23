import hashlib,json,tempfile,unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch
import xhs_expired_link_return as expired
import xhs_bridge as bridge
import xhs_note_link_return as link
import xhs_manual_tab_recovery as manual
import test_source404_recovery as fixture

class Tests(unittest.TestCase):
    def test_exact_code_message_and_target(self):
        failed='https://www.xiaohongshu.com/explore/'+'a'*24
        target='https://www.xiaohongshu.com/explore/'+'b'*24
        args=dict(redirectPath=failed,error_code='300017',error_msg='当前访问链接已失效',uuid='a'*36,verifyMsg='')
        url=lambda q:'https://www.xiaohongshu.com/website-login/error?'+urlencode(q)
        expired.validate_expired(url(args),failed,target)
        for change in [dict(error_code='300031'),dict(verifyMsg='captcha'),dict(error_msg='访问受限'),dict(redirectPath=target),dict(extra='1')]:
            with self.assertRaises(bridge.BridgeError):expired.validate_expired(url(dict(args,**change)),failed,target)
        with self.assertRaises(bridge.BridgeError):expired.validate_expired(url(args),failed,failed)
        with self.assertRaises(bridge.BridgeError):expired.validate_expired(url(args)+'&error_code=300017',failed,target)
    def test_signed_observed_source_preferred_and_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);bare='https://www.xiaohongshu.com/explore/'+'a'*24;signed=bare+'?xsec_token=synthetic&xsec_source=pc_search'
            raw=json.dumps({'first':bare,'later':signed}).encode();(p/'source.json').write_bytes(raw)
            task={'payload':{'stage':'body','target':'a'*24,'bridge_url':bare,'provenance':[{'archive':'source.json','sha256':hashlib.sha256(raw).hexdigest()}]}}
            self.assertEqual(bridge.observed_url(p,task),signed)
            (p/'source.json').write_text('{}')
            self.assertEqual(bridge.observed_url(p,task),bare)
    def prepare(self):
        f=fixture.ManualTests(methodName='runTest');f.setUp();self.addCleanup(f.doCleanups)
        b=bridge.load_binding(f.p.path,f.name);b['last_error']['code']='ACCESS_STOP'
        with expired.nr.locked_stops(b['profile_guard_key'],b['account_uid']) as keys:stops=expired.nr.current_stops(keys)
        b['last_error']['at']=next(iter(stops.values()))['at'];bridge.save_binding(f.p.path,f.name,b)
        args=dict(redirectPath=b['tabs']['detail']['navigation_intent']['url'],error_code='300017',error_msg='当前访问链接已失效',uuid='a'*36,verifyMsg='')
        f.driver.url='https://www.xiaohongshu.com/website-login/error?'+urlencode(args)
        return f,b
    def test_no_grant_no_stop_or_binding_change_and_no_replay(self):
        f,b=self.prepare();before=bridge.binding_path(f.p.path,f.name).read_bytes();calls=[]
        def nav(binding,error,failed,target):calls.append(target);f.driver.url=target;return {'navigationSubmitted':True}
        result=expired.restore(f.p.path,f.name,'Authorized return',nav,f.driver)
        self.assertFalse(result['collection_permitted']);self.assertEqual(bridge.binding_path(f.p.path,f.name).read_bytes(),before);f.unchanged()
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):expired.restore(f.p.path,f.name,'Authorized return',nav,f.driver)
        self.assertEqual(len(calls),1)
    def test_unknown_navigation_is_never_replayed(self):
        f,b=self.prepare();calls=[]
        def nav(*args):calls.append(1);raise bridge.BridgeError('OUTCOME_UNKNOWN')
        for _ in range(2):
            with self.assertRaises(bridge.BridgeError):expired.restore(f.p.path,f.name,'Authorized return',nav,f.driver)
        self.assertEqual(len(calls),1);f.unchanged()

    def archived_source(self,f,b,**change):
        data={'stage':'body','uid':b['account_uid'],'url':'https://www.xiaohongshu.com/explore/'+'4'*24,
              'title':'Synthetic previously read note','body':'Synthetic normal narrative.',
              'authors':[{'name':'Example author'}]}
        data.update(change);raw=json.dumps(data).encode();directory=f.p.path/'bridge'/'raw';directory.mkdir(parents=True,exist_ok=True)
        source=directory/(hashlib.sha256(raw).hexdigest()+'.json');source.write_bytes(raw)
        b['web_validation']={'snapshot':str(source),'uid':b['account_uid'],'context_id':b['context_id'],
            'target':'4'*24,'at':'2026-11-10T00:00:00+00:00'}
        b['tabs']['detail']['last_url']='https://www.xiaohongshu.com/search_result?keyword=synthetic'
        bridge.save_binding(f.p.path,f.name,b);return source
    def test_explicit_linked_prior_body_can_return_from_a_search_origin(self):
        f,b=self.prepare();source=self.archived_source(f,b);before=bridge.binding_path(f.p.path,f.name).read_bytes();calls=[]
        def nav(binding,error,failed,target):calls.append(target);f.driver.url=target;return {'navigationSubmitted':True}
        result=expired.restore(f.p.path,f.name,'Authorized return',nav,f.driver,normal_source=source)
        self.assertEqual(calls,['https://www.xiaohongshu.com/explore/'+'4'*24]);self.assertFalse(result['collection_permitted'])
        self.assertEqual(bridge.binding_path(f.p.path,f.name).read_bytes(),before);f.unchanged()
    def test_changed_or_unlinked_prior_body_never_navigates(self):
        f,b=self.prepare();source=self.archived_source(f,b);source.write_bytes(source.read_bytes()+b' ')
        with self.assertRaisesRegex(bridge.BridgeError,'HASH_CHANGED'):
            expired.restore(f.p.path,f.name,'Authorized return',driver=f.driver,normal_source=source)
        self.assertEqual(f.driver.calls,[]);f.unchanged()
    def test_wrong_account_and_post_failure_capture_never_navigate(self):
        f,b=self.prepare();source=self.archived_source(f,b,uid='9'*24)
        with self.assertRaisesRegex(bridge.BridgeError,'PRE_FAILURE'):
            expired.restore(f.p.path,f.name,'Authorized return',driver=f.driver,normal_source=source)
        source=self.archived_source(f,b);b['web_validation']['at']='2099-01-01T00:00:00+00:00';bridge.save_binding(f.p.path,f.name,b)
        with self.assertRaisesRegex(bridge.BridgeError,'PRE_FAILURE'):
            expired.restore(f.p.path,f.name,'Authorized return',driver=f.driver,normal_source=source)
        self.assertEqual(f.driver.calls,[]);f.unchanged()
    def test_unreferenced_source_never_navigates(self):
        f,b=self.prepare();source=self.archived_source(f,b);b['web_validation']['snapshot']='different.json';bridge.save_binding(f.p.path,f.name,b)
        with self.assertRaisesRegex(bridge.BridgeError,'LINKED_NORMAL'):
            expired.restore(f.p.path,f.name,'Authorized return',driver=f.driver,normal_source=source)
        self.assertEqual(f.driver.calls,[])

if __name__=='__main__':unittest.main()
