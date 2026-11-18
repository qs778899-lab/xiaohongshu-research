"""Synthetic post-user-verification continuation; no browser or account access."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

import xhs_bridge as bridge
import xhs_manual_tab_recovery as m
import xhs_native as native
import xhs_project as xp


class SearchConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.project=self.root/'project'
        self.uid='1'*24;self.page='A'*32;self.query='Synthetic person'
        self.home='https://www.xiaohongshu.com/explore'
        self.search='https://www.xiaohongshu.com/search_result?keyword='+quote(self.query)+'&type=51'
        self.note='https://www.xiaohongshu.com/explore/'+'3'*24
        self.captcha='https://www.xiaohongshu.com/website-login/captcha?redirectPath='+quote(self.search.replace('search_result?','search_result/?')+'&exSource=',safe='')
        self.parent={'format':'xhs-bridge-v1','project':str(self.project),'profile_label':'test',
          'context_id':'test-context','account_uid':self.uid,'profile_guard_key':'k','runtime':{},
          'extension_version':'1.0.27','binding_name':'old','session':'old-session',
          'tabs':{'detail':{'navigation_intent':{'url':'https://www.xiaohongshu.com/explore/'+'2'*24}}}}
        self.stop=self.put('STOP.json',{'at':'2026-01-01T00:00:00+00:00','reason':m.nr.STOP_REASON,
                                      'channel':'bridge','credential_free':True})
        self.stops={'k':dict(m.pointer(self.stop),at='2026-01-01T00:00:00+00:00')}
        self.initial_user=self.put('initial-user.json',{'user_statement':'I restored the original account.',
          'normal_other_posts_statement':'A normal note is readable.', 'processed_at':'2026-01-01T01:00:00+00:00'})
        self.meta=self.put('meta.json',{'tabId':11,'windowId':22,'url':self.home,'active':True})
        db=xp.Project(self.project,create=True)
        manifest=self.put('manifest.json',{'units':[{'stage':'search','target':self.query,'sort':'general',
            'sources':[str(self.meta)],'next':'Read search candidates'}]})
        self.tid=native.import_manifest(db,manifest)['task_ids'][0];db.close()
        self.calls=[];self.stop_calls=[];self.current_url=self.home;self.current_uid=self.uid;self.current_page=self.page
        self.identity_page=None
        self.identity_url=None;self.capture_url=None
        self.captcha_on_search=True;self.capture_error=None;self.change_stops=False
        case=self
        class Driver:
            def __init__(self,*args):pass
            def close(self):pass
            def call(self,action,b=None,role=None,**kw):
                case.calls.append(action)
                if action=='adopt_user_tab':return {'page':case.page,'url':case.current_url,'owned':False}
                if action=='metadata':return {'page':case.current_page,'url':case.current_url,'windowId':22}
                if action=='identity':return {'uid':case.current_uid,'url':case.identity_url or case.current_url,'page':case.identity_page or case.current_page}
                if action=='navigate':
                    case.current_url=case.captcha if case.captcha_on_search and 'search_result?' in kw['url'] else kw['url']
                    return {'navigationSubmitted':True}
                if action=='capture':
                    if case.capture_error:raise bridge.BridgeError(case.capture_error)
                    if case.change_stops:case.stops['k']['sha256']='changed'
                    if kw['stage']=='search':return {'uid':case.uid,'url':case.capture_url or case.current_url,'cards':[{'title':'Synthetic candidate','url':case.note}]}
                    return {'uid':case.uid,'url':case.current_url,'title':'Synthetic note','body':'Actual synthetic text','authors':[{'id':'4'*24}]}
                raise AssertionError(action)
        self.driver=Driver
        guard=patch.object(xp.legacy,'account_root',return_value=self.root/'account-guard');guard.start();self.addCleanup(guard.stop)
        with self.mocks():
            self.inspection=Path(m.inspect(self.project,'old',self.meta,self.initial_user)['inspection'])
            with self.assertRaisesRegex(bridge.BridgeError,'ACCESS_STOP'):m.probe_search(self.inspection,self.tid)
        self.folder=self.inspection.parent
        failed=json.loads((self.folder/'search-failure.json').read_text())
        self.confirmed_at=(datetime.fromisoformat(failed['at'])+timedelta(seconds=1)).isoformat()
        self.user=self.put('confirmed-user.json',{'user_statement':'Verification completed; search results are visible.',
            'normal_other_posts_statement':'Normal posts are still readable.','processed_at':self.confirmed_at,
            'inspection':m.pointer(self.inspection),'account_uid':self.uid,'context_id':'test-context','tab_id':11,'page':self.page,
            'history':self.history()})
        self.current_url=self.search;self.captcha_on_search=False;self.calls=[]

    def put(self,name,value):
        path=self.root/name;path.write_text(json.dumps(value));return path

    def history(self):
        return {key:m.pointer(self.folder/name) for key,name in {
            'failure':'search-failure.json','navigation':'search-navigation.json',
            'previous_metadata':'search-metadata-0.json','search_attempt':'search-attempt.json'}.items()}

    @contextmanager
    def mocks(self):
        @contextmanager
        def locked(*args):yield ['k']
        with patch.object(bridge,'Driver',self.driver),patch.object(bridge,'load_binding',return_value=self.parent),\
             patch.object(m.nr,'locked_stops',locked),patch.object(m.nr,'current_stops',side_effect=lambda keys:copy.deepcopy(self.stops)),\
             patch.object(m.nr.identity,'stop_all',side_effect=lambda keys,reason:self.stop_calls.append((list(keys),reason))),\
             patch.object(m.time,'sleep'):
            yield

    def resume(self):
        with self.mocks():return m.resume_verified_search(self.inspection,self.user)

    def change_user(self,**fields):
        value=json.loads(self.user.read_text());value.update(fields);self.user.write_text(json.dumps(value))

    def test_user_confirmation_reads_current_exact_search_without_navigation(self):
        original={p:p.read_bytes() for p in self.folder.iterdir() if p.is_file()}
        stop=self.stop.read_bytes();result=self.resume()
        self.assertEqual(self.calls,['metadata','identity','capture'])
        self.assertFalse(result['collection_permitted']);self.assertEqual(self.stop.read_bytes(),stop)
        self.assertFalse((self.folder/'grant.json').exists())
        for path,content in original.items():self.assertEqual(path.read_bytes(),content)
        search=m.check_pointer(m.pointer(result['inspection']));m.validate_verified_search(search)

    def test_old_or_missing_user_confirmation_never_reads_page(self):
        self.change_user(processed_at='2026-01-01T02:00:00+00:00')
        with self.assertRaisesRegex(bridge.BridgeError,'PREDATES_FAILURE'):self.resume()
        self.change_user(processed_at=self.confirmed_at,normal_other_posts_statement='')
        with self.assertRaisesRegex(bridge.BridgeError,'USER_RESTORATION'):self.resume()
        self.assertEqual(self.calls,[])

    def test_confirmation_is_bound_to_inspection_account_context_tab_and_page(self):
        original=self.user.read_text()
        for field,value in [('inspection',{}),('account_uid','9'*24),('context_id','other'),('tab_id',12),('page','B'*32)]:
            self.user.write_text(original);self.change_user(**{field:value})
            with self.subTest(field=field),self.assertRaisesRegex(bridge.BridgeError,'CONFIRMATION_SCOPE'):self.resume()
        self.assertEqual(self.calls,[])

    def test_only_proven_captcha_redirect_for_original_query_is_eligible(self):
        path=self.folder/'search-metadata-0.json';original=path.read_text()
        for url in [self.search,self.captcha.replace('www.xiaohongshu.com/website','evil.example/website'),
                    'https://www.xiaohongshu.com/website-login/captcha?redirectPath='+quote(self.search.replace('Synthetic','Other'),safe='')]:
            data=json.loads(original);data['url']=url;path.write_text(json.dumps(data));self.change_user(history=self.history())
            with self.subTest(url=url),self.assertRaises(bridge.BridgeError):self.resume()
        self.assertEqual(self.calls,[])

    def test_other_failure_or_unknown_navigation_is_not_retried(self):
        path=self.folder/'search-failure.json';failure=json.loads(path.read_text())
        path.write_text(json.dumps(dict(failure,error='COMMAND_OUTCOME_UNKNOWN')))
        self.change_user(history=self.history())
        with self.assertRaisesRegex(bridge.BridgeError,'NOT_APPLICABLE'):self.resume()
        path.write_text(json.dumps(failure));(self.folder/'search-navigation.json').write_text('{}')
        self.change_user(history=self.history())
        with self.assertRaisesRegex(bridge.BridgeError,'NOT_APPLICABLE'):self.resume()
        self.assertEqual(self.calls,[])

    def test_current_wrong_query_tab_or_account_stops_without_navigation(self):
        for field,value,expected in [('current_url',self.search.replace('Synthetic','Other'),'PAGE_CHANGED'),
                                     ('current_page','B'*32,'PAGE_CHANGED'),('current_uid','9'*24,'IDENTITY_CHANGED')]:
            # Independent synthetic attempts, never reset a consumed continuation.
            with self.subTest(field=field):
                case=SearchConfirmationTests();case.setUp()
                try:
                    setattr(case,field,value)
                    with case.assertRaisesRegex(bridge.BridgeError,expected):case.resume()
                    case.assertNotIn('navigate',case.calls);case.assertNotIn('capture',case.calls)
                    case.assertEqual(case.stop_calls,[])
                    case.assertFalse((case.folder/'search-probe.json').exists())
                finally:case.doCleanups()

    def test_new_stop_blocks_before_read_and_changed_stop_after_capture_stays_stopped(self):
        self.stops['k']['sha256']='changed'
        with self.assertRaisesRegex(bridge.BridgeError,'STOP_CHANGED'):self.resume()
        self.assertEqual(self.calls,[])
        self.stops['k']['sha256']=m.pointer(self.stop)['sha256'];self.change_stops=True
        with self.assertRaisesRegex(bridge.BridgeError,'STOP_CHANGED'):self.resume()
        self.assertFalse((self.folder/'search-probe.json').exists());self.assertFalse((self.folder/'grant.json').exists())

    def test_new_captcha_consumes_the_only_continuation(self):
        frozen={path:path.read_bytes() for path in self.folder.glob('stop-*.json')}
        self.capture_error='ACCESS_STOP'
        with self.assertRaisesRegex(bridge.BridgeError,'ACCESS_STOP'):self.resume()
        self.assertEqual(self.stop_calls,[(['k'],m.nr.STOP_REASON)])
        for path,content in frozen.items():self.assertEqual(path.read_bytes(),content)
        self.capture_error=None;self.change_user(user_statement='A later repeated confirmation.')
        before=list(self.calls)
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.resume()
        self.assertEqual(self.calls,before);self.assertFalse((self.folder/'search-probe.json').exists())

    def test_rendered_captcha_url_at_each_read_creates_new_shared_stop(self):
        for field,expected in [('current_url',['metadata']),('identity_url',['metadata','identity']),
                               ('capture_url',['metadata','identity','capture'])]:
            with self.subTest(field=field):
                case=SearchConfirmationTests();case.setUp()
                try:
                    setattr(case,field,case.captcha)
                    with case.assertRaisesRegex(bridge.BridgeError,'ACCESS_STOP'):case.resume()
                    case.assertEqual(case.calls,expected)
                    case.assertEqual(case.stop_calls,[(['k'],m.nr.STOP_REASON)])
                    case.assertFalse((case.folder/'search-probe.json').exists())
                finally:case.doCleanups()

    def test_success_cannot_be_repeated_or_moved_to_another_attempt_directory(self):
        self.resume();before=list(self.calls)
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.resume()
        copied=self.folder.parent/'copied';shutil.copytree(self.folder,copied)
        with self.mocks(),self.assertRaisesRegex(bridge.BridgeError,'LOCATION_CHANGED'):
            m.resume_verified_search(copied/'inspection.json',self.user)
        self.assertEqual(self.calls,before)

    def test_normal_note_review_and_attestation_are_still_required(self):
        result=self.resume()
        with self.mocks():note=m.probe_note(result['inspection'],self.note)
        self.assertFalse(note['collection_permitted'])
        probe,attempt,capture=m.validated_probe(m.pointer(note['probe']))
        self.assertEqual(capture['body'],'Actual synthetic text')
        review=self.put('review.json',{'reader':'codex','reviewed':True,'body_read':True,
            'capture_sha256':probe['capture']['sha256'],'summary':'Read synthetic body and author.',
            'positions':['/title','/body','/authors'],'comments_read':False,'media_read':False})
        with self.mocks(),patch.object(bridge,'save_binding') as save:
            grant=m.attest(note['probe'],review,'new-binding')
        self.assertEqual(grant['scope'],'rendered_public_web_only');self.assertTrue(grant['stops_unchanged'])
        save.assert_called_once()

    def test_later_chain_validation_rejects_changed_confirmation_or_history(self):
        result=self.resume();search=m.check_pointer(m.pointer(result['inspection']))
        path=self.folder/'verified-search-user.json';value=json.loads(path.read_text());value['account_uid']='9'*24
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(bridge.BridgeError,'EVIDENCE_CHANGED|SCOPE_CHANGED'):m.validate_verified_search(search)

    def test_later_probe_cannot_drop_the_confirmation_chain(self):
        result=self.resume()
        with self.mocks():note=m.probe_note(result['inspection'],self.note)
        search_file=Path(result['inspection']);search=json.loads(search_file.read_text())
        del search['verified_search_continuation'];search_file.write_text(json.dumps(search))
        probe_file=Path(note['probe']);probe=json.loads(probe_file.read_text())
        probe['inspection']=m.pointer(search_file);probe_file.write_text(json.dumps(probe))
        with self.assertRaisesRegex(bridge.BridgeError,'CHAIN_CHANGED'):m.validated_probe(m.pointer(probe_file))

    def test_parent_binding_change_rejects_before_read(self):
        self.parent['context_id']='changed-context'
        with self.assertRaisesRegex(bridge.BridgeError,'PARENT_CHANGED'):self.resume()
        self.assertEqual(self.calls,[])

    def test_inspection_session_change_cannot_rebind_current_tab(self):
        inspected=json.loads(self.inspection.read_text());inspected['binding']['session']='another-session'
        self.inspection.write_text(json.dumps(inspected));self.change_user(inspection=m.pointer(self.inspection))
        with self.assertRaisesRegex(bridge.BridgeError,'AUTHORITY_CHANGED'):self.resume()
        self.assertEqual(self.calls,[])

    def test_changed_search_history_rejected_even_when_redirect_matches_new_query(self):
        started=self.folder/'search-attempt.json';value=json.loads(started.read_text())
        value['url']=self.search.replace('Synthetic','Other');started.write_text(json.dumps(value))
        meta=self.folder/'search-metadata-0.json';value=json.loads(meta.read_text())
        value['url']='https://www.xiaohongshu.com/website-login/captcha?redirectPath='+quote(self.search.replace('Synthetic','Other'),safe='')
        meta.write_text(json.dumps(value))
        with self.assertRaisesRegex(bridge.BridgeError,'HISTORY_CHANGED'):self.resume()
        self.change_user(history=self.history())
        with self.assertRaisesRegex(bridge.BridgeError,'SEARCH_TASK_CHANGED'):self.resume()
        self.assertEqual(self.calls,[])

    def test_confirmation_must_postdate_search_start_and_hash_bound_failure(self):
        failed=self.folder/'search-failure.json';value=json.loads(failed.read_text())
        value['at']='2026-01-01T01:30:00+00:00';failed.write_text(json.dumps(value))
        self.change_user(processed_at='2026-01-01T02:00:00+00:00')
        with self.assertRaisesRegex(bridge.BridgeError,'HISTORY_CHANGED'):self.resume()
        self.change_user(history=self.history())
        with self.assertRaisesRegex(bridge.BridgeError,'PREDATES_FAILURE'):self.resume()
        self.assertEqual(self.calls,[])

    def test_different_identity_page_rejects_before_capture(self):
        self.identity_page='B'*32
        with self.assertRaisesRegex(bridge.BridgeError,'IDENTITY_CHANGED'):self.resume()
        self.assertEqual(self.calls,['metadata','identity'])
        self.assertFalse((self.folder/'search-probe.json').exists())

    def test_original_task_lookup_is_read_only_and_rejects_changed_target(self):
        dbfile=self.project/'project.sqlite3';before=dbfile.read_bytes()
        m.verified_search_context(self.inspection,json.loads(self.user.read_text()))
        self.assertEqual(dbfile.read_bytes(),before)
        db=xp.Project(self.project);task=db.task(self.tid);task['payload']['target']='Other person'
        db.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),self.tid));db.db.commit();db.close()
        with self.assertRaisesRegex(bridge.BridgeError,'SEARCH_TASK_CHANGED'):self.resume()
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
