#!/usr/bin/env python3
"""Offline unit tests for xhs_public_reply.py safety gates, dedupe, cooldown, and STOP."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_account_identity as identity
import xhs_bridge as bridge
import xhs_project as xp
import xhs_public_reply as pr
import xhs_research as legacy

OWN_UID = '1' * 24
OTHER_UID = '2' * 24
NOTE_ID = '3' * 24
ROOT_CID = '4' * 24
TARGET_CID = '5' * 24
NOTE_URL = f'https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=synthetic'


class FakeReplyDriver:
    def __init__(self, *, prepare_res=None, submit_res=None, submit_error=None, deep_res=None):
        self.url = NOTE_URL
        self.calls = []
        self.prepare_res = prepare_res or {
            'readOnly': True, 'uid': OWN_UID, 'url': NOTE_URL, 'visible': True,
            'destination': 'reply_to_comment', 'targetVerified': True,
            'readyForSubmit': True, 'ownCommentsInTargetScopeCount': 0, 'gaps': [], 'sendClicked': False,
        }
        self.submit_res = submit_res or {
            'readOnly': False, 'action': 'public_reply_submit', 'sendClicked': True,
            'clicksSubmitted': 1, 'clickedAt': '2026-01-01T10:00:00+00:00',
            'composerCleared': True, 'immediateDomVisible': True, 'captchaObserved': False,
            'toasts': [], 'verificationRequired': True, 'noAutomaticRetry': True,
        }
        self.submit_error = submit_error
        self.deep_res = deep_res or {
            'url': NOTE_URL,
            'results': [{'commentId': '6' * 24, 'authorHrefRaw': f'/user/profile/{OWN_UID}',
                         'parentCommentId': ROOT_CID, 'text': 'Synthetic inquiry?'}],
            'deepCoverage': {'rootPaginationState': 'complete', 'explicitEnd': True, 'remainingThreads': []},
        }

    def close(self):
        pass

    def call(self, action, binding=None, role=None, **fields):
        self.calls.append((action, role, fields))
        page = binding['tabs'][role]['page'] if binding and role else 'B' * 32
        if action == 'metadata':
            return {'page': page, 'url': self.url, 'windowId': 7}
        if action == 'identity':
            return {'uid': OWN_UID, 'url': self.url, 'visible': True}
        if action == 'navigate':
            self.url = fields['url']
            return {'navigationSubmitted': True}
        if action == 'reload':
            return {'reloadSubmitted': True}
        if action == 'public_reply_prepare':
            return dict(self.prepare_res)
        if action == 'public_reply_submit':
            if self.submit_error:
                raise self.submit_error
            return dict(self.submit_res)
        if action == 'deep':
            return dict(self.deep_res)
        raise AssertionError(action)


class PublicReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.guard = self.root / 'guards'
        self.guard.mkdir(mode=0o700)
        self.patch = patch.object(legacy, 'account_root', lambda: self.guard)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.project = xp.Project(self.root / 'project', create=True)
        self.addCleanup(self.project.close)
        self.binding = {
            'format': bridge.VERSION,
            'binding_name': 'alpha',
            'project': str(self.project.path.resolve()),
            'profile_label': 'alpha',
            'context_id': 'ctx-alpha',
            'account_uid': OWN_UID,
            'profile_guard_key': 'a' * 64,
            'session': 'xhs-alpha',
            'runtime': {'package_root': 'pinned', 'node': 'node', 'config_dir': 'cfg', 'cache_dir': 'cache'},
            'extension_version': '1.0.27',
            'background_text_collection': True,
            'tabs': {
                'search': {'page': 'A' * 32, 'session': 'xhs-alpha-search', 'owned': True, 'last_url': 'https://www.xiaohongshu.com/explore'},
                'detail': {'page': 'B' * 32, 'session': 'xhs-alpha-detail', 'owned': True, 'last_url': NOTE_URL},
            },
            'state': 'ready',
        }
        bridge.save_binding(self.project.path, 'alpha', self.binding)
        self.ledger = self.root / 'ledger.json'
        self.ledger.write_text(json.dumps({'excluded_accounts': [], 'attempts': []}))
        self.intent_path = self.root / 'intent.json'
        self._write_intent()

    def _write_intent(self, overrides=None):
        data = {
            'destination': 'reply_to_comment',
            'note_id': NOTE_ID,
            'root_comment_id': ROOT_CID,
            'recipient_comment_id': TARGET_CID,
            'recipient_uid': OTHER_UID,
            'recipient_name': 'Synthetic Author',
            'account_uid': OWN_UID,
            'url': NOTE_URL,
            'message': 'Synthetic inquiry question?',
            'maximum_send_clicks': 1,
            'private_messages': False,
            'follow': False,
            'join': False,
            'authorization': 'Explicit synthetic user authorization',
        }
        if overrides:
            data.update(overrides)
        self.intent_path.write_text(json.dumps(data, ensure_ascii=False))

    def test_dry_run_without_execute_never_submits(self):
        drv = FakeReplyDriver()
        res = pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                                   execute=False, driver=drv, sleep=lambda _: None)
        self.assertTrue(res['dry_run'])
        self.assertFalse(res['send_clicked'])
        actions = [c[0] for c in drv.calls]
        self.assertIn('public_reply_prepare', actions)
        self.assertNotIn('public_reply_submit', actions)

    def test_execute_sends_once_and_blocks_duplicate_recipient_and_intent(self):
        drv = FakeReplyDriver()
        res = pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                                   execute=True, cooldown_seconds=90, driver=drv, sleep=lambda _: None,
                                   now_ts=1767261700.0)
        self.assertTrue(res['ok'])
        self.assertTrue(res['send_clicked'])
        self.assertEqual(res['clicks_submitted'], 1)
        self.assertEqual([c[0] for c in drv.calls].count('public_reply_submit'), 1)
        # Re-attempting same recipient must fail closed before calling driver.
        drv2 = FakeReplyDriver()
        with self.assertRaisesRegex(pr.PublicReplyError, 'RECIPIENT_ALREADY_ATTEMPTED'):
            pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                                 execute=True, cooldown_seconds=0, driver=drv2, sleep=lambda _: None)
        self.assertEqual(drv2.calls, [])

    def test_cross_account_ledger_dedupe_blocks_asked_and_excluded_uids(self):
        self.ledger.write_text(json.dumps({
            'excluded_accounts': [{'uid': OTHER_UID, 'reason': 'user already asked'}],
            'attempts': [],
        }))
        with self.assertRaisesRegex(pr.PublicReplyError, 'RECIPIENT_EXCLUDED_IN_LEDGER'):
            pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                                 execute=True, driver=FakeReplyDriver(), sleep=lambda _: None)

        self.ledger.write_text(json.dumps({
            'excluded_accounts': [],
            'attempts': [{'uid': OTHER_UID, 'status': 'sent_confirmed', 'send_action_count': 1}],
        }))
        with self.assertRaisesRegex(pr.PublicReplyError, 'RECIPIENT_ALREADY_IN_LEDGER'):
            pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                                 execute=True, driver=FakeReplyDriver(), sleep=lambda _: None)

        # A zero-click not_submitted_ui_unavailable attempt IS allowed to proceed.
        self.ledger.write_text(json.dumps({
            'excluded_accounts': [],
            'attempts': [{'uid': OTHER_UID, 'status': 'not_submitted_ui_unavailable', 'send_action_count': 0}],
        }))
        res = pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                                   execute=False, driver=FakeReplyDriver(), sleep=lambda _: None)
        self.assertTrue(res['ready_to_execute'])

    def test_cooldown_blocks_too_fast_second_recipient(self):
        drv = FakeReplyDriver()
        pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                             execute=True, cooldown_seconds=90, driver=drv, sleep=lambda _: None,
                             now_ts=1767261600.0)
        # Second different recipient only 30s later must be blocked.
        self._write_intent({'recipient_uid': '9' * 24, 'recipient_comment_id': '8' * 24})
        drv2 = FakeReplyDriver()
        with self.assertRaisesRegex(pr.PublicReplyError, 'COOLDOWN_ACTIVE'):
            pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                                 execute=True, cooldown_seconds=90, driver=drv2, sleep=lambda _: None,
                                 now_ts=1767261630.0)
        self.assertEqual(drv2.calls, [])

    def test_unknown_outcome_locks_recipient_permanently_without_retry(self):
        drv = FakeReplyDriver(submit_error=bridge.BridgeError('COMMAND_OUTCOME_UNKNOWN'))
        with self.assertRaises(bridge.BridgeError):
            pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                                 execute=True, cooldown_seconds=0, driver=drv, sleep=lambda _: None)
        rec = json.loads((self.project.path / 'bridge/public-replies/by-recipient' / f'{OTHER_UID}.json').read_text())
        self.assertEqual(rec['status'], 'send_outcome_unknown_no_retry')
        self.assertTrue(rec['never_resend'])
        with self.assertRaisesRegex(pr.PublicReplyError, 'RECIPIENT_ALREADY_ATTEMPTED'):
            pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                                 execute=True, cooldown_seconds=0, driver=FakeReplyDriver(), sleep=lambda _: None)

    def test_captcha_observed_writes_shared_stop_and_locks_recipient(self):
        drv = FakeReplyDriver(submit_res={
            'sendClicked': True, 'clicksSubmitted': 1, 'clickedAt': '2026-01-01T10:00:00+00:00',
            'captchaObserved': True, 'accessWarningText': '扫码验证', 'composerCleared': False,
        })
        with self.assertRaises(legacy.StopRun):
            pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                                 execute=True, cooldown_seconds=0, driver=drv, sleep=lambda _: None)
        keys = identity.related_keys(self.binding['profile_guard_key'], OWN_UID)
        self.assertTrue(any((identity.guard_directory(k) / 'STOP.json').is_file() for k in keys))

    def test_abort_before_click_does_not_permanently_lock_recipient(self):
        drv = FakeReplyDriver(submit_res={'sendClicked': False, 'gap': 'DRAFT_TEXT_MISMATCH'})
        res = pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                                   execute=True, cooldown_seconds=0, driver=drv, sleep=lambda _: None)
        self.assertFalse(res['ok'])
        self.assertFalse(res['send_clicked'])
        self.assertEqual(res['status'], 'aborted_before_click')
        # Because zero clicks occurred, a subsequent clean attempt is permitted.
        drv2 = FakeReplyDriver()
        res2 = pr.send_public_reply(self.project.path, 'alpha', self.intent_path, self.ledger,
                                    execute=True, cooldown_seconds=0, driver=drv2, sleep=lambda _: None)
        self.assertTrue(res2['ok'])
        self.assertTrue(res2['send_clicked'])

    def test_top_level_intent_validation_and_verify_capture(self):
        self._write_intent({
            'destination': 'top_level_comment',
            'root_comment_id': None,
            'recipient_comment_id': None,
            'recipient_name': '',
        })
        out = self.root / 'capture.json'
        cap = pr.capture_verification_snapshot(self.project.path, 'alpha', self.intent_path, out,
                                               reload_page=True, driver=FakeReplyDriver(), sleep=lambda _: None)
        self.assertTrue(cap['ok'])
        self.assertTrue(out.is_file())
        self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), cap['sha256'])
        # Refuses overwriting existing verification capture.
        with self.assertRaisesRegex(pr.PublicReplyError, 'OUTPUT_ALREADY_EXISTS'):
            pr.capture_verification_snapshot(self.project.path, 'alpha', self.intent_path, out,
                                             driver=FakeReplyDriver(), sleep=lambda _: None)


if __name__ == '__main__':
    unittest.main()
