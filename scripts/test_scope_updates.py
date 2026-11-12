#!/usr/bin/env python3
"""Offline regressions for incomplete-media scope and later evidence updates."""
import json
from pathlib import Path
import tempfile
import unittest
import xhs_project as xp


class ScopeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.p = xp.Project(Path(self.temp.name) / 'project', create=True)
        self.nid = '0123456789abcdef01234567'
        self.task = self.p.add_task('note', self.nid, {'note_id': self.nid, 'needs_triage': True})
        self.p.db.commit()

    def tearDown(self):
        self.p.close()
        self.temp.cleanup()

    def test_include_queues_discussion_without_reviewing_collected_body(self):
        self.p.db.execute("UPDATE tasks SET status='complete' WHERE id=?", (self.task,))
        before = self.p.status()['outstanding_tasks']
        result = self.p.review(self.task, 'include', 'Same public author has relevant treatment diary; images unread')
        self.assertEqual((result['status'], result['review_status']), ('complete', 'pending'))
        self.assertEqual(self.p.status()['outstanding_tasks'], before + 2)
        self.assertTrue(result['payload']['scope_included'])
        self.assertFalse(result['payload']['needs_triage'])
        self.p.review(self.task, 'include', 'Scope remains relevant')
        self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 3)

    def test_include_does_not_clear_access_stop_or_missing_body(self):
        stop = {'reason': 'HTTP 461'}
        self.p.set_meta('stop', stop)
        self.p.db.execute("UPDATE tasks SET status='blocked',reason='access_stop' WHERE id=?", (self.task,))
        self.p.db.commit()
        result = self.p.review(self.task, 'include', 'Scope known but inaccessible body')
        self.assertEqual((result['status'], result['review_status'], result['reason']), ('blocked', 'pending', 'access_stop'))
        self.assertEqual(self.p.get_meta('stop'), stop)

    def test_non_note_and_excluded_not_silently_reopened(self):
        media = self.p.add_task('media', 'm', {'media_id': 'm'})
        for task in [media]:
            with self.assertRaises(xp.legacy.ResearchError):
                self.p.review(task, 'include', 'Cannot certify a media item this way')
        self.p.review(self.task, 'exclude', 'Different geographic target')
        with self.assertRaises(xp.legacy.ResearchError):
            self.p.review(self.task, 'include', 'Needs explicit reconsideration')
        self.assertEqual(self.p.task(self.task)['status'], 'excluded')

    def add_evidence(self):
        self.p.db.execute("INSERT INTO snapshots VALUES('s','snapshots/s.json','source.json','comments','2026-10-30')")
        values = [
            {'id': 'old', 'summary': 'Only one of six replies read', 'attribution': 'insufficient', 'sources': [{'snapshot_id': 's'}]},
            {'id': 'new', 'summary': 'All six replies read; doctor still unidentified', 'attribution': 'insufficient', 'sources': [{'snapshot_id': 's'}]}
        ]
        for value in values:
            self.p.evidence(value)

    def test_resolved_subgap_preserves_observations_and_unrelated_task_gap(self):
        self.add_evidence()
        original = list(self.p.db.execute('SELECT * FROM evidence ORDER BY id'))
        self.p.db.execute("UPDATE tasks SET status='blocked',reason='another_thread_missing_body' WHERE id=?", (self.task,))
        self.p.db.commit()
        first = self.p.link_evidence('old', 'new', 'resolves_gap', 'Only the six-reply collection gap; identity remains unknown')
        second = self.p.link_evidence('old', 'new', 'resolves_gap', 'Only the six-reply collection gap; identity remains unknown')
        self.assertEqual(first, second)
        self.assertEqual([tuple(r) for r in original], [tuple(r) for r in self.p.db.execute('SELECT * FROM evidence ORDER BY id')])
        self.assertEqual(self.p.task(self.task)['reason'], 'another_thread_missing_body')
        self.p.report()
        report = xp.load(self.p.path / 'coverage-report.json')
        self.assertEqual(len(report['evidence_links']), 1)
        self.assertTrue(report['evidence_links'][0]['current_sources_match'])
        self.assertEqual(report['outstanding_tasks'], 1)

    def test_changed_evidence_invalidates_link_hash_and_unknown_refs_fail(self):
        self.add_evidence()
        with self.assertRaises(xp.legacy.ResearchError):
            self.p.link_evidence('old', 'unknown', 'updates', 'Missing source')
        with self.assertRaises(xp.legacy.ResearchError):
            self.p.link_evidence('old', 'old', 'updates', 'Self reference')
        self.p.link_evidence('old', 'new', 'updates', 'Later qualified observation')
        changed = json.loads(self.p.db.execute("SELECT payload FROM evidence WHERE id='new'").fetchone()[0])
        changed['summary'] = 'Different corrected observation'
        self.p.evidence(changed)
        self.p.report()
        self.assertFalse(xp.load(self.p.path / 'coverage-report.json')['evidence_links'][0]['current_sources_match'])

    def test_import_old_stop_cannot_replace_newer_stop(self):
        old = Path(self.temp.name) / 'old'
        old.mkdir()
        (old / 'STOP.json').write_text('{"reason":"old restriction"}')
        current = {'reason': 'new restriction', 'run': '/new/run', 'at': '2026-10-30T15:00:00+00:00'}
        self.p.set_meta('stop', current)
        self.p.db.commit()
        self.p.import_run(old)
        self.assertEqual(self.p.get_meta('stop'), current)


if __name__ == '__main__':
    unittest.main()
