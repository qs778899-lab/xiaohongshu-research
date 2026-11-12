import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from xhs_context import read_context


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.p = Path(self.temp.name)
        (self.p / 'snapshots').mkdir()
        self.db = sqlite3.connect(self.p / 'project.sqlite3')
        self.db.executescript('CREATE TABLE documents(id, note_id, kind, identity_kind); CREATE TABLE snapshots(id,path,operation,captured_at); CREATE TABLE document_sources(document_id,snapshot_id,locator);')
        self.db.execute('INSERT INTO documents VALUES(?,?,?,?)', ('comment:n:c', 'n', 'reply', 'platform_id'))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add(self, record, date='1'):
        b = json.dumps(record).encode()
        sid = hashlib.sha256(b).hexdigest()
        path = 'snapshots/' + sid + '.json'
        (self.p / path).write_bytes(b)
        self.db.execute('INSERT INTO snapshots VALUES(?,?,?,?)', (sid, path, record['operation'], date))
        self.db.execute('INSERT INTO document_sources VALUES(?,?,?)', ('comment:n:c', sid, 'unused'))
        self.db.commit()
        return self.p / path

    def api(self, uid='bot', flag=True):
        return {'note_id': 'n', 'operation': 'comments', 'data': {'comments': [{'id': 'r', 'content': '哪家', 'user_info': {'user_id': 'asker'}, 'sub_comments': [{'id': 'c', 'content': '完整回复', 'user_info': {'user_id': uid, 'ai_agent': flag}, 'target_comment': {'id': 'r', 'user_info': {'user_id': 'asker'}}}]}]}}

    def browser(self, uid='bot'):
        return {'note_id': 'n', 'operation': 'browser_comments', 'data': [{'commentId': 'r', 'text': '哪家', 'userId': 'asker'}, {'commentId': 'c', 'text': '完整...展开', 'userId': uid, 'is_reply': True, 'parentCommentId': 'r', 'replyToCommentId': 'r'}]}

    def test_short_browser_keeps_full_api_context_and_ai_marker(self):
        self.add(self.api()); self.add(self.browser(), '2')
        before = (self.p / 'project.sqlite3').read_bytes()
        result = read_context(self.p, 'comment:n:c')
        self.assertEqual(before, (self.p / 'project.sqlite3').read_bytes())
        self.assertEqual(result['text_variants'], 2)
        self.assertTrue(result['ai_agent_observed_true'])
        self.assertEqual(result['observations'][0]['text'], '完整回复')
        self.assertEqual(result['observations'][0]['reply_to_user_id'], 'asker')
        self.assertEqual(result['observations'][1]['parent_text'], '哪家')
        self.assertIsNone(result['observations'][1]['ai_agent'])

    def test_uid_and_explicit_flag_conflict_not_merged(self):
        self.add(self.api()); self.add(self.api('different', False), '2')
        result = read_context(self.p, 'comment:n:c')
        self.assertTrue(result['identity_conflict'])
        self.assertTrue(result['ai_agent_conflict'])

    def test_tamper_and_missing_snapshot(self):
        path = self.add(self.api()); original = path.read_bytes()
        path.write_bytes(original + b' ')
        with self.assertRaisesRegex(ValueError, 'checksum'): read_context(self.p, 'comment:n:c')
        path.unlink()
        self.assertEqual(read_context(self.p, 'comment:n:c')['gaps'][0]['reason'], 'snapshot_unreadable')

    def test_duplicate_and_wrong_note_do_not_make_identity(self):
        a = self.browser(); a['data'].append(dict(a['data'][1])); self.add(a)
        b = self.api(); b['note_id'] = 'other'; self.add(b, '2')
        result = read_context(self.p, 'comment:n:c')
        self.assertEqual(result['observations'], [])
        self.assertEqual(len(result['gaps']), 2)

    def test_local_identity_refused(self):
        self.db.execute("UPDATE documents SET identity_kind='snapshot_local'"); self.db.commit()
        with self.assertRaisesRegex(ValueError, 'Snapshot-local'): read_context(self.p, 'comment:n:c')

    def test_path_outside_project_refused(self):
        self.add(self.api()); self.db.execute("UPDATE snapshots SET path='../outside.json'"); self.db.commit()
        with self.assertRaisesRegex(ValueError, 'outside'): read_context(self.p, 'comment:n:c')

    def test_duplicate_parent_never_chooses_last_account(self):
        a = self.browser(); a['data'].append({'commentId': 'r', 'text': 'another root', 'userId': 'other'})
        self.add(a)
        observed = read_context(self.p, 'comment:n:c')['observations'][0]
        self.assertIsNone(observed['parent_text'])
        self.assertIsNone(observed['parent_user_id'])
        self.assertEqual(observed['parent_context_status'], 'not_established')

    def test_missing_parent_id_never_matches_missing_root_id(self):
        a = self.browser(); a['data'][0].pop('commentId'); a['data'][1].pop('parentCommentId')
        self.add(a)
        observed = read_context(self.p, 'comment:n:c')['observations'][0]
        self.assertIsNone(observed['parent_text'])
        self.assertIsNone(observed['parent_user_id'])


if __name__ == '__main__':
    unittest.main()
