"""Offline boundary checks for migrating an attested old web project."""
import json
import unittest
import xhs_bridge as bridge
import xhs_native as native
import xhs_project as xp
from test_native_recovery import NativeRecoveryTests


class BridgeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = NativeRecoveryTests()
        self.fixture.setUp()
        self.f = self.fixture

    def tearDown(self):
        self.fixture.tearDown()

    def test_exact_recovery_permits_web_and_preserves_api_stop(self):
        f = self.f
        before = f.p.get_meta('stop')
        with self.assertRaises(xp.legacy.StopRun):
            bridge.assert_web_project_access(f.p.path, f.uid)
        f.attest()
        bridge.assert_web_project_access(f.p.path, f.uid)
        self.assertEqual(f.p.get_meta('stop'), before)
        with self.assertRaises(xp.legacy.StopRun):
            f.p.collect_one(f.tid, f.root/'unused-run')
        with self.assertRaises(xp.legacy.StopRun):
            bridge.assert_web_project_access(f.p.path, '3'*24)

    def test_new_access_event_is_not_hidden_by_old_recovery(self):
        f = self.f
        f.attest()
        r = json.loads(f.stop_receipt.read_text())
        r['observed_at'] = '2026-11-02T10:00:00+08:00'
        f.stop_receipt.write_text(xp.dumps(r))
        native.record(f.p, f.tid, f.stop_receipt)
        with self.assertRaises(xp.legacy.StopRun):
            bridge.assert_web_project_access(f.p.path, f.uid)

    def test_missing_archive_restores_web_stop(self):
        f = self.f
        f.attest()
        row = f.p.db.execute('select * from native_web_recoveries').fetchone()
        (f.p.path/row['observation_artifact']).unlink()
        with self.assertRaises(xp.legacy.StopRun):
            bridge.assert_web_project_access(f.p.path, f.uid)

    def test_explicit_existing_grant_bound_to_context_and_account(self):
        f = self.f
        path = f.root/'existing-grant.json'
        grant = dict(approved=True, scope='rendered_public_web_only', context_id='observed',
                     account_uid=f.uid, user_text='User approved web-only verification',
                     recorded_at='2026-11-03T10:00:00+08:00', preserved_media_stops={})
        path.write_text(json.dumps(grant))
        binding = dict(context_id='other', account_uid=f.uid)
        with self.assertRaises(bridge.BridgeError):
            bridge.attach_web_authorization(f.p.path, binding, path)
        self.assertFalse((f.p.path/'bridge'/'web-channel-authorization.json').exists())
        binding['context_id'] = 'observed'
        bridge.attach_web_authorization(f.p.path, binding, path)
        self.assertEqual(path.read_bytes(), (f.p.path/'bridge'/'web-channel-authorization.json').read_bytes())
        grant['recorded_at'] = '2026-11-03T11:00:00+08:00'
        path.write_text(json.dumps(grant))
        with self.assertRaises(bridge.BridgeError):
            bridge.attach_web_authorization(f.p.path, binding, path)


if __name__ == '__main__':
    unittest.main()
