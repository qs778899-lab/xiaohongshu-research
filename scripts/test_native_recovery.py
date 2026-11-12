import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import xhs_project as xp
import xhs_native as xn
import xhs_native_recovery as nr


class NativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.p = xp.Project(self.root/'project', create=True)
        self.note = '0123456789abcdef01234567'
        self.uid = '1123456789abcdef01234567'
        self.source = self.root/'stop.txt';self.source.write_text(xp.dumps(dict(account_public_uid=self.uid,
            original_stop=dict(error_url='https://www.xiaohongshu.com/search_result/'+self.note))))
        self.observation = self.root/'success.txt';self.observation.write_text('Same account, same note, actual in-site card, body visible.')
        self.tid = self.p.add_task('native', 'example', dict(stage='body', target=self.note, next='read'))
        self.stop_receipt = self.root/'stop.json'
        self.stop_receipt.write_text(xp.dumps(dict(target=self.note,action='navigate',outcome='access_stop',
            observed_at='2026-11-02T05:00:00+08:00',summary='Actual native link error',
            source_file=str(self.source),gaps=['unread'])))
        self.event = xn.record(self.p,self.tid,self.stop_receipt)['event_id']
        self.p.set_meta('stop',dict(reason='shared_native_access_state_unknown',code='300017',previous_stop=None,
            source=str(self.source),at='2026-11-02T05:00:00+08:00'))
        self.p.db.commit()
        self.r = dict(kind='same_note_native_web_recovery',event_id=self.event,
            original_event_sha256=hashlib.sha256(self.source.read_bytes()).hexdigest(),
            note_id=self.note,original_failed_url='https://www.xiaohongshu.com/search_result/'+self.note,
            recovered_url='https://www.xiaohongshu.com/explore/'+self.note+'?xsec_token=synthetic',
            original_account_uid=self.uid,account_uid=self.uid,account_verified=True,
            target_verified=True,content_ready=True,view_consistent=True,no_access_warning=True,
            entry_method='observed_in_site_card',authorization_basis='Authorized ongoing read-only research',
            positions=['body, author, account, end'],observed_at='2026-11-02T09:00:00+08:00',
            observation_file=str(self.observation),observation_sha256=hashlib.sha256(self.observation.read_bytes()).hexdigest(),
            project_stop_digest=xp.digest(self.p.get_meta('stop')))
        self.receipt = self.root/'recovery.json'
        self.observation.write_text(xp.dumps(dict(observed_at=self.r['observed_at'],note=dict(id=self.note),recovery=dict(
            original_account_uid=self.uid,current_account_uid=self.uid,
            original_failed_url=self.r['original_failed_url'],actual_success_url=self.r['recovered_url'],
            target_verified=True,content_ready=True,view_consistent=True))))
        self.r['observation_sha256']=hashlib.sha256(self.observation.read_bytes()).hexdigest()

    def tearDown(self):
        self.p.close();self.tmp.cleanup()

    def attest(self, **changes):
        self.receipt.write_text(xp.dumps(dict(self.r,**changes)))
        return nr.attest(self.p,self.receipt)

    def test_recovery_preserves_history_metadata_and_counts(self):
        before = [tuple(row) for row in self.p.db.execute('select * from native_events')]
        stop = self.p.get_meta('stop'); counts = xn.coverage(self.p)['stages']
        self.attest(); self.assertTrue(nr.attest(self.p,self.receipt)['duplicate'])
        self.p.close();self.p=xp.Project(self.root/'project')
        self.assertEqual(before,[tuple(row) for row in self.p.db.execute('select * from native_events')])
        self.assertEqual(stop,self.p.get_meta('stop'));self.assertEqual(counts,xn.coverage(self.p)['stages'])
        self.assertFalse(xn.channel_health(self.p)['access']['hold'])
        self.assertFalse(nr.native_project_stop_applies(self.p))
        self.assertEqual(self.p.task(self.tid)['next_action'],'codex_native_ui')
        self.assertEqual(self.p.task(self.tid)['status'],'pending')
        with self.assertRaises(xp.legacy.StopRun):
            self.p.collect_one(self.tid, self.root/'unused-run')
        self.p.report()
        report=xp.load(self.p.path/'coverage-report.json')
        self.assertEqual(report['stop'],stop)
        self.assertFalse(report['native_coverage']['native_web_recovery']['project_stop_applies_to_native_ui'])

    def test_wrong_account_note_old_time_and_unverified_view_rejected(self):
        for change in [dict(account_uid='2123456789abcdef01234567'),
                       dict(recovered_url='https://www.xiaohongshu.com/explore/another'),
                       dict(original_failed_url='https://www.xiaohongshu.com/explore/another'),
                       dict(observed_at='2026-11-02T04:00:00+08:00'),
                       dict(observed_at='2026-11-02T10:00:00'),
                       dict(content_ready=False),dict(no_access_warning=False),
                       dict(entry_method='guessed_url'),dict(original_event_sha256='wrong')]:
            with self.assertRaises(xp.legacy.ResearchError):self.attest(**change)
            self.assertTrue(xn.channel_health(self.p)['access']['hold'])

    def test_changed_project_stop_cannot_commit_old_attestation(self):
        self.p.set_meta('stop',{'reason':'new access stop'});self.p.db.commit()
        with self.assertRaises(xp.legacy.ResearchError):self.attest()

    def test_other_api_stop_remains_effective(self):
        for stop in [dict(reason='Imported API STOP'),dict(reason='shared_native_access_state_unknown',code='300017',previous_stop={'api':True})]:
            self.p.set_meta('stop',stop);self.p.db.commit()
            if not nr.valid_rows(self.p):
                self.r['project_stop_digest']=xp.digest(stop);self.attest()
            self.assertTrue(nr.native_project_stop_applies(self.p))
            self.assertEqual(self.p.task(self.tid)['next_action'],'preserve_access_stop')
            self.assertEqual(self.p.get_meta('stop'),stop)

    def test_new_access_stop_after_recovery_still_blocks(self):
        self.attest()
        r=json.loads(self.stop_receipt.read_text());r['observed_at']='2026-11-02T10:00:00+08:00'
        self.stop_receipt.write_text(xp.dumps(r));xn.record(self.p,self.tid,self.stop_receipt)
        self.assertTrue(xn.channel_health(self.p)['access']['hold'])
        self.assertEqual(self.p.task(self.tid)['next_action'],'preserve_access_stop')

    def test_missing_or_changed_recovery_archive_restores_hold(self):
        self.attest()
        row=self.p.db.execute('select * from native_web_recoveries').fetchone()
        f=self.p.path/row['observation_artifact'];f.write_text('changed')
        self.assertTrue(xn.channel_health(self.p)['access']['hold'])
        self.assertTrue(nr.native_project_stop_applies(self.p))
        with self.assertRaises(xp.legacy.ResearchError):nr.attest(self.p,self.receipt)

    def test_unread_recovery_observation_and_missing_stop_rejected(self):
        original = self.observation.read_bytes()
        self.observation.write_text('changed after capture')
        with self.assertRaises(xp.legacy.ResearchError):self.attest()
        self.observation.write_bytes(original)
        row=self.p.db.execute('select artifact from native_events where id=?',(self.event,)).fetchone()
        (self.p.path/row[0]).unlink()
        with self.assertRaises(xp.legacy.ResearchError):self.attest()

    def test_window_failure_not_cleared_by_web_recovery(self):
        r=json.loads(self.stop_receipt.read_text());r.update(outcome='window_missing',action='read')
        for t in ['2026-11-02T08:00:00+08:00','2026-11-02T08:01:00+08:00']:
            r['observed_at']=t;self.stop_receipt.write_text(xp.dumps(r));xn.record(self.p,self.tid,self.stop_receipt)
        self.attest()
        self.assertTrue(xn.channel_health(self.p)['read']['hold'])
        self.assertEqual(self.p.task(self.tid)['next_action'],'channel_probe_required_no_repeat_action')

    def test_self_consistent_other_note_or_account_cannot_resolve_original(self):
        other='2123456789abcdef01234567'
        for change in [
            dict(note_id=other,original_failed_url='https://www.xiaohongshu.com/search_result/'+other,
                 recovered_url='https://www.xiaohongshu.com/explore/'+other),
            dict(original_account_uid=other,account_uid=other)]:
            with self.assertRaises(xp.legacy.ResearchError):self.attest(**change)
            self.assertTrue(xn.channel_health(self.p)['access']['hold'])

    def test_rebinding_digest_cannot_override_newer_or_unrelated_meta_stop(self):
        for change in [dict(at='2026-11-02T10:00:00+08:00'),dict(source='/different/original-source.json')]:
            stop=dict(self.p.get_meta('stop'),**change);self.p.set_meta('stop',stop);self.p.db.commit()
            with self.assertRaises(xp.legacy.ResearchError):self.attest(project_stop_digest=xp.digest(stop))

    def test_receipt_cannot_rewrite_observed_success_time_or_url(self):
        for change in [dict(observed_at='2026-11-02T11:00:00+08:00'),
                       dict(recovered_url='https://www.xiaohongshu.com/explore/'+self.note+'?xsec_token=different')]:
            with self.assertRaises(xp.legacy.ResearchError):self.attest(**change)


if __name__=='__main__':unittest.main()
