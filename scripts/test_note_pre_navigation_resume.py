import json,unittest
from pathlib import Path
from unittest.mock import patch
import xhs_bridge as bridge
import xhs_manual_tab_recovery as m
import test_manual_tab_recovery as fixture

class Tests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ManualTabTests(methodName='runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.url=self.f.capture['cards'][0]['url'];self.other='https://www.xiaohongshu.com/explore/'+'4'*24
        self.f.capture['cards'].append({'url':self.other,'text':'another diary'})
        self.inspection=Path(self.f.run_inspect()['inspection']);self.root=self.inspection.parent
        self.started={'at':'2026-11-11T02:00:00+00:00','url':self.url,'note_id':'3'*24,'inspection':m.pointer(self.inspection)}
        m.evidence(self.root/'note-attempt.json',self.started)
        m.evidence(self.root/'note-before.json',{'url':self.other,'page':'A'*32})
        m.evidence(self.root/'note-failure.json',{'at':'2026-11-11T02:00:01+00:00','error':'MANUAL_PAGE_CHANGED'})
        self.current=self.f.url;self.calls=[];self.fail=None;case=self
        class Driver:
            def __init__(self,*args):pass
            def close(self):pass
            def call(self,action,b,role,**kwargs):
                case.calls.append(action)
                if action=='metadata':return {'page':'A'*32,'url':case.current}
                if action=='identity':return {'uid':case.f.identity_uid,'url':case.current}
                if action=='navigate':
                    if case.fail=='unknown':raise bridge.BridgeError('COMMAND_OUTCOME_UNKNOWN')
                    case.current=kwargs['url'];return {'navigationSubmitted':True}
                if action=='capture':return {'uid':case.f.uid,'url':case.current,'stage':'body','title':'Diary','body':'Synthetic public diary','authors':[{'name':'Example','url':'https://www.xiaohongshu.com/user/profile/'+'5'*24}]}
                raise AssertionError(action)
        self.driver=Driver
    def run_resume(self):
        with self.f.mocks(),patch.object(bridge,'Driver',self.driver),patch.object(m.time,'sleep'):
            return m.resume_note(self.inspection)
    def test_preserves_originals_and_stop_requires_review(self):
        originals={f:f.read_bytes() for f in self.root.glob('note-*.json')};stop=self.f.stop.read_bytes()
        result=self.run_resume();self.assertFalse(result['collection_permitted']);self.assertEqual(self.calls.count('navigate'),1)
        self.assertEqual(self.f.stop.read_bytes(),stop)
        for f,raw in originals.items():self.assertEqual(f.read_bytes(),raw)
        m.validated_probe(m.pointer(result['probe']))
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.run_resume()
        self.assertEqual(self.calls.count('navigate'),1)
    def test_submitted_navigation_cannot_resume(self):
        m.evidence(self.root/'note-navigation.json',{'navigationSubmitted':True})
        with self.assertRaises(bridge.BridgeError):self.run_resume()
        self.assertEqual(self.calls,[])
    def test_unrelated_prior_page_rejected(self):
        (self.root/'note-before.json').write_text(json.dumps({'page':'A'*32,'url':'https://www.xiaohongshu.com/explore/'+'7'*24}))
        with self.assertRaises(bridge.BridgeError):self.run_resume()
        self.assertEqual(self.calls,[])
    def test_search_not_restored_consumes_once_without_navigation(self):
        self.current=self.other
        for _ in range(2):
            with self.assertRaises(bridge.BridgeError):self.run_resume()
        self.assertNotIn('navigate',self.calls)
    def test_changed_identity_rejected(self):
        self.f.identity_uid='8'*24
        with self.assertRaises(bridge.BridgeError):self.run_resume()
        self.assertNotIn('navigate',self.calls)
    def test_unknown_navigation_not_replayed(self):
        self.fail='unknown'
        for _ in range(2):
            with self.assertRaises(bridge.BridgeError):self.run_resume()
        self.assertEqual(self.calls.count('navigate'),1)
    def test_changed_stop_rejected_before_browser(self):
        self.f.stops=dict(self.f.stops,k=dict(self.f.stops['k'],sha256='f'*64))
        with self.assertRaisesRegex(bridge.BridgeError,'STOP_CHANGED'):self.run_resume()
        self.assertEqual(self.calls,[])
    def test_old_history_change_invalidates_probe(self):
        result=self.run_resume();(self.root/'note-failure.json').write_text('{}')
        with self.assertRaises(bridge.BridgeError):m.validated_probe(m.pointer(result['probe']))

if __name__=='__main__':unittest.main()
