import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import sqlite3
from unittest.mock import patch

import xhs_project as xp
import xhs_native as native


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = xp.Project(self.root / 'project', create=True)
        self.source = self.root / 'source.txt'
        self.source.write_text('Observed root A, reply B. No real comment IDs available.')
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'units': [dict(stage='replies', target='note1', next='read remaining floors', sources=[str(self.source)])]}))
        self.tid = native.import_manifest(self.p, self.manifest)['task_ids'][0]
        self.sequence = 0

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def receipt(self, **changes):
        self.sequence += 1
        r = dict(target='note1', action='expand', outcome='ready', observed_at=str(self.sequence),
                 summary='Observed replies', source_file=str(self.source), gaps=[],
                 target_verified=True, content_ready=True, view_consistent=True,
                 url='https://www.xiaohongshu.com/explore/note1')
        r.update(changes)
        f = self.root / ('receipt%d.json' % self.sequence)
        f.write_text(json.dumps(r))
        return f

    def complete(self):
        return native.record(self.p, self.tid, self.receipt(complete=True, reviewed=True,
                             end='all_discovered_threads_read', positions=['root A / reply B']))

    def test_import_idempotent_preserves_legacy(self):
        native.import_manifest(self.p, self.manifest)
        self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
        self.assertEqual(self.p.task(self.tid)['status'], 'pending')
        self.assertEqual(self.source.read_text(), 'Observed root A, reply B. No real comment IDs available.')
        self.assertFalse(self.p.task(self.tid)['automatable'])
        self.assertEqual(self.p.next(10, automatic=True), [])

    def test_lookup_exact_target_preserves_queue_and_stop(self):
        self.complete()
        native.record(self.p, self.tid, self.receipt(gaps=['smaller viewport']))
        self.p.set_meta('stop', {'reason': 'preserved'}); self.p.db.commit()
        before = list(self.p.db.iterdump())
        result = native.lookup(self.p, 'note1')
        self.assertTrue(result['registered_scopes'][0]['processed'])
        self.assertIsNone(result['registered_scopes'][0]['next'])
        self.assertEqual(len(result['registered_scopes'][0]['sources']), 2)
        self.assertEqual(native.lookup(self.p, 'note')['registered_scopes'], [])
        self.assertEqual(list(self.p.db.iterdump()), before)

    def test_lookup_completion_source_loss_and_new_gap(self):
        self.complete()
        native.record(self.p, self.tid, self.receipt(new_gap=True, gaps=['new floor'], next='read new floor'))
        item = native.lookup(self.p, 'note1')['registered_scopes'][0]
        self.assertFalse(item['processed'])
        self.assertEqual(item['next'], 'read new floor')
        self.complete()
        event = self.p.db.execute('SELECT artifact FROM native_events LIMIT 1').fetchone()
        (self.p.path / event[0]).unlink()
        item = native.lookup(self.p, 'note1')['registered_scopes'][0]
        self.assertFalse(item['processed'])
        self.assertFalse(item['sources'][0]['source_valid'])
        self.assertEqual(item['next_action'], 'audit_completion_source')

    def test_lookup_real_cli_does_not_rewrite_database(self):
        self.complete()
        self.p.set_meta('stop', {'reason': 'preserved'}); self.p.db.commit()
        path = self.p.path / 'project.sqlite3'
        before = path.read_bytes()
        result = subprocess.run([sys.executable, native.__file__, '--project', str(self.p.path),
                                 'lookup', '--target', 'note1'], capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(result.stdout)['registered_scopes'][0]['processed'])
        self.assertEqual(path.read_bytes(), before)
        ro = native.open_lookup_project(self.p.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro.set_meta('stop', None)
        finally:
            ro.close()
        missing = self.root / 'nonexistent'
        with self.assertRaises(xp.legacy.ResearchError):
            native.open_lookup_project(missing)
        self.assertFalse(missing.exists())

    def test_lookup_lists_separate_composite_gaps_not_similar_ids(self):
        note = 'a' * 24
        self.manifest.write_text(json.dumps({'units': [
            dict(stage='body', target=note, next='body', sources=[str(self.source)]),
            dict(stage='legacy_audit', target=note + ':deleted-reply-context', next='missing context', sources=[str(self.source)]),
            dict(stage='legacy_audit', target=note + '0:deleted-reply-context', next='different target', sources=[str(self.source)])]}))
        native.import_manifest(self.p, self.manifest)
        result = native.lookup(self.p, note)
        self.assertEqual(len(result['registered_scopes']), 1)
        self.assertEqual(len(result['related_scopes']), 1)
        self.assertEqual(result['related_scopes'][0]['next'], 'missing context')
        self.assertFalse(result['related_scopes'][0]['processed'])
        self.assertEqual(native.lookup(self.p, note[:8])['related_scopes'], [])

    def test_mismatched_target_rejected(self):
        with self.assertRaises(xp.legacy.ResearchError):
            native.record(self.p, self.tid, self.receipt(target='different-note'))
        with self.assertRaises(xp.legacy.ResearchError):
            native.record(self.p, self.tid, self.receipt(url='https://www.xiaohongshu.com/explore/old-note'))

    def test_optional_task_id_mismatch_rejected_before_any_side_effect(self):
        before = list(self.p.db.iterdump())
        files = {str(f.relative_to(self.p.path)): f.read_bytes()
                 for f in self.p.path.rglob('*') if f.is_file()}
        for task_id in ('another-native-task', None):
            with self.subTest(task_id=task_id):
                receipt = self.receipt(task_id=task_id, complete=True, reviewed=True,
                                       end='all_discovered_threads_read', positions=['root A / reply B'])
                with patch.object(native, 'archive', wraps=native.archive) as archive:
                    with self.assertRaisesRegex(xp.legacy.ResearchError, 'Receipt task ID differs'):
                        native.record(self.p, self.tid, receipt)
                    archive.assert_not_called()
                self.assertEqual(list(self.p.db.iterdump()), before)
                self.assertEqual(files, {str(f.relative_to(self.p.path)): f.read_bytes()
                                        for f in self.p.path.rglob('*') if f.is_file()})

    def test_optional_matching_task_id_and_legacy_missing_field_remain_compatible(self):
        result = native.record(self.p, self.tid, self.receipt(task_id=self.tid))
        event = self.p.db.execute('SELECT receipt FROM native_events WHERE id=?',
                                 (result['event_id'],)).fetchone()
        self.assertEqual(json.loads(event[0])['task_id'], self.tid)
        self.assertEqual(self.p.task(self.tid)['status'], 'pending')
        result = self.complete()
        event = self.p.db.execute('SELECT receipt FROM native_events WHERE id=?',
                                 (result['event_id'],)).fetchone()
        self.assertNotIn('task_id', json.loads(event[0]))
        self.assertEqual(self.p.task(self.tid)['status'], 'complete')
        self.assertTrue(native.valid_completion(self.p, self.p.task(self.tid)))

    def test_stale_view_cannot_complete_or_reset_health(self):
        for outcome in ['no_effect', 'snapshot_mismatch']:
            native.record(self.p, self.tid, self.receipt(outcome=outcome, gaps=['unread']))
        self.assertEqual(self.p.next()[0]['next_action'], 'channel_probe_required_no_repeat_action')
        with self.assertRaises(xp.legacy.ResearchError):
            native.record(self.p, self.tid, self.receipt(view_consistent=False))
        self.assertTrue(native.channel_health(self.p)['expand']['hold'])

    def test_failure_budget_survives_restart_and_deduplicates(self):
        f = self.receipt(outcome='no_effect', gaps=['13 replies'])
        native.record(self.p, self.tid, f)
        native.record(self.p, self.tid, f)
        self.p.close(); self.p = xp.Project(self.root / 'project')
        self.assertEqual(native.channel_health(self.p)['expand']['consecutive_failures'], 1)
        native.record(self.p, self.tid, self.receipt(outcome='window_missing', gaps=['13 replies']))
        self.assertTrue(native.channel_health(self.p)['expand']['hold'])
        native.record(self.p, self.tid, self.receipt())
        self.assertFalse(native.channel_health(self.p)['expand']['hold'])
        self.assertEqual(self.p.task(self.tid)['status'], 'pending')

    def test_no_false_complete_from_partial_or_wrong_end(self):
        for changes in [dict(gaps=['remaining root 201']), dict(reviewed=False),
                        dict(end='platform_end'), dict(outcome='content_loading')]:
            params = dict(complete=True, reviewed=True, end='all_discovered_threads_read', positions=['root'])
            params.update(changes)
            with self.assertRaises(xp.legacy.ResearchError):
                native.record(self.p, self.tid, self.receipt(**params))
        self.assertEqual(self.p.status()['outstanding_tasks'], 1)

    def test_smaller_snapshot_keeps_accumulated_completion_new_gap_reopens(self):
        self.complete()
        native.record(self.p, self.tid, self.receipt(gaps=['current viewport small']))
        self.assertEqual(self.p.task(self.tid)['status'], 'complete')
        native.record(self.p, self.tid, self.receipt(gaps=['new root 201'], new_gap=True))
        self.assertEqual(self.p.task(self.tid)['status'], 'pending')
        self.assertIsNotNone(self.p.task(self.tid)['payload']['completion_event'])

    def test_completion_source_loss_reported(self):
        self.complete()
        self.assertEqual(self.p.status()['outstanding_tasks'], 0)
        event = self.p.db.execute('SELECT artifact FROM native_events').fetchone()
        (self.p.path / event[0]).unlink()
        self.assertEqual(self.p.status()['outstanding_tasks'], 1)
        self.assertEqual(self.p.next()[0]['id'], self.tid)
        self.assertEqual(self.p.next()[0]['review_status'], 'pending')
        self.p.report()
        report = xp.load(self.p.path / 'coverage-report.json')
        self.assertEqual(report['remaining'][0]['review_status'], 'pending')

    def test_stop_not_cleared_by_ready_record(self):
        native.record(self.p, self.tid, self.receipt(outcome='access_stop', gaps=['login']))
        native.record(self.p, self.tid, self.receipt())
        self.assertEqual(self.p.task(self.tid)['next_action'], 'preserve_access_stop')
        self.p.set_meta('stop', {'reason': 'preserved'}); self.p.db.commit()
        native.import_manifest(self.p, self.manifest)
        self.assertEqual(self.p.get_meta('stop'), {'reason': 'preserved'})

    def test_generic_review_cannot_bypass_receipt(self):
        with self.assertRaises(xp.legacy.ResearchError):
            self.p.review(self.tid, 'reviewed', 'looks done')

    def test_reimport_does_not_reopen_reviewed_scope(self):
        self.complete(); native.import_manifest(self.p, self.manifest)
        self.assertEqual(self.p.status()['outstanding_tasks'], 0)
        self.assertEqual(self.p.next(), [])

    def test_reimport_preserves_newer_checkpoint(self):
        native.record(self.p, self.tid, self.receipt(next='parent 201, next 10 replies'))
        native.import_manifest(self.p, self.manifest)
        self.assertEqual(self.p.task(self.tid)['payload']['next'], 'parent 201, next 10 replies')

    def test_receipts_bound_to_each_scope(self):
        other = self.p.add_task('native', 'other', dict(stage='roots', target='note1', next='read roots'))
        receipt = self.receipt(action='read')
        native.record(self.p, self.tid, receipt)
        native.record(self.p, other, receipt)
        self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM native_events').fetchone()[0], 2)

    def test_unread_gap_is_not_observed_reviewed_or_complete(self):
        native.record(self.p, self.tid, self.receipt(action='offline', outcome='offline',
                      content_observed=False, reviewed=False, gaps=['not opened']))
        counts = native.coverage(self.p)['stages']['replies']
        self.assertEqual((counts['observed_units'], counts['reviewed_observation_units'],
                          counts['processed_units'], counts['pending_units']), (0, 0, 0, 1))
        for changes in [dict(reviewed=True), dict(outcome='ready', action='expand'),
                        dict(complete=True)]:
            params = dict(action='offline', outcome='offline', content_observed=False, reviewed=False)
            params.update(changes)
            with self.assertRaises(xp.legacy.ResearchError):
                native.record(self.p, self.tid, self.receipt(**params))

    def test_audit_correction_is_append_only_and_later_read_counts(self):
        original = native.record(self.p, self.tid, self.receipt(reviewed=True, gaps=['never read']))
        correction = self.receipt(action='offline', outcome='offline', content_observed=False,
                                  reviewed=False, corrects_observation_event=original['event_id'])
        native.record(self.p, self.tid, correction)
        self.assertTrue(native.record(self.p, self.tid, correction)['duplicate'])
        self.p.close(); self.p = xp.Project(self.root / 'project')
        self.assertEqual(native.coverage(self.p)['stages']['replies']['observed_units'], 0)
        self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM native_events').fetchone()[0], 2)
        native.record(self.p, self.tid, self.receipt(reviewed=True, content_observed=True))
        self.assertEqual(native.coverage(self.p)['stages']['replies']['reviewed_observation_units'], 1)
        self.complete()
        native.record(self.p, self.tid, self.receipt(action='offline', outcome='offline',
                      content_observed=False, reviewed=False, gaps=['future follow-up']))
        self.assertEqual(native.coverage(self.p)['stages']['replies']['processed_units'], 1)

    def test_correction_cannot_target_other_scope_or_completion(self):
        completed = self.complete()
        other = self.p.add_task('native', 'other', dict(stage='roots', target='note1', next='read roots'))
        receipt = self.receipt(action='offline', outcome='offline', content_observed=False,
                              reviewed=False, corrects_observation_event=completed['event_id'])
        for task in [other, self.tid]:
            with self.assertRaises(xp.legacy.ResearchError):
                native.record(self.p, task, receipt)
        self.assertEqual(native.coverage(self.p)['stages']['replies']['processed_units'], 1)

    def test_correction_does_not_suppress_real_channel_failure(self):
        for _ in range(2):
            native.record(self.p, self.tid, self.receipt(outcome='no_effect', gaps=['unread']))
        bad = native.record(self.p, self.tid, self.receipt(reviewed=True))
        native.record(self.p, self.tid, self.receipt(action='offline', outcome='offline',
                      content_observed=False, reviewed=False, corrects_observation_event=bad['event_id']))
        self.assertTrue(native.channel_health(self.p)['expand']['hold'])
        native.record(self.p, self.tid, self.receipt(content_observed=True))
        self.assertFalse(native.channel_health(self.p)['expand']['hold'])

    def test_correction_source_loss_is_visible_and_cannot_erase_counts(self):
        original = native.record(self.p, self.tid, self.receipt(reviewed=True))
        corrected = native.record(self.p, self.tid, self.receipt(action='offline', outcome='offline',
                      content_observed=False, reviewed=False, corrects_observation_event=original['event_id']))
        row = self.p.db.execute('SELECT receipt FROM native_events WHERE id=?', (corrected['event_id'],)).fetchone()
        (self.p.path / json.loads(row[0])['receipt_archive']).unlink()
        result = native.coverage(self.p)
        self.assertEqual(result['stages']['replies']['observed_units'], 1)
        self.assertEqual(result['invalid_review_bindings'][0]['reason'],
                         'native_observation_correction_source_missing_or_changed')

    def test_observation_correction_cannot_erase_failures_or_access_stop(self):
        events = [native.record(self.p, self.tid, self.receipt(outcome=outcome))
                  for outcome in ['no_effect', 'no_effect', 'access_stop']]
        for event in events:
            with self.assertRaises(xp.legacy.ResearchError):
                native.record(self.p, self.tid, self.receipt(action='offline', outcome='offline',
                              content_observed=False, reviewed=False,
                              corrects_observation_event=event['event_id']))
        health = native.channel_health(self.p)
        self.assertTrue(health['expand']['hold'])
        self.assertTrue(health['access']['hold'])
        self.assertEqual(self.p.task(self.tid)['next_action'], 'preserve_access_stop')

    def test_invalid_corrections_are_audits_not_duplicate_pending_tasks(self):
        for _ in range(2):
            original = native.record(self.p, self.tid, self.receipt(reviewed=True))
            result = native.record(self.p, self.tid, self.receipt(action='offline', outcome='offline',
                                   content_observed=False, reviewed=False,
                                   corrects_observation_event=original['event_id']))
            event = self.p.db.execute('SELECT receipt FROM native_events WHERE id=?', (result['event_id'],)).fetchone()
            (self.p.path / json.loads(event[0])['receipt_archive']).unlink()
        other = self.p.add_task('native', 'other', dict(stage='replies', target='note1', next='read'))
        native.record(self.p, other, self.receipt(complete=True, reviewed=True,
                      end='all_discovered_threads_read', positions=['root A / reply B']))
        status = self.p.status()
        self.assertEqual(status['outstanding_tasks'], 1)
        self.assertEqual(status['outstanding_observation_audits'], 1)
        self.assertIn(dict(kind='native', status='complete', review_status='reviewed', count=1), status['task_counts'])
        self.complete()
        status = self.p.status()
        self.assertEqual(status['outstanding_tasks'], 0)
        self.assertEqual(status['outstanding_observation_audits'], 1)
        self.assertEqual(status['state'], 'work_remaining')
        task = self.p.next()[0]
        self.assertTrue(task['observation_audit_required'])
        self.assertEqual((task['status'], task['review_status']), ('complete', 'reviewed'))
        self.p.report()
        remaining = xp.load(self.p.path / 'coverage-report.json')['remaining']
        self.assertEqual(len(remaining), 1)
        self.assertEqual((remaining[0]['status'], remaining[0]['review_status']), ('complete', 'reviewed'))

    def test_receipt_archive_loss_invalidates_completion(self):
        self.complete()
        row = self.p.db.execute('SELECT receipt FROM native_events').fetchone()
        (self.p.path / json.loads(row[0])['receipt_archive']).unlink()
        self.assertEqual(self.p.status()['outstanding_tasks'], 1)

    def test_encoded_search_query_must_match(self):
        task = {'payload': {'stage': 'search', 'target': '示例机构 常见'}}
        native.validate_url(task, 'https://www.xiaohongshu.com/search_result?keyword=%25E7%25A4%25BA%25E4%25BE%258B%25E6%259C%25BA%25E6%259E%2584%2520%25E5%25B8%25B8%25E8%25A7%2581')
        with self.assertRaises(xp.legacy.ResearchError):
            native.validate_url(task, 'https://www.xiaohongshu.com/search_result?keyword=old')

    def test_input_failure_holds_search_until_input_recovery(self):
        search = self.p.add_task('native', 'search', dict(stage='search', target='note1', sort='general', next='input query'))
        for _ in range(2):
            native.record(self.p, search, self.receipt(action='input', outcome='input_stale', gaps=['input did not change']))
        self.assertEqual(self.p.task(search)['next_action'], 'channel_probe_required_no_repeat_action')
        new_search = self.p.add_task('native', 'search2', dict(stage='search', target='other query', sort='general', next='input query'))
        self.assertEqual(self.p.task(new_search)['next_action'], 'channel_probe_required_no_repeat_action')
        native.record(self.p, search, self.receipt(action='read', url='https://www.xiaohongshu.com/search_result?keyword=note1'))
        self.assertEqual(self.p.task(search)['next_action'], 'channel_probe_required_no_repeat_action')
        native.record(self.p, search, self.receipt(action='input', url='https://www.xiaohongshu.com/search_result?keyword=note1'))
        self.assertEqual(self.p.task(search)['next_action'], 'codex_native_ui')

    def legacy_author(self, target='note1:comment:unknown:uid-pending'):
        key = xp.dumps(['author', target, ''])
        archive, sha = native.archive(self.p, self.source)
        tid = self.p.add_task('native', key, dict(stage='author', target=target, next='resolve public UID',
                             provenance=[dict(archive=archive, sha256=sha)]))
        self.p.db.commit()
        return tid

    def test_placeholder_migration_preserves_failures_and_reimport(self):
        tid = self.legacy_author(); target = self.p.task(tid)['payload']['target']
        for _ in range(2):
            native.record(self.p, tid, self.receipt(target=target, action='navigate', outcome='no_effect', reviewed=False))
        native.record(self.p, tid, self.receipt(target=target, action='navigate', outcome='access_stop', reviewed=False))
        self.p.set_meta('stop', {'reason': 'shared account stop'}); self.p.db.commit()
        before = [tuple(row) for row in self.p.db.execute('SELECT * FROM native_events')]
        result = native.migrate_author_placeholder(self.p, tid, self.source, 'UID unknown; history unread')
        self.assertEqual(result['stage'], 'identity')
        self.assertEqual(before, [tuple(row) for row in self.p.db.execute('SELECT * FROM native_events')])
        self.p.close(); self.p = xp.Project(self.root / 'project')
        self.assertTrue(native.migrate_author_placeholder(self.p, tid, self.source, 'same request')['duplicate'])
        for stage in ('author', 'identity'):
            self.manifest.write_text(json.dumps({'units': [dict(stage=stage, target=target, next='old checkpoint', sources=[str(self.source)])]}))
            self.assertEqual(native.import_manifest(self.p, self.manifest)['task_ids'], [tid])
            self.assertEqual(self.p.task(tid)['payload']['stage'], 'identity')
        self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 2)
        health = native.channel_health(self.p)
        self.assertTrue(health['navigate']['hold']); self.assertTrue(health['access']['hold'])
        self.assertEqual(self.p.get_meta('stop'), {'reason': 'shared account stop'})
        self.assertEqual(self.p.task(tid)['next_action'], 'preserve_access_stop')
        counts = native.coverage(self.p)['stages']['identity']
        self.assertEqual((counts['known_units'], counts['observed_units'], counts['processed_units'], counts['pending_units']), (1, 0, 0, 1))

    def test_author_import_rejects_placeholders_atomically(self):
        for bad in ('昵称', 'uid-pending', 'https://www.xiaohongshu.com/user/profile/0123456789abcdef01234567'):
            self.manifest.write_text(json.dumps({'units': [
                dict(stage='author', target='0123456789abcdef01234567', next='read history', sources=[str(self.source)]),
                dict(stage='author', target=bad, next='read history', sources=[str(self.source)])]}))
            with self.assertRaises(xp.legacy.ResearchError):
                native.import_manifest(self.p, self.manifest)
            self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_migration_rejects_observation_completion_real_uid_and_collision(self):
        for changes in ({'reviewed': True}, {}, {'content_observed': True}):
            tid = self.legacy_author('unknown-' + str(changes))
            native.record(self.p, tid, self.receipt(target=self.p.task(tid)['payload']['target'],
                          action='offline', outcome='offline', **changes))
            with self.assertRaises(xp.legacy.ResearchError):
                native.migrate_author_placeholder(self.p, tid, self.source, 'reason')
        tid = self.legacy_author('0123456789abcdef01234567')
        with self.assertRaises(xp.legacy.ResearchError):
            native.migrate_author_placeholder(self.p, tid, self.source, 'reason')
        tid = self.legacy_author('completed-placeholder')
        self.p.db.execute("UPDATE tasks SET status='complete' WHERE id=?", (tid,)); self.p.db.commit()
        with self.assertRaises(xp.legacy.ResearchError):
            native.migrate_author_placeholder(self.p, tid, self.source, 'reason')
        tid = self.legacy_author(); target = self.p.task(tid)['payload']['target']
        self.p.add_task('native', xp.dumps(['identity', target, '']), dict(stage='identity', target=target, next='existing'))
        self.p.db.commit()
        with self.assertRaises(xp.legacy.ResearchError):
            native.migrate_author_placeholder(self.p, tid, self.source, 'reason')
        self.assertEqual(self.p.task(tid)['payload']['stage'], 'author')

    def test_migration_source_loss_reported_and_reimport_blocked(self):
        tid = self.legacy_author()
        result = native.migrate_author_placeholder(self.p, tid, self.source, 'resolve unknown account')
        # Keep a separate real observation source so only migration provenance is lost.
        observed = self.root / 'resolved.txt'; observed.write_text('Identity link checked; author history remains separate.')
        native.record(self.p, tid, self.receipt(target=self.p.task(tid)['payload']['target'], action='offline',
                      outcome='offline', source_file=str(observed), complete=True, reviewed=True,
                      end='assessment_recorded', positions=['resolved link']))
        (self.p.path / result['migration']['source_archive']).unlink()
        self.assertEqual(native.coverage(self.p)['invalid_review_bindings'][0]['reason'],
                         'native_scope_migration_source_missing_or_changed')
        with self.assertRaises(xp.legacy.ResearchError):
            native.migrate_author_placeholder(self.p, tid, self.source, 'again')
        status = self.p.status()
        self.assertEqual(status['outstanding_observation_audits'], 1)
        self.assertEqual(status['state'], 'work_remaining')
        task = next(t for t in self.p.next() if t['id'] == tid)
        self.assertTrue(task['observation_audit_required'])
        self.p.report()
        self.assertIn(tid, [t['id'] for t in xp.load(self.p.path / 'coverage-report.json')['remaining']])

    def test_migration_transaction_rolls_back_on_update_error(self):
        tid = self.legacy_author()
        self.p.db.execute("CREATE TRIGGER reject_scope_update BEFORE UPDATE OF entity_key ON tasks BEGIN SELECT RAISE(ABORT, 'synthetic crash'); END")
        self.p.db.commit()
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            native.migrate_author_placeholder(self.p, tid, self.source, 'reason')
        self.assertEqual(self.p.task(tid)['payload']['stage'], 'author')
        self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM native_scope_migrations').fetchone()[0], 0)

    def test_migration_rejects_missing_unobserved_import_provenance(self):
        tid = self.legacy_author()
        archive = self.p.task(tid)['payload']['provenance'][0]['archive']
        archived = self.p.path / archive
        original = archived.read_bytes()
        for corrupt in ('changed', 'missing'):
            if corrupt == 'changed': archived.write_text('different source')
            else: archived.unlink()
            with self.assertRaises(xp.legacy.ResearchError):
                native.migrate_author_placeholder(self.p, tid, self.source, 'reason')
            self.assertEqual(self.p.task(tid)['payload']['stage'], 'author')
            self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM native_scope_migrations').fetchone()[0], 0)
        archived.write_bytes(original)
        native.migrate_author_placeholder(self.p, tid, self.source, 'valid original archive')

    def test_identity_resolution_does_not_complete_real_author(self):
        tid = self.legacy_author(); target = self.p.task(tid)['payload']['target']
        native.migrate_author_placeholder(self.p, tid, self.source, 'UID pending')
        native.record(self.p, tid, self.receipt(target=target, action='offline', outcome='offline',
                      complete=True, reviewed=True, end='assessment_recorded', positions=['verified account link']))
        uid = '0123456789abcdef01234567'
        self.manifest.write_text(json.dumps({'units': [dict(stage='author', target=uid, next='read related history', sources=[str(self.source)])]}))
        author = native.import_manifest(self.p, self.manifest)['task_ids'][0]
        self.assertEqual(self.p.task(author)['status'], 'pending')
        native.record(self.p, author, self.receipt(target=uid, action='navigate', url='https://www.xiaohongshu.com/user/profile/'+uid,
                      complete=True, reviewed=True, end='platform_end', positions=['all related history']))
        self.assertEqual(native.coverage(self.p)['stages']['author']['processed_units'], 1)


if __name__ == '__main__':
    unittest.main()
