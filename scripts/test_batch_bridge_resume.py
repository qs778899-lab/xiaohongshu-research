"""Offline reviewed-scope quota and returned bridge checkpoint regressions."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import xhs_batch as batch
import xhs_bridge as bridge
import xhs_project as xp
import xhs_native as native
import test_bridge as fixture


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.BridgeTests(methodName='runTest');self.fixture.setUp();self.p=self.fixture.project
        task=self.p.task(self.fixture.tid);task['payload']['stage']='roots'
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task['id']));self.p.db.commit()
        bridge.store_capture(self.p,self.p.task(task['id']),{'url':fixture.URL,'results':[{'commentId':'1','text':'Actually read synthetic comment','parentCommentId':''}]})
        task=self.p.task(task['id']);pr=task['payload']['bridge']
        receipt={'target':fixture.NOTE,'action':'read','outcome':'ready','observed_at':batch.legacy.now(),
            'summary':'Actually read the one saved comment; pagination remains pending.','gaps':['pagination pending'],
            'source_file':pr['last_snapshot'],'url':fixture.URL,'target_verified':True,'content_ready':True,
            'view_consistent':True,'reviewed':True,'complete':False,'positions':['comments'],
            'bridge_snapshot_hashes':pr['hashes']}
        f=self.fixture.root/'quota-review.json';batch.legacy.write_json(f,receipt);bridge.review(self.p,task['id'],f)
        self.task=self.p.task(task['id']);self.state={'active_group':fixture.NOTE,'active_note':fixture.NOTE,'group_units':10}
    def tearDown(self):self.fixture.tearDown()
    def select(self,task=None,state=None,ids=None):
        return batch.select_ready([task or self.task],self.state if state is None else state,ids or [self.task['id']])
    def test_real_hash_review_renews_explicit_same_note_scheduling_quota(self):
        before=copy.deepcopy(self.task);self.select();self.assertEqual(self.state['group_units'],0)
        self.assertEqual(self.task,before);self.assertTrue(self.task['payload']['bridge']['last_review_event'])
    def test_reproduced_caller_counter_can_run_ten_after_review_not_one(self):
        # The unchanged bridge.work increments and hands off exactly this way.
        done=0
        while done<10:
            task=self.select();group=batch.routing_group(task)
            self.state['group_units']=self.state.get('group_units',0)+1 if self.state.get('active_group')==group else 1
            self.state['active_group']=group;self.state['active_note']=task['payload']['target'];done+=1
            if self.state['group_units']>=10:break
        self.assertEqual(done,10)
    def test_unread_empty_missing_or_diagnostic_only_context_never_renews(self):
        for changes in [{'hashes':[]},{'reviewed_hashes':[]},{'awaiting_review':True},
                        {'awaiting_review':None},{'last_review_event':None},
                        {'hashes':['new-unread-hash']},{'diagnostic_hashes':['new-unread-hash'],'reviewed_hashes':[]}]:
            task=copy.deepcopy(self.task);task['payload']['bridge'].update(changes);state=copy.deepcopy(self.state)
            batch.select_ready([task],state,[task['id']]);self.assertEqual(state['group_units'],10)
    def test_counter_only_changes_not_stall_hold_cursor_or_review_state(self):
        task=copy.deepcopy(self.task);pr=task['payload']['bridge'];pr.update(stalled_count=3,cursor={'scrollTop':120},fault={'code':'COMMENT_PAGINATION_STALLED','hold':False})
        before=copy.deepcopy(task);self.select(task);self.assertEqual(self.state['group_units'],0);self.assertEqual(before,task)
        task['payload']['bridge']['fault']['hold']=True;state={**self.state,'group_units':10}
        batch.select_ready([task],state,[task['id']]);self.assertEqual(state['group_units'],10)
    def test_no_early_reset_and_no_cross_note_or_group_reset(self):
        for state in [{**self.state,'group_units':9},{**self.state,'active_note':'elsewhere'},
                      {**self.state,'active_group':'elsewhere'}]:
            before=copy.deepcopy(state);self.select(state=state);self.assertEqual(before,state)
    def test_default_doctor_rotation_and_explicit_order_unchanged(self):
        a=copy.deepcopy(self.task);a['payload']['routing_doctors']=['A'];z=copy.deepcopy(self.task)
        z['id']='other';z['payload']['target']='other-note';z['payload']['routing_doctors']=['B'];z['turn']=100
        state={'active_group':'A','active_note':fixture.NOTE,'group_units':10};before=copy.deepcopy(state)
        self.assertEqual(batch.select_ready([a,z],state)['id'],z['id']);self.assertEqual(state,before)
        self.assertEqual(batch.select_ready([a,z],state,[z['id'],a['id']])['id'],z['id'])
        self.assertEqual(state,before)
    def test_explicit_multiple_comment_stages_same_note_can_renew(self):
        replies=copy.deepcopy(self.task);replies['id']='reply-stage';replies['payload']['stage']='replies'
        selected=batch.select_ready([self.task,replies],self.state,[replies['id'],self.task['id']])
        self.assertEqual(selected['id'],replies['id']);self.assertEqual(self.state['group_units'],0)
    def test_explicit_multiple_notes_keep_order_and_do_not_renew_single_note_quota(self):
        other=copy.deepcopy(self.task);other['id']='other-note';other['payload']['target']='other-note'
        before=copy.deepcopy(self.state)
        chosen=batch.select_ready([self.task,other],self.state,[self.task['id'],other['id']])
        self.assertEqual(chosen['id'],self.task['id']);self.assertEqual(self.state,before)
    def test_other_transport_or_unsupported_stage_does_not_renew(self):
        for field,value in [('kind','comments'),('status','blocked')]:
            task=copy.deepcopy(self.task);task[field]=value;state=copy.deepcopy(self.state)
            batch.select_ready([task],state,[task['id']]);self.assertEqual(state['group_units'],10)
        for stage in ['body','images','identity','video']:
            task=copy.deepcopy(self.task);task['payload']['stage']=stage;state=copy.deepcopy(self.state)
            batch.select_ready([task],state,[task['id']]);self.assertEqual(state['group_units'],10)


class ListingQuotaTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.BridgeTests(methodName='runTest');self.fixture.setUp();self.p=self.fixture.project
    def tearDown(self):self.fixture.tearDown()
    def listing(self,stage='search',sort='general',target=None):
        target=target or ('钢牙隐形经历' if stage=='search' else 'b'*24)
        url=('https://www.xiaohongshu.com/search_result?keyword='+target if stage=='search'
             else 'https://www.xiaohongshu.com/user/profile/'+target)
        source=self.fixture.root/(stage+'-'+sort+'-'+str(len(list(self.fixture.root.glob('listing-*'))))+'.json')
        source.write_text(json.dumps({'url':url,'cards':[{'url':fixture.URL,'text':'Observed card'}]}))
        manifest=self.fixture.root/('listing-'+source.name)
        unit={'stage':stage,'target':target,'sources':[str(source)],'next':'Read next observed listing page'}
        if stage=='search':unit['sort']=sort
        manifest.write_text(json.dumps({'units':[unit]}));tid=native.import_manifest(self.p,manifest)['task_ids'][0]
        bridge.store_capture(self.p,self.p.task(tid),{'stage':stage,'url':url,'cards':[{'url':fixture.URL,'text':'Observed card'}],
                             'explicitEnd':False,'position':{'scrollTop':0,'scrollHeight':1000,'viewportHeight':500}})
        task=self.p.task(tid);pr=task['payload']['bridge']
        receipt={'target':target,'action':'read','outcome':'ready','observed_at':batch.legacy.now(),
            'summary':'Actually read this archived listing; more pages remain.','gaps':['pagination pending'],
            'source_file':pr['last_snapshot'],'url':url,'target_verified':True,'content_ready':True,
            'view_consistent':True,'reviewed':True,'complete':False,'positions':['cards'],
            'bridge_snapshot_hashes':pr['hashes']}
        file=self.fixture.root/('review-'+tid+'.json');batch.legacy.write_json(file,receipt);bridge.review(self.p,tid,file)
        return self.p.task(tid)
    def state(self,task):return {'active_group':batch.routing_group(task),'active_note':task['payload']['target'],'group_units':10}
    def test_actual_reviewed_search_scope_renews_after_checkpoint(self):
        task=self.listing();before=copy.deepcopy(task);state=self.state(task)
        self.assertEqual(batch.select_ready([task],state,[task['id']]),task);self.assertEqual(state['group_units'],0)
        self.assertEqual(task,before);self.assertTrue(task['payload']['bridge']['last_review_event'])
    def test_actual_reviewed_author_scope_renews_after_checkpoint(self):
        task=self.listing('author');state=self.state(task);before=copy.deepcopy(task)
        batch.select_ready([task],state,[task['id']]);self.assertEqual(state['group_units'],0);self.assertEqual(task,before)
    def test_listing_unread_hashes_or_missing_review_proof_never_reset(self):
        for stage in ['search','author']:
            original=self.listing(stage)
            for changes in [{'hashes':[]},{'reviewed_hashes':[]},{'hashes':original['payload']['bridge']['hashes']+['new-unread']},
                            {'last_review_event':None},{'awaiting_review':True},{'awaiting_review':None},
                            {'fault':{'code':'PAGINATION_STALLED','hold':True}}]:
                task=copy.deepcopy(original);task['payload']['bridge'].update(changes);state=self.state(task)
                batch.select_ready([task],state,[task['id']]);self.assertEqual(state['group_units'],10)
    def test_same_query_different_sorts_do_not_reset_and_keep_explicit_order(self):
        general=self.listing(sort='general');latest=self.listing(sort='latest');state=self.state(general)
        chosen=batch.select_ready([general,latest],state,[general['id'],latest['id']])
        self.assertEqual(chosen['id'],general['id']);self.assertEqual(state['group_units'],10)
    def test_even_same_entity_key_does_not_merge_different_search_sorts(self):
        general=self.listing();latest=copy.deepcopy(general);latest['id']='different-sort';latest['payload']['sort']='latest'
        state=self.state(general);batch.select_ready([general,latest],state,[general['id'],latest['id']])
        self.assertEqual(state['group_units'],10)
    def test_listing_requires_same_nonempty_entity_stage_and_target(self):
        for stage in ['search','author']:
            original=self.listing(stage)
            for changes in [{'entity_key':''},{'entity_key':None}]:
                task=copy.deepcopy(original);task.update(changes);state=self.state(task)
                batch.select_ready([task],state,[task['id']]);self.assertEqual(state['group_units'],10)
            for field,value in [('entity_key','different-scope'),('payload',dict(original['payload'],target='other')),
                                ('payload',dict(original['payload'],stage='body'))]:
                other=copy.deepcopy(original);other['id']='other';other[field]=value;state=self.state(original)
                batch.select_ready([original,other],state,[original['id'],other['id']]);self.assertEqual(state['group_units'],10)
    def test_search_sort_must_be_explicit_and_valid(self):
        original=self.listing()
        for value in [None,'','unknown']:
            task=copy.deepcopy(original);task['payload']['sort']=value;state=self.state(task)
            batch.select_ready([task],state,[task['id']]);self.assertEqual(state['group_units'],10)
    def test_listing_default_rotation_and_collection_faults_remain_unchanged(self):
        task=self.listing();other=self.listing(target='other-query');state=self.state(task);before=copy.deepcopy(state)
        self.assertEqual(batch.select_ready([task,other],state)['id'],other['id']);self.assertEqual(state,before)
        task['payload']['bridge'].update(stalled_count=2,cursor={'scrollTop':500},fault={'code':'PAGINATION_STALLED','hold':False})
        before=copy.deepcopy(task);batch.select_ready([task],state,[task['id']]);self.assertEqual(state['group_units'],0)
        self.assertEqual(task,before)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.list=self.root/'tasks.json'
        self.list.write_text(json.dumps(['second','first']));self.checkpoint=self.root/'actual-result.json';self.calls=[]
    def tearDown(self):self.tmp.cleanup()
    def run_cli(self,result,extra=(),terminal_emit=True,command='run'):
        def fake(project,binding,**kwargs):
            self.calls.append((project,binding,kwargs))
            kwargs['emit']({'event':'unit','task_id':'second','snapshot':'real-saved-id'})
            if terminal_emit:kwargs['emit'](result)
            return result
        argv=['xhs_batch.py','--project',str(self.root/'project'),command,'--transport','bridge','--binding','chosen',
              '--task-list',str(self.list),'--units','3','--max-seconds','90','--interval','4','--search-pages','7',
              '--review-limit','13','--round','round-one','--home-restore-authorization','Existing authorization',*extra]
        output=io.StringIO()
        with patch('sys.argv',argv),patch.object(bridge,'work',side_effect=fake),redirect_stdout(output):batch.main()
        return [json.loads(line) for line in output.getvalue().splitlines()]
    def test_final_return_checkpoint_and_one_final_line_keep_unit_events_and_order(self):
        result={'event':'handoff','reason':'codex_reading_checkpoint','processed_units':3,
                'metrics':{'calls':9,'processes':1},'wall_seconds':12.5,'extra_return_field':'保留'}
        lines=self.run_cli(result,['--checkpoint-file',str(self.checkpoint)])
        self.assertEqual(lines[-1],result);self.assertEqual(len(lines),2)
        self.assertEqual(json.loads(self.checkpoint.read_text()),result)
        project,binding,kw=self.calls[0];self.assertEqual(project,self.root/'project');self.assertEqual(binding,'chosen')
        self.assertEqual(kw['task_ids'],['second','first']);self.assertEqual(kw['units'],3)
        self.assertEqual((kw['max_seconds'],kw['interval'],kw['search_pages'],kw['review_limit'],kw['round_id']),
                         (90,4,7,13,'round-one'));self.assertEqual(kw['home_restore_authorization'],'Existing authorization')
    def test_zero_unit_blocked_result_preserves_reason_metrics(self):
        result={'event':'blocked','reason':'SCOPE_CHANGED','processed_units':0,'metrics':{'calls':1,'processes':1}}
        lines=self.run_cli(result,['--checkpoint-file',str(self.checkpoint)],terminal_emit=False)
        self.assertEqual(lines[-1],result);self.assertEqual(json.loads(self.checkpoint.read_text()),result)
    def test_work_multiplier_and_final_output_without_checkpoint(self):
        result={'event':'checkpoint','reason':'time_budget','processed_units':0}
        lines=self.run_cli(result,['--max-batches','2'],command='work');self.assertEqual(lines[-1],result)
        self.assertEqual(self.calls[0][2]['units'],6);self.assertFalse(self.checkpoint.exists())
    def test_returned_error_result_is_saved_without_rewriting_fields(self):
        result={'event':'error','reason':'Typed collector failure','processed_units':0,'metrics':{'calls':0},'failure':{'code':'TEST'}}
        lines=self.run_cli(result,['--checkpoint-file',str(self.checkpoint)])
        self.assertEqual(lines[-1],result);self.assertEqual(json.loads(self.checkpoint.read_text()),result)
    def test_existing_checkpoint_evidence_is_never_overwritten_or_run(self):
        self.checkpoint.write_bytes(b'original evidence')
        with self.assertRaises(batch.legacy.ResearchError):self.run_cli({'processed_units':0},['--checkpoint-file',str(self.checkpoint)])
        self.assertEqual(self.calls,[]);self.assertEqual(self.checkpoint.read_bytes(),b'original evidence')

if __name__=='__main__':unittest.main()
