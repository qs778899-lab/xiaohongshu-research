import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import xhs_research_desk as desk
import xhs_media_report as reports
import xhs_rounds as rounds


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.p = Path(self.tmp.name)
        self.db = sqlite3.connect(self.p / 'project.sqlite3')
        self.db.executescript('CREATE TABLE meta(key TEXT,value TEXT); CREATE TABLE tasks(id TEXT,kind TEXT,entity_key TEXT,payload TEXT,status TEXT,review_status TEXT,turn INTEGER,priority INTEGER,reason TEXT);')
        (self.p / 'bridge/raw').mkdir(parents=True)

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def source(self, value):
        raw = json.dumps(value).encode(); h = hashlib.sha256(raw).hexdigest()
        (self.p / 'bridge/raw' / (h + '.json')).write_bytes(raw)
        return h

    def task(self, tid, stage='search', target='query', hashes=None, **bridge):
        payload = {'stage': stage, 'target': target, 'sort': 'general',
                   'bridge': {'hashes': hashes or [], **bridge}}
        self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)', (tid, 'native', target, json.dumps(payload), 'pending', 'pending', 0, 50, ''))
        self.db.commit()

    def card(self, nid='a'*24, text='真实经历'):
        return {'url': 'https://www.xiaohongshu.com/explore/' + nid + '?xsec_token=fixture', 'text': text}

    def state(self):
        return {str(f.relative_to(self.p)): hashlib.sha256(f.read_bytes()).hexdigest() for f in self.p.rglob('*') if f.is_file()}

    def test_scan_is_read_only_deduplicates_cards_preserves_sources(self):
        h = self.source({'cards': [self.card()]}); self.task('a', hashes=[h]); self.task('b', hashes=[h])
        before = self.state(); result = desk.scan(self.p, ['a', 'b'])
        self.assertEqual(before, self.state())
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(len(result['candidates'][0]['sources']), 2)
        self.assertEqual(len(result['candidates'][0]['title_versions']), 1)
        self.assertEqual(desk.triage_pack(result)['items'][0]['versions'][0]['additional_source_count'], 1)

    def test_foreign_scope_never_imported(self):
        a = self.source({'cards': [self.card()]}); b = self.source({'cards': [self.card('b'*24)]})
        self.task('a', hashes=[a]); self.task('b', hashes=[b])
        self.assertEqual([c['note_id'] for c in desk.scan(self.p, ['a'])['candidates']], ['a'*24])
        with self.assertRaises(ValueError): desk.scan(self.p, ['nonexistent'])

    def test_missing_changed_sources_are_gaps_not_complete(self):
        self.task('a', hashes=['f'*64]); result = desk.scan(self.p, ['a'])
        self.assertEqual(result['summary']['verified_source_files'], 0)
        self.assertTrue(result['gaps'])
        self.assertFalse(desk.recommend(result)['scope_processed'])

    def test_changed_title_reopens_old_exclusion(self):
        a = self.source({'cards': [self.card(text='普通生活')]})
        b = self.source({'cards': [self.card(text='正畸后续牙龈变化')]})
        self.task('a', hashes=[a, b])
        decision = [{'note_id': 'a'*24, 'decision': 'exclude', 'reason': '旧标题已读', 'reviewer': 'fixture',
                     'reviewed_sources': [{'sha256': a, 'locator': 'cards/0'}]}]
        result = desk.scan(self.p, ['a'], decision)
        self.assertEqual(result['candidates'][0]['decision'], 'needs_reassessment')
        self.assertEqual(len(desk.triage_pack(result)['items'][0]['versions']), 1)
        decision[0]['reviewed_sources'][0]['locator'] = 'cards/1'
        with self.assertRaises(ValueError): desk.scan(self.p, ['a'], decision)

    def test_late_name_and_no_keyword_candidates_not_discarded(self):
        cards = [self.card(f'{i:024x}', '普通日记' if i != 210 else '后来是王医生接诊') for i in range(231)]
        self.task('a', hashes=[self.source({'cards': cards})])
        inventory = desk.scan(self.p, ['a']); seen = []; after = None
        while True:
            pack = desk.triage_pack(inventory, 40, after)
            seen.extend(i['note_id'] for i in pack['items']); after = pack['next_after']
            if not pack['more']: break
        self.assertEqual(len(set(seen)), 231)
        self.assertIn(f'{210:024x}', seen)

    def test_smaller_snapshot_does_not_erase_candidates_or_media(self):
        a = self.source({'url': self.card()['url'], 'cards': [self.card(), self.card('b'*24)],
                         'results': [{'commentId': 'c', 'images': [{'src': 'observed'}]}]})
        b = self.source({'url': self.card()['url'], 'cards': [self.card()], 'results': []})
        self.task('a', stage='roots', target='a'*24, hashes=[a, b])
        result = desk.scan(self.p, ['a'])
        self.assertEqual(len(result['candidates']), 2); self.assertEqual(len(result['media']), 1)
        self.assertEqual(result['media'][0]['recorded_state'], 'uninspected')

    def test_no_ids_not_merged_and_emoji_not_image(self):
        a = self.source({'results': [{'text': 'same', 'images': ['a']}, {'text': 'same', 'images': ['a']},
                                     {'commentId': 'emoji', 'images': [], 'renderedInlineImages': [{'class': 'note-content-emoji'}]}]})
        self.task('a', stage='roots', target='a'*24, hashes=[a])
        result = desk.scan(self.p, ['a'])
        self.assertEqual(len(result['media']), 2)
        self.assertTrue(all(m['observations'][0]['identity_uncertain'] for m in result['media']))

    def test_source_wrong_note_does_not_import_media(self):
        a = self.source({'url': self.card('b'*24)['url'], 'results': [{'commentId': 'c', 'images': ['a']}]})
        self.task('a', stage='roots', target='a'*24, hashes=[a])
        result = desk.scan(self.p, ['a'])
        self.assertEqual(result['media'], []); self.assertTrue(result['gaps'])

    def test_hold_isolated_dependencies_and_priority_preserved(self):
        self.task('held', stage='roots', target='a'*24, fault={'hold': True, 'code': 'PAGINATION_STALLED'})
        self.task('child', stage='replies', target='a'*24)
        self.task('first', stage='roots', target='b'*24)
        self.task('second', stage='roots', target='c'*24)
        result = desk.scan(self.p, ['held', 'child', 'first', 'second'])
        advice = desk.recommend(result, preferred=['second', 'held', 'child', 'first'])
        self.assertEqual([x['task_id'] for x in advice['choices']], ['second', 'first'])
        self.assertEqual(advice['held_task_ids'], ['held'])
        advice = desk.recommend(result, preferred=['child'])
        self.assertFalse(any(x.get('task_id') == 'child' and x['action'] == 'collect' for x in advice['choices']))

    def test_included_without_body_is_registered_before_more_search(self):
        h = self.source({'cards': [self.card()]}); self.task('a', hashes=[h], reviewed_hashes=[h])
        decision = [{'note_id': 'a'*24, 'decision': 'include', 'reason': '相关', 'reviewer': 'fixture',
                     'reviewed_sources': [{'sha256': h, 'locator': 'cards/0'}]}]
        result = desk.scan(self.p, ['a'], decision)
        self.assertEqual(desk.recommend(result)['choices'][0]['action'], 'register_scope')

    def test_end_receipt_pending_never_continues_search(self):
        self.task('a', collection_end=True)
        advice = desk.recommend(desk.scan(self.p, ['a']))
        self.assertEqual(advice['choices'][0]['action'], 'verify_end_receipt')
        self.assertFalse(advice['scope_processed'])

    def test_direct_body_without_search_card_can_be_screened(self):
        h = self.source({'url': self.card()['url'], 'title': '直接原帖', 'body': '本人经历'})
        self.task('a', stage='body', target='a'*24, hashes=[h], reviewed_hashes=[h])
        result = desk.scan(self.p, ['a'])
        self.assertEqual(desk.triage_pack(result)['items'][0]['versions'][0]['words']['body'], '本人经历')
        decision = [{'note_id': 'a'*24, 'decision': 'include', 'reason': '相关', 'reviewer': 'fixture',
                     'reviewed_sources': [{'sha256': h, 'locator': 'body'}]}]
        self.assertEqual(desk.scan(self.p, ['a'], decision)['candidates'][0]['decision'], 'include')

    def test_body_identity_change_requires_reassessment_collector_is_not_author(self):
        a = self.source({'url': self.card()['url'], 'title': '日记', 'body': '原话', 'authors': ['A'], 'uid': 'collector'})
        b = self.source({'url': self.card()['url'], 'title': '日记', 'body': '原话', 'authors': ['B'], 'uid': 'collector'})
        self.task('a', stage='body', target='a'*24, hashes=[a, b])
        decision = [{'note_id': 'a'*24, 'decision': 'include', 'reason': '相关', 'reviewer': 'fixture',
                     'reviewed_sources': [{'sha256': a, 'locator': 'body'}]}]
        c = desk.scan(self.p, ['a'], decision)['candidates'][0]
        self.assertEqual(c['decision'], 'needs_reassessment')
        self.assertEqual(len(c['title_versions']), 2)
        self.assertTrue(all('uid' not in v['words'] for v in c['title_versions']))

    def test_stop_blocks_collection_but_allows_offline_review(self):
        self.db.execute('INSERT INTO meta VALUES(?,?)', ('stop', json.dumps({'reason': 'ACCESS_STOP'}))); self.db.commit()
        self.task('a', stage='roots', target='a'*24)
        self.assertEqual(desk.recommend(desk.scan(self.p, ['a']))['choices'][0]['action'], 'inspect_gaps')

    def test_old_api_layout_is_not_claimed_zero_coverage(self):
        self.task('a'); self.db.execute("UPDATE tasks SET kind='search',payload='{}'"); self.db.commit()
        result = desk.scan(self.p, ['a'])
        self.assertEqual(result['summary']['unsupported_adapter_tasks'], 1)
        self.assertEqual(desk.recommend(result)['choices'][0]['action'], 'inspect_gaps')

    def test_metrics_missing_phases_and_duplicate_runs(self):
        f = self.p / 'checkpoint.json'; f.write_text(json.dumps({'wall_seconds': 68, 'metrics': {'calls': 45}, 'processed_units': 5}))
        r = {'measurement_id': 'run', 'phase': 'collection', 'file': str(f), 'sha256': hashlib.sha256(f.read_bytes()).hexdigest()}
        result = desk.metrics({'measurements': [r]})
        self.assertEqual(result['phases']['collection']['seconds'], 68)
        self.assertIn('review', result['unmeasured_phases']); self.assertIsNone(result['end_to_end_seconds'])
        with self.assertRaises(ValueError): desk.metrics({'measurements': [r, r]})


class MediaReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.p = Path(self.tmp.name)

    def tearDown(self): self.tmp.cleanup()

    def entry(self, nid='a'*24, text='本帖自己的实际观察', position=1):
        image = self.p / (nid + '.png'); image.write_bytes(b'fixture-image')
        sha = hashlib.sha256(image.read_bytes()).hexdigest()
        meta = self.p / (nid + '-capture.json')
        meta.write_text(json.dumps({'position': position, 'kind': 'image', 'sha256': sha,
                                   'url': 'https://www.xiaohongshu.com/explore/' + nid}))
        review = self.p / (nid + '-review.json')
        review.write_text(json.dumps({'note_id': nid, 'position': position, 'actual_visual_review': True,
                                     'observation': text, 'file': str(image), 'file_sha256': sha,
                                     'capture_metadata': str(meta), 'capture_metadata_sha256': hashlib.sha256(meta.read_bytes()).hexdigest()}))
        return {'file': str(review), 'sha256': hashlib.sha256(review.read_bytes()).hexdigest()}

    def mutate(self, entry, **fields):
        p = Path(entry['file']); data = json.loads(p.read_text()); data.update(fields); p.write_text(json.dumps(data))
        return {'file': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}

    def test_different_notes_keep_their_own_observations(self):
        a = self.entry(text='牙槽骨薄，尚未起治'); b = self.entry('b'*24, '侧脸对比，缺少量尺')
        text, rows = reports.render_media_reviews([a, b])
        self.assertEqual([r['observation'] for r in rows], ['牙槽骨薄，尚未起治', '侧脸对比，缺少量尺'])
        self.assertNotIn('50+', text); self.assertNotIn('白斑', text)

    def test_wrong_identity_or_position_rejected(self):
        a = self.entry(); a = self.mutate(a, position=2)
        with self.assertRaises(ValueError): reports.render_media_reviews([a])
        a = self.entry(); a = self.mutate(a, note_id='b'*24)
        with self.assertRaises(ValueError): reports.render_media_reviews([a])

    def test_missing_reading_and_empty_observation_no_fallback(self):
        a = self.entry(); a = self.mutate(a, actual_visual_review=False)
        with self.assertRaises(ValueError): reports.render_media_reviews([a])
        a = self.entry(text='')
        with self.assertRaises(ValueError): reports.render_media_reviews([a])

    def test_changed_image_or_receipt_rejected(self):
        a = self.entry(); (self.p / ('a'*24 + '.png')).write_bytes(b'changed')
        with self.assertRaises(ValueError): reports.render_media_reviews([a])
        a = self.entry(); Path(a['file']).write_text('{}')
        with self.assertRaises(ValueError): reports.render_media_reviews([a])

    def test_comment_or_video_never_labeled_body_image(self):
        a = self.entry(); a = self.mutate(a, comment_id='c')
        with self.assertRaises(ValueError): reports.render_media_reviews([a])

    def test_capture_url_cannot_be_overridden_by_activation(self):
        a = self.entry(); review = json.loads(Path(a['file']).read_text()); meta = Path(review['capture_metadata'])
        data = json.loads(meta.read_text()); data['url'] = 'https://www.xiaohongshu.com/explore/' + 'b'*24
        data['activation'] = {'target_note_id': 'a'*24}; meta.write_text(json.dumps(data))
        a = self.mutate(a, capture_metadata_sha256=hashlib.sha256(meta.read_bytes()).hexdigest())
        with self.assertRaises(ValueError): reports.render_media_reviews([a])

    def test_legacy_sidecar_uses_actual_original_file_hash(self):
        a = self.entry(); review = json.loads(Path(a['file']).read_text()); meta = Path(review['capture_metadata'])
        data = json.loads(meta.read_text()); del data['sha256']; data['file'] = review['file']; meta.write_text(json.dumps(data))
        a = self.mutate(a, capture_metadata_sha256=hashlib.sha256(meta.read_bytes()).hexdigest())
        self.assertEqual(reports.observation(a)['note_id'], 'a'*24)
        a = self.entry(); a = self.mutate(a, kind='video')
        with self.assertRaises(ValueError): reports.render_media_reviews([a])


class ReplyContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.p = Path(self.tmp.name)
        self.db = sqlite3.connect(':memory:'); self.db.row_factory = sqlite3.Row
        self.task_record = {'id': 't', 'payload': {'stage': 'roots', 'target': 'a'*24, 'bridge': {'hashes': []}}}
        self.project = type('P', (), {'db': self.db, 'path': self.p, 'task': lambda _, tid: copy.deepcopy(self.task_record)})()
        rounds.schema(self.project); (self.p / 'bridge/raw').mkdir(parents=True)

    def tearDown(self): self.db.close(); self.tmp.cleanup()

    def add(self, rows):
        raw = json.dumps({'results': rows}).encode(); h = hashlib.sha256(raw).hexdigest()
        (self.p / 'bridge/raw' / (h + '.json')).write_bytes(raw)
        self.task_record['payload']['bridge']['hashes'].append(h)
        return h

    def test_direct_reply_author_and_parent_history_survive(self):
        self.add([{'commentId': 'root', 'text': '一开始不满', 'authorId': 'r'},
                  {'commentId': 'other', 'text': '我找的是另一位医生', 'authorId': 'b', 'parentCommentId': 'root'}])
        self.add([{'commentId': 'root', 'text': '后来改善了', 'authorId': 'r'},
                  {'commentId': 'reply', 'text': '你说的是谁', 'authorId': 'c', 'parentCommentId': 'root', 'replyToCommentId': 'other'}])
        rounds.index_task(self.project, self.task_record)
        self.db.execute("UPDATE entity_versions SET reviewed=1 WHERE entity_id!='reply'"); self.db.commit()
        pack = rounds.review_next(self.project, 't'); output = rounds.compact_review(pack)
        self.assertEqual(len(output['items']), 1)
        self.assertEqual([c['content']['text'] for c in output['parent_context']['root']], ['一开始不满', '后来改善了'])
        self.assertEqual(output['reply_target_context']['other'][0]['content']['author_uid'], 'b')
        self.assertEqual(output['items'][0]['reply_to_id'], 'other')

    def test_missing_context_explicit_and_not_merged_by_nickname(self):
        self.add([{'commentId': 'reply', 'text': '回复', 'author': 'momo', 'parentCommentId': 'missing', 'replyToCommentId': 'other'}])
        pack = rounds.compact_review(rounds.review_next(self.project, 't'))
        self.assertEqual({g['relation'] for g in pack['items'][0]['context_gaps']}, {'parent', 'reply_to'})

    def test_relative_time_no_new_version_but_author_change_does(self):
        self.add([{'commentId': 'c', 'text': '原话', 'authorId': 'a', 'time': '3小时前'}])
        self.add([{'commentId': 'c', 'text': '原话', 'authorId': 'a', 'time': '4小时前'}])
        self.assertEqual(len(rounds.review_next(self.project, 't')['items']), 1)
        self.add([{'commentId': 'c', 'text': '原话', 'authorId': 'b', 'time': '4小时前'}])
        self.assertEqual(len(rounds.review_next(self.project, 't')['items']), 2)

    def test_modified_context_cannot_be_committed(self):
        h = self.add([{'commentId': 'root', 'text': '父文'}])
        self.add([{'commentId': 'reply', 'text': '回复', 'parentCommentId': 'root'}])
        rounds.index_task(self.project, self.task_record)
        self.db.execute("UPDATE entity_versions SET reviewed=1 WHERE entity_id='root'"); self.db.commit()
        packet = rounds.review_next(self.project, 't')
        (self.p / 'bridge/raw' / (h + '.json')).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'context source'):
            rounds.commit_packet(self.project, 't', {'review_packet_id': packet['id'], 'reviewed': True, 'summary': '已读'}, apply=True)
        self.assertEqual(self.db.execute("SELECT reviewed FROM entity_versions WHERE entity_id='reply'").fetchone()[0], 0)


if __name__ == '__main__': unittest.main()
