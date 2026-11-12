"""Offline compact reading must preserve attribution and changed context."""
import unittest

import xhs_rounds as rounds


class CompactReviewTests(unittest.TestCase):
    def item(self, entity_id, text, **fields):
        return {'entity_id': entity_id, 'source': 'raw.json', 'locator': 'results/' + entity_id,
                'body': {'text': text, 'author': '同昵称', 'authorId': 'a' * 24, **fields}}

    def packet(self, items):
        return {'id': 'packet', 'file': 'packet.json', 'task_id': 'task', 'stage': 'replies',
                'target': 'note', 'round': None, 'more_entities': True, 'items': items, 'snapshots': []}

    def context(self, item, text, source='new.json', **fields):
        item['parent'] = {'source': source, 'locator': 'results/0',
                          'body': {'text': text, 'author': '同昵称', 'authorId': 'a' * 24, **fields}}
        return item

    def test_exact_text_and_ids_survive(self):
        items = [self.item(str(i), ('不是这位；后续改善。\n' * 90) + str(i)) for i in range(18)]
        output = rounds.compact_review(self.packet(items))
        self.assertEqual([i['id'] for i in output['items']], [i['entity_id'] for i in items])
        self.assertEqual([i['text'] for i in output['items']], [i['body']['text'] for i in items])

    def test_real_reply_target_survives_same_nickname(self):
        item = self.item('reply', '不是这位', parentCommentId='root', reply_to='同昵称',
                         replyToCommentId='other-person-comment', replyToIdentity='observed_id')
        output = rounds.compact_review(self.packet([item]))
        self.assertEqual(output['items'][0]['reply_to_id'], 'other-person-comment')

    def test_new_parent_text_is_not_deduplicated_against_old_visible_parent(self):
        parent = self.item('root', '开始对 A 不满意')
        child = self.context(self.item('reply', '对，后来转诊了', parentCommentId='root'), '更正：后来是 B 接诊')
        output = rounds.compact_review(self.packet([parent, child]))
        context = output['parent_context']['root'][0]
        self.assertEqual(context['content']['text'], '更正：后来是 B 接诊')
        self.assertEqual(output['sources'][context['source']], 'new.json')

    def test_multiple_versions_of_one_parent_are_preserved(self):
        one = self.context(self.item('reply1', '是的', parentCommentId='root'), '初始说明')
        two = self.context(self.item('reply2', '已更正', parentCommentId='root'), '更正说明', 'newer.json')
        output = rounds.compact_review(self.packet([one, two]))
        self.assertEqual([c['content']['text'] for c in output['parent_context']['root']], ['初始说明', '更正说明'])

    def test_only_identical_parent_content_is_deduplicated(self):
        parent = self.item('root', '相同内容')
        same = self.context(self.item('reply1', '回复', parentCommentId='root'), '相同内容')
        different_identity = self.context(self.item('reply2', '另一个人', parentCommentId='root'),
                                          '相同内容', authorId='b' * 24)
        output = rounds.compact_review(self.packet([parent, same, different_identity]))
        self.assertEqual(len(output['parent_context']['root']), 1)
        self.assertEqual(output['parent_context']['root'][0]['content']['author_uid'], 'b' * 24)

    def test_repeated_identical_context_is_printed_once(self):
        items = [self.context(self.item('reply' + str(i), '回复', parentCommentId='root'), '同一父文')
                 for i in range(5)]
        output = rounds.compact_review(self.packet(items))
        self.assertEqual(len(output['parent_context']['root']), 1)
        self.assertEqual(len(output['items']), 5)


if __name__ == '__main__':
    unittest.main()
