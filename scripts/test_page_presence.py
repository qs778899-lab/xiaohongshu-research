"""Synthetic failed-manual page presence; no live browser or account."""
import copy
import hashlib
import json
from pathlib import Path
import shlex
import unittest
from unittest.mock import patch

import xhs_page_presence as presence
import xhs_current_page_recovery as current
import xhs_bridge as bridge
import xhs_project as xp
import xhs_account_identity as identity
import test_current_page_recovery as fixture
Fake,UID=fixture.Fake,fixture.UID


class PresenceDriver(Fake):
    def call(self,action,b,role,**kwargs):
        result=super().call(action,b,role,**kwargs)
        if action in {'identity','capture'}:result['visible']=True
        if action=='capture':
            result['body']='';result['stage']='body';result['authors']=[{'url':'https://www.xiaohongshu.com/user/profile/'+'7'*24,'name':'Patient author'}]
            result['media']=[{'position':1,'tag':'IMG','url':'https://example.invalid/not-read.png','alt':''}]
        return result


class PresenceTests(unittest.TestCase):
    FORK=False
    def setUp(self):
        self.base=fixture.Tests(methodName='runTest');self.base.FORK=self.FORK;self.base.setUp()
        self.p=self.base.p;self.root=self.base.root;self.name=self.base.name
        self.old=self.base.failed_original();manual=self.base.manual_authorization(self.old)
        self.driver=PresenceDriver()
        with self.assertRaisesRegex(bridge.BridgeError,'ACTUAL_BODY'):self.base.manual_probe(manual,self.driver)
        self.directory=next(d for d in (self.p.path/'bridge/current-page-recoveries').iterdir() if d!=self.old)
        self.attempt=self.directory/'attempt.json';attempt=json.loads(self.attempt.read_text())
        self.execution=self.root/'execution.json';self.receipt=self.root/'presence-review.json'
        ref=lambda p:{'file':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        archive=self.root/'code-archive';archive.mkdir();rows=[]
        for name in sorted(presence.EXECUTED_FILES):
            file=Path(current.__file__).with_name(name);copyfile=archive/name;copyfile.write_bytes(file.read_bytes())
            rows.append({'original_file':str(file),**ref(copyfile)})
        manifest=archive/'manifest.json';manifest.write_text(json.dumps({'kind':'captured_manual_probe_code_archive_v1','files':rows}))
        args=['/synthetic/python',str(Path(current.__file__)),'--project',str(self.p.path),'probe','--binding',self.name,
              '--task',attempt['task_id'],'--confirmation',attempt['confirmation']['file'],'--manual-restoration',manual.as_posix(),
              '--authorization',attempt['authorization']]
        self.execution.write_text(json.dumps({'kind':'manual-page-probe-execution_v1','reader':'codex','actual_tool_call':True,
            'binding_name':self.name,'attempt':ref(self.attempt),'failure':ref(self.directory/'failure.json'),
            'executed_code_manifest':ref(manifest),'tool_name':'synthetic test fixture','command':shlex.join(args),
            'recorded_at':xp.legacy.now(),'tool_result':{'exit_code':1,'stdout_code':'CURRENT_ACTUAL_BODY_AND_TITLE_REQUIRED'},
            'extra_browser_calls_after_probe':False}))
    def tearDown(self):self.base.tearDown()
    def prepare(self):return presence.attest(self.p.path,self.name,self.attempt,self.execution)
    def attest(self):
        result=self.prepare();self.receipt.write_text(json.dumps({'kind':presence.KIND,'reader':'codex','reviewed':True,
            'probe_id':result['probe_id'],'evidence_sha256':result['evidence_sha256'],'title_read':True,'author_read':True,
            'media_metadata_read':True,'body_read':False,'comment_read':False,'media_read':False,
            'summary':'Read title, author and media metadata; body is empty, images/comments were not read.',
            'positions':['/sources/capture/result/title','/sources/capture/result/authors','/sources/capture/result/media']}))
        return presence.attest(self.p.path,self.name,self.attempt,self.execution,self.receipt)
    def reject(self):
        with self.assertRaises(xp.legacy.ResearchError):self.prepare()
    def test_offline_prepare_only_preserves_failure_stops_and_tasks(self):
        old={p:p.read_bytes() for p in self.directory.iterdir() if p.is_file()};b=bridge.load_binding(self.p.path,self.name)
        task=self.p.task(self.base.task)
        with patch.object(bridge,'Driver',side_effect=AssertionError('NO_BROWSER_CALLS')):result=self.prepare()
        self.assertFalse(result['collection_permitted']);self.assertEqual(b,bridge.load_binding(self.p.path,self.name))
        for p,raw in old.items():self.assertEqual(p.read_bytes(),raw)
        self.assertEqual(task,self.p.task(self.base.task));self.base.unchanged();self.base.blocked()
    def test_typed_metadata_only_attest_allows_original_web_not_api(self):
        before=bridge.load_binding(self.p.path,self.name);task=self.p.task(self.base.task)
        with patch.object(bridge,'Driver',side_effect=AssertionError('NO_BROWSER_CALLS')):result=self.attest()
        self.base.allowed();self.base.unchanged();after=bridge.load_binding(self.p.path,self.name)
        self.assertEqual(before['tabs'],after['tabs']);self.assertEqual(before.get('web_validation'),after.get('web_validation'))
        self.assertEqual(task,self.p.task(self.base.task));self.assertFalse(result['body_read']);self.assertTrue(result['no_tasks_completed'])
        with self.assertRaises(xp.legacy.StopRun):
            with identity.account_lease(before['profile_guard_key'],UID):pass
        again=presence.attest(self.p.path,self.name,self.attempt,self.execution,self.receipt);self.assertTrue(again['duplicate'])
    def test_body_read_receipt_or_missing_media_metadata_read_is_rejected(self):
        result=self.prepare()
        # Actual shape must explicitly preserve all three unread flags.
        self.receipt.write_text(json.dumps({'kind':presence.KIND,'reader':'codex','reviewed':True,'probe_id':result['probe_id'],
            'evidence_sha256':result['evidence_sha256'],'title_read':True,'author_read':True,'media_metadata_read':True,
            'body_read':True,'comment_read':False,'media_read':False,'summary':'bad','positions':['title']}))
        with self.assertRaisesRegex(bridge.BridgeError,'METADATA_ONLY_REVIEW'):presence.attest(self.p.path,self.name,self.attempt,self.execution,self.receipt)
        self.base.blocked()
    def test_non_manual_or_other_failure_cannot_prepare(self):
        f=self.directory/'failure.json';value=json.loads(f.read_text());value['code']='ACCESS_STOP';f.write_text(json.dumps(value));self.reject()
    def test_capture_or_failed_source_tamper_invalidates_grant(self):
        self.attest();self.base.allowed();f=self.directory/'capture.json';f.write_bytes(f.read_bytes()+b' ');self.base.blocked()
    def test_execution_command_must_match_actual_context(self):
        value=json.loads(self.execution.read_text());value['command']=value['command'].replace('--task ', '--other-task ')
        self.execution.write_text(json.dumps(value));self.reject()
    def test_execution_kind_scope_marker_is_not_evidence(self):
        self.execution.write_text(json.dumps({'kind':'manual-page-probe-execution_v1','actual_tool_call':True}));self.reject()
    def test_executed_guard_archive_tampering_rejected(self):
        f=self.root/'code-archive/xhs_bridge_driver.mjs';f.write_bytes(f.read_bytes()+b' ');self.reject()
    def test_current_validator_or_source_changes_revoke(self):
        self.attest();self.base.allowed();changed=presence.code_hashes();changed['xhs_page_presence.py']='0'*64
        with patch.object(presence,'code_hashes',return_value=changed):self.base.blocked()
        self.base.allowed();self.execution.write_bytes(self.execution.read_bytes()+b' ');self.base.blocked()
    def test_new_stop_and_other_binding_revoke(self):
        self.attest();b=bridge.load_binding(self.p.path,self.name);b['binding_name']='copy';bridge.save_binding(self.p.path,'copy',b)
        with self.assertRaises(xp.legacy.ResearchError):
            with bridge.lease(self.p.path,b):pass
        f=identity.guard_directory(self.base.keys[0])/'STOP.json';f.write_bytes(f.read_bytes()+b' ');self.base.blocked()
    def test_title_must_exist_in_original_observed_task_source(self):
        source=self.root/'current-source.json';value=json.loads(source.read_text());value['cards'][0]['text']='Different';source.write_text(json.dumps(value));self.reject()
    def test_strict_body_attest_does_not_accept_presence_failure(self):
        with self.assertRaises(bridge.BridgeError):current.attest(self.p.path,self.name,self.directory/'probe.json',self.receipt)
        self.base.blocked()
    def test_invalid_capture_semantics_rejected_even_with_refreshed_hashes(self):
        # Hashes alone cannot substitute for the actual allowed result shape.
        for field,value in [('stage','comments'),('uid','9'*24),('visible',False),('body','Actual text'),
                ('authors',[]),('authors',[{'url':'https://www.xiaohongshu.com/user/profile/'+'7'*24},
                                         {'url':'https://www.xiaohongshu.com/user/profile/'+'8'*24}])]:
            test=PresenceTests();test.FORK=self.FORK;test.setUp()
            try:
                capture=test.directory/'capture.json';raw=json.loads(capture.read_text());raw['result'][field]=value;capture.write_text(json.dumps(raw))
                failure=test.directory/'failure.json';raw=json.loads(failure.read_text());raw['observations']['capture']=current.nr.freeze(test.p,capture)[0];failure.write_text(json.dumps(raw))
                execution=json.loads(test.execution.read_text());execution['failure']['sha256']=hashlib.sha256(failure.read_bytes()).hexdigest();test.execution.write_text(json.dumps(execution))
                with self.subTest(field=field):test.reject()
            finally:test.tearDown()


class ForkPresenceTests(PresenceTests):
    FORK=True
    def test_parent_authority_is_revalidated(self):
        self.attest();b=bridge.load_binding(self.p.path,'test');b['authorization']='changed';bridge.save_binding(self.p.path,'test',b);self.base.blocked()

if __name__=='__main__':unittest.main()
