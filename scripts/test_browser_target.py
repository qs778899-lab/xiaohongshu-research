"""Offline target ownership, URL scope, and no-fallback tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xhs_browser as b
import xhs_research as r

T1, T2 = "A"*32, "B"*32
ENDPOINT = "http://127.0.0.1:9222"
NOTE = "https://www.xiaohongshu.com/explore/" + "a"*24

def target(tid=T1, url=NOTE):
    return {"id":tid,"type":"page","url":url,"webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/page/"+tid}

class TargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.run=Path(self.tmp.name).resolve()
        (self.run/'browser').mkdir();(self.run/'browser/DevToolsActivePort').write_text('9222\n/devtools/browser/owned')
        r.write_json(self.run/'run.json',{'format':r.FORMAT,'directory':str(self.run),'closed':False})
        self.state={'pid':987,'profile':str(self.run/'browser'),'endpoint':ENDPOINT}
        r.write_json(self.run/'browser.json',self.state)
    def tearDown(self):self.tmp.cleanup()
    def call(self,pages,explicit=None):
        with patch.object(b,'target_metadata',return_value=pages):
            return b.bound_target(self.run,self.state,explicit_target_id=explicit)
    def test_owned_endpoint_requires_pid_profile_and_created_port(self):
        with patch.object(b,'process_command',return_value='chrome --user-data-dir='+str(self.run/'browser')):
            self.assertEqual(b.owned_endpoint(self.run,self.state),(ENDPOINT,9222))
            self.state['endpoint']='http://127.0.0.1:9444'
            with self.assertRaises(r.ResearchError):b.owned_endpoint(self.run,self.state)
    def test_wrong_pid_never_requests_target_list(self):
        with patch.object(b,'process_command',return_value='unrelated'),patch.object(b.http.client,'HTTPConnection') as net:
            with self.assertRaises(r.ResearchError):b.target_metadata(self.run,self.state)
            self.assertEqual(net.call_count,0)
    def test_multiple_unbound_tabs_require_explicit_selection(self):
        with self.assertRaises(b.TargetBindingRequired):self.call([target(T2,'https://www.xiaohongshu.com/user/profile/'+'b'*24),target()])
        self.assertNotIn('target_id',r.read_json(self.run/'browser.json'))
    def test_explicit_second_page_binds_without_touching_first(self):
        selected=self.call([target(T2,'https://www.xiaohongshu.com/user/profile/'+'b'*24),target()],T1)
        self.assertEqual(selected['target_id'],T1)
        record=r.read_json(Path(self.state['target_binding_record']))
        self.assertTrue(record['url_only_checked']);self.assertFalse(record['other_tabs_modified'])
        self.assertNotIn(T2,json.dumps(record))
    def test_order_changes_do_not_change_binding(self):
        self.call([target()],T1)
        selected=self.call([target(T2,'https://www.xiaohongshu.com/chat'),target()])
        self.assertEqual(selected['target_id'],T1)
    def test_bound_target_disappears_never_falls_back(self):
        self.call([target()],T1)
        with self.assertRaises(r.StopRun):self.call([target(T2)])
        self.assertEqual(r.read_json(self.run/'browser.json')['target_id'],T1)
        self.assertTrue((self.run/'STOP.json').exists())
    def test_private_or_profile_bound_page_no_body_read_or_rebind(self):
        self.call([target()],T1)
        for url in ['https://www.xiaohongshu.com/chat','https://www.xiaohongshu.com/user/profile/'+'b'*24]:
            with self.assertRaises(b.NoteScopeChanged):self.call([target(T1,url),target(T2)])
        self.assertFalse((self.run/'STOP.json').exists())
        self.assertEqual(self.state['target_id'],T1)
    def test_unknown_and_restricted_target_url_stop(self):
        for url in ['https://example.org/','https://www.xiaohongshu.com/website-login/captcha',NOTE+'?error_code=300017']:
            with self.subTest(url=url):
                self.state.update(target_id=T1,target_websocket=target()['webSocketDebuggerUrl'])
                with self.assertRaises(r.StopRun):self.call([target(T1,url)])
    def test_single_legacy_public_page_compatibility(self):
        selected=self.call([target()]);self.assertEqual(selected['target_id'],T1)
        self.assertEqual(r.read_json(Path(self.state['target_binding_record']))['mode'],'legacy_single_page')
    def test_single_legacy_profile_is_not_silently_bound(self):
        with self.assertRaises(b.NoteScopeChanged):self.call([target(T1,'https://www.xiaohongshu.com/user/profile/'+'b'*24)])
        self.assertNotIn('target_id',self.state)
    def test_explicit_cannot_replace_existing_binding(self):
        self.call([target()],T1)
        with self.assertRaises(b.TargetBindingRequired):self.call([target(),target(T2)],T2)
    def test_changed_or_external_websocket_rejected(self):
        for ws in ['ws://example.org:9222/devtools/page/'+T1,'ws://127.0.0.1:9223/devtools/page/'+T1,
                   'ws://127.0.0.1:9222/devtools/page/'+T2,'ws://127.0.0.1:9222/devtools/browser/'+T1,
                   'ws://u@127.0.0.1:9222/devtools/page/'+T1,'ws://127.0.0.1:9222/devtools/page/'+T1+'?x=1']:
            t=target();t['webSocketDebuggerUrl']=ws
            with self.subTest(ws=ws),self.assertRaises(r.ResearchError):b.checked_target(t,ENDPOINT)
    def test_duplicate_target_identity_is_not_accepted(self):
        with self.assertRaises(b.TargetBindingRequired):self.call([target(),target()],T1)
    def test_listing_uses_loopback_direct_and_drops_titles(self):
        response=type('Response',(),{'status':200,'read':lambda _:json.dumps([{**target(),'title':'unrelated secret title'}]).encode()})()
        with patch.object(b,'process_command',return_value='chrome --user-data-dir='+str(self.run/'browser')),patch.object(b.http.client,'HTTPConnection') as cls:
            conn=cls.return_value;conn.getresponse.return_value=response
            rows=b.target_metadata(self.run,self.state)
            cls.assert_called_once_with('127.0.0.1',9222,timeout=3);conn.request.assert_called_once_with('GET','/json/list')
            self.assertNotIn('title',rows[0]);self.assertTrue(conn.close.called)
    def test_listing_does_not_follow_redirect(self):
        response=type('Response',(),{'status':302})()
        with patch.object(b,'process_command',return_value='chrome --user-data-dir='+str(self.run/'browser')),patch.object(b.http.client,'HTTPConnection') as cls:
            cls.return_value.getresponse.return_value=response
            with self.assertRaises(r.ResearchError):b.target_metadata(self.run,self.state)
            self.assertEqual(cls.return_value.request.call_count,1)
    def test_existing_stop_prevents_explicit_bind(self):
        r.write_json(self.run/'STOP.json',{'reason':'existing'})
        with patch.object(b,'target_metadata') as rows:
            with self.assertRaises(r.StopRun):b.bind_target(self.run,T1)
            self.assertEqual(rows.call_count,0)

if __name__=='__main__':unittest.main()
