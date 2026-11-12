"""Offline precise source attachment; it cannot reset acquisition or account stops."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_bridge as bridge
import xhs_native as native
import xhs_project as xp

NOTE='a'*24
UID='b'*24
URL='https://www.xiaohongshu.com/explore/'+NOTE+'?xsec_token=observed'

class AttachSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.p=xp.Project(self.root/'project',create=True)
        self.old=self.root/'old.json';self.old.write_text('{"summary":"Legacy source has no URL"}')
        self.tid=self.create('replies',NOTE)
        t=self.p.task(self.tid);p=t['payload']
        p['bridge']={'hashes':['original-hash'],'reviewed_hashes':['original-hash'],'cursor':{'scrollTop':399},
                     'comments':{'observedCommentIds':['real-comment']},'awaiting_review':False,'stalled_count':3,
                     'fault':{'code':'OBSERVED_SOURCE_URL_REQUIRED','hold':True,'at':'old-time','recovery':{'exhausted':True}}}
        p['latest_observation']={'event_id':'old-receipt','summary':'already read content'}
        self.p.db.execute("UPDATE tasks SET payload=?,reason='OBSERVED_SOURCE_URL_REQUIRED' WHERE id=?",(xp.dumps(p),self.tid))
        self.p.set_meta('stop',{'reason':'keep original STOP'});self.p.db.commit()
        self.stop=self.p.path/'STOP';self.stop.write_text('do not erase this account stop')
        self.source=self.root/'source.json'
        self.source.write_text(json.dumps({'nested':[{'url':URL}]}))
        self.driver=patch.object(bridge,'Driver',side_effect=AssertionError('No network allowed'));self.driver.start()
    def tearDown(self):self.driver.stop();self.p.close();self.tmp.cleanup()
    def create(self,stage,target):
        manifest=self.root/(stage+'-'+target+'.json')
        manifest.write_text(json.dumps({'units':[{'stage':stage,'target':target,'next':'keep old next','sources':[str(self.old)]}]}))
        return native.import_manifest(self.p,manifest)['task_ids'][0]
    def test_exact_source_clears_only_missing_url_hold_and_keeps_history(self):
        before=self.p.task(self.tid)
        result=bridge.attach_source(self.p.path,self.tid,self.source)
        after=self.p.task(self.tid);payload=after['payload'];progress=payload['bridge']
        self.assertTrue(result['source_fault_resolved']);self.assertEqual(result['url'],URL)
        self.assertFalse(result['content_acquired']);self.assertNotIn('fault',progress)
        expected=dict(before['payload']['bridge']);old=expected.pop('fault')
        for k,v in expected.items():self.assertEqual(progress[k],v)
        self.assertEqual(progress['resolved_source_faults'][0]['prior_fault'],old)
        for k in ['status','review_status','reason','turn','priority']:
            self.assertEqual(after[k],before[k])
        self.assertEqual(payload['latest_observation'],before['payload']['latest_observation'])
        self.assertEqual(payload['next'],before['payload']['next'])
        self.assertEqual(self.p.get_meta('stop'),{'reason':'keep original STOP'})
        self.assertEqual(self.stop.read_text(),'do not erase this account stop')
        self.assertEqual(bridge.observed_url(self.p.path,after),URL)
        ref=payload['source_attachments'][0];audit=Path(ref['audit_file'])
        self.assertEqual(hashlib.sha256(audit.read_bytes()).hexdigest(),ref['audit_sha256'])
        proof=json.loads(audit.read_text())
        self.assertEqual(proof['source_sha256'],hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertIn('basis',proof)
        self.assertTrue((self.p.path/proof['source_archive']).is_file())
        self.assertTrue(any(v['sha256']==proof['source_sha256'] for v in payload['provenance']))
    def test_repeated_import_does_not_duplicate_provenance_or_rewind(self):
        bridge.attach_source(self.p.path,self.tid,self.source)
        before=self.p.task(self.tid)
        result=bridge.attach_source(self.p.path,self.tid,self.source)
        after=self.p.task(self.tid)
        self.assertFalse(result['source_fault_resolved'])
        self.assertEqual(after['payload'],before['payload'])
    def test_other_fault_is_preserved_even_with_valid_source(self):
        t=self.p.task(self.tid);t['payload']['bridge']['fault']={'code':'PAGINATION_STALLED','hold':True,'recovery':{'reload':'already'}}
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(t['payload']),self.tid));self.p.db.commit()
        result=bridge.attach_source(self.p.path,self.tid,self.source)
        self.assertFalse(result['source_fault_resolved'])
        self.assertEqual(self.p.task(self.tid)['payload']['bridge']['fault'],t['payload']['bridge']['fault'])
    def test_foreign_note_or_author_url_never_changes_task(self):
        for url in ['https://www.xiaohongshu.com/explore/'+'c'*24,
                    'https://www.xiaohongshu.com/user/profile/'+NOTE,
                    'https://www.xiaohongshu.com/chat',
                    'https://www.xiaohongshu.com.evil/explore/'+NOTE]:
            with self.subTest(url=url):
                self.source.write_text(json.dumps({'url':url}));before=self.p.task(self.tid)
                with self.assertRaisesRegex(bridge.BridgeError,'EXACT_TARGET_SOURCE_URL_REQUIRED'):
                    bridge.attach_source(self.p.path,self.tid,self.source)
                self.assertEqual(self.p.task(self.tid),before)
    def test_author_task_accepts_only_exact_profile(self):
        tid=self.create('author',UID)
        for url in ['https://www.xiaohongshu.com/explore/'+UID,'https://www.xiaohongshu.com/user/profile/'+UID+'/'+NOTE]:
            self.source.write_text(json.dumps({'url':url}))
            with self.assertRaisesRegex(bridge.BridgeError,'EXACT_TARGET_SOURCE_URL_REQUIRED'):
                bridge.attach_source(self.p.path,tid,self.source)
        url='https://www.xiaohongshu.com/user/profile/'+UID+'?xsec_token=observed'
        self.source.write_text(json.dumps({'nested':[{'url':url}]}))
        self.assertEqual(bridge.attach_source(self.p.path,tid,self.source)['url'],url)
    def test_note_accepts_search_or_author_note_route(self):
        for url in ['https://www.xiaohongshu.com/search_result/'+NOTE,
                    'https://www.xiaohongshu.com/user/profile/'+UID+'/'+NOTE+'?xsec_token=observed']:
            self.source.write_text(json.dumps({'url':url}))
            result=bridge.attach_source(self.p.path,self.tid,self.source)
            self.assertTrue(result['url'].endswith(NOTE) or NOTE in result['url'])
    def test_invalid_missing_and_symlink_sources_are_rejected(self):
        for raw in ['not JSON','{"summary":"no observed link"}']:
            self.source.write_text(raw);before=self.p.task(self.tid)
            with self.assertRaises(bridge.BridgeError):bridge.attach_source(self.p.path,self.tid,self.source)
            self.assertEqual(before,self.p.task(self.tid))
        link=self.root/'link.json';link.symlink_to(self.source)
        for source in [link,self.root/'missing.json',Path('relative.json')]:
            with self.assertRaises(bridge.BridgeError):bridge.attach_source(self.p.path,self.tid,source)
    def test_existing_completed_state_and_receipt_are_retained(self):
        self.p.db.execute("UPDATE tasks SET status='complete',review_status='reviewed',reason='original receipt' WHERE id=?",(self.tid,));self.p.db.commit()
        before=self.p.task(self.tid)
        bridge.attach_source(self.p.path,self.tid,self.source)
        after=self.p.task(self.tid)
        self.assertEqual((after['status'],after['review_status'],after['reason']),(before['status'],before['review_status'],before['reason']))
    def test_same_target_other_native_stage_not_created(self):
        before=self.p.db.execute('select count(*) from tasks').fetchone()[0]
        bridge.attach_source(self.p.path,self.tid,self.source)
        self.assertEqual(self.p.db.execute('select count(*) from tasks').fetchone()[0],before)
    def test_non_id_target_cannot_be_repaired(self):
        tid=self.create('identity','unresolved nickname')
        with self.assertRaisesRegex(bridge.BridgeError,'SOURCE_ATTACHMENT_TARGET_REQUIRED'):
            bridge.attach_source(self.p.path,tid,self.source)

    def relative_source(self,href=None,key='authorHrefRaw'):
        href=href or '/user/profile/'+UID+'?channel_type=web_note_detail_r10&xsec_token=observed%3D&xsec_source=pc_comment'
        self.source.write_text(json.dumps({'results':[{'commentId':'c'*24,key:href}]}))
        return href

    def test_relative_named_author_href_has_exact_source_audit(self):
        tid=self.create('author',UID);href=self.relative_source()
        before=self.p.task(tid)
        result=bridge.attach_source(self.p.path,tid,self.source)
        expected='https://www.xiaohongshu.com'+href
        self.assertEqual(result['url'],expected)
        after=self.p.task(tid)
        self.assertEqual(bridge.observed_url(self.p.path,after),expected)
        audit=json.loads(Path(result['audit_file']).read_text())
        self.assertEqual(audit['url_observations'],[{
            'original_href':href,'keypath':['results',0,'authorHrefRaw'],
            'normalized_url':expected,'normalization':'observed_author_href_fixed_origin',
            'source_sha256':hashlib.sha256(self.source.read_bytes()).hexdigest(),
            'content_acquired':False}])
        self.assertFalse(audit['content_acquired']);self.assertIn('not a new page observation',audit['basis'])
        self.assertEqual((self.p.path/audit['source_archive']).read_bytes(),self.source.read_bytes())
        for key in ['status','review_status','reason','turn','priority']:
            self.assertEqual(after[key],before[key])

    def test_relative_lookup_from_provenance_needs_no_attachment_or_write(self):
        tid=self.create('author',UID);href=self.relative_source()
        archive,sha=native.archive(self.p,self.source)
        task=self.p.task(tid);task['payload']['provenance']=[{'archive':archive,'sha256':sha}]
        before=self.p.task(tid);stop=self.stop.read_bytes()
        self.assertEqual(bridge.observed_url(self.p.path,task),'https://www.xiaohongshu.com'+href)
        self.assertEqual(self.p.task(tid),before);self.assertEqual(self.stop.read_bytes(),stop)

    def test_relative_wrong_field_or_prose_cannot_supply_source(self):
        tid=self.create('author',UID);href='/user/profile/'+UID+'?xsec_token=observed'
        cases=[{'url':href},{'authorUrl':href},{'summary':href},{'authorHrefRaw':'see '+href},
               {'authorHrefRaw':[href]},{'authorHrefRaw':{'text':href}},[href]]
        for data in cases:
            with self.subTest(data=data):
                self.source.write_text(json.dumps(data));before=self.p.task(tid)
                with self.assertRaisesRegex(bridge.BridgeError,'EXACT_TARGET_SOURCE_URL_REQUIRED'):
                    bridge.attach_source(self.p.path,tid,self.source)
                self.assertEqual(self.p.task(tid),before)

    def test_relative_author_href_rejects_unsafe_or_nonexact_paths(self):
        tid=self.create('author',UID)
        bad=['//www.xiaohongshu.com/user/profile/'+UID,
             '///user/profile/'+UID,'//evil.test/user/profile/'+UID,
             '/user/profile/'+UID+'0','/user/profile/a'+UID[1:],
             '/user/profile/'+UID+'/', '/user/profile/'+UID+'/'+NOTE,
             '/explore/'+UID,'/chat','/user/profile/../profile/'+UID,
             '/user/profile/%62'+UID[1:],'/user/profile/'+UID+';private',
             '/user/profile/'+UID+'#private','/user/profile/'+UID+'?x=\\evil',
             '/user/profile/'+UID+'?x=bad\nvalue','/user/profile/'+UID+'?x=bad value']
        for href in bad:
            with self.subTest(href=href):
                self.relative_source(href);before=self.p.task(tid)
                with self.assertRaisesRegex(bridge.BridgeError,'EXACT_TARGET_SOURCE_URL_REQUIRED'):
                    bridge.attach_source(self.p.path,tid,self.source)
                self.assertEqual(self.p.task(tid),before)
                self.assertEqual(bridge.exact_target_observed_urls(json.loads(self.source.read_text()),'author',UID),[])

    def test_relative_author_href_cannot_supply_note_or_bridge_url(self):
        self.relative_source('/user/profile/'+NOTE)
        with self.assertRaisesRegex(bridge.BridgeError,'EXACT_TARGET_SOURCE_URL_REQUIRED'):
            bridge.attach_source(self.p.path,self.tid,self.source)
        task={'payload':{'stage':'author','target':UID,'bridge_url':'/user/profile/'+UID}}
        with self.assertRaisesRegex(bridge.BridgeError,'OBSERVED_SOURCE_URL_REQUIRED'):
            bridge.observed_url(self.p.path,task)

    def test_lookup_also_rejects_cross_stage_absolute_target(self):
        task={'payload':{'stage':'author','target':UID,
            'bridge_url':'https://www.xiaohongshu.com/explore/'+UID}}
        with self.assertRaisesRegex(bridge.BridgeError,'OBSERVED_SOURCE_URL_REQUIRED'):
            bridge.observed_url(self.p.path,task)

    def test_relative_tampered_archive_or_hash_is_not_evidence(self):
        tid=self.create('author',UID);self.relative_source()
        archive,sha=native.archive(self.p,self.source)
        task=self.p.task(tid);task['payload']['provenance']=[{'archive':archive,'sha256':sha}]
        (self.p.path/archive).write_text('{"authorHrefRaw":"/user/profile/'+UID+'?xsec_token=changed"}')
        with self.assertRaisesRegex(bridge.BridgeError,'OBSERVED_SOURCE_URL_REQUIRED'):
            bridge.observed_url(self.p.path,task)
        task['payload']['provenance'][0]['sha256']='0'*64
        with self.assertRaisesRegex(bridge.BridgeError,'OBSERVED_SOURCE_URL_REQUIRED'):
            bridge.observed_url(self.p.path,task)

    def test_relative_attachment_does_not_release_collection_fault(self):
        tid=self.create('author',UID);self.relative_source()
        task=self.p.task(tid);task['payload']['bridge']={
            'fault':{'code':'AUTHOR_PAGINATION_STALLED','hold':True},'stalled_count':3,
            'cursor':{'scrollTop':1024},'hashes':['prior'],'reviewed_hashes':['prior']}
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),tid));self.p.db.commit()
        before=self.p.task(tid);stop=self.stop.read_bytes()
        result=bridge.attach_source(self.p.path,tid,self.source)
        self.assertFalse(result['source_fault_resolved'])
        self.assertEqual(self.p.task(tid)['payload']['bridge'],before['payload']['bridge'])
        self.assertEqual(self.stop.read_bytes(),stop);self.assertEqual(self.p.get_meta('stop'),{'reason':'keep original STOP'})

if __name__=='__main__':unittest.main()
