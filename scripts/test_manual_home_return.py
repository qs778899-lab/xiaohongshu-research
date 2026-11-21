"""Synthetic exact-home returns; real existing grants and account STOP checks."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_manual_tab_recovery as fixture
import xhs_account_identity as identity
import xhs_bridge as bridge
import xhs_manual_tab_recovery as manual
import xhs_manual_home_return as helper
import xhs_native as native
import xhs_project as xp


class Driver:
    def __init__(self):
        self.calls=[];self.url=helper.HOME;self.fail=None;self.capture_url=None
    def call(self,action,b,role,**kw):
        self.calls.append(action);slot=b['tabs'][role]
        if self.fail==action+'_access':raise bridge.BridgeError('ACCESS_STOP')
        if action=='metadata':
            return {'page':'B'*32 if self.fail=='page' else slot['page'],
                    'url':self.url,'windowId':23 if self.fail=='metadata_window' else None,
                    'securityBlock':self.fail=='metadata_warning','notFound':self.fail=='metadata_not_found'}
        if action=='identity':
            return {'page':'B'*32 if self.fail=='identity_page' else slot['page'],
                    'uid':'9'*24 if self.fail=='uid' else b['account_uid'],
                    'url':'https://www.xiaohongshu.com/website-login/captcha' if self.fail=='identity_captcha' else self.url,
                    'loginWall':self.fail=='login_wall'}
        if action=='locate_registered_url':
            return {'matches':[{'tabId':12 if self.fail=='tab' else slot['chrome_tab_id'],
                'windowId':23 if self.fail=='window' else slot['window_id'],'url':self.url}]}
        if action=='navigate':
            self.url=kw['url']
            if self.fail=='unknown':raise bridge.BridgeError('COMMAND_OUTCOME_UNKNOWN')
            if self.fail=='interrupt':raise KeyboardInterrupt()
            if self.fail=='canonical':self.url=self.url.replace('/explore/','/search_result/')+'?xsec_source=synthetic'
            return {'navigationSubmitted':True}
        if action=='capture':
            return {'url':self.capture_url or self.url,'uid':b['account_uid'],'title':'Synthetic note',
                    'body':'' if self.fail=='empty' else 'Synthetic normal narrative.',
                    'authors':[{'name':'Example author'}],'loginWall':self.fail=='capture_warning',
                    'notFound':self.fail=='capture_not_found'}
        raise AssertionError(action)


class HomeReturnTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.ManualTabTests(methodName='runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups);f=self.fixture
        f.parent['profile_guard_key']='c'*64;xp.legacy.account_root().mkdir(exist_ok=True)
        self.keys=identity.related_keys(f.parent['profile_guard_key'],f.uid);self.stops={};f.stops={}
        for key in self.keys:
            path=identity.guard_directory(key)/'STOP.json';path.write_bytes(f.stop.read_bytes())
            self.stops[key]=path.read_bytes();f.stops[key]=dict(manual.pointer(path),at='2026-11-11T00:00:00+00:00')
        _,b=f.create_grant_fixture();self.project=f.p;self.name='manual'
        self.db=xp.Project(self.project,create=True);self.addCleanup(self.db.close)
        source=f.put('candidate.json',{'query':'Synthetic person'})
        manifest=f.put('tasks.json',{'units':[{'stage':'search','target':'Synthetic person','sort':'general',
            'sources':[str(source)],'next':'Read next search page'}]})
        self.task=native.import_manifest(self.db,manifest)['task_ids'][0]
        # Existing searches may have progress; the failure occurred before THIS navigation.
        t=self.db.task(self.task);t['payload']['bridge']={'hashes':['a'*64],'page':3}
        self.db.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(t['payload']),self.task));self.db.db.commit()
        b.update(state='blocked',last_error={'code':'UNEXPECTED_TARGET_CHANGE','at':xp.legacy.now()})
        b['tabs']['detail']['verified_at']=xp.legacy.now();bridge.save_binding(self.project,self.name,b)
        self.before=copy.deepcopy(b);self.task_before=self.db.task(self.task)
        self.metadata=f.put('observed-home.json',{'page':b['tabs']['detail']['page'],'url':helper.HOME,'windowId':None})
        self.log=f.put('batch.jsonl',{'event':'blocked','reason':'UNEXPECTED_TARGET_CHANGE','processed_units':0,
            'task_id':self.task,'actor':'codex','metrics':{'calls':1}})
        self.driver=Driver();self.authorization='The user previously authorized returning the original tab and continuing.'
        patcher=patch.object(helper.time,'sleep');patcher.start();self.addCleanup(patcher.stop)

    def restore(self):
        return helper.restore(self.project,self.name,self.task,self.metadata,self.log,self.authorization,self.driver)

    def review(self,result):
        return self.fixture.put('return-review.json',{'reader':'codex','reviewed':True,'body_read':True,
            'capture_sha256':result['capture']['sha256'],'summary':'Read synthetic title, author and full body.',
            'positions':['/title','/body','/authors'],'comments_read':False,'media_read':False})

    def attest(self,result,review=None):
        return helper.attest(self.project,self.name,result['probe']['file'],review or self.review(result),self.driver)

    def unchanged_stops(self):
        for key,raw in self.stops.items():self.assertEqual((identity.guard_directory(key)/'STOP.json').read_bytes(),raw)

    def test_single_navigation_stays_blocked_until_real_review(self):
        result=self.restore();b=bridge.load_binding(self.project,self.name)
        self.assertEqual(self.driver.calls,['metadata','identity','locate_registered_url','navigate','metadata','identity','locate_registered_url','capture'])
        self.assertEqual(b['state'],'blocked');self.assertEqual(b['last_error'],self.before['last_error'])
        self.assertEqual(manual.authority(b),manual.authority(self.before));self.assertFalse(result['collection_permitted'])
        attempt=manual.check_pointer(manual.check_pointer(result['probe'])['attempt'])
        self.assertEqual(attempt['binding'],self.before);self.assertEqual(attempt['task'],self.task_before)
        self.assertEqual(self.db.task(self.task),self.task_before);self.unchanged_stops()
        outcome=self.attest(result);b=bridge.load_binding(self.project,self.name)
        self.assertEqual(b['state'],'ready');self.assertFalse(outcome['new_grant']);self.assertEqual(outcome['tasks_completed'],0)
        self.assertEqual(b['manual_tab_revalidation'],self.before['manual_tab_revalidation'])
        self.assertIn(self.before['last_error'],b['resolved_errors']);self.assertEqual(self.db.task(self.task),self.task_before)
        self.assertEqual(self.driver.calls.count('navigate'),1);self.unchanged_stops()
        with bridge.lease(self.project,b):pass
        with self.assertRaises(xp.legacy.StopRun):
            with identity.account_lease(b['profile_guard_key'],b['account_uid']):pass

    def test_existing_progress_does_not_require_new_empty_search(self):
        self.assertTrue(self.task_before['payload']['bridge']['hashes']);self.restore()
        self.assertEqual(self.db.task(self.task),self.task_before)

    def test_note_stages_share_the_same_pre_navigation_home_return(self):
        for stage in ['body','roots','replies','images','video']:
            with self.subTest(stage=stage):
                case=HomeReturnTests(methodName='runTest');case.setUp()
                try:
                    target=case.before['tabs']['detail']['last_url']
                    source=case.fixture.put('note-source.json',{'url':target})
                    manifest=case.fixture.put('note-task.json',{'units':[{'stage':stage,
                        'target':manual.note_id(target),'sources':[str(source)],'next':'Read remaining scope'}]})
                    case.task=native.import_manifest(case.db,manifest)['task_ids'][0]
                    case.task_before=case.db.task(case.task)
                    log=json.loads(case.log.read_text());log['task_id']=case.task;case.log.write_text(json.dumps(log))
                    result=case.restore();case.attest(result)
                    self.assertEqual(case.db.task(case.task),case.task_before)
                    self.assertEqual(case.driver.calls.count('navigate'),1);case.unchanged_stops()
                finally:case.doCleanups()

    def test_recommendation_home_requires_exact_observed_url(self):
        home=helper.HOME+'?channel_id=homefeed_recommend'
        self.metadata.write_text(json.dumps({'page':self.before['tabs']['detail']['page'],'url':home,'windowId':None}))
        self.driver.url=home
        result=self.restore();self.attest(result)
        self.assertEqual(self.driver.calls.count('navigate'),1);self.unchanged_stops()

    def test_exact_normal_home_only_not_404_query_or_other_page(self):
        for url in [helper.HOME+'?source=404',helper.HOME+'/',helper.HOME+'?other=1',self.before['tabs']['detail']['last_url']]:
            with self.subTest(url=url):
                case=HomeReturnTests();case.setUp()
                try:
                    case.driver.url=url
                    with case.assertRaises(xp.legacy.ResearchError):case.restore()
                    case.assertNotIn('navigate',case.driver.calls);case.unchanged_stops()
                finally:case.doCleanups()

    def test_changed_page_identity_native_tab_or_window_never_navigates(self):
        for fail in ['page','uid','identity_page','metadata_window','tab','window']:
            with self.subTest(fail=fail):
                case=HomeReturnTests();case.setUp()
                try:
                    case.driver.fail=fail
                    with case.assertRaises(bridge.BridgeError):case.restore()
                    case.assertNotIn('navigate',case.driver.calls);case.unchanged_stops()
                finally:case.doCleanups()

    def test_new_stop_and_changed_grant_reject_before_browser(self):
        path=identity.guard_directory(self.keys[0])/'STOP.json';path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(xp.legacy.StopRun):self.restore()
        self.assertEqual(self.driver.calls,[])

    def test_tampered_grant_rejects_before_browser(self):
        path=Path(self.before['manual_tab_revalidation']['file']);path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(xp.legacy.StopRun):self.restore()
        self.assertEqual(self.driver.calls,[])

    def test_fresh_access_errors_write_shared_stop_and_revoke_old_grant(self):
        for fail in ['metadata_access','metadata_warning','identity_captcha','login_wall','capture_access','capture_warning']:
            with self.subTest(fail=fail):
                case=HomeReturnTests();case.setUp()
                try:
                    case.driver.fail=fail
                    with case.assertRaises(xp.legacy.ResearchError):case.restore()
                    for key,old in case.stops.items():case.assertNotEqual((identity.guard_directory(key)/'STOP.json').read_bytes(),old)
                    with case.assertRaises(xp.legacy.StopRun):
                        with bridge.lease(case.project,bridge.load_binding(case.project,case.name)):pass
                finally:case.doCleanups()

    def test_direct_metadata_and_capture_captcha_urls_write_shared_stop(self):
        for field in ['url','capture_url']:
            with self.subTest(field=field):
                case=HomeReturnTests();case.setUp()
                try:
                    setattr(case.driver,field,'https://www.xiaohongshu.com/website-login/captcha')
                    with case.assertRaises(xp.legacy.StopRun):case.restore()
                    for key,old in case.stops.items():case.assertNotEqual((identity.guard_directory(key)/'STOP.json').read_bytes(),old)
                finally:case.doCleanups()

    def test_not_found_with_residual_content_is_not_a_normal_page(self):
        for fail in ['metadata_not_found','capture_not_found']:
            with self.subTest(fail=fail):
                case=HomeReturnTests();case.setUp()
                try:
                    case.driver.fail=fail
                    with case.assertRaisesRegex(bridge.BridgeError,'NORMAL_PAGE'):case.restore()
                    case.assertEqual(bridge.load_binding(case.project,case.name)['state'],'blocked')
                    case.unchanged_stops()
                finally:case.doCleanups()

    def test_unknown_navigation_and_interrupt_cannot_replay(self):
        for fail in ['unknown','interrupt']:
            with self.subTest(fail=fail):
                case=HomeReturnTests();case.setUp()
                try:
                    case.driver.fail=fail
                    with case.assertRaises(BaseException):case.restore()
                    with case.assertRaises(bridge.BridgeError):case.restore()
                    case.assertEqual(case.driver.calls.count('navigate'),1);case.unchanged_stops()
                finally:case.doCleanups()

    def test_attempt_cannot_reset_by_failure_time_task_or_authorization(self):
        self.driver.fail='page'
        with self.assertRaises(bridge.BridgeError):self.restore()
        b=copy.deepcopy(self.before);b['last_error']['at']=xp.legacy.now();bridge.save_binding(self.project,self.name,b)
        self.authorization='Another phrasing of the same permission.';self.task='different-task'
        with self.assertRaisesRegex(bridge.BridgeError,'ALREADY_ATTEMPTED'):self.restore()
        self.assertEqual(self.driver.calls,['metadata'])

    def test_missing_prior_authorization_or_inflight_navigation_rejects(self):
        self.authorization=''
        with self.assertRaisesRegex(bridge.BridgeError,'AUTHORIZATION'):self.restore()
        self.authorization='Already authorized';b=bridge.load_binding(self.project,self.name)
        b['tabs']['detail']['navigation_intent']={'url':helper.HOME};bridge.save_binding(self.project,self.name,b)
        with self.assertRaisesRegex(bridge.BridgeError,'INFLIGHT'):self.restore()
        self.assertEqual(self.driver.calls,[])

    def test_unproven_failure_metrics_reject_before_browser(self):
        value=json.loads(self.log.read_text());value['metrics']['calls']=2;self.log.write_text(json.dumps(value))
        with self.assertRaisesRegex(bridge.BridgeError,'PRE_NAVIGATION'):self.restore()
        self.assertEqual(self.driver.calls,[])

    def test_unobserved_and_excluded_last_note_reject_before_browser(self):
        b=bridge.load_binding(self.project,self.name);b['tabs']['detail']['last_url']='https://www.xiaohongshu.com/explore/'+'4'*24
        bridge.save_binding(self.project,self.name,b)
        with self.assertRaisesRegex(bridge.BridgeError,'OBSERVED_NORMAL'):self.restore()
        b['tabs']['detail']['last_url']='https://www.xiaohongshu.com/explore/'+'2'*24;bridge.save_binding(self.project,self.name,b)
        with self.assertRaisesRegex(bridge.BridgeError,'NORMAL_NOTE'):self.restore()
        self.assertEqual(self.driver.calls,[])

    def test_imported_same_account_body_source_and_hash_validation(self):
        target='https://www.xiaohongshu.com/explore/'+'4'*24
        b=bridge.load_binding(self.project,self.name);b['tabs']['detail']['last_url']=target;bridge.save_binding(self.project,self.name,b)
        value={'url':target,'uid':b['account_uid'],'stage':'body','title':'Archived','body':'Original body','authors':[{'name':'Synthetic'}]}
        raw=json.dumps(value).encode();folder=self.project/'native-sources';folder.mkdir(exist_ok=True)
        path=folder/hashlib.sha256(raw).hexdigest();path.write_bytes(raw+b' ')
        with self.assertRaisesRegex(bridge.BridgeError,'OBSERVED_NORMAL'):self.restore()
        self.assertEqual(self.driver.calls,[]);path.write_bytes(raw);self.restore()

    def test_other_account_archive_is_not_return_source(self):
        target='https://www.xiaohongshu.com/explore/'+'4'*24
        b=bridge.load_binding(self.project,self.name);b['tabs']['detail']['last_url']=target;bridge.save_binding(self.project,self.name,b)
        raw=json.dumps({'url':target,'uid':'9'*24,'stage':'body','title':'Other','body':'Other','authors':[{'name':'Other'}]}).encode()
        folder=self.project/'native-sources';folder.mkdir(exist_ok=True);(folder/hashlib.sha256(raw).hexdigest()).write_bytes(raw)
        with self.assertRaisesRegex(bridge.BridgeError,'OBSERVED_NORMAL'):self.restore()
        self.assertEqual(self.driver.calls,[])

    def test_empty_returned_body_does_not_promote_ready(self):
        self.driver.fail='empty'
        with self.assertRaisesRegex(bridge.BridgeError,'NOTE_CONTENT'):self.restore()
        self.assertEqual(bridge.load_binding(self.project,self.name)['state'],'blocked')
        with self.assertRaises(bridge.BridgeError):self.restore()
        self.assertEqual(self.driver.calls.count('navigate'),1)

    def test_same_note_canonical_url_is_accepted_without_extra_navigation(self):
        self.driver.fail='canonical';result=self.restore();self.attest(result)
        self.assertEqual(self.driver.calls.count('navigate'),1)

    def test_review_missing_or_changed_capture_cannot_promote(self):
        result=self.restore();review=self.review(result);value=json.loads(review.read_text());value['body_read']=False
        review.write_text(json.dumps(value))
        with self.assertRaisesRegex(bridge.BridgeError,'ACTUAL_CODEX_REVIEW'):self.attest(result,review)
        self.assertEqual(bridge.load_binding(self.project,self.name)['state'],'blocked')
        Path(result['capture']['file']).write_text('{}')
        with self.assertRaises(bridge.BridgeError):self.attest(result)

    def test_changed_binding_or_stop_after_review_stays_blocked(self):
        result=self.restore();path=identity.guard_directory(self.keys[0])/'STOP.json';path.write_bytes(path.read_bytes()+b' ')
        before=list(self.driver.calls)
        with self.assertRaises(xp.legacy.StopRun):self.attest(result)
        self.assertEqual(self.driver.calls,before);self.assertEqual(bridge.load_binding(self.project,self.name)['state'],'blocked')

    def test_page_changed_during_reading_does_not_promote_or_renavigate(self):
        result=self.restore();self.driver.url=helper.HOME
        with self.assertRaises(bridge.BridgeError):self.attest(result)
        self.assertEqual(self.driver.calls.count('navigate'),1);self.assertEqual(bridge.load_binding(self.project,self.name)['state'],'blocked')


class AuthorReturnTests(unittest.TestCase):
    def setUp(self):
        self.base=HomeReturnTests(methodName='runTest');self.base.setUp()
        self.addCleanup(self.base.doCleanups);s=self.base
        self.uid='4'*24;url='https://www.xiaohongshu.com/user/profile/'+self.uid+'?xsec_token=synthetic'
        self.capture={'stage':'author','uid':s.before['account_uid'],'url':url,
            'cards':[{'url':'https://www.xiaohongshu.com/user/profile/'+self.uid+'/'+('6'*24),
                      'text':'Synthetic treatment diary'}]}
        raw=xp.dumps(self.capture).encode();h=hashlib.sha256(raw).hexdigest()
        self.source=s.project/'bridge/raw'/(h+'.json');self.source.parent.mkdir(exist_ok=True)
        self.source.write_bytes(raw)
        manifest=s.fixture.put('author-task.json',{'units':[{'stage':'author','target':self.uid,
            'sources':[str(self.source)],'next':'Read related public diaries'}]})
        s.task=native.import_manifest(s.db,manifest)['task_ids'][0]
        t=s.db.task(s.task);t['payload']['bridge']={'hashes':[h],'last_snapshot':str(self.source)}
        s.db.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(t['payload']),s.task));s.db.db.commit()
        s.task_before=s.db.task(s.task);s.before['tabs']['detail']['last_url']=url
        bridge.save_binding(s.project,s.name,s.before)
        home=helper.HOME+'?channel_id=homefeed_recommend'
        s.metadata.write_text(json.dumps({'page':s.before['tabs']['detail']['page'],'url':home,'windowId':None}))
        self.rows=[{'event':'unit','task_id':s.task,'snapshot':str(self.source)},
            {'event':'blocked','reason':'UNEXPECTED_TARGET_CHANGE','processed_units':1,
             'task_id':s.task,'actor':'codex','metrics':{'calls':11}}]
        self.write_log()
        capture=self.capture
        class AuthorDriver(Driver):
            def call(self,action,b,role,**kw):
                if action=='capture':
                    self.calls.append(action)
                    if self.fail=='capture_access':raise bridge.BridgeError('ACCESS_STOP')
                    return copy.deepcopy(capture)
                return super().call(action,b,role,**kw)
        s.driver=AuthorDriver();s.driver.url=home

    def write_log(self):
        self.base.log.write_text('\n'.join(json.dumps(r) for r in self.rows))

    def review(self,result):
        return self.base.fixture.put('author-return-review.json',{'reader':'codex','reviewed':True,
            'cards_read':True,'body_read':False,'capture_sha256':result['capture']['sha256'],
            'summary':'Actually read the returned public diary titles.','positions':['/cards'],
            'comments_read':False,'media_read':False})

    def test_author_progress_returns_same_page_and_requires_card_review(self):
        s=self.base;result=s.restore()
        self.assertEqual(s.db.task(s.task),s.task_before)
        with self.assertRaisesRegex(bridge.BridgeError,'AUTHOR_REVIEW'):s.attest(result)
        outcome=s.attest(result,self.review(result))
        self.assertFalse(outcome['new_grant']);self.assertEqual(outcome['tasks_completed'],0)
        self.assertEqual(s.driver.calls.count('navigate'),1);s.unchanged_stops()

    def test_unregistered_progress_cannot_authorize_return(self):
        self.rows[0]['snapshot']=str(self.source.with_name('5'*64+'.json'));self.write_log()
        with self.assertRaisesRegex(bridge.BridgeError,'AUTHOR_PROGRESS'):self.base.restore()
        self.assertEqual(self.base.driver.calls,[])

    def test_progress_count_and_task_must_match(self):
        self.rows[-1]['processed_units']=2;self.write_log()
        with self.assertRaisesRegex(bridge.BridgeError,'AUTHOR_PROGRESS'):self.base.restore()
        self.assertEqual(self.base.driver.calls,[])

    def test_changed_archive_rejects_before_browser(self):
        self.source.write_bytes(self.source.read_bytes()+b' ')
        with self.assertRaises(xp.legacy.ResearchError):self.base.restore()
        self.assertEqual(self.base.driver.calls,[])

    def test_extra_home_query_rejects_before_navigation(self):
        meta=json.loads(self.base.metadata.read_text());meta['url']+='&extra=1'
        self.base.metadata.write_text(json.dumps(meta));self.base.driver.url=meta['url']
        with self.assertRaisesRegex(bridge.BridgeError,'EXACT_HOME'):self.base.restore()
        self.assertEqual(self.base.driver.calls,[])

    def test_changed_author_capture_remains_blocked(self):
        self.capture['cards'][0]['url']=self.capture['cards'][0]['url'].replace(self.uid,'7'*24)
        with self.assertRaisesRegex(bridge.BridgeError,'AUTHOR_CAPTURE'):self.base.restore()
        self.assertEqual(self.base.driver.calls.count('navigate'),1);self.base.unchanged_stops()

    def test_author_access_stop_revokes_existing_grant(self):
        self.base.driver.fail='capture_access'
        with self.assertRaises(xp.legacy.ResearchError):self.base.restore()
        for key,old in self.base.stops.items():
            self.assertNotEqual((identity.guard_directory(key)/'STOP.json').read_bytes(),old)

    def test_unknown_author_navigation_not_replayed(self):
        s=self.base;s.driver.fail='unknown'
        with self.assertRaises(bridge.BridgeError):s.restore()
        with self.assertRaises(bridge.BridgeError):s.restore()
        self.assertEqual(s.driver.calls.count('navigate'),1);s.unchanged_stops()


if __name__=='__main__':unittest.main()
