import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xhs_rounds as r
import xhs_bridge as b
import xhs_batch as batch
import xhs_project as xp
from test_bridge import BridgeTests, URL, NOTE


class V26Tests(unittest.TestCase):
    def setUp(self):
        self.fixture=BridgeTests();self.fixture.setUp();self.p=self.fixture.project
        r.schema(self.p)
    def tearDown(self):self.fixture.tearDown()
    def task(self,stage):
        t=self.p.task(self.fixture.tid);t['payload']['stage']=stage
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(t['payload']),t['id']));self.p.db.commit();return self.p.task(t['id'])
    def test_frozen_scope_cutoff_and_close_refuses_pending(self):
        s=r.create(self.p,'one',{'cutoff':'2026-11-04T00:00:00+08:00','doctors':['A'],'employment_rule':'official recent'})
        self.assertEqual(s['pending'],1);self.assertFalse(r.close(self.p,'one')['closed'])
        self.assertEqual(r.round_config(self.p,'one')['doctors'],['A'])
        with self.assertRaises(ValueError):r.create(self.p,'one',{})
    def test_stall_survives_review_and_signed_url_change(self):
        t=self.task('search');t['payload']['sort']='general'
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(t['payload']),t['id']));self.p.db.commit()
        for i in range(4):
            snap={'url':'https://www.xiaohongshu.com/search_result?keyword='+NOTE,'cards':[{'url':URL+'?xsec_token='+str(i),'text':'one'}],'position':{'scrollTop':0,'scrollHeight':1000},'explicitEnd':False}
            b.store_capture(self.p,self.p.task(t['id']),snap)
        pr=self.p.task(t['id'])['payload']['bridge'];self.assertEqual(pr['stalled_count'],3);self.assertTrue(pr['fault']['hold'])
        # New actual note ID resolves, not a receipt/relative time/URL signature.
        snap['cards'].append({'url':URL.replace(NOTE,'c'*24),'text':'two'})
        b.store_capture(self.p,self.p.task(t['id']),snap)
        self.assertNotIn('fault',self.p.task(t['id'])['payload']['bridge'])
    def test_243_comments_partial_packets_parent_and_idempotence(self):
        t=self.task('roots');rows=[{'commentId':str(i),'text':('医生 A' if i==21 else '普通文字'),'author':'同昵称','parentCommentId':'0' if i else '', 'time':'1小时前'} for i in range(243)]
        b.store_capture(self.p,t,{'url':URL,'results':rows,'explicitEnd':False})
        packet=r.review_next(self.p,t['id'],40);self.assertEqual(len(packet['items']),40);self.assertTrue(packet['more_entities']);self.assertTrue(packet['items'][1]['parent'])
        rec={'review_packet_id':packet['id'],'reviewed':True,'summary':'read','bridge_snapshot_hashes':[]}
        r.commit_packet(self.p,t['id'],rec,True)
        r.commit_packet(self.p,t['id'],rec,True)
        packet2=r.review_next(self.p,t['id'],40);self.assertFalse({x['entity_id'] for x in packet['items']}&{x['entity_id'] for x in packet2['items']})
        with self.assertRaises(ValueError):r.commit_packet(self.p,t['id'],dict(rec,complete=True))
        # Changed relative clock/likes are not a new semantic version.
        rows[0]['time']='2小时前';rows[0]['likes']=9
        b.store_capture(self.p,self.p.task(t['id']),{'url':URL,'results':rows,'explicitEnd':False});r.index_task(self.p,self.p.task(t['id']))
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM entity_versions').fetchone()[0],243)
    def test_changed_text_new_version_smaller_snapshot_does_not_erase(self):
        t=self.task('roots');b.store_capture(self.p,t,{'url':URL,'results':[{'commentId':'1','text':'不满意','parentCommentId':''},{'commentId':'2','text':'A','parentCommentId':'1'}]})
        r.index_task(self.p,self.p.task(t['id']))
        b.store_capture(self.p,self.p.task(t['id']),{'url':URL,'results':[{'commentId':'1','text':'后来改善','parentCommentId':''}]});r.index_task(self.p,self.p.task(t['id']))
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM entity_versions').fetchone()[0],3)
    def test_explicit_order_and_rotation(self):
        a={'id':'a','entity_key':'a','payload':{'stage':'replies','target':'A'},'turn':5,'priority':1}
        z={'id':'z','entity_key':'z','payload':{'stage':'body','target':'Z'},'turn':0,'priority':1}
        self.assertEqual(batch.select_ready([a,z],{},['z','a'])['id'],'z')
        self.assertEqual(batch.select_ready([a,z],{'active_group':'A','group_units':3})['id'],'a')
        self.assertEqual(batch.select_ready([a,z],{'active_group':'A','group_units':10})['id'],'z')
    def test_legacy_export_never_fabricates_adult_or_current_job(self):
        r.create(self.p,'one',{'cutoff':'2026-11-04T00:00:00+08:00','doctors':['A'],'employment_rule':'official'})
        source=self.p.path/'source.txt';source.write_text('原文')
        f=self.p.path/'e.json';f.write_text(json.dumps({'evaluation_entries':[{'note_id':NOTE,'id':'E1','doctor':'A','source':'source.txt','url':URL,'summary':'未知年龄'}]}))
        r.import_evidence(self.p,f);r.export(self.p,'one')
        self.assertIn('未结构化核验',(self.p.path/'exports/one/成人专项.md').read_text())
        self.assertEqual(self.p.task(self.fixture.tid)['status'],'pending')
    def test_new_version_does_not_overwrite_evidence_history(self):
        src=self.p.path/'s.txt';src.write_text('27岁起治')
        f=self.p.path/'e.json';d={'evaluation_entries':[{'id':'E1','source':'s.txt','url':URL,'summary':'年龄待核'}]};f.write_text(json.dumps(d));r.import_evidence(self.p,f)
        d['evaluation_entries'][0]['summary']='27岁起治';f.write_text(json.dumps(d));r.import_evidence(self.p,f)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM evidence_versions').fetchone()[0],2)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM evidence_versions WHERE current=1').fetchone()[0],1)

    def test_partial_carousel_cannot_complete_and_changed_file_rejected(self):
        import hashlib
        t=self.task('images');f=self.p.path/'image.png';f.write_bytes(b'fixture')
        record={'position':1,'count':3,'kind':'image','file':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest()}
        self.p.db.execute('INSERT INTO media_inventory VALUES(?,?,?,?,?)',(NOTE,'body:1','image',r.packed(record),'captured_unreviewed'));self.p.db.commit()
        receipt={'media_verified':True,'media_reviewed_keys':['body:1'],'complete':True}
        with self.assertRaisesRegex(ValueError,'Uncaptured'):r.review_media(self.p,t,receipt)
        receipt['complete']=False;r.review_media(self.p,t,receipt,True)
        f.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):r.review_media(self.p,t,receipt)

    def test_all_carousel_images_still_need_comment_scan(self):
        import hashlib
        t=self.task('images');f=self.p.path/'one.png';f.write_bytes(b'fixture')
        record={'position':1,'count':1,'kind':'image','file':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest()}
        self.p.db.execute('INSERT INTO media_inventory VALUES(?,?,?,?,?)',(NOTE,'body:1','image',r.packed(record),'captured_unreviewed'));self.p.db.commit()
        receipt={'media_verified':True,'media_reviewed_keys':['body:1'],'complete':True}
        with self.assertRaisesRegex(ValueError,'attachment scan'):r.review_media(self.p,t,receipt)

    def test_entity_dates_unknown_kept_future_deferred(self):
        end='2026-11-04T00:00:00+08:00'
        self.assertEqual(r.entity_disposition({'time':'3小时前'},end),'date_unknown')
        self.assertEqual(r.entity_disposition({'published_at':'2026-11-03T23:00:00+08:00'},end),'included')
        self.assertEqual(r.entity_disposition({'published_at':'2026-11-04T01:00:00+08:00'},end),'next_round')
        self.assertEqual(r.entity_disposition({'published_at':'2026-11-04'},end),'date_unknown')

    def test_doctor_route_rotates_without_assigning_comment_doctor(self):
        a={'id':'a','entity_key':'one','payload':{'stage':'replies','target':'one','routing_doctors':['甲']},'turn':0}
        other_same={'id':'b','entity_key':'two','payload':{'stage':'replies','target':'two','routing_doctors':['甲']},'turn':0}
        other_doctor={'id':'c','entity_key':'three','payload':{'stage':'body','target':'three','routing_doctors':['乙']},'turn':100}
        state={'active_group':'甲','active_note':'one','group_units':10}
        self.assertEqual(batch.select_ready([a,other_same,other_doctor],state)['id'],'c')
        self.assertNotIn('doctor',a['payload'])

    def test_reviewed_media_disappeared_cannot_complete(self):
        import hashlib
        t=self.task('images');f=self.p.path/'gone.png';f.write_bytes(b'fixture')
        record={'position':1,'count':1,'kind':'image','file':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest()}
        self.p.db.execute('INSERT INTO media_inventory VALUES(?,?,?,?,?)',(NOTE,'body:1','image',r.packed(record),'reviewed'));self.p.db.commit();f.unlink()
        with self.assertRaisesRegex(ValueError,'missing or changed'):r.review_media(self.p,t,{'complete':True,'media_reviewed_keys':[]})


if __name__=='__main__':unittest.main()
