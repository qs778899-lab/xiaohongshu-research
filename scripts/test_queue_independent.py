"""Independent offline multiprocess forward tests; no real account or network."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import sqlite3
from unittest import mock
from contextlib import contextmanager

import xhs_queue
import xhs_research
import xhs_media
import xhs_project
from test_media import note, Response


def fixture_database(project, stop=None):
    project.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(project/'project.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
    if stop is not None:db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('stop',json.dumps(stop)))
    db.commit();db.close()

WORKER = r'''
import sys,time,json
from pathlib import Path
import xhs_queue as q
import xhs_research as r
b=Path(sys.argv[1]); name=sys.argv[2]; mode=sys.argv[3]
r.account_root=lambda:b/'guards'
run=b/(name+'run') if mode=='separate' else b/'run'; project=b/name
if len(sys.argv)>5:time.time_ns=lambda:int(sys.argv[5])
def log(event):
 with (b/'events.jsonl').open('a') as f:f.write(json.dumps([name,event,time.monotonic()])+'\n')
try:
 if mode in ('holder','account_holder'):
  with (r.run_lock(run) if mode=='holder' else r.account_lock(run)):
   (b/'holder-ready').touch()
   while not (b/'release').exists():time.sleep(.02)
 else:
  if mode=='gate':
   (b/(name+'-ready')).touch()
   while not (b/(name+'-register')).exists():time.sleep(.02)
  with q.collection_turn(project,run,time.time()+float(sys.argv[4]),b/(name+'-pause')):
   log('enter');time.sleep(.15);log('exit')
except q.QueueCheckpoint as e:log(e.reason)
except r.StopRun:log('STOP')
'''


class IndependentQueue(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.b=Path(self.temp.name).resolve()
        (self.b/'guards').mkdir();(self.b/'run').mkdir()
        (self.b/'run/run.json').write_text('{}')
        (self.b/'run/account.json').write_text(json.dumps({'key':'f'*64}))
        self.procs=[]

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:p.terminate()
            p.wait(timeout=5)
            if p.stdout:p.stdout.close()
            if p.stderr:p.stderr.close()
        self.temp.cleanup()

    def spawn(self,name,mode='queue',seconds=8,stamp=None):
        args=[sys.executable,'-c',WORKER,str(self.b),name,mode,str(seconds)]
        if stamp is not None:args.append(str(stamp))
        p=subprocess.Popen(args,cwd=Path(__file__).parent,
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.procs.append(p);return p

    def waitfor(self,predicate):
        due=time.monotonic()+5
        while not predicate():
            if time.monotonic()>due:self.fail('condition timeout')
            time.sleep(.02)

    def tickets(self):return list((self.b/'guards'/('f'*64)/'collection-queue').glob('*.ticket'))
    def events(self):
        f=self.b/'events.jsonl'
        return [json.loads(s) for s in f.read_text().splitlines()] if f.exists() else []
    def holder(self):
        p=self.spawn('legacy','holder');self.waitfor(lambda:(self.b/'holder-ready').exists());return p
    def finish(self,p):
        stdout,stderr=p.communicate(timeout=8);self.assertEqual(p.returncode,0,stderr)

    def test_fifo_and_no_overlap_with_legacy_run_owner(self):
        h=self.holder();jobs=[]
        for index,name in enumerate(['A','B','C'],1):
            jobs.append(self.spawn(name));self.waitfor(lambda:len(self.tickets())==index)
        self.assertEqual(self.events(),[])
        (self.b/'release').touch();self.finish(h)
        for job in jobs:self.finish(job)
        self.assertEqual([(x[0],x[1]) for x in self.events()],[(n,e) for n in ['A','B','C'] for e in ['enter','exit']])
        self.assertFalse(self.tickets());self.assertFalse((self.b/'run/.collection.lock').exists())

    def test_wait_budget_preserves_legacy_lock(self):
        self.holder();p=self.spawn('A',seconds=.3);self.finish(p)
        self.assertEqual(self.events()[0][1],'time_budget');self.assertTrue((self.b/'run/.collection.lock').exists())
        self.assertFalse(self.tickets())

    def test_local_review_project_lock_waits_then_resumes(self):
        with xhs_project.project_lock(self.b/'A'):
            p=self.spawn('A');self.waitfor(lambda:len(self.tickets())==1)
            time.sleep(.15)
            self.assertIsNone(p.poll());self.assertEqual(self.events(),[])
        self.finish(p)
        self.assertEqual([(x[0],x[1]) for x in self.events()],[('A','enter'),('A','exit')])
        self.assertFalse(self.tickets())

    def test_project_lock_wait_budget_does_not_unlock_other_writer(self):
        with xhs_project.project_lock(self.b/'A'):
            p=self.spawn('A',seconds=.2);self.finish(p)
            self.assertEqual(self.events()[0][1],'time_budget')
            with self.assertRaises(xhs_research.CollectionBusy):
                with xhs_project.project_lock(self.b/'A'):pass
        self.assertFalse(self.tickets())

    def test_stop_while_waiting_on_local_writer(self):
        with xhs_project.project_lock(self.b/'A'):
            fixture_database(self.b/'A')
            p=self.spawn('A');self.waitfor(lambda:len(self.tickets())==1)
            fixture_database(self.b/'A',{'reason':'stop during local writer'})
            self.finish(p)
        self.assertEqual(self.events()[0][1],'STOP');self.assertFalse(self.tickets())

    def test_sqlite_exclusive_writer_waits_then_resumes(self):
        fixture_database(self.b/'A')
        db=sqlite3.connect(self.b/'A/project.sqlite3')
        try:
            db.execute('BEGIN EXCLUSIVE')
            p=self.spawn('A');self.waitfor(lambda:len(self.tickets())==1)
            time.sleep(.15)
            self.assertIsNone(p.poll());self.assertEqual(self.events(),[])
            db.rollback();self.finish(p)
        finally:db.close()
        self.assertEqual([r[1] for r in self.events()],['enter','exit'])
        self.assertFalse(self.tickets())

    def test_sqlite_busy_stop_committed_before_resume_prevents_collection(self):
        fixture_database(self.b/'A')
        db=sqlite3.connect(self.b/'A/project.sqlite3')
        try:
            db.execute('BEGIN EXCLUSIVE')
            db.execute('INSERT INTO meta VALUES (?,?)',('stop',json.dumps({'reason':'new STOP'})))
            p=self.spawn('A');self.waitfor(lambda:len(self.tickets())==1)
            time.sleep(.15);self.assertEqual(self.events(),[])
            db.commit();self.finish(p)
        finally:db.close()
        self.assertEqual([r[1] for r in self.events()],['STOP'])
        self.assertFalse(self.tickets())

    def test_sqlite_busy_budget_does_not_commit_or_rollback_writer(self):
        fixture_database(self.b/'A')
        db=sqlite3.connect(self.b/'A/project.sqlite3')
        try:
            db.execute('BEGIN EXCLUSIVE')
            p=self.spawn('A',seconds=.2);self.finish(p)
            self.assertTrue(db.in_transaction)
            self.assertEqual([r[1] for r in self.events()],['time_budget'])
        finally:db.rollback();db.close()
        self.assertFalse(self.tickets())

    def test_account_stop_preempts_busy_project_database(self):
        fixture_database(self.b/'A')
        db=sqlite3.connect(self.b/'A/project.sqlite3')
        try:
            db.execute('BEGIN EXCLUSIVE')
            p=self.spawn('A');self.waitfor(lambda:len(self.tickets())==1)
            stop=self.b/'guards'/('f'*64)/'STOP.json';stop.write_text('{}')
            self.finish(p);self.assertTrue(stop.exists());self.assertTrue(db.in_transaction)
        finally:db.rollback();db.close()
        self.assertEqual([r[1] for r in self.events()],['STOP']);self.assertFalse(self.tickets())

    def test_pause_preempts_busy_project_database(self):
        fixture_database(self.b/'A')
        db=sqlite3.connect(self.b/'A/project.sqlite3')
        try:
            db.execute('BEGIN EXCLUSIVE')
            p=self.spawn('A');self.waitfor(lambda:len(self.tickets())==1)
            pause=self.b/'A-pause';pause.touch();self.finish(p)
            self.assertTrue(pause.exists());self.assertTrue(db.in_transaction)
        finally:db.rollback();db.close()
        self.assertEqual([r[1] for r in self.events()],['requested']);self.assertFalse(self.tickets())

    def test_non_contention_sqlite_error_is_not_waited_away(self):
        (self.b/'A').mkdir();db=sqlite3.connect(self.b/'A/project.sqlite3');db.close()
        with self.assertRaises(sqlite3.OperationalError) as caught:
            xhs_queue.ensure_project_active(self.b/'A')
        self.assertEqual(caught.exception.sqlite_errorcode,sqlite3.SQLITE_ERROR)

    def test_typed_sqlite_locked_is_waitable(self):
        fixture_database(self.b/'A')
        error=sqlite3.OperationalError('typed locked')
        error.sqlite_errorcode=sqlite3.SQLITE_LOCKED
        connection=mock.Mock();connection.execute.side_effect=error
        with mock.patch.object(xhs_queue.sqlite3,'connect',return_value=connection):
            with self.assertRaises(xhs_research.CollectionBusy):xhs_queue.ensure_project_active(self.b/'A')
        connection.close.assert_called_once()

    def test_post_lock_stop_read_contention_releases_all_locks_before_wait(self):
        calls=[];sleeps=[]
        def inspect(project):
            calls.append(project)
            if len(calls)==2:
                self.assertTrue((self.b/'run/.collection.lock').exists())
                raise xhs_research.CollectionBusy('post-lock STOP read waits')
        def waiting(seconds):
            sleeps.append(seconds)
            self.assertFalse((self.b/'run/.collection.lock').exists())
            with xhs_project.project_lock(self.b/'A'):pass
        with mock.patch.object(xhs_research,'account_root',return_value=self.b/'guards'), mock.patch.object(xhs_queue,'ensure_project_active',side_effect=inspect):
            with xhs_queue.collection_turn(self.b/'A',self.b/'run',time.time()+3,None,sleep=waiting):
                self.assertEqual(len(calls),4)
        self.assertEqual(len(sleeps),1);self.assertFalse(self.tickets())

    def test_two_runs_same_account_fifo(self):
        h=self.spawn('legacy','account_holder');self.waitfor(lambda:(self.b/'holder-ready').exists())
        jobs=[]
        for index,name in enumerate(['A','B'],1):
            target=self.b/(name+'run');target.mkdir();(target/'run.json').write_text('{}')
            (target/'account.json').write_text(json.dumps({'key':'f'*64}))
            jobs.append(self.spawn(name,'separate'));self.waitfor(lambda:len(self.tickets())==index)
        self.assertEqual(self.events(),[]);(self.b/'release').touch();self.finish(h)
        for job in jobs:self.finish(job)
        self.assertEqual([(x[0],x[1]) for x in self.events()],[(n,e) for n in ['A','B'] for e in ['enter','exit']])
        self.assertFalse(self.tickets())

    def test_clock_rollback_does_not_reorder_registered_tickets(self):
        h=self.holder();a=self.spawn('A',stamp=200);self.waitfor(lambda:len(self.tickets())==1)
        b=self.spawn('B',stamp=100);self.waitfor(lambda:len(self.tickets())==2)
        prefixes=[int(f.name.split('-')[0]) for f in sorted(self.tickets())]
        self.assertEqual(prefixes,[1,2]);(self.b/'release').touch();self.finish(h)
        self.finish(a);self.finish(b)
        self.assertEqual([(x[0],x[1]) for x in self.events()],[(n,e) for n in ['A','B'] for e in ['enter','exit']])

    def test_fifo_follows_registration_not_process_start(self):
        h=self.holder();a=self.spawn('A',mode='gate');self.waitfor(lambda:(self.b/'A-ready').exists())
        b=self.spawn('B');self.waitfor(lambda:len(self.tickets())==1)
        (self.b/'A-register').touch();self.waitfor(lambda:len(self.tickets())==2)
        (self.b/'release').touch();self.finish(h);self.finish(a);self.finish(b)
        self.assertEqual([(x[0],x[1]) for x in self.events()],[(n,e) for n in ['B','A'] for e in ['enter','exit']])

    def test_stop_while_waiting(self):
        self.holder();p=self.spawn('A');self.waitfor(lambda:len(self.tickets())==1)
        stop=self.b/'guards'/('f'*64)/'STOP.json';stop.write_text('{}');self.finish(p)
        self.assertEqual(self.events()[0][1],'STOP');self.assertTrue(stop.exists());self.assertFalse(self.tickets())

    def test_pause_while_waiting(self):
        self.holder();p=self.spawn('A');self.waitfor(lambda:len(self.tickets())==1)
        pause=self.b/'A-pause';pause.touch();self.finish(p)
        self.assertEqual(self.events()[0][1],'requested');self.assertTrue(pause.exists());self.assertFalse(self.tickets())

    def test_project_stop_while_waiting_without_run_stop(self):
        self.holder();fixture_database(self.b/'A');p=self.spawn('A');self.waitfor(lambda:len(self.tickets())==1)
        fixture_database(self.b/'A',{'reason':'test project stop'});self.finish(p)
        self.assertEqual(self.events()[0][1],'STOP');self.assertFalse((self.b/'run/STOP.json').exists())
        self.assertFalse(self.tickets())
        db=sqlite3.connect(self.b/'A/project.sqlite3');value=db.execute("SELECT value FROM meta WHERE key='stop'").fetchone()[0];db.close()
        self.assertEqual(json.loads(value),{'reason':'test project stop'})

    def test_crashed_waiter_ticket_reclaimed_without_clearing_run_lock(self):
        h=self.holder();a=self.spawn('A');self.waitfor(lambda:len(self.tickets())==1)
        a.kill();a.wait();b=self.spawn('B');self.waitfor(lambda:len(self.tickets())==1)
        self.assertTrue((self.b/'run/.collection.lock').exists());(self.b/'release').touch();self.finish(h);self.finish(b)
        self.assertEqual([(r[0],r[1]) for r in self.events()],[('B','enter'),('B','exit')]);self.assertFalse(self.tickets())


class MediaQueueIntegration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.b=Path(self.temp.name).resolve()
        self.project=self.b/'project';self.project.mkdir()
        self.run=self.b/'run';self.run.mkdir();(self.b/'guards').mkdir()
        (self.run/'run.json').write_text(json.dumps({'format':xhs_research.FORMAT,'directory':str(self.run)}))
        (self.run/'account.json').write_text(json.dumps({'key':'f'*64}))
        source=self.project/'source.json';source.write_text(json.dumps(note()))
        self.directory=self.project/'media'
        manifest=xhs_media.inspect_sources([source],self.directory)
        self.mid=next(x['media_id'] for x in manifest['items'].values() if x['kind']=='image')
        self.patch=mock.patch.object(xhs_research,'account_root',return_value=self.b/'guards');self.patch.start()

    def tearDown(self):
        self.patch.stop();self.temp.cleanup()

    def test_project_download_uses_real_queue_before_mock_http(self):
        fixture_database(self.project);events=[];original=xhs_queue.collection_turn
        @contextmanager
        def observed(*args,**kwargs):
            with original(*args,**kwargs):
                events.append('queue_enter');yield
            events.append('queue_exit')
        def fake_open(*args,**kwargs):
            self.assertTrue((self.run/'.collection.lock').exists())
            self.assertTrue(xhs_research._ACCOUNT_LOCKS)
            self.assertEqual(len(list((self.b/'guards'/('f'*64)/'collection-queue').glob('*.ticket'))),1)
            events.append('http');return Response(b'offline image')
        with mock.patch.object(xhs_queue,'collection_turn',side_effect=observed), mock.patch.object(xhs_media,'build_opener') as builder:
            builder.return_value.open.side_effect=fake_open
            result=xhs_media.download_media(self.directory,self.mid,run=self.run)
        self.assertEqual(events,['queue_enter','http','queue_exit']);self.assertEqual(result['acquisition_status'],'downloaded')
        self.assertEqual(result['review_status'],'pending');self.assertFalse((self.run/'.collection.lock').exists())

    def test_project_pause_prevents_http(self):
        fixture_database(self.project);(self.project/'batch').mkdir();(self.project/'batch/PAUSE').touch()
        with mock.patch.object(xhs_media,'build_opener') as builder:
            with self.assertRaises(xhs_queue.QueueCheckpoint) as caught:
                xhs_media.download_media(self.directory,self.mid,run=self.run)
            self.assertEqual(caught.exception.reason,'requested');builder.assert_not_called()
        self.assertEqual(xhs_media.get_item(self.directory,self.mid)[2]['acquisition_status'],'pending')

    def test_project_stop_prevents_http(self):
        fixture_database(self.project);(self.run/'STOP.json').write_text('{}')
        with mock.patch.object(xhs_media,'build_opener') as builder:
            with self.assertRaises(xhs_research.StopRun):xhs_media.download_media(self.directory,self.mid,run=self.run)
            builder.assert_not_called()
        self.assertTrue((self.run/'STOP.json').exists())

    def test_project_meta_stop_prevents_http_without_run_stop(self):
        fixture_database(self.project,{'reason':'test project stop'})
        with mock.patch.object(xhs_media,'build_opener') as builder:
            with self.assertRaises(xhs_research.StopRun):xhs_media.download_media(self.directory,self.mid,run=self.run)
            builder.assert_not_called()
        self.assertFalse((self.run/'STOP.json').exists())
        self.assertEqual(xhs_media.get_item(self.directory,self.mid)[2]['acquisition_status'],'pending')

    def test_standalone_media_keeps_account_lock_without_creating_project(self):
        def fake_open(*args,**kwargs):
            self.assertTrue(xhs_research._ACCOUNT_LOCKS)
            self.assertFalse((self.run/'.collection.lock').exists());return Response(b'offline image')
        with mock.patch.object(xhs_queue,'collection_turn') as turn, mock.patch.object(xhs_media,'build_opener') as builder:
            builder.return_value.open.side_effect=fake_open
            result=xhs_media.download_media(self.directory,self.mid,run=self.run)
            turn.assert_not_called()
        self.assertEqual(result['acquisition_status'],'downloaded');self.assertFalse((self.project/'project.sqlite3').exists())


if __name__=='__main__':unittest.main()
