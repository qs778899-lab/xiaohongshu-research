"""Saved original note text can be reviewed offline without inventing live UI."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_native as native
import xhs_project as xp


NOTE = 'a' * 24
URL = 'https://www.xiaohongshu.com/explore/' + NOTE + '?xsec_token=observed-fixture'
TITLE = '/data/items/0/note_card/title'
BODY = '/data/items/0/note_card/desc'


def sha(data):
    return hashlib.sha256(data).hexdigest()


class SavedOriginalBodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.p = xp.Project(self.root / 'project', create=True)
        self.source = self.root / 'original-note.json'
        self.original = {'operation': 'note', 'note_id': NOTE, 'url': URL,
                         'captured_at': '2026-10-30T06:51:26.179770+00:00',
                         'data': {'items': [{'model_type': 'note', 'id': NOTE, 'ignore': False,
                                            'note_card': {'note_id': NOTE, 'type': 'normal',
                                                          'title': 'Orthodontic visit diary',
                                                          'desc': 'I attended a consultation. Treatment has not started.',
                                                          'user': {'user_id': 'b' * 24},
                                                          'interact_info': {}, 'image_list': [{'file_id': 'unread-image'}],
                                                          'tag_list': []}}]}}
        self.write_source()
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'units': [dict(stage=s, target=NOTE, next='Pending actual scope reading',
                                                       sources=[str(self.source)])
                                                for s in ['body', 'images', 'roots', 'replies', 'identity', 'employment']]}))
        ids = native.import_manifest(self.p, manifest)['task_ids']
        self.tasks = {self.p.task(t)['payload']['stage']: t for t in ids}; self.tid = self.tasks['body']
        self.rf = self.root / 'receipt.json'

    def tearDown(self):
        self.p.close(); self.tmp.cleanup()

    def write_source(self, value=None):
        self.source.write_text(json.dumps(value or self.original))

    def receipt(self, **changes):
        value = json.loads(self.source.read_text()); card = value['data']['items'][0]['note_card']
        r = dict(review_scope='saved_original_body_text_v1', task_id=self.tid, target=NOTE,
                 action='offline', outcome='offline', observed_at='2026-11-05T10:30:00+00:00',
                 summary='Actually read the full archived title and description; other scopes remain separate.',
                 source_file=str(self.source), source_sha256=sha(self.source.read_bytes()), url=URL,
                 source_captured_at=value['captured_at'], body_pointer=BODY, title_pointer=TITLE,
                 body_sha256=sha(card['desc'].encode()), title_sha256=sha(card['title'].encode()),
                 reader='codex', full_body_read=True, title_read=True, source_body_complete=True,
                 body_text_truncated=False, unread_body_segments=[], ui_observed=False,
                 content_ready=False, view_consistent=False, content_observed=True, reviewed=True,
                 complete=True, end='body_text_read', gaps=[], positions=[TITLE, BODY])
        r.update(changes); self.rf.write_text(json.dumps(r)); return self.rf

    def reject(self, receipt=None):
        before_events = [tuple(r) for r in self.p.db.execute('SELECT * FROM native_events ORDER BY id')]
        before_task = self.p.db.execute('SELECT * FROM tasks WHERE id=?', (self.tid,)).fetchone()
        with self.assertRaises(xp.legacy.ResearchError):
            native.record(self.p, self.tid, receipt or self.rf)
        self.assertEqual(before_events, [tuple(r) for r in self.p.db.execute('SELECT * FROM native_events ORDER BY id')])
        self.assertEqual(tuple(before_task), tuple(self.p.db.execute('SELECT * FROM tasks WHERE id=?', (self.tid,)).fetchone()))

    def test_offline_saved_text_completion_preserves_stops_other_scopes_and_sources(self):
        for index, outcome in enumerate(['no_effect', 'no_effect', 'access_stop']):
            f = self.root / ('failure' + str(index) + '.json')
            f.write_text(json.dumps(dict(target=NOTE, action='read', outcome=outcome, observed_at=str(index),
                                         summary='Prior real UI failure', source_file=str(self.source), gaps=['UI blocked'])))
            native.record(self.p, self.tid, f)
        self.p.set_meta('stop', {'reason': 'preserved API stop'}); self.p.db.commit()
        rows = [tuple(r) for r in self.p.db.execute('SELECT * FROM native_events ORDER BY rowid')]
        health = native.channel_health(self.p); meta = list(self.p.db.execute('SELECT * FROM meta ORDER BY key'))
        others = [tuple(r) for r in self.p.db.execute('SELECT * FROM tasks WHERE id<>? ORDER BY id', (self.tid,))]
        original_bytes = self.source.read_bytes()
        result = native.record(self.p, self.tid, self.receipt())
        self.assertEqual(self.p.task(self.tid)['status'], 'complete')
        self.assertTrue(native.valid_completion(self.p, self.p.task(self.tid)))
        self.assertEqual(rows, [tuple(r) for r in self.p.db.execute('SELECT * FROM native_events ORDER BY rowid')][:-1])
        self.assertEqual(others, [tuple(r) for r in self.p.db.execute('SELECT * FROM tasks WHERE id<>? ORDER BY id', (self.tid,))])
        after_health = native.channel_health(self.p)
        self.assertEqual(health, {key: after_health[key] for key in health})
        self.assertEqual(meta, list(self.p.db.execute('SELECT * FROM meta ORDER BY key')))
        self.assertEqual(self.p.task(self.tid)['payload']['pending_action'], 'read')
        self.assertEqual(self.source.read_bytes(), original_bytes)
        event = self.p.db.execute('SELECT * FROM native_events WHERE id=?', (result['event_id'],)).fetchone()
        saved = json.loads(event['receipt'])
        self.assertEqual(event['outcome'], 'offline'); self.assertEqual(event['action'], 'offline')
        self.assertEqual(saved['source_captured_at'], self.original['captured_at'])
        self.assertEqual((self.p.path / event['artifact']).read_bytes(), original_bytes)
        self.assertTrue(native.record(self.p, self.tid, self.rf)['duplicate'])
        for stage in ['images', 'roots', 'replies', 'identity', 'employment']:
            self.assertEqual(native.coverage(self.p)['stages'][stage]['processed_units'], 0)

    def test_actual_reading_scope_hash_date_and_position_declarations_are_required(self):
        changes = [('reader', 'automatic'), ('full_body_read', False), ('title_read', False),
                   ('source_body_complete', False), ('body_text_truncated', True),
                   ('unread_body_segments', ['ending']), ('ui_observed', True), ('content_ready', True),
                   ('view_consistent', True), ('source_sha256', '0' * 64), ('body_sha256', '0' * 64),
                   ('title_sha256', '0' * 64), ('task_id', 'another-task'), ('target', 'c' * 24),
                   ('url', URL.replace(NOTE, 'c' * 24)), ('positions', [BODY]), ('body_pointer', '/summary'),
                   ('source_captured_at', '2026-11-05T10:30:00+00:00'),
                   ('observed_at', '2026-10-29T10:30:00+00:00'), ('observed_at', '2026-11-05T10:30:00'),
                   ('content_observed', False), ('reviewed', False), ('complete', False),
                   ('media_verified', True), ('comment_media_scan_complete', True)]
        for key, value in changes:
            with self.subTest(key=key, value=value):self.reject(self.receipt(**{key: value}))

    def test_manual_summaries_search_cards_error_empty_and_truncated_sources_are_rejected(self):
        variants = []
        for operation in ['search', 'manual-summary']:
            value = copy.deepcopy(self.original); value['operation'] = operation; variants.append(value)
        for level, key, val in [('root', 'error', 'failed'), ('data', 'success', False),
                                ('item', 'ignore', True), ('card', 'truncated', True),
                                ('card', 'hasMoreText', True), ('card', 'desc', ''),
                                ('card', 'desc', 'Unfinished tail...'), ('card', 'note_id', 'c' * 24)]:
            value = copy.deepcopy(self.original)
            block = {'root': value, 'data': value['data'], 'item': value['data']['items'][0],
                     'card': value['data']['items'][0]['note_card']}[level]
            block[key] = val; variants.append(value)
        value = copy.deepcopy(self.original); value['data']['items'].append(copy.deepcopy(value['data']['items'][0])); variants.append(value)
        for index, value in enumerate(variants):
            with self.subTest(index=index):
                self.write_source(value); self.reject(self.receipt())

    def test_saved_body_scope_cannot_complete_images_or_other_scope(self):
        self.receipt(task_id=self.tasks['images'], media_verified=True)
        with self.assertRaisesRegex(xp.legacy.ResearchError, 'body task'):
            native.record(self.p, self.tasks['images'], self.rf)
        self.assertEqual(self.p.task(self.tasks['images'])['status'], 'pending')

    def test_ordinary_offline_body_completion_is_still_rejected_and_live_behavior_remains(self):
        self.reject(self.receipt(review_scope=None))
        self.receipt(review_scope=None, action='read', outcome='ready', target_verified=True,
                     content_ready=True, view_consistent=True)
        native.record(self.p, self.tid, self.rf)
        self.assertEqual(self.p.task(self.tid)['status'], 'complete')

    def test_empty_title_is_valid_but_not_empty_description_and_archive_loss_is_a_gap(self):
        self.original['data']['items'][0]['note_card']['title'] = ''; self.write_source()
        result = native.record(self.p, self.tid, self.receipt())
        event = self.p.db.execute('SELECT * FROM native_events WHERE id=?', (result['event_id'],)).fetchone()
        (self.p.path / event['artifact']).write_text('changed')
        self.assertFalse(native.valid_completion(self.p, self.p.task(self.tid)))
        self.assertEqual(native.coverage(self.p)['stages']['body']['pending_units'], 1)

    def test_source_or_receipt_changed_after_validation_cannot_publish_completion(self):
        archive = native.archive
        def mutate_source(project, source):
            if Path(source) == self.source:self.source.write_text(self.source.read_text() + '\n')
            return archive(project, source)
        self.receipt()
        with patch.object(native, 'archive', side_effect=mutate_source):self.reject()
        self.write_source(); self.receipt()
        validate = native.validate_saved_original_body
        def mutate_receipt(task, receipt):
            result = validate(task, receipt)
            changed = json.loads(self.rf.read_text()); changed['summary'] = 'changed during validation'
            self.rf.write_text(json.dumps(changed)); return result
        with patch.object(native, 'validate_saved_original_body', side_effect=mutate_receipt):self.reject()

    def test_receipt_changed_inside_archival_cannot_publish_completion(self):
        self.receipt(); archive = native.archive
        def mutate_receipt_during_archive(project, source):
            if Path(source) == self.rf:
                changed = json.loads(self.rf.read_text())
                changed.update(reviewed=False, complete=False)
                self.rf.write_text(json.dumps(changed))
            return archive(project, source)
        with patch.object(native, 'archive', side_effect=mutate_receipt_during_archive):
            self.reject()
        self.assertEqual(self.p.task(self.tid)['status'], 'pending')
        self.assertFalse(native.valid_completion(self.p, self.p.task(self.tid)))

    def test_regular_ready_complete_or_partial_receipt_cannot_change_during_archival(self):
        archive = native.archive
        for complete in [False, True]:
            with self.subTest(complete=complete):
                self.receipt(review_scope=None, action='read', outcome='ready', target_verified=True,
                             content_ready=True, view_consistent=True, complete=complete)
                def mutate_receipt_during_archive(project, source):
                    if Path(source) == self.rf:
                        changed = json.loads(self.rf.read_text())
                        changed.update(reviewed=False, complete=not complete)
                        self.rf.write_text(json.dumps(changed))
                    return archive(project, source)
                with patch.object(native, 'archive', side_effect=mutate_receipt_during_archive):
                    self.reject()
                self.assertEqual(self.p.task(self.tid)['status'], 'pending')
                self.assertFalse(native.valid_completion(self.p, self.p.task(self.tid)))


if __name__ == '__main__':
    unittest.main()
