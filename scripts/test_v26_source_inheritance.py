"""Source-index optimization must not change round scope or completion."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_native as native
import xhs_project as xp
import xhs_rounds as rounds


class SourceInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.p = xp.Project(Path(self.temp.name) / 'project', create=True)
        native.setup(self.p)
        rounds.schema(self.p)

    def tearDown(self):
        self.p.close()
        self.temp.cleanup()

    def task(self, tid, *, stage='body', hashes=(), provenance=(), kind='native'):
        payload = {'stage': stage, 'target': 'a' * 24, 'next': 'read actual source',
                   'bridge': {'hashes': list(hashes)}, 'provenance': list(provenance)}
        if stage == 'search':
            payload.update(target='new query', sort='general')
        self.p.db.execute('INSERT INTO tasks(id,kind,entity_key,payload,updated) VALUES(?,?,?,?,?)',
                          (tid, kind, tid, xp.dumps(payload), xp.legacy.now()))
        self.p.db.commit()
        return tid

    def round(self, rid, parent, *, state='active'):
        cfg = {'cutoff': '2026-11-04T00:00:00+00:00', 'doctors': ['A'],
               'employment_rule': 'official', 'query_matrix': []}
        self.p.db.execute('INSERT INTO research_rounds VALUES(?,?,?,?)',
                          (rid, rounds.packed(cfg), rounds.now(), state))
        self.p.db.execute('INSERT INTO round_tasks(round_id,task_id) VALUES(?,?)', (rid, parent))
        self.p.db.commit()

    def task_rows(self):
        return [tuple(row) for row in self.p.db.execute('SELECT * FROM tasks ORDER BY id')]

    def test_real_manifest_source_inherits_without_scanning_task_health(self):
        source = self.p.path / 'observed.json'
        source.write_text(json.dumps({'url': 'https://www.xiaohongshu.com/explore/' + 'a' * 24,
                                      'body': 'observed public text'}))
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        parent = self.task('parent', hashes=[sha])
        self.task('unrelated')
        self.round('active', parent)
        manifest = self.p.path / 'manifest.json'
        manifest.write_text(json.dumps({'units': [{'stage': 'body', 'target': 'b' * 24,
                                                   'next': 'read', 'sources': [str(source)]}]}))
        original = self.p.task
        touched = []
        def lookup(tid, **kwargs):
            touched.append(tid)
            self.assertNotIn(tid, ['parent', 'unrelated'], 'Source indexing must not request dispatch/health')
            return original(tid, **kwargs)
        with patch.object(self.p, 'task', side_effect=lookup):
            child = native.import_manifest(self.p, manifest)['task_ids'][0]
        row = self.p.db.execute('SELECT disposition,basis FROM round_tasks WHERE round_id=? AND task_id=?',
                                ('active', child)).fetchone()
        self.assertEqual(row['disposition'], 'included')
        self.assertEqual(json.loads(row['basis'])['source']['parent_task'], parent)
        self.assertEqual(set(touched), {child})
        self.assertEqual(self.p.task(child)['status'], 'pending')
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM native_events').fetchone()[0], 0)

    def test_existing_membership_and_closed_round_are_preserved(self):
        parent = self.task('parent', hashes=['captured'])
        child = self.task('child', provenance=[{'sha256': 'captured'}])
        self.round('active', parent)
        self.round('closed', parent, state='scope_processed')
        basis = rounds.packed({'date_unknown': False, 'source': 'existing reviewed source'})
        self.p.db.execute('INSERT INTO round_tasks VALUES(?,?,?,?)', ('active', child, 'included', basis))
        self.p.db.commit()
        before = self.task_rows()
        rounds.inherit_from_sources(self.p, [child])
        rounds.inherit_from_sources(self.p, [child])
        self.assertEqual(self.p.db.execute('SELECT basis FROM round_tasks WHERE round_id=? AND task_id=?',
                                          ('active', child)).fetchone()[0], basis)
        self.assertIsNone(self.p.db.execute('SELECT 1 FROM round_tasks WHERE round_id=? AND task_id=?',
                                           ('closed', child)).fetchone())
        self.assertEqual(self.task_rows(), before)

    def test_new_search_is_deferred_and_no_task_is_completed(self):
        parent = self.task('parent', hashes=['captured'], kind='note')
        child = self.task('child-search', stage='search', provenance=[{'sha256': 'captured'}])
        self.round('active', parent)
        before = self.task_rows()
        rounds.inherit_from_sources(self.p, [child])
        row = self.p.db.execute('SELECT disposition FROM round_tasks WHERE round_id=? AND task_id=?',
                                ('active', child)).fetchone()
        self.assertEqual(row[0], 'next_round')
        self.assertEqual(self.task_rows(), before)

    def test_shared_hash_keeps_id_order_and_unknown_child_error(self):
        self.task('z-parent', hashes=['captured'])
        self.task('a-parent', hashes=['captured'], kind='note')
        child = self.task('child', provenance=[{'sha256': 'captured'}])
        self.round('active', 'z-parent')
        self.p.db.execute('INSERT INTO round_tasks(round_id,task_id) VALUES(?,?)', ('active', 'a-parent'))
        self.p.db.commit()
        rounds.inherit_from_sources(self.p, [child])
        basis = json.loads(self.p.db.execute('SELECT basis FROM round_tasks WHERE round_id=? AND task_id=?',
                                             ('active', child)).fetchone()[0])
        self.assertEqual(basis['source']['parent_task'], 'a-parent')
        with self.assertRaisesRegex(xp.legacy.ResearchError, 'Unknown task'):
            rounds.inherit_from_sources(self.p, ['not-registered'])


if __name__ == '__main__':
    unittest.main()
