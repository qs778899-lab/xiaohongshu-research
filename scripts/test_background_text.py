"""Background DOM execution, without relaxing visual or identity contracts."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch,Mock
import xhs_bridge as b
import xhs_project as xp
import xhs_native as native
from test_bridge import Fake, VisibilityFake, UID, NOTE, URL


class BackgroundTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.project=xp.Project(self.root/'project',create=True)
        source=self.root/'source.json';source.write_text(json.dumps({'url':URL}))
        manifest=self.root/'manifest.json'
        manifest.write_text(json.dumps({'units':[
            {'stage':stage,'target':NOTE,'url':URL,'sources':[str(source)],'next':'read'}
            for stage in ['body','roots']]}))
        self.ids=native.import_manifest(self.project,manifest)['task_ids']
        self.binding={'binding_name':'test','context_id':'synthetic-context',
            'session':'xhs-synthetic','account_uid':UID,'background_text_collection':True,
            'tabs':{'detail':{'page':'synthetic-page','owned':True,'last_url':URL}}}
        self.body=self.project.task(self.ids[0])

    def tearDown(self):
        self.project.close();self.tmp.cleanup()

    def test_hidden_body_can_acquire_real_text_without_focus_or_process_activation(self):
        driver=VisibilityFake()
        with patch.object(b,'save_binding'),patch.object(b.subprocess,'run',side_effect=AssertionError('OS activation')):
            result=b.collect_one(self.project,self.binding,driver,self.body)
        self.assertTrue(Path(result['snapshot']).is_file())
        self.assertIn('body',json.loads(Path(result['snapshot']).read_text()))
        self.assertNotIn('activate',[x[0] for x in driver.calls])
        captures=[x[3] for x in driver.calls if x[0]=='capture']
        self.assertTrue(captures and all(x['backgroundText'] is True for x in captures))
        self.assertNotEqual(self.project.task(self.ids[0])['status'],'complete')

    def test_all_and_only_dom_text_stages_are_opted_in(self):
        for stage in ['search','body','roots','replies','author']:
            self.assertTrue(b.background_text_allowed(self.binding,stage))
        for stage in ['images','video','identity','media','unknown']:
            self.assertFalse(b.background_text_allowed(self.binding,stage))

    def test_legacy_and_nonboolean_mode_keep_foreground_contract(self):
        for value in [None,False,'true',1]:
            binding=dict(self.binding,background_text_collection=value)
            driver=VisibilityFake()
            with self.assertRaisesRegex(b.BridgeError,'TARGET_NOT_VISIBLE'):
                b.prepare_target(self.project,binding,driver,self.body,text_only=True)
            self.assertEqual([x[0] for x in driver.calls].count('activate'),1)

    def test_media_using_a_body_task_does_not_inherit_text_purpose(self):
        driver=VisibilityFake()
        with self.assertRaisesRegex(b.BridgeError,'TARGET_NOT_VISIBLE'):
            b.prepare_target(self.project,self.binding,driver,self.body)
        self.assertIn('activate',[x[0] for x in driver.calls])

    def test_wrong_account_still_rejected_before_capture(self):
        driver=VisibilityFake(uid='9'*24)
        with self.assertRaisesRegex(b.BridgeError,'ACCOUNT_MISMATCH'):
            b.prepare_target(self.project,self.binding,driver,self.body,text_only=True)
        self.assertFalse(any(x[0] in {'activate','capture','deep'} for x in driver.calls))

    def test_wrong_registered_url_is_not_adopted_in_background(self):
        driver=VisibilityFake();driver.url=URL.replace(NOTE,'9'*24)
        with self.assertRaisesRegex(b.BridgeError,'UNEXPECTED_TARGET_CHANGE'):
            b.prepare_target(self.project,self.binding,driver,self.body,text_only=True)
        self.assertFalse(any(x[0] in {'navigate','activate','capture'} for x in driver.calls))

    def test_unknown_document_visibility_is_not_accepted(self):
        driver=VisibilityFake();driver.visible=None
        with self.assertRaisesRegex(b.BridgeError,'TARGET_VISIBILITY_UNCONFIRMED'):
            b.prepare_target(self.project,self.binding,driver,self.body,text_only=True)

    def test_access_stop_is_not_a_background_loading_error(self):
        class Restricted(VisibilityFake):
            def call(self,action,*args,**kwargs):
                if action=='identity':raise b.BridgeError('ACCESS_STOP')
                return super().call(action,*args,**kwargs)
        driver=Restricted()
        with self.assertRaisesRegex(b.BridgeError,'ACCESS_STOP'):
            b.prepare_target(self.project,self.binding,driver,self.body,text_only=True)
        self.assertFalse(any(x[0] in {'activate','capture','deep','reload'} for x in driver.calls))

    def test_bounded_loading_observation_reloads_once_without_selecting_tab(self):
        class Loading(VisibilityFake):
            def __init__(self):super().__init__();self.captures=0;self.reloads=0
            def call(self,action,*args,**kwargs):
                if action=='capture':
                    self.captures+=1
                    if self.captures<=3:raise b.BridgeError('CONTENT_LOADING')
                if action=='reload':self.reloads+=1;return {'reloadSubmitted':True}
                return super().call(action,*args,**kwargs)
        driver=Loading()
        with patch.object(b,'save_binding'),patch.object(b.time,'sleep'):
            data=b.ready_capture(self.project,self.binding,driver,'detail',
                                 {'stage':'body','expectedPath':'/explore/'+NOTE,'backgroundText':True})
        self.assertTrue(data['body']);self.assertEqual(driver.captures,4)
        self.assertEqual(driver.reloads,1);self.assertNotIn('activate',[x[0] for x in driver.calls])

    def test_background_deep_flag_is_invocation_local_not_stored_authority(self):
        class Deep(VisibilityFake):
            def call(self,action,binding=None,role=None,**fields):
                if action=='deep':
                    self.calls.append((action,binding['context_id'],role,fields))
                    return {'results':[],'deepCoverage':{'threads':[]}}
                return super().call(action,binding,role,**fields)
        driver=Deep()
        with patch.object(b,'store_capture',return_value={'snapshot':'synthetic','awaiting_review':True}),patch.object(b.legacy,'write_json'):
            b.collect_one(self.project,self.binding,driver,self.project.task(self.ids[1]))
        fields=[x[3] for x in driver.calls if x[0]=='deep'][0]
        self.assertIs(fields['backgroundText'],True)
        self.assertNotIn('backgroundText',fields['progress'])
        self.assertNotIn('activate',[x[0] for x in driver.calls])

    def test_pagination_recovery_does_not_reintroduce_activation_in_background(self):
        for background in [True,False]:
            driver=Mock();task={'id':'search-task','payload':{'stage':'search','target':'synthetic query',
                'sort':'general','bridge':{'fault':{'recovery':{}},'cursor':{'scrollTop':15}}}}
            project=SimpleNamespace(path=self.project.path,db=Mock(),
                task=Mock(return_value={'id':'search-task','payload':{'bridge':{}}}))
            args={'stage':'search','query':'synthetic query','expectedPath':'/search_result/'}
            if background:args['backgroundText']=True
            with patch.object(b,'prepare_target',return_value=('synthetic','search',args)),\
                    patch.object(b,'ready_capture',return_value={'selected':['综合']}),\
                    patch.object(b,'store_capture'),patch.object(b.legacy,'write_json'),patch.object(b.time,'sleep'):
                result=b.repair_pagination(project,self.binding,driver,task)
            actions=[x.args[0] for x in driver.call.call_args_list]
            self.assertTrue(result['recovered'])
            self.assertEqual('activate' in actions,not background)
            self.assertEqual('reload' in actions,background)
            self.assertEqual('activate' in task['payload']['bridge']['fault']['recovery'],not background)


if __name__=='__main__':unittest.main()
