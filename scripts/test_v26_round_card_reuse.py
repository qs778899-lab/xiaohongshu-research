"""Synthetic same-round card reading reuse; no collection or production project."""
import copy
import json
from pathlib import Path
import unittest
from urllib.parse import quote

import test_bridge as fixture
import xhs_bridge as bridge
import xhs_native as native
import xhs_project as xp
import xhs_rounds as rounds


class RoundCardReuseTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BridgeTests();self.f.setUp();self.p=self.f.project
        self.serial=0
        task=self.p.task(self.f.tid);task['payload'].update(stage='search',target='Fixture doctor A',sort='general')
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task['id']));self.p.db.commit()
        self.a=task['id'];self.b=self.add_task('Fixture doctor B');self.sorted=self.add_task('Fixture doctor A','latest')
        self.body=self.add_task('f'*24,stage='body');self.comments=self.add_task('f'*24,stage='roots')
        cfg={'cutoff':'2026-01-01T00:00:00+00:00','doctors':['Fixture A','Fixture B'],'employment_rule':'synthetic official rule'}
        rounds.create(self.p,'one',cfg);rounds.create(self.p,'two',cfg)

    def tearDown(self):self.f.tearDown()

    def add_task(self,target,sort='general',stage='search'):
        self.serial+=1
        source=self.p.path/f'input-{self.serial}.json';source.write_text(json.dumps({'synthetic':True}))
        manifest=self.p.path/f'import-{self.serial}.json'
        unit={'stage':stage,'target':target,'sources':[str(source)],'next':'Read actual new words; independently verify collection.'}
        if stage=='search':unit['sort']=sort
        manifest.write_text(json.dumps({'units':[unit]}))
        return native.import_manifest(self.p,manifest)['task_ids'][0]

    def card(self,i=1,text='Complete synthetic card words',**fields):
        return {'url':'https://www.xiaohongshu.com/explore/'+f'{i:024x}'+'?xsec_token=synthetic-token',
            'title':'Synthetic title','text':text,'author':'Fixture account',**fields}

    def capture(self,tid,cards,end=False):
        task=self.p.task(tid);target=task['payload']['target']
        return bridge.store_capture(self.p,task,{'url':'https://www.xiaohongshu.com/search_result?keyword='+quote(target),
            'cards':cards,'selected':[task['payload']['sort']],'explicitEnd':end,'position':{'scrollTop':100,'scrollHeight':2000}})

    def packet(self,tid,rid='one'):return rounds.review_next(self.p,tid,round_id=rid)

    def receipt(self,tid,packet,complete=False):
        task=self.p.task(tid);source=packet['snapshots'][0]['file']
        self.serial+=1;path=self.p.path/f'review-{self.serial}.json'
        value={'target':task['payload']['target'],'action':'read','outcome':'ready','observed_at':bridge.legacy.now(),
            'summary':'Read the full new synthetic card words and this query context. No doctor attribution inherited.',
            'gaps':['pagination remains'] if not complete else [],'source_file':source,
            'url':'https://www.xiaohongshu.com/search_result?keyword='+quote(task['payload']['target']),
            'target_verified':True,'content_ready':True,'view_consistent':True,'reviewed':True,'complete':complete,
            'sort':task['payload']['sort'],'end':'search_exhausted','positions':['packet snapshots'],
            'review_packet_id':packet['id'],'bridge_snapshot_hashes':[s['hash'] for s in packet['snapshots']]}
        path.write_text(json.dumps(value));return path

    def read(self,tid,packet):return bridge.review(self.p,tid,self.receipt(tid,packet))

    def seed(self,cards=None):
        self.capture(self.a,cards or [self.card()]);packet=self.packet(self.a);self.read(self.a,packet);return packet

    def returned(self,packet):return [c for s in packet['snapshots'] for c in s['reading'].get('cards',[])]

    def test_cross_query_omits_only_previously_read_words_and_retains_full_proof(self):
        original=self.seed();self.capture(self.b,[self.card()]);packet=self.packet(self.b)
        self.assertEqual(self.returned(packet),[])
        reading=packet['snapshots'][0]['reading'];self.assertEqual(reading['card_delta']['cross_task_reviewed'],1)
        self.assertEqual(reading['search_scope']['query'],'Fixture doctor B');self.assertTrue(packet['query_assignment_requires_review'])
        proof=packet['shared_card_reviews']['sources'][0]
        self.assertEqual(proof['packet_id'],original['id']);self.assertEqual(proof['task_id'],self.a)
        self.assertEqual(proof['query'],'Fixture doctor A');self.assertTrue(Path(proof['receipt_file']).is_file())
        self.assertEqual(packet['shared_card_reviews']['matches'][0]['locator'],'cards/0')
        self.assertEqual(json.loads(Path(packet['file']).read_text())['shared_card_reviews'],packet['shared_card_reviews'])

    def test_cross_sort_and_signed_alias_match_but_sort_review_stays_current(self):
        self.seed();card=self.card();card['url']=card['url'].replace('/explore/','/user/profile/'+'e'*24+'/')+'&other=changed'
        self.capture(self.sorted,[card]);packet=self.packet(self.sorted)
        self.assertEqual(self.returned(packet),[])
        self.assertEqual(packet['snapshots'][0]['reading']['search_scope']['sort'],'latest')
        self.assertFalse(self.p.task(self.sorted)['payload']['bridge']['collection_end'])

    def test_same_task_exact_words_are_reused_with_a_same_round_proof(self):
        self.seed();card=self.card();card['url']+='&new=1';self.capture(self.a,[card]);packet=self.packet(self.a)
        self.assertEqual(self.returned(packet),[])
        self.assertEqual(packet['snapshots'][0]['reading']['card_delta']['already_reviewed'],1)
        self.assertEqual(packet['snapshots'][0]['reading']['card_delta']['cross_task_reviewed'],0)

    def test_other_round_and_no_round_do_not_borrow_cross_task_reading(self):
        self.seed();self.capture(self.b,[self.card()])
        self.assertEqual(len(self.returned(self.packet(self.b,'two'))),1)
        self.assertEqual(len(self.returned(rounds.review_next(self.p,self.b))),1)

    def test_other_round_same_task_new_capture_does_not_borrow_old_reading(self):
        self.seed();card=self.card();card['url']+='&new=1';self.capture(self.a,[card])
        self.assertEqual(len(self.returned(self.packet(self.a,'two'))),1)

    def test_changed_full_words_title_author_or_late_loaded_content_are_read(self):
        self.seed()
        incoming=[self.card(text='Complete synthetic card words with a new final negation'),
            self.card(title='Changed title'),self.card(author='A different account'),self.card(description='new observed detail')]
        self.capture(self.b,incoming);self.assertEqual(self.returned(self.packet(self.b)),incoming)

    def test_same_display_name_different_observed_author_identity_is_read_again(self):
        self.seed([self.card(authorId='a'*24,author_id='a'*24,authorHrefRaw='/user/profile/'+'a'*24)])
        self.capture(self.b,[self.card(authorId='a'*24,author_id='a'*24,authorHrefRaw='/user/profile/'+'a'*24)])
        self.assertEqual(self.returned(self.packet(self.b)),[])
        incoming=[self.card(authorId='b'*24,author_id='a'*24,authorHrefRaw='/user/profile/'+'a'*24),
            self.card(authorId='a'*24,author_id='b'*24,authorHrefRaw='/user/profile/'+'a'*24),
            self.card(authorId='a'*24,author_id='a'*24,authorHrefRaw='/user/profile/'+'b'*24)]
        self.capture(self.b,incoming);self.assertEqual(self.returned(self.packet(self.b)),incoming)

    def test_nonreal_ids_and_empty_words_are_not_shared(self):
        incoming=[{'url':'https://www.xiaohongshu.com/explore','text':'same'},
            {'url':'https://www.xiaohongshu.com/explore/not-real','text':'same'},
            {'url':'https://www.xiaohongshu.com.evil/explore/'+'a'*24,'text':'same'},
            {'url':self.card()['url'],'text':''}]
        self.seed(incoming);self.capture(self.b,incoming)
        self.assertEqual(self.returned(self.packet(self.b)),incoming)

    def test_uncommitted_or_receiptless_packet_cannot_seed_a_shared_read(self):
        self.capture(self.a,[self.card()]);packet=self.packet(self.a)
        self.capture(self.b,[self.card()]);self.assertEqual(len(self.returned(self.packet(self.b))),1)
        rec={'review_packet_id':packet['id'],'reviewed':True,'summary':'Direct packet call is not an archived native reading receipt.'}
        rounds.commit_packet(self.p,self.a,rec,True)
        self.assertEqual(len(self.returned(self.packet(self.b))),1)

    def test_changed_original_raw_packet_or_receipt_forces_current_words_back(self):
        for broken in ['raw','packet','receipt']:
            with self.subTest(broken=broken):
                if broken!='raw':self.tearDown();self.setUp()
                original=self.seed();self.capture(self.b,[self.card()]);first=self.packet(self.b)
                proof=first['shared_card_reviews']['sources'][0]
                path=Path(proof[{'raw':'source_file','packet':'packet_file','receipt':'receipt_file'}[broken]])
                path.write_text('{}')
                self.assertEqual(len(self.returned(self.packet(self.b))),1)

    def test_dependency_changed_after_packet_creation_rejects_before_receipt_mutation(self):
        self.seed();self.capture(self.b,[self.card()]);packet=self.packet(self.b);receipt=self.receipt(self.b,packet)
        proof=packet['shared_card_reviews']['sources'][0];Path(proof['receipt_file']).write_text('{}')
        before=self.p.db.execute('SELECT count(*) FROM native_events').fetchone()[0]
        state=copy.deepcopy(self.p.task(self.b)['payload'])
        with self.assertRaisesRegex(ValueError,'Shared card review evidence'):bridge.review(self.p,self.b,receipt)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM native_events').fetchone()[0],before)
        self.assertEqual(self.p.task(self.b)['payload'],state)
        self.assertEqual(self.p.db.execute('SELECT committed FROM review_packets WHERE id=?',(packet['id'],)).fetchone()[0],0)

    def test_page_fault_pending_body_and_comment_states_never_promote(self):
        self.seed();self.capture(self.b,[self.card()]);bridge.hold_task(self.p,self.b,'PAGINATION_STALLED')
        before_fault=copy.deepcopy(self.p.task(self.b)['payload']['bridge']['fault'])
        unrelated={tid:tuple(self.p.db.execute('SELECT status,review_status,payload FROM tasks WHERE id=?',(tid,)).fetchone()) for tid in [self.body,self.comments]}
        packet=self.packet(self.b);self.read(self.b,packet)
        task=self.p.task(self.b);self.assertEqual(task['status'],'pending');self.assertEqual(task['payload']['bridge']['fault'],before_fault)
        self.assertFalse(task['payload']['bridge']['collection_end'])
        for tid,prior in unrelated.items():self.assertEqual(tuple(self.p.db.execute('SELECT status,review_status,payload FROM tasks WHERE id=?',(tid,)).fetchone()),prior)
        with self.assertRaisesRegex(bridge.BridgeError,'COLLECTION_END_NOT_OBSERVED'):
            bridge.review(self.p,self.b,self.receipt(self.b,packet,True))

    def test_compact_delivery_saves_tokens_and_keeps_shared_hashes_and_urls_in_file(self):
        cards=[self.card(i,'Synthetic full card words '+str(i)+' complete context '*50) for i in range(1,31)]
        self.seed(cards);self.capture(self.b,cards);packet=self.packet(self.b);compact=rounds.compact_review(packet)
        self.assertEqual(compact['card_review_reuse']['cross_task_reviewed'],30)
        self.assertEqual(compact['card_review_reuse']['source_groups'],1)
        self.assertTrue(compact['query_assignment_requires_review'])
        text=json.dumps(compact);full=json.dumps(packet)
        self.assertNotIn('shared_card_reviews',compact);self.assertNotIn('synthetic-token',text)
        self.assertNotIn(packet['shared_card_reviews']['sources'][0]['receipt_sha256'],text)
        self.assertLess(len(text),len(full)/3)
        new=self.card(40,text='Same visible text',title='New title must remain visible',author='New author')
        self.capture(self.b,[new]);fresh=rounds.compact_review(self.packet(self.b))
        card=fresh['snapshots'][-1]['reading']['cards'][0]
        self.assertEqual(card['title'],new['title']);self.assertEqual(card['author'],new['author'])

    def test_incremental_index_does_not_duplicate_and_reused_text_does_not_chain(self):
        self.seed();self.capture(self.b,[self.card()]);first=self.packet(self.b)
        count=self.p.db.execute('SELECT count(*) FROM round_card_reads').fetchone()[0]
        second=self.packet(self.b);self.assertEqual(first['id'],second['id'])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM round_card_reads').fetchone()[0],count)
        self.read(self.b,second)
        self.capture(self.sorted,[self.card()]);third=self.packet(self.sorted)
        self.assertEqual(third['shared_card_reviews']['sources'][0]['task_id'],self.a,'An omitted card never becomes a new direct reading proof')


if __name__=='__main__':unittest.main()
