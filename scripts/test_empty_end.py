"""Offline regression for terminal API pages with no result-list field."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_project as xp
import xhs_media as media


class EmptyEndTests(unittest.TestCase):
    def test_only_explicit_false_can_omit_list(self):
        for op, key in [('search', 'items'), ('comments', 'comments'), ('author_notes', 'notes')]:
            for data in [{'has_more': False}, {key: None, 'has_more': False}]:
                self.assertEqual(xp.collection_rows(op, data), [])
            for data in [{}, {'has_more': True}, {'has_more': 0}, {'has_more': 'false'}, {key: {}, 'has_more': False}, None]:
                with self.subTest(op=op, data=data), self.assertRaises(xp.legacy.ResearchError):
                    xp.collection_rows(op, data)
        with self.assertRaises(xp.legacy.ResearchError):
            xp.collection_rows('note', {'has_more': False})

    def test_terminal_pages_and_invalid_response_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            p = xp.Project(base / 'project', create=True)
            run = base / 'run'
            run.mkdir()
            tid = p.add_task('search', '医生\0general', {'query': '医生', 'sort': 'general'}, 40)

            class Api:
                result = {'has_more': True}
                calls = 0
                def search_notes(self, query, page, sort):
                    self.calls += 1
                    from test_search_adapter import receipt
                    self.last_search_request = receipt(query, page, sort)
                    return self.result

            api = Api()
            @contextmanager
            def client(_):
                yield api

            with patch.object(xp.legacy, 'ensure_active'), patch.object(xp.legacy, 'client', client):
                with self.assertRaises(xp.legacy.ResearchError):
                    p.collect_one(tid, run)
                files = list((run / 'raw').glob('diagnostic-project-*.json'))
                self.assertEqual(len(files), 1)
                self.assertEqual(json.loads(files[0].read_text())['data'], api.result)
                self.assertIn('collection_error', json.loads(files[0].read_text()))
                self.assertEqual(p.ingest(files[0])['reason'], 'collection_error')
                self.assertEqual(p.task(tid)['status'], 'pending')
                api.result = {'has_more': False}
                result = p.collect_one(tid, run)
                self.assertEqual(result['task']['status'], 'complete')
                self.assertEqual(result['task']['reason'], 'platform_returned_end')
                self.assertEqual(json.loads(Path(result['file']).read_text())['data'], {'has_more': False})
                self.assertEqual(p.db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 1)
                # Even an explicitly ingested diagnostic cannot downgrade success.
                self.assertEqual(p.ingest(files[0])['reason'], 'collection_error')
                p.resume(run, steps=1)
                self.assertEqual(p.task(tid)['status'], 'complete')
                self.assertEqual(api.calls, 2)
            p.close()

    def test_invalid_response_diagnostic_does_not_prevent_explicit_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            p = xp.Project(base / 'project', create=True)
            run = base / 'run'
            run.mkdir()
            tid = p.add_task('search', 'retry\0general', {'query': 'retry', 'sort': 'general'}, 40)

            class Api:
                result = {'has_more': True}
                calls = 0
                def search_notes(self, query, page, sort):
                    self.calls += 1
                    from test_search_adapter import receipt
                    self.last_search_request = receipt(query, page, sort)
                    return self.result

            api = Api()
            @contextmanager
            def client(_):
                yield api

            with patch.object(xp.legacy, 'ensure_active'), patch.object(xp.legacy, 'client', client):
                p.resume(run, steps=1, task_id=tid)
                self.assertEqual(p.task(tid)['status'], 'blocked')
                self.assertEqual(api.calls, 1)
                p.review(tid, 'retry', 'Retry the ordinary schema failure')
                api.result = {'has_more': False}
                p.resume(run, steps=1, task_id=tid)
                self.assertEqual(p.task(tid)['status'], 'complete')
                self.assertEqual(api.calls, 2)
                p.resume(run, steps=1)
                self.assertEqual(p.task(tid)['status'], 'complete')
                self.assertEqual(api.calls, 2)
            p.close()

    def test_null_terminal_comments_ingest_preserves_original_data(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            p = xp.Project(base / 'project', create=True)
            record = {'operation': 'comments', 'note_id': 'a' * 24, 'page': 1,
                      'cursor_in': '', 'data': {'comments': None, 'has_more': False}}
            file = base / 'terminal.json'
            original = json.dumps(record).encode()
            file.write_bytes(original)
            result = p.ingest(file)
            task = p.db.execute("SELECT status FROM tasks WHERE kind='comments'").fetchone()
            self.assertEqual(task['status'], 'complete')
            self.assertEqual((p.path / 'snapshots' / (result['snapshot'] + '.json')).read_bytes(), original)
            self.assertEqual(media.extract_media(record, str(file)), [])
            p.close()
        for data in ({'comments': None}, {'comments': None, 'has_more': True},
                     {'comments': None, 'has_more': 0}, {'comments': None, 'has_more': 'false'},
                     {'comments': {}, 'has_more': False}, {'comments': False, 'has_more': False},
                     {'comments': '', 'has_more': False}, {'comments': 0, 'has_more': False}):
            with self.subTest(data=data), self.assertRaises(media.MediaError):
                media.extract_media({'operation': 'comments', 'data': data}, 'synthetic')

    def test_unflagged_diagnostic_terminal_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            p = xp.Project(base / 'project', create=True)
            file = base / 'diagnostic-search.json'
            file.write_text(json.dumps({'operation': 'search', 'query': 'terminal', 'sort': 'general',
                                        'page': 1, 'data': {'has_more': False}}))
            self.assertIn('snapshot', p.ingest(file))
            self.assertEqual(p.db.execute("SELECT status FROM tasks WHERE kind='search'").fetchone()['status'], 'complete')
            p.close()


if __name__ == '__main__':
    unittest.main()
