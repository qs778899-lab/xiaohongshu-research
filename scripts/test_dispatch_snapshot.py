"""Share advisory health within one dispatch; never cache live access guards."""
import copy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_bridge as bridge
import xhs_native as native
import xhs_project as xp
import xhs_rounds as rounds

NOTE='a'*24
UID='b'*24
PROFILE='c'*64
URL='https://www.xiaohongshu.com/explore/'+NOTE


class Driver:
    def __init__(self):self.metrics={'calls':0,'process_starts':0}
    def close(self):pass


class DispatchSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.guards=self.root/'guards';self.guards.mkdir(mode=0o700)
        self.guard_patch=patch.object(bridge.legacy,'account_root',return_value=self.guards);self.guard_patch.start()
        self.p=xp.Project(self.root/'project',create=True)
        self.source=self.root/'source.json';self.source.write_text(json.dumps({'url':URL,'cards':[{'url':URL}]}))
        manifest=self.root/'manifest.json';manifest.write_text(json.dumps({'units':[
            {'stage':'search','target':'示例机构 测试丙','sort':'general','sources':[str(self.source)],'next':'search'},
            {'stage':'body','target':NOTE,'sources':[str(self.source)],'next':'body'},
            {'stage':'replies','target':NOTE,'sources':[str(self.source)],'next':'replies'}]}))
        self.ids=native.import_manifest(self.p,manifest)['task_ids']
        task=self.p.task(self.ids[0]);task['payload']['bridge']={'hashes':[hashlib.sha256(self.source.read_bytes()).hexdigest()]}
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),self.ids[0]));self.p.db.commit()
        self.legacy=self.p.add_task('search','legacy-route',{'stage':'search','target':'示例机构 测试丁'})
        self.p.db.commit();rounds.create(self.p,'r1',{'cutoff':'2026-11-05T00:00:00+08:00','doctors':['测试丙','测试丁'],'employment_rule':'existing scope'})
    def tearDown(self):self.p.close();self.guard_patch.stop();self.tmp.cleanup()
    def event(self,outcome='access_stop',action='navigate'):
        file=self.root/('event-'+str(self.p.db.execute('select count(*) from native_events').fetchone()[0])+'.json')
        file.write_text(json.dumps({'target':NOTE,'action':action,'outcome':outcome,'observed_at':datetime.now(timezone.utc).isoformat(),
            'source_file':str(self.source),'summary':'Actual synthetic event','gaps':['unresolved access' if outcome=='access_stop' else 'window failure']}))
        native.record(self.p,self.ids[1],file)
    def test_task_and_routes_match_original_results_exactly(self):
        before=[self.p.task(tid) for tid in self.ids];routes=rounds.scheduler_routes(self.p,'r1')
        snapshot=native.dispatch_snapshot(self.p)
        after=[self.p.task(tid,native_dispatch=snapshot) for tid in self.ids]
        optimized=rounds.scheduler_routes(self.p,'r1',tasks=after,native_dispatch=snapshot)
        self.assertEqual(before,after);self.assertEqual(routes,optimized)
        self.assertIn(self.legacy,optimized);self.assertEqual(optimized[self.ids[1]],['测试丙'])
    def test_one_global_health_calculation_and_existing_rows_reused(self):
        real=native.channel_health
        with patch.object(native,'channel_health',wraps=real) as health,patch.object(self.p,'task',wraps=self.p.task) as task:
            snapshot=native.dispatch_snapshot(self.p)
            rows=[self.p.task(tid,native_dispatch=snapshot) for tid in self.ids]
            rounds.scheduler_routes(self.p,'r1',tasks=rows,native_dispatch=snapshot)
        self.assertEqual(health.call_count,1)
        self.assertEqual(task.call_count,len(self.ids)+1) # only missing legacy round member loads once
    def test_new_event_requires_new_dispatch_and_default_task_reader_stays_fresh(self):
        old=native.dispatch_snapshot(self.p)
        self.assertEqual(self.p.task(self.ids[1],native_dispatch=old)['next_action'],'codex_native_ui')
        self.event();new=native.dispatch_snapshot(self.p)
        self.assertFalse(old['health'].get('access',{}).get('hold',False));self.assertTrue(new['health']['access']['hold'])
        self.assertEqual(self.p.task(self.ids[1],native_dispatch=new)['next_action'],'preserve_access_stop')
        self.assertEqual(self.p.task(self.ids[1])['next_action'],'preserve_access_stop')
    def test_non_access_channel_faults_keep_the_same_hint_boundary(self):
        self.event('window_missing','read');self.event('window_missing','read')
        snapshot=native.dispatch_snapshot(self.p)
        self.assertEqual(self.p.task(self.ids[1])['next_action'],'channel_probe_required_no_repeat_action')
        self.assertEqual(self.p.task(self.ids[1],native_dispatch=snapshot),self.p.task(self.ids[1]))
    def test_changed_project_stop_is_not_masked_by_previous_dispatch(self):
        old=native.dispatch_snapshot(self.p);self.assertFalse(old['project_stop_applies'])
        self.p.set_meta('stop',{'reason':'new project access limit'});self.p.db.commit()
        new=native.dispatch_snapshot(self.p);self.assertTrue(new['project_stop_applies'])
        self.assertEqual(self.p.task(self.ids[1],native_dispatch=new)['next_action'],'preserve_access_stop')
    def test_work_rebuilds_snapshot_each_unit_and_lease_sees_new_access_stop(self):
        binding={'binding_name':'test','runtime':{},'state':'ready','profile_guard_key':PROFILE,'account_uid':UID}
        states=[];real=native.dispatch_snapshot;calls=[]
        def dispatch(project):
            state=real(project);states.append(copy.deepcopy(state));return state
        def collect(project,b,driver,task):
            calls.append(task['id']);self.event();return {'duplicate':False}
        with patch.object(bridge,'load_binding',return_value=binding),patch.object(bridge,'Driver',return_value=Driver()), \
             patch.object(native,'dispatch_snapshot',side_effect=dispatch),patch.object(bridge,'collect_one',side_effect=collect), \
             patch.object(bridge,'save_binding'),patch.object(bridge.time,'sleep'):
            result=bridge.work(self.p.path,'test',units=2,max_seconds=20,interval=3,task_ids=[self.ids[1]])
        self.assertEqual(len(states),2);self.assertFalse(states[0]['health'].get('access',{}).get('hold',False))
        self.assertTrue(states[1]['health']['access']['hold']);self.assertEqual(len(calls),1)
        self.assertEqual(result['event'],'blocked');self.assertEqual(result['processed_units'],1)
    def test_wall_seconds_include_dispatch_but_preserve_driver_call_timer(self):
        clock=[0.0]
        binding={'binding_name':'test','runtime':{},'state':'ready'}
        driver=Driver();driver.metrics['elapsed_seconds']=1.696
        def load(*args):clock[0]=10.0;return binding
        real=native.dispatch_snapshot
        def snapshot(project):clock[0]=30.0;return real(project)
        with patch.object(bridge.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(bridge,'load_binding',side_effect=load),patch.object(bridge,'Driver',return_value=driver), \
             patch.object(native,'dispatch_snapshot',side_effect=snapshot):
            result=bridge.work(self.p.path,'test',units=1,max_seconds=100,interval=3,task_ids=[])
        self.assertEqual(result['wall_seconds'],30.0)
        self.assertEqual(result['metrics']['elapsed_seconds'],1.696)
        self.assertEqual(result['driver_metrics_scope'],'driver_call_seconds_only')
        stored=json.loads((self.p.path/'bridge/handoff.json').read_text())
        self.assertEqual(stored['wall_seconds'],30.0)
    def test_old_dispatch_does_not_change_actual_access_guard(self):
        snapshot=native.dispatch_snapshot(self.p);self.event()
        self.assertFalse(snapshot['health'].get('access',{}).get('hold',False))
        with self.assertRaises(xp.legacy.StopRun):bridge.assert_web_project_access(self.p.path,UID)

if __name__=='__main__':unittest.main()
