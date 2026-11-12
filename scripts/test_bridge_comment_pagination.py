"""A persistent root-scroll stall is a local gap, never an end or endless retry."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import xhs_bridge as b
import xhs_native as native
import xhs_research as legacy
import xhs_project as xp
from xhs_browser import merge_browser_progress
import test_bridge_comment_local_fix as local_fix
import test_bridge as fixture

ROOT=local_fix.ROOT
REPLY=local_fix.REPLY

class CommentPaginationStall(unittest.TestCase):
    def setUp(self):
        self.c=local_fix.CommentLocalFix();self.c.setUp();self.p=self.c.p
        task=self.p.task(self.c.f.tid);task['payload']['bridge'].pop('fault')
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task['id']));self.p.db.commit()
        self.stalled=copy.deepcopy(self.c.snapshot)
        self.stalled['deepCoverage'].update(rootPaginationState='stalled',stalledScrollRounds=3,
            rootScrollProgress={'state':'stalled','attempts':3,'stalledRounds':3,'pendingIntent':None,
                'lastObservation':{'state':'no_new_observed_root_ids'}})
        self.capture=b.store_capture(self.p,self.p.task(task['id']),self.stalled)
        self.c.original_fault=copy.deepcopy(self.c.progress()['fault'])

    def tearDown(self):self.c.tearDown()

    def write_shared(self):
        path=self.p.path/'bridge'/'comment-progress'/(fixture.NOTE+'.json')
        value=merge_browser_progress({}, {'coverage':self.stalled['deepCoverage'],'data':self.stalled['results']},self.capture['snapshot'])
        legacy.write_json(path,value);return path

    def import_stage(self,stage,target=fixture.NOTE):
        file=self.c.f.root/(stage+'-manifest.json')
        file.write_text(json.dumps({'units':[{'stage':stage,'target':target,'sources':[str(self.c.f.source)],'next':'read'}]}))
        return native.import_manifest(self.p,file)['task_ids'][0]

    def test_raw_stall_is_local_hold_and_not_completion_or_account_stop(self):
        progress=self.c.progress();self.assertEqual(progress['fault']['code'],'COMMENT_PAGINATION_STALLED')
        self.assertTrue(progress['fault']['hold']);self.assertFalse(progress['collection_end'])
        self.assertEqual(self.p.task(self.c.f.tid)['status'],'pending')
        self.assertEqual(Path(progress['fault']['source_snapshot']).read_text(),Path(self.capture['snapshot']).read_text())
        self.assertFalse(list(self.c.f.guard.rglob('STOP.json')))

    def test_stall_does_not_replace_existing_fault_or_erase_acquisition(self):
        b.hold_task(self.p,self.c.f.tid,'DEEP_READ_FAILED');old=copy.deepcopy(self.c.progress()['fault'])
        hashes=list(self.c.progress()['hashes'])
        b.store_capture(self.p,self.p.task(self.c.f.tid),self.stalled)
        self.assertEqual(self.c.progress()['fault'],old);self.assertEqual(self.c.progress()['hashes'],hashes)

    def test_reading_actual_snapshot_does_not_remove_local_fault(self):
        progress=self.c.progress()
        receipt={'target':fixture.NOTE,'action':'read','outcome':'ready','observed_at':legacy.now(),
            'summary':'Read all archived text; root pagination is still stalled.',
            'gaps':['root pagination stalled'],'source_file':progress['last_snapshot'],'url':fixture.URL,
            'target_verified':True,'content_ready':True,'view_consistent':True,'reviewed':True,
            'complete':False,'positions':['all captured comment text'],'bridge_snapshot_hashes':progress['hashes']}
        file=self.c.f.root/'stall-review.json';legacy.write_json(file,receipt)
        result=b.review(self.p,self.c.f.tid,file)
        self.assertEqual(self.c.progress()['fault'],self.c.original_fault)
        self.assertEqual(result['status'],'pending');self.assertFalse(result['awaiting_review'])
        self.assertFalse(self.c.progress()['collection_end'])

    def test_shared_stall_is_registered_on_new_sibling_without_fake_acquisition(self):
        self.write_shared();sibling=self.import_stage('roots')
        task=b.sync_shared_comment_stall(self.p,self.p.task(sibling));progress=task['payload']['bridge']
        self.assertEqual(progress['fault']['code'],'COMMENT_PAGINATION_STALLED')
        self.assertTrue(progress['fault']['inherited_shared_stall']);self.assertFalse(progress['collection_end'])
        self.assertNotIn('hashes',progress);self.assertEqual(task['status'],'pending')

    def test_work_skips_stall_and_rotates_to_healthy_task_without_search_recovery(self):
        healthy=self.import_stage('body','8'*24);calls=[]
        def collect(db,binding,driver,task):
            calls.append(task['id'])
            return b.store_capture(db,task,{'url':fixture.URL.replace(fixture.NOTE,'8'*24),'body':'Other task text'})
        with patch.object(b,'collect_one',collect),patch.object(b,'repair_pagination') as recovery:
            result=b.work(self.p.path,'test',units=2,max_seconds=30,interval=3,task_ids=[self.c.f.tid,healthy])
        self.assertEqual(calls,[healthy]);recovery.assert_not_called()
        self.assertEqual(result['processed_units'],1);self.assertEqual(self.c.progress()['fault'],self.c.original_fault)

    def test_work_first_sees_sibling_shared_stall_and_skips_its_network(self):
        self.write_shared();sibling=self.import_stage('roots');healthy=self.import_stage('body','8'*24);calls=[]
        def collect(db,binding,driver,task):
            calls.append(task['id'])
            return b.store_capture(db,task,{'url':fixture.URL.replace(fixture.NOTE,'8'*24),'body':'Other task text'})
        with patch.object(b,'collect_one',collect):
            b.work(self.p.path,'test',units=1,max_seconds=30,interval=3,task_ids=[sibling,healthy])
        self.assertEqual(calls,[healthy])
        self.assertEqual(self.p.task(sibling)['payload']['bridge']['fault']['code'],'COMMENT_PAGINATION_STALLED')

    def test_shared_source_hash_mutation_cannot_be_used_as_stall_proof(self):
        self.write_shared();sibling=self.import_stage('roots')
        Path(self.capture['snapshot']).write_text('{}')
        with self.assertRaisesRegex(b.BridgeError,'IMMUTABLE_SOURCE_CHANGED'):
            b.sync_shared_comment_stall(self.p,self.p.task(sibling))
        self.assertNotIn('bridge',self.p.task(sibling)['payload'])

    def discovered_reply_task(self):
        parent=self.p.task(self.c.f.tid);parent['payload']['stage']='roots'
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(parent['payload']),parent['id']));self.p.db.commit()
        self.write_shared()
        return self.import_stage('replies')

    def test_reply_dependency_probe_can_start_without_fake_reply_failure(self):
        sibling=self.discovered_reply_task();self.assertNotIn('bridge',self.p.task(sibling)['payload'])
        root_fault=copy.deepcopy(self.c.progress()['fault']);markers=[]
        def collect(db,binding,driver,task,*,discovered_reply_probe=None,**kw):
            markers.append(copy.deepcopy(discovered_reply_probe))
            value=copy.deepcopy(self.stalled)
            value['results'].append({'commentId':REPLY,'parentCommentId':ROOT,'authorId':'6'*24,
                'is_reply':True,'text':'真实新子回复'})
            value['deepCoverage']['discoveredReplyScope']={**discovered_reply_probe,'state':'visible_controls_pending',
                'parentPaginationAttempted':False,'rootScrollsSubmitted':0}
            return b.store_capture(db,task,value)
        with patch.object(b,'collect_one',collect):
            result=b.verify_local_fix(self.p.path,'test',sibling,'Separate known reply reads from failed parent pagination')
        self.assertTrue(result['repair_verified']);self.assertEqual(len(markers),1)
        self.assertEqual(markers[0]['rootIds'],[ROOT]);self.assertEqual(markers[0]['maxRootScrolls'],0)
        child=self.p.task(sibling)['payload']['bridge']
        self.assertEqual(child['discovered_reply_scope']['state'],'verified')
        self.assertEqual(next(iter(child['repair_probes'].values()))['prior_fault'],None)
        self.assertNotIn('fault',child);self.assertFalse(child['collection_end'])
        self.assertEqual(self.c.progress()['fault'],root_fault)

    def invoke_discovered(self,values,legacy_inherited=False,state='visible_controls_pending'):
        sibling=self.discovered_reply_task()
        if legacy_inherited:
            task=self.p.task(sibling);fault=copy.deepcopy(self.c.progress()['fault']);fault['inherited_shared_stall']=True
            task['payload']['bridge']={'fault':fault,'collection_end':False}
            self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),sibling));self.p.db.commit()
        values=iter(values);markers=[]
        def collect(db,binding,driver,task,*,discovered_reply_probe=None,**kw):
            markers.append(copy.deepcopy(discovered_reply_probe));value=copy.deepcopy(next(values))
            value['deepCoverage']['discoveredReplyScope']={**discovered_reply_probe,'state':state,
                'parentPaginationAttempted':False,'rootScrollsSubmitted':0}
            return b.store_capture(db,task,value)
        with patch.object(b,'collect_one',collect):
            result=b.verify_local_fix(self.p.path,'test',sibling,'Read already discovered parent replies only')
        return sibling,result,markers

    def test_new_parent_counts_or_time_do_not_authorise_reply_phase(self):
        for kind in ['parent','count','time']:
            with self.subTest(kind=kind):
                value=copy.deepcopy(self.stalled)
                if kind=='parent':value['results'].append({'commentId':'8'*24,'is_reply':False,'text':'新父楼'})
                elif kind=='count':value['commentTotal']=999
                else:value['results'][0]['time']='4小时前'
                sibling,result,markers=self.invoke_discovered([value,value])
                self.assertFalse(result['repair_verified']);self.assertEqual(len(markers),2)
                child=self.p.task(sibling)['payload']['bridge']
                self.assertNotIn('fault',child);self.assertNotIn('discovered_reply_scope',child)
                self.assertEqual(self.c.progress()['fault'],self.c.original_fault)
                self.assertEqual(next(iter(child['repair_probes'].values()))['prior_fault'],None)
                # Each subcase gets another real scope source, not an unbounded same-probe retry.
                if kind!='time':
                    self.tearDown();self.setUp()

    def test_unknown_parent_reply_cannot_authorise_known_scope(self):
        value=copy.deepcopy(self.stalled)
        value['results'].append({'commentId':REPLY,'parentCommentId':'8'*24,'authorId':'6'*24,
            'is_reply':True,'text':'其他父楼的回复'})
        sibling,result,_=self.invoke_discovered([value,value])
        self.assertFalse(result['repair_verified']);self.assertFalse(self.p.task(sibling)['payload']['bridge']['collection_end'])

    def test_old_inherited_fault_is_archived_only_after_child_progress(self):
        value=copy.deepcopy(self.stalled)
        value['results'].append({'commentId':REPLY,'parentCommentId':ROOT,'authorId':'6'*24,
            'is_reply':True,'text':'实际新子回复'})
        sibling,result,_=self.invoke_discovered([value],legacy_inherited=True)
        self.assertTrue(result['repair_verified']);child=self.p.task(sibling)['payload']['bridge']
        self.assertNotIn('fault',child);self.assertEqual(len(child['reply_dependency_fault_history']),1)
        old=child['reply_dependency_fault_history'][0]['fault'];self.assertTrue(old['inherited_shared_stall'])
        self.assertEqual({k:v for k,v in old.items() if k!='inherited_shared_stall'},self.c.original_fault)
        self.assertEqual(self.c.progress()['fault'],self.c.original_fault)

    def test_zero_progress_preserves_old_inherited_fault_and_probe_bound(self):
        sibling,result,_=self.invoke_discovered([self.stalled,self.stalled],legacy_inherited=True)
        child=self.p.task(sibling)['payload']['bridge'];old=copy.deepcopy(child['fault'])
        self.assertFalse(result['repair_verified']);self.assertTrue(old['inherited_shared_stall'])
        self.assertNotIn('discovered_reply_scope',child)
        with self.assertRaisesRegex(b.BridgeError,'REPAIR_PROBE_ALREADY_USED'):
            b.verify_local_fix(self.p.path,'test',sibling,'No automatic same-version retry')
        self.assertEqual(self.p.task(sibling)['payload']['bridge']['fault'],old)

    def test_new_reply_dependency_is_not_mislabelled_as_a_reply_failure(self):
        sibling=self.discovered_reply_task();task=b.sync_shared_comment_stall(self.p,self.p.task(sibling))
        self.assertNotIn('fault',task['payload']['bridge'])
        self.assertEqual(task['payload']['bridge']['root_dependency_gap']['kind'],'parent_pagination_dependency')
        self.assertEqual(self.c.progress()['fault'],self.c.original_fault)

    def reply_baseline(self):
        body={'commentId':REPLY,'parentCommentId':ROOT,'authorId':'6'*24,
            'text':'原有子回复正文','textSource':'rendered_text'}
        self.stalled['results'].append(dict(body,is_reply=True,authorHrefRaw='/user/profile/'+'6'*24))
        self.stalled['deepCoverage']['threads'][0]['replyBodyCaptures']={REPLY:body}
        b.store_capture(self.p,self.p.task(self.c.f.tid),self.stalled)
        return body

    def test_same_id_same_parent_same_uid_actual_body_change_authorises(self):
        self.reply_baseline();value=copy.deepcopy(self.stalled);value['results'][-1]['text']='真正新增的可读正文'
        sibling,result,_=self.invoke_discovered([value])
        self.assertTrue(result['repair_verified']);self.assertEqual(result['new_reply_ids'],[])
        self.assertEqual(result['changed_reply_ids'],[REPLY]);self.assertFalse(result['parent_repair_verified'])
        self.assertEqual(self.c.progress()['fault'],self.c.original_fault)

    def test_changed_uid_cannot_borrow_same_reply_id(self):
        self.reply_baseline();value=copy.deepcopy(self.stalled)
        value['results'][-1].update(authorId='7'*24,authorHrefRaw='/user/profile/'+'7'*24,text='另一个身份的文字')
        sibling,result,_=self.invoke_discovered([value,value])
        self.assertFalse(result['repair_verified']);self.assertEqual(result['changed_reply_ids'],[])

    def test_strict_known_restoration_authorises_ui_reading_not_new_research(self):
        body=self.reply_baseline();value=copy.deepcopy(self.stalled)
        value['deepCoverage']['threads'][0]['lastReplyExpansion']={
            'state':'render_progress_observed','progressBasis':'known_reply_render_restored','newResearchEvidence':False,
            'noteId':fixture.NOTE,'rootCommentId':ROOT,'clickedAt':1000,'observedAt':2000,'expanderText':'展开 1 条回复',
            'beforeReplyIds':[],'beforeKnownReplyIds':[REPLY],'beforeReplyBodyCaptures':{},
            'beforeKnownReplyBodyCaptures':{REPLY:body},'observedReplyIds':[REPLY],'restoredReplyIds':[REPLY]}
        sibling,result,_=self.invoke_discovered([value])
        self.assertTrue(result['repair_verified']);self.assertEqual(result['new_reply_ids'],[])
        self.assertEqual(result['changed_reply_ids'],[]);self.assertEqual(len(result['restored_known_replies']),1)
        self.assertFalse(self.p.task(sibling)['payload']['bridge']['collection_end'])
        self.assertEqual(self.c.progress()['fault'],self.c.original_fault)

    def test_unproven_or_finished_marker_cannot_reach_driver(self):
        sibling=self.discovered_reply_task();context=b.discovered_reply_context(self.p,self.p.task(sibling))
        marker=dict(context,phase='probe',probeId='8'*64,acquisition=1)
        with patch.object(b,'prepare_target') as prepare,patch.object(b,'Driver') as driver:
            with self.assertRaisesRegex(b.BridgeError,'DISCOVERED_REPLY_AUTHORITY_REQUIRED'):
                b.collect_one(self.p,self.c.binding,driver,self.p.task(sibling),discovered_reply_probe=marker)
            prepare.assert_not_called();driver.call.assert_not_called()
        with patch.object(b,'prepare_target') as prepare:
            with self.assertRaisesRegex(b.BridgeError,'DISCOVERED_REPLY_PROBE_REQUIRED'):
                b.collect_one(self.p,self.c.binding,None,self.p.task(sibling))
            prepare.assert_not_called()

    def test_successful_probe_enables_normal_work_then_no_controls_stops_scope(self):
        value=copy.deepcopy(self.stalled);value['results'].append({'commentId':REPLY,'parentCommentId':ROOT,
            'authorId':'6'*24,'is_reply':True,'text':'真实新回复'})
        sibling,result,_=self.invoke_discovered([value]);self.assertTrue(result['repair_verified'])
        child=self.p.task(sibling);child['payload']['bridge']['awaiting_review']=False
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(child['payload']),sibling));self.p.db.commit()
        calls=[]
        def collect(db,binding,driver,task):
            calls.append(task['id']);marker=b.active_discovered_reply_scope(db,task)
            data=copy.deepcopy(value);data['deepCoverage']['discoveredReplyScope']={**marker,'state':'no_current_safe_controls',
                'parentPaginationAttempted':False,'rootScrollsSubmitted':0}
            return b.store_capture(db,task,data)
        with patch.object(b,'collect_one',collect):
            worked=b.work(self.p.path,'test',units=3,max_seconds=30,interval=3,task_ids=[sibling])
        self.assertEqual(calls,[sibling]);self.assertEqual(worked['processed_units'],1)
        child=self.p.task(sibling)['payload']['bridge'];self.assertEqual(child['discovered_reply_gap']['reason'],'no_current_safe_controls')
        self.assertFalse(child['collection_end']);self.assertEqual(self.c.progress()['fault'],self.c.original_fault)

    def test_root_immutable_source_changed_does_not_unlock(self):
        sibling=self.discovered_reply_task();Path(self.capture['snapshot']).write_text('{}')
        with patch.object(b,'collect_one') as collect:
            with self.assertRaisesRegex(b.BridgeError,'IMMUTABLE_SOURCE_CHANGED'):
                b.verify_local_fix(self.p.path,'test',sibling,'No changed root source authority')
            collect.assert_not_called()
        self.assertNotIn('discovered_reply_scope',self.p.task(sibling)['payload'].get('bridge',{}))

    def test_denied_account_lease_cannot_acquire_or_authorise_reply_scope(self):
        sibling=self.discovered_reply_task();root_fault=copy.deepcopy(self.c.progress()['fault'])
        with patch.object(b,'lease',side_effect=b.BridgeError('SHARED_STOP')),patch.object(b,'collect_one') as collect:
            with self.assertRaisesRegex(b.BridgeError,'SHARED_STOP'):
                b.verify_local_fix(self.p.path,'test',sibling,'Account lease remains mandatory')
            collect.assert_not_called()
        child=self.p.task(sibling)['payload']['bridge'];self.assertNotIn('discovered_reply_scope',child)
        self.assertEqual(self.c.progress()['fault'],root_fault)

    def test_progress_with_no_remaining_control_keeps_an_explicit_gap(self):
        value=copy.deepcopy(self.stalled);value['results'].append({'commentId':REPLY,'parentCommentId':ROOT,
            'authorId':'6'*24,'is_reply':True,'text':'刚读到的新回复'})
        sibling,result,_=self.invoke_discovered([value],state='no_current_safe_controls')
        self.assertTrue(result['repair_verified']);child=self.p.task(sibling)['payload']['bridge']
        self.assertEqual(child['discovered_reply_gap']['reason'],'no_current_safe_controls')
        self.assertFalse(child['collection_end']);self.assertEqual(self.c.progress()['fault'],self.c.original_fault)

    def test_own_reply_failure_is_not_relabelled_as_parent_dependency(self):
        sibling=self.discovered_reply_task();b.hold_task(self.p,sibling,'DEEP_READ_FAILED')
        self.assertIsNone(b.discovered_reply_context(self.p,self.p.task(sibling)))
        self.assertIsNone(b.active_discovered_reply_scope(self.p,self.p.task(sibling)))
        self.assertEqual(self.p.task(sibling)['payload']['bridge']['fault']['code'],'DEEP_READ_FAILED')

    def test_finished_known_capture_pass_is_a_gap_not_parent_end(self):
        value=copy.deepcopy(self.stalled);value['results'].append({'commentId':REPLY,'parentCommentId':ROOT,
            'authorId':'6'*24,'is_reply':True,'text':'真实新回复已采集但根没有末尾'})
        sibling,result,_=self.invoke_discovered([value],state='known_parent_visit_pass_finished')
        self.assertTrue(result['repair_verified']);child=self.p.task(sibling)['payload']['bridge']
        self.assertEqual(child['discovered_reply_gap']['reason'],'known_parent_visit_pass_finished')
        self.assertFalse(child['collection_end']);self.assertEqual(self.p.task(sibling)['status'],'pending')
        self.assertEqual(self.c.progress()['fault'],self.c.original_fault)

    def test_yield_or_thread_completion_does_not_repair_root_pagination(self):
        value=copy.deepcopy(self.c.snapshot);thread=value['deepCoverage']['threads'][0]
        thread.update(state='visible_replies_read',expanders=[],scanIncomplete=False)
        thread.pop('pendingReplyExpansion')
        result=self.c.invoke([value,value])
        self.assertFalse(result['repair_verified']);self.assertEqual(self.c.progress()['fault'],self.c.original_fault)

    def test_new_reply_or_changed_text_does_not_repair_root_pagination(self):
        value=self.c.changed();value['results'][0]['text']+='新文字'
        result=self.c.invoke([value,value]);self.assertFalse(result['repair_verified'])
        self.assertGreater(result['new_comment_ids'],0);self.assertEqual(result['new_root_comment_ids'],0)
        self.assertEqual(self.c.progress()['fault'],self.c.original_fault)

    def test_new_rendered_parent_repairs_stall_with_pending_review(self):
        value=self.c.changed();value['results'][-1].pop('parentCommentId');value['results'][-1]['is_reply']=False
        value['deepCoverage']['observedRootIds'].append(REPLY)
        result=self.c.invoke([value]);self.assertTrue(result['repair_verified'])
        self.assertEqual(result['new_root_comment_ids'],1);self.assertNotIn('fault',self.c.progress())
        self.assertEqual(self.p.task(self.c.f.tid)['status'],'pending');self.assertTrue(result['requires_codex_review'])

    def test_latest_stalled_state_prevents_unlock_even_with_new_parent(self):
        value=copy.deepcopy(self.stalled)
        value['results'].append({'commentId':REPLY,'text':'实际新父楼','authorId':'9'*24,'is_reply':False})
        value['deepCoverage']['observedRootIds'].append(REPLY)
        result=self.c.invoke([value,value]);self.assertFalse(result['repair_verified'])
        self.assertEqual(self.c.progress()['fault'],self.c.original_fault);self.assertFalse(self.c.progress()['collection_end'])

    def test_deep_failure_probe_cannot_clear_new_pagination_stall_with_new_text(self):
        b.hold_task(self.p,self.c.f.tid,'DEEP_READ_FAILED');old=copy.deepcopy(self.c.progress()['fault'])
        value=copy.deepcopy(self.stalled);value['results'][0]['text']+='，实际新文字。'
        result=self.c.invoke([value,value]);self.assertFalse(result['repair_verified'])
        self.assertEqual(self.c.progress()['fault'],old)
        self.assertTrue(result['requires_codex_review']);self.assertTrue(self.c.progress()['awaiting_review'])

    def test_explicit_rendered_end_can_resolve_pagination_but_not_mark_task_reviewed(self):
        value=copy.deepcopy(self.c.snapshot);value['deepCoverage'].update(rootPaginationState='complete',explicitEnd=True)
        value['deepCoverage'].pop('checkpoint');value['deepCoverage'].pop('waitHandedToScheduler')
        result=self.c.invoke([value]);self.assertTrue(result['repair_verified'])
        self.assertEqual(self.p.task(self.c.f.tid)['status'],'pending');self.assertTrue(result['requires_codex_review'])

    def test_probe_marker_is_one_scroll_then_observation_only_and_metrics_do_not_unlock(self):
        markers=[]
        value=copy.deepcopy(self.stalled)
        value['deepCoverage']['rootScrollProgress'].update(attempts=4,
            selectedBefore={'scrollTop':0},selectedAfter={'scrollTop':900})
        def collect(db,binding,driver,task,*,root_repair_probe=None):
            markers.append(copy.deepcopy(root_repair_probe))
            return b.store_capture(db,task,value)
        with patch.object(b,'collect_one',collect):
            result=b.verify_local_fix(self.p.path,'test',self.c.f.tid,'verified scroller selection correction')
        self.assertEqual(len(markers),2)
        self.assertEqual([m['allowScroll'] for m in markers],[True,False])
        self.assertEqual([m['acquisition'] for m in markers],[1,2])
        self.assertEqual(len({m['probeId'] for m in markers}),1)
        self.assertTrue(all(m['maxScrolls']==1 and m['noteId']==fixture.NOTE for m in markers))
        self.assertEqual(self.c.sleeps,[10]);self.assertFalse(result['repair_verified'])
        self.assertEqual(self.c.progress()['fault'],self.c.original_fault)
        self.assertEqual(self.p.task(self.c.f.tid)['status'],'pending')

    def test_normal_collection_does_not_reuse_persisted_probe_authority(self):
        path=self.write_shared();shared=legacy.read_json(path,{})
        shared['rootRepairProbe']={'kind':'single_bounded_root_repair_v1','allowScroll':True}
        shared['discoveredReplyScope']={'kind':'discovered_replies_v1','phase':'continuation','maxRootScrolls':0}
        legacy.write_json(path,shared)
        class Driver:
            def call(inner,action,binding,role,**fields):
                self.assertEqual(action,'deep')
                self.assertNotIn('rootRepairProbe',fields['progress'])
                self.assertNotIn('discoveredReplyScope',fields['progress'])
                return copy.deepcopy(self.stalled)
        with patch.object(b,'prepare_target',return_value=(fixture.URL,'detail',{})):
            b.collect_one(self.p,self.c.binding,Driver(),self.p.task(self.c.f.tid))
        self.assertEqual(self.c.progress()['fault'],self.c.original_fault)

    def test_probe_marker_requires_current_formal_attempt_before_driver_action(self):
        marker={'kind':'single_bounded_root_repair_v1','probeId':'b'*64,'noteId':fixture.NOTE,
            'acquisition':1,'maxScrolls':1,'allowScroll':True}
        with patch.object(b,'prepare_target',return_value=(fixture.URL,'detail',{})) as prepare, \
             patch.object(b,'Driver') as driver:
            with self.assertRaisesRegex(b.BridgeError,'ROOT_REPAIR_PROBE_AUTHORITY_REQUIRED'):
                b.collect_one(self.p,self.c.binding,driver,self.p.task(self.c.f.tid),root_repair_probe=marker)
            driver.call.assert_not_called()
            prepare.assert_not_called()

    def test_empty_state_cannot_erase_previously_observed_parent_end_gap(self):
        value={'url':fixture.URL,'results':[],'deepCoverage':{'rootPaginationState':'complete',
            'explicitEnd':False,'observedRootIds':[],'rootEndBasis':'rendered_comment_empty_state',
            'emptyCommentState':{'scope':'rendered_comments_container_empty_state','acceptedAsEnd':True,
                'visible':True,'loadingVisible':False,'text':'暂无评论'},'threads':[]}}
        result=self.c.invoke([value,value])
        self.assertFalse(result['repair_verified'])
        self.assertEqual(self.c.progress()['fault'],self.c.original_fault)

    def test_finished_probe_attempt_cannot_authorize_another_internal_marker(self):
        task=self.p.task(self.c.f.tid);version='b'*64
        task['payload']['bridge']['repair_probes']={version:{'scope':'same_note_rendered_comments',
            'maximum_acquisitions':2,'status':'failed','observations':[]}}
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task['id']));self.p.db.commit()
        marker={'kind':'single_bounded_root_repair_v1','probeId':version,'noteId':fixture.NOTE,
            'acquisition':1,'maxScrolls':1,'allowScroll':True}
        with patch.object(b,'prepare_target') as prepare:
            with self.assertRaisesRegex(b.BridgeError,'ROOT_REPAIR_PROBE_AUTHORITY_REQUIRED'):
                b.collect_one(self.p,self.c.binding,None,self.p.task(task['id']),root_repair_probe=marker)
            prepare.assert_not_called()

    def test_actual_rendered_empty_proof_can_resolve_a_zero_parent_stall(self):
        note='8'*24;tid=self.import_stage('roots',note)
        url=fixture.URL.replace(fixture.NOTE,note)
        stalled={'url':url,'results':[],'deepCoverage':{'threads':[],'observedRootIds':[],
            'rootPaginationState':'stalled','explicitEnd':False,'stalledScrollRounds':3}}
        b.store_capture(self.p,self.p.task(tid),stalled)
        value={'url':url,'results':[],'deepCoverage':{'threads':[],'observedRootIds':[],
            'rootPaginationState':'complete','explicitEnd':False,'rootEndBasis':'rendered_comment_empty_state',
            'emptyCommentState':{'scope':'rendered_comments_container_empty_state','acceptedAsEnd':True,
                'visible':True,'loadingVisible':False,'text':'暂无评论'},
            'commentCountObservation':{'latest':{'positiveTotalObserved':False}}}}
        with patch.object(b,'collect_one',side_effect=lambda db,binding,driver,task,**kw:b.store_capture(db,task,value)):
            result=b.verify_local_fix(self.p.path,'test',tid,'actual visible comment empty state')
        self.assertTrue(result['repair_verified']);self.assertTrue(result['requires_codex_review'])
        self.assertEqual(self.p.task(tid)['status'],'pending')
        self.assertNotIn('fault',self.p.task(tid)['payload']['bridge'])


if __name__=='__main__':unittest.main()
