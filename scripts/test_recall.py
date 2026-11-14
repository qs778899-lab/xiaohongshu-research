"""Synthetic offline recall tests; no accounts, browsers or network calls."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import xhs_paths
import xhs_project as xp
import xhs_recall as recall

N = 'a' * 24
URL = 'https://www.xiaohongshu.com/explore/' + N


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve(); self.project = self.base / 'project'
        p = xp.Project(self.project, create=True)
        p.close()
        self.cfg = {'format': recall.FORMAT, 'projects': [{'path': str(self.project), 'all_tasks': True}],
                    'terms': [{'id': 'initials', 'text': 'AB', 'mode': 'initials',
                               'ambiguities': ['synthetic person', 'synthetic institution']}]}

    def db(self):
        return sqlite3.connect(self.project / 'project.sqlite3')

    def source(self, value):
        raw = json.dumps(value, ensure_ascii=False).encode(); sha = hashlib.sha256(raw).hexdigest()
        folder = self.project / 'snapshots'; folder.mkdir(exist_ok=True)
        path = folder / (sha + '.json'); path.write_bytes(raw)
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?)',
                       (sha, str(path.relative_to(self.project)), str(path), value.get('operation', 'bridge_roots'), 'fixture'))
        return sha, path

    def browser(self, rows):
        return self.source({'url': URL, 'results': rows})

    def row(self, cid, text='AB', **fields):
        return dict(commentId=cid, text=text, authorId='b' * 24, **fields)

    def state(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.project.rglob('*') if p.is_file()}

    def test_variants_offsets_and_ambiguity(self):
        text = 'ＡＢ / a.b / A B / ab / CAB / abacus'
        terms = recall.compile_terms(self.cfg['terms'])
        hits = recall.matches(text, terms)
        self.assertEqual([x['original'] for x in hits], ['ＡＢ', 'a.b', 'A B', 'ab'])
        for hit in hits:
            self.assertEqual(text[hit['start']:hit['end']], hit['original'])
            self.assertFalse(hit['identity_confirmed'])
            self.assertEqual(len(hit['ambiguities']), 2)

    def test_literals_do_not_guess_masked_names(self):
        terms = recall.compile_terms([{'id': 'name', 'text': '示例甲'}])
        self.assertEqual(recall.matches('示例*甲', terms), [])
        self.assertEqual(len(recall.matches('示例甲', terms)), 1)

    def test_more_than_fifty_and_no_database_writes(self):
        self.browser([self.row(str(i)) for i in range(121)])
        before = self.state(); after = None; seen = []
        while True:
            result = recall.recall(self.cfg, after=after)
            self.assertEqual(result['total_matches'], 121)
            seen.extend(x['id'] for x in result['items'])
            if not result['more']:
                self.assertIsNone(result['next_after']); break
            after = result['next_after']
        self.assertEqual(len(set(seen)), 121)
        self.assertEqual(self.state(), before)

    def test_historical_api_word_survives_shorter_latest_document(self):
        p = xp.Project(self.project)
        for i, text in enumerate(['AB 曾用于此处', '已更新']):
            value = {'operation': 'comments', 'note_id': N, 'url': URL,
                     'captured_at': str(i), 'page': i + 1, 'cursor_in': '',
                     'data': {'comments': [{'id': 'root', 'content': text, 'sub_comments': []}], 'has_more': False}}
            file = self.base / f'raw-{i}.json'; file.write_text(json.dumps(value))
            p.ingest(file); p.db.commit()
        self.assertEqual(p.search_local('AB'), [])
        p.close()
        result = recall.recall(self.cfg)
        self.assertEqual(result['total_matches'], 1)
        self.assertIn('曾用于', result['items'][0]['text'])

    def test_same_comment_deduplicated_but_versions_kept(self):
        self.browser([self.row('one'), self.row('two', 'ordinary')])
        self.browser([self.row('one')])
        self.browser([self.row('one', 'AB 后续更正')])
        result = recall.recall(self.cfg)
        self.assertEqual(result['total_matches'], 2)
        self.assertEqual(sorted(len(r['sources']) for r in result['items']), [1, 2])
        self.assertTrue(result['counts_are_not_patients'])

    def test_missing_ids_not_merged_by_nickname(self):
        self.browser([{'text': 'AB', 'author': 'same label'}, {'text': 'AB', 'author': 'same label'}])
        self.assertEqual(recall.recall(self.cfg)['total_matches'], 2)

    def test_parent_direct_reply_and_unmatched_clarification(self):
        self.browser([self.row('root', 'which one'), self.row('answer', 'AB', is_reply=True, parentCommentId='root', replyToCommentId='root'),
                      self.row('clarify', 'another institution', is_reply=True, parentCommentId='root', replyToCommentId='answer')])
        found = recall.recall(self.cfg)['items'][0]
        self.assertEqual(found['context']['parent']['versions'][0]['text'], 'which one')
        whole = recall.recall(self.cfg, thread=(N, 'answer'), limit=1)
        self.assertEqual(whole['total_matches'], 3)
        self.assertTrue(whole['more'])
        all_rows = recall.recall(self.cfg, thread=(N, 'answer'))['items']
        self.assertTrue(any(r['text'] == 'another institution' and not r['matches'] for r in all_rows))

    def test_conflicting_parent_identity_is_not_chosen(self):
        a = self.row('root', 'first'); b = self.row('root', 'second'); b['authorId'] = 'c' * 24
        self.browser([a, b, self.row('answer', is_reply=True, parentCommentId='root')])
        context = recall.recall(self.cfg)['items'][0]['context']['parent']
        self.assertTrue(context['unique_identity_not_assumed'])
        self.assertEqual(len(context['versions']), 2)

    def test_missing_parent_is_explicit_gap(self):
        self.browser([self.row('answer', is_reply=True, parentCommentId='missing')])
        self.assertEqual(recall.recall(self.cfg)['items'][0]['context']['parent']['status'], 'body_missing')

    def test_api_nested_reply_without_root_id_is_still_reply(self):
        self.source({'operation': 'comments', 'note_id': N, 'data': {'comments': [{'content': 'root', 'sub_comments': [{'id': 'child', 'content': 'AB'}]}]}})
        row = recall.recall(self.cfg)['items'][0]
        self.assertEqual(row['kind'], 'reply'); self.assertIsNone(row['parent_comment_id'])

    def test_changed_scope_invalidates_cursor(self):
        self.browser([self.row(str(i)) for i in range(3)])
        after = recall.recall(self.cfg, limit=1)['next_after']
        self.browser([self.row('later')])
        with self.assertRaisesRegex(ValueError, 'scope changed'):
            recall.recall(self.cfg, after=after)

    def test_missing_and_corrupt_sources_are_reported(self):
        _, a = self.browser([self.row('a')]); _, b = self.browser([self.row('b')])
        a.unlink(); b.write_text('{}')
        result = recall.recall(self.cfg)
        self.assertEqual(result['total_matches'], 0)
        self.assertEqual(len(result['coverage']['gaps']), 2)
        self.assertFalse(result['coverage']['sources_complete'])

    def test_scope_selects_tasks_and_keeps_other_topic_out(self):
        wanted, _ = self.browser([self.row('wanted')])
        self.browser([self.row('unrelated')])
        p = xp.Project(self.project)
        tid = p.add_task('native', 'synthetic', {'stage': 'roots', 'target': N, 'bridge': {'hashes': [wanted]}})
        p.db.commit(); p.close()
        self.cfg['projects'][0] = {'path': str(self.project), 'task_ids': [tid]}
        result = recall.recall(self.cfg)
        self.assertEqual([r['comment_id'] for r in result['items']], ['wanted'])
        self.assertEqual(len(result['coverage']['tasks']), 1)

    def test_selected_native_task_never_expands_to_other_same_note_task(self):
        wanted, _ = self.browser([self.row('wanted')])
        other, _ = self.browser([self.row('unselected')])
        p = xp.Project(self.project)
        tid = p.add_task('native', 'roots', {'stage': 'roots', 'target': N, 'bridge': {'hashes': [wanted]}})
        p.add_task('native', 'replies', {'stage': 'replies', 'target': N, 'bridge': {'hashes': [other]}})
        p.doc(other, N, 'bridge_comment', 'AB', {}, 'comment/0')
        p.db.commit(); p.close()
        self.cfg['projects'][0] = {'path': str(self.project), 'task_ids': [tid]}
        self.assertEqual([r['comment_id'] for r in recall.recall(self.cfg)['items']], ['wanted'])

    def test_task_target_mismatch_is_not_evidence(self):
        sha, _ = self.browser([self.row('wrong-note')])
        p = xp.Project(self.project)
        tid = p.add_task('native', 'roots', {'stage': 'roots', 'target': 'd' * 24, 'bridge': {'hashes': [sha]}})
        p.db.commit(); p.close()
        self.cfg['projects'][0] = {'path': str(self.project), 'task_ids': [tid]}
        result = recall.recall(self.cfg)
        self.assertEqual(result['total_matches'], 0)
        self.assertIn('identity', result['coverage']['gaps'][0]['reason'])

    def test_direct_reply_identity_versions_do_not_merge(self):
        for basis in ('thread_root', 'observed_id'):
            self.browser([self.row('root', 'question'), self.row('child', 'AB', is_reply=True,
                          parentCommentId='root', replyToCommentId='root', replyToIdentity=basis)])
        result = recall.recall(self.cfg)
        self.assertEqual(result['total_matches'], 2)
        by_basis = {r['reply_to_identity']: r['context']['direct_reply'] for r in result['items']}
        self.assertFalse(by_basis['thread_root']['direct_target_confirmed'])
        self.assertTrue(by_basis['observed_id']['direct_target_confirmed'])

    def test_query_sort_and_incomplete_reply_status_preserved(self):
        p = xp.Project(self.project)
        for sort in ('general', 'latest', 'popular'):
            p.add_task('native', sort, {'stage': 'search', 'target': 'synthetic search', 'sort': sort,
                                       'bridge': {'observed_note_ids': [N], 'collection_end': True}})
        p.add_task('native', 'replies', {'stage': 'replies', 'target': N,
                   'bridge': {'root_collection_end': True, 'collection_end': False, 'fault': {'code': 'COMMENT_REPLY_BODY_UNAVAILABLE'}}})
        p.db.commit(); p.close()
        result = recall.recall(self.cfg, coverage_only=True)
        self.assertEqual(result['coverage']['stages'], {'search': 3, 'replies': 1})
        row = next(t for t in result['coverage']['tasks'] if t['stage'] == 'replies')
        self.assertFalse(row['end_recorded']); self.assertEqual(row['status_recorded'], 'pending')

    def test_external_paths_and_symlink_escape_rejected(self):
        for path in (xhs_paths.REPO_ROOT, xhs_paths.REPO_ROOT / '.local' / 'topic'):
            self.cfg['projects'][0]['path'] = str(path)
            with self.assertRaises(ValueError): recall.recall(self.cfg)
        self.cfg['projects'][0]['path'] = str(self.project)
        _, file = self.browser([self.row('a')]); outside = self.base / 'outside.json'
        file.rename(outside); file.symlink_to(outside)
        result = recall.recall(self.cfg)
        self.assertEqual(result['total_matches'], 0)
        self.assertIn('symlink', result['coverage']['gaps'][0]['reason'])

    def test_reference_is_not_original_patient_evidence(self):
        file = self.base / 'reference.md'; file.write_text('AB synthetic summary')
        self.cfg['references'] = [{'path': str(file), 'sha256': recall.digest(file.read_bytes())}]
        row = recall.recall(self.cfg)['items'][0]
        self.assertTrue(row['original_source_recheck_required'])
        self.assertEqual(row['kind'], 'reference_text')

    def test_unread_or_invalid_media_not_admitted_as_reviewed(self):
        with self.db() as db:
            for i, state in enumerate(['pending', 'reviewed']):
                db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
                    (str(i), N, 'media', 'AB', json.dumps({'kind': 'image', 'review_status': state}), 'media_locator', 'fixture'))
        result = recall.recall(self.cfg)
        self.assertEqual(result['total_matches'], 0)
        self.assertEqual(len(result['coverage']['gaps']), 2)

    def test_collector_uid_not_an_author(self):
        self.source({'url': URL, 'uid': 'collector', 'body': 'AB'})
        self.assertIsNone(recall.recall(self.cfg)['items'][0]['author_id'])

    def test_orphan_index_is_not_silently_complete(self):
        with self.db() as db:
            db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)', ('orphan', N, 'note', 'AB', '{}', 'platform_id', 'fixture'))
        self.assertEqual(recall.recall(self.cfg)['coverage']['gaps'][0]['reason'], 'indexed_text_has_no_immutable_source')

    def test_explicit_visual_reading_is_verified_and_not_patient_quote(self):
        image = self.project / 'synthetic-image'; image.write_bytes(b'synthetic media bytes')
        receipt = self.project / 'visual.json'
        receipt.write_text(json.dumps({'note_id': N, 'position': 2, 'actual_visual_read': True,
                           'observation': 'AB synthetic visual observation', 'file': str(image),
                           'file_sha256': recall.digest(image.read_bytes())}))
        self.cfg['media_reviews'] = [{'project': str(self.project), 'file': str(receipt), 'sha256': recall.digest(receipt.read_bytes())}]
        row = recall.recall(self.cfg)['items'][0]
        self.assertEqual(row['media_locator'], 'body_image/2')
        self.assertTrue(row['verbatim_patient_text_not_assumed'])
        image.write_bytes(b'changed')
        result = recall.recall(self.cfg)
        self.assertEqual(result['total_matches'], 0)
        self.assertFalse(result['coverage']['sources_complete'])

    def test_media_review_cannot_read_other_project(self):
        image = self.base / 'outside-image'; image.write_bytes(b'synthetic')
        receipt = self.project / 'visual.json'
        receipt.write_text(json.dumps({'note_id': N, 'position': 1, 'actual_visual_read': True,
                           'observation': 'AB', 'file': str(image), 'file_sha256': recall.digest(image.read_bytes())}))
        self.cfg['media_reviews'] = [{'project': str(self.project), 'file': str(receipt), 'sha256': recall.digest(receipt.read_bytes())}]
        self.assertEqual(recall.recall(self.cfg)['total_matches'], 0)

    def test_media_observation_never_becomes_parent_comment_words(self):
        self.browser([self.row('root', 'which clinic'), self.row('child', 'AB', parentCommentId='root', is_reply=True)])
        inv = recall.collect(self.cfg)
        child = next(r for r in inv['records'] if r['comment_id'] == 'child')
        root = next(r for r in inv['records'] if r['comment_id'] == 'root')
        media = dict(root, kind='media_observation', text='analyst reading', verbatim_patient_text_not_assumed=True)
        context = recall.related(child, inv['records'] + [media])
        self.assertEqual([v['text'] for v in context['parent']['versions']], ['which clinic'])

    def test_reviewed_media_row_and_file_hashes_are_distinct(self):
        image = self.project / 'synthetic-image'; image.write_bytes(b'synthetic media bytes')
        item = {'media_id': 'synthetic-media', 'kind': 'image', 'review_status': 'reviewed',
                'file': str(image), 'sha256': recall.digest(image.read_bytes()), 'observation': 'AB visual description'}
        with self.db() as db:
            db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
                       ('media:synthetic', N, 'media', item['observation'], json.dumps(item), 'media_locator', 'fixture'))
        row = recall.recall(self.cfg)['items'][0]
        source = row['sources'][0]
        self.assertNotIn('sha256', source)
        self.assertEqual(source['hash_scope'], 'sqlite_row')
        self.assertEqual(source['artifact_sources'][0]['sha256'], recall.digest(image.read_bytes()))
        self.assertTrue(row['verbatim_patient_text_not_assumed'])
        p = xp.Project(self.project)
        tid = p.add_task('native', 'root-only', {'stage': 'roots', 'target': N, 'bridge': {}})
        p.db.commit(); p.close()
        self.cfg['projects'][0] = {'path': str(self.project), 'task_ids': [tid]}
        self.assertEqual(recall.recall(self.cfg)['total_matches'], 0)

    def test_cli_reads_external_config_without_mutation(self):
        self.browser([self.row('one')])
        config = self.base / 'recall.json'; config.write_text(json.dumps(self.cfg))
        before = self.state()
        out = subprocess.check_output([sys.executable, recall.__file__, 'search', '--config', str(config)], text=True)
        self.assertEqual(json.loads(out)['total_matches'], 1)
        self.assertEqual(self.state(), before)


if __name__ == '__main__':
    unittest.main()
