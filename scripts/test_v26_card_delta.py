"""Offline cross-packet card deltas retain full immutable snapshot bindings."""
import hashlib
import json
import unittest

import test_bridge as fixture
import xhs_bridge as bridge
import xhs_project as project
import xhs_rounds as rounds


class CardDeltaTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.BridgeTests();self.fixture.setUp();self.p=self.fixture.project
        task=self.p.task(self.fixture.tid);task['payload'].update(stage='search',sort='general')
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(project.dumps(task['payload']),task['id']));self.p.db.commit()

    def tearDown(self):self.fixture.tearDown()

    def card(self,number,text,token='one',**fields):
        return {'url':'https://www.xiaohongshu.com/explore/'+format(number,'024x')+'?xsec_token='+token,'text':text,**fields}

    def capture(self,cards):
        task=self.p.task(self.fixture.tid)
        return bridge.store_capture(self.p,task,{'url':'https://www.xiaohongshu.com/search_result?keyword='+fixture.NOTE,'cards':cards,'explicitEnd':False})

    def mark_reviewed(self,hashes=None):
        task=self.p.task(self.fixture.tid);state=task['payload']['bridge']
        state['reviewed_hashes']=list(state['hashes']) if hashes is None else hashes
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(project.dumps(task['payload']),task['id']));self.p.db.commit()

    def packet(self):return rounds.review_next(self.p,self.fixture.tid)

    def cards(self,packet):return [card for snap in packet['snapshots'] for card in snap['reading'].get('cards',[])]

    def test_twenty_reviewed_cards_are_omitted_but_new_card_and_binding_remain(self):
        self.capture([self.card(i,'完整文字'+str(i)) for i in range(1,21)]);self.mark_reviewed()
        incoming=[self.card(i,'完整文字'+str(i),'rotated') for i in range(1,21)]+[self.card(21,'新帖')]
        self.capture(incoming);packet=self.packet()
        self.assertEqual([c['text'] for c in self.cards(packet)],['新帖'])
        self.assertEqual(len(packet['snapshots']),1)
        snapshot=packet['snapshots'][0]
        from pathlib import Path
        raw=Path(snapshot['file']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),snapshot['hash'])
        self.assertEqual(json.loads(raw)['cards'],incoming)
        self.assertEqual(snapshot['reading']['card_delta'],{'observed':21,'returned':1,'already_reviewed':20,'duplicate_in_packet':0})
        self.assertEqual(self.p.task(self.fixture.tid)['status'],'pending')

    def test_changed_complete_text_is_shown(self):
        self.capture([self.card(1,'共同前缀'*100+'以前不满意')]);self.mark_reviewed()
        self.capture([self.card(1,'共同前缀'*100+'后来改善')])
        self.assertTrue(self.cards(self.packet())[0]['text'].endswith('后来改善'))

    def test_changed_title_with_identical_text_is_shown(self):
        self.capture([self.card(1,'相同正文',title='旧标题')]);self.mark_reviewed()
        self.capture([self.card(1,'相同正文',title='新标题')])
        self.assertEqual(self.cards(self.packet())[0]['title'],'新标题')

    def test_empty_text_then_loaded_text_is_shown(self):
        self.capture([self.card(1,'')]);self.mark_reviewed();self.capture([self.card(1,'后来加载的标题')])
        self.assertEqual(self.cards(self.packet())[0]['text'],'后来加载的标题')

    def test_changed_note_route_and_signature_do_not_repeat_same_words(self):
        self.capture([self.card(1,'同一正文')]);self.mark_reviewed()
        card=self.card(1,'同一正文','new');card['url']=card['url'].replace('/explore/','/user/profile/'+'f'*24+'/')
        self.capture([card]);packet=self.packet()
        self.assertEqual(self.cards(packet),[])
        self.assertEqual(len(packet['snapshots']),1)
        self.assertEqual(packet['snapshots'][0]['reading']['card_delta']['observed'],1)

    def test_unknown_identity_is_not_merged(self):
        cards=[{'url':'https://www.xiaohongshu.com/explore','text':'相同文字'},{'url':'','text':'相同文字'}]
        self.capture(cards);self.mark_reviewed();self.capture(cards+[self.card(2,'new')])
        self.assertEqual(len(self.cards(self.packet())),3)

    def test_missing_reviewed_source_does_not_hide_new_card(self):
        self.capture([self.card(1,'可见')]);self.mark_reviewed(['a'*64])
        self.assertEqual(len(self.cards(self.packet())),1)

    def test_invalid_hash_reviewed_source_does_not_hide_new_card(self):
        self.capture([self.card(1,'可见')]);fake='a'*64
        (self.p.path/'bridge/raw'/f'{fake}.json').write_text(json.dumps({'cards':[self.card(1,'可见')]}))
        self.mark_reviewed([fake]);self.assertEqual(len(self.cards(self.packet())),1)

    def test_within_packet_keeps_changed_words_and_deduplicates_only_exact_words(self):
        self.capture([self.card(1,'甲'),self.card(1,'乙','two'),self.card(1,'甲','three')])
        self.assertEqual([c['text'] for c in self.cards(self.packet())],['甲','乙'])

    def test_delivery_without_review_is_not_treated_as_read(self):
        self.capture([self.card(1,'待审')]);first=self.packet();second=self.packet()
        self.assertEqual(self.cards(first),self.cards(second))
        self.assertEqual(self.p.task(self.fixture.tid)['payload']['bridge'].get('reviewed_hashes',[]),[])


if __name__=='__main__':unittest.main()
