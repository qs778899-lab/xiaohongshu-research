#!/usr/bin/env python3
"""Offline unit tests for automation upgrades: busy retry, most_commented sort,
unsettled page-1 fallback, detail tab auto-activation, multi-pass verify merge,
and pipeline --stages filter.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_bridge as bridge
import xhs_gemini_pipeline as pipeline
import xhs_native as native
import xhs_project as xp
import xhs_public_reply as pr
import xhs_research as legacy
import xhs_search

OWN_UID = '1' * 24
OTHER_UID = '2' * 24
NOTE_ID = '3' * 24
ROOT_CID = '4' * 24
NOTE_URL = f'https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=synthetic'


class AutomationUpgradesTests(unittest.TestCase):
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
            'background_text_collection': True,
            'runtime': {'package_root': 'pinned', 'node': 'node', 'config_dir': 'cfg', 'cache_dir': 'cache'},
            'extension_version': '1.0.27',
            'state': 'ready',
            'tabs': {
                'search': {'page': 'A' * 32, 'owned': True, 'state': 'created', 'last_url': 'https://www.xiaohongshu.com/explore'},
                'detail': {'page': 'B' * 32, 'owned': True, 'state': 'created', 'last_url': NOTE_URL},
            },
        }
        bridge.save_binding(self.project.path, 'alpha', self.binding)

    def test_most_commented_sort_constants(self):
        self.assertEqual(xp.BROWSER_SORTS['most_commented'], 'comment_descending')
        self.assertEqual(bridge.SORT_LABELS['most_commented'], '最多评论')

    def test_retry_on_busy_succeeds_after_transient_collisions(self):
        calls = {'n': 0}
        sleeps = []

        def flaky():
            calls['n'] += 1
            if calls['n'] < 3:
                raise legacy.CollectionBusy('Account identity or collection is busy')
            return 'ok'

        self.assertEqual(bridge.retry_on_busy(flaky, max_attempts=5, wait_seconds=0.5, sleep=sleeps.append), 'ok')
        self.assertEqual(calls['n'], 3)
        self.assertEqual(sleeps, [0.5, 0.5])

    def test_collect_one_unsettled_page1_fallback_skips_advance(self):
        src = self.root / 'src.json'
        src.write_text('{}', encoding='utf-8')
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'units': [{
            'stage': 'search', 'target': 'Synthetic query', 'sort': 'general',
            'priority': 60, 'sources': [str(src)], 'next': 'Test unsettled page1',
        }]}), encoding='utf-8')
        imp = native.import_manifest(self.project, manifest)
        task = self.project.task(imp['task_ids'][0])

        search_url = 'https://www.xiaohongshu.com/search_result?keyword=Synthetic%20query&type=51'
        self.binding['tabs']['search']['last_url'] = search_url
        bridge.save_binding(self.project.path, 'alpha', self.binding)

        class UnsettledDriver:
            def __init__(self):
                self.calls = []

            def call(self, action, binding=None, role=None, **fields):
                self.calls.append((action, role, fields))
                page = binding['tabs'][role]['page']
                if action == 'metadata':
                    return {'page': page, 'url': search_url, 'windowId': 1}
                if action == 'identity':
                    return {'uid': OWN_UID, 'url': search_url, 'visible': False}
                if action == 'activate':
                    return {'activated': page, 'windowFocusRequested': False}
                if action == 'verified_capture':
                    raise bridge.BridgeError('BACKGROUND_LAYOUT_UNSETTLED')
                if action == 'capture':
                    if fields.get('allowUnsettledPage1') is True:
                        return {
                            'url': search_url, 'uid': OWN_UID, 'stage': 'search',
                            'cards': [{'url': f'https://www.xiaohongshu.com/explore/{NOTE_ID}', 'text': 'Synthetic card'}],
                            'selected': [], 'explicitEnd': False, 'visible': False,
                            'layoutUnsettledPage1Only': True,
                        }
                    raise bridge.BridgeError('BACKGROUND_LAYOUT_UNSETTLED')
                raise AssertionError(f'Unexpected action {action}')

        drv = UnsettledDriver()
        res = bridge.collect_one(self.project, self.binding, drv, task, fast_mode=True, allow_unsettled_page1=True)
        self.assertTrue(Path(res['snapshot']).is_file())
        saved = json.loads(Path(res['snapshot']).read_text(encoding='utf-8'))
        self.assertTrue(saved.get('layoutUnsettledPage1Only'))
        self.assertTrue(saved['sortVerification']['default_general_unsettled_page1'])
        self.assertFalse(any(c[0] == 'advance' for c in drv.calls))

    def test_public_reply_auto_activates_detail_and_flags_occlusion(self):
        ledger = self.root / 'ledger.json'
        ledger.write_text(json.dumps({'attempts': [], 'excluded_accounts': []}), encoding='utf-8')
        intent = self.root / 'intent.json'
        intent.write_text(json.dumps({
            'destination': 'top_level_comment',
            'note_id': NOTE_ID,
            'recipient_uid': OTHER_UID,
            'recipient_name': 'SyntheticAuthor',
            'account_uid': OWN_UID,
            'url': NOTE_URL,
            'message': 'Synthetic top-level inquiry?',
            'maximum_send_clicks': 1,
            'private_messages': False,
            'follow': False,
            'join': False,
            'authorization': 'Explicit synthetic authorization',
        }), encoding='utf-8')

        class OccludedDriver:
            def __init__(self):
                self.calls = []
                self.activated = False

            def close(self):
                pass

            def call(self, action, binding=None, role=None, **fields):
                self.calls.append((action, role, fields))
                page = binding['tabs'][role]['page']
                if action == 'metadata':
                    return {'page': page, 'url': NOTE_URL, 'windowId': 7}
                if action == 'identity':
                    return {'uid': OWN_UID, 'url': NOTE_URL, 'visible': False}
                if action == 'activate':
                    self.activated = True
                    return {'activated': page, 'windowFocusRequested': False}
                if action == 'public_reply_prepare':
                    return {
                        'readOnly': True, 'uid': OWN_UID, 'url': NOTE_URL, 'visible': False,
                        'destination': 'top_level_comment', 'targetVerified': True,
                        'readyForSubmit': True, 'ownCommentsInTargetScopeCount': 0, 'gaps': [], 'sendClicked': False,
                    }
                if action == 'public_reply_submit':
                    return {
                        'readOnly': False, 'uid': OWN_UID, 'url': NOTE_URL, 'visible': False,
                        'destination': 'top_level_comment', 'gap': 'COMPOSER_DID_NOT_OPEN', 'sendClicked': False,
                    }
                raise AssertionError(action)

        drv = OccludedDriver()
        res = pr.send_public_reply(self.project.path, 'alpha', intent, ledger, execute=True, driver=drv, sleep=lambda _: None)
        self.assertTrue(drv.activated)
        self.assertEqual(res['status'], 'aborted_before_click')
        self.assertTrue(res.get('window_occluded'))
        self.assertIn('CalculateNativeWinOcclusion', res.get('remediation_hint', ''))

    def test_verify_capture_merges_multipass_results_and_uses_root_progress(self):
        intent = self.root / 'intent-top.json'
        intent.write_text(json.dumps({
            'destination': 'top_level_comment',
            'note_id': NOTE_ID,
            'recipient_uid': OTHER_UID,
            'recipient_name': 'SyntheticAuthor',
            'account_uid': OWN_UID,
            'url': NOTE_URL,
            'message': 'Synthetic top-level inquiry?',
            'maximum_send_clicks': 1,
            'private_messages': False,
            'follow': False,
            'join': False,
            'authorization': 'Explicit synthetic authorization',
        }), encoding='utf-8')
        cp_dir = self.project.path / 'bridge' / 'comment-progress'
        cp_dir.mkdir(parents=True, exist_ok=True)
        (cp_dir / f'{NOTE_ID}.json').write_text(json.dumps({
            'observedRootIds': ['a' * 24],
            'threads': {'a' * 24: {'state': 'previously_read'}},
        }), encoding='utf-8')

        class MultiPassDriver:
            def __init__(self):
                self.pass_num = 0
                self.deep_calls = []

            def close(self):
                pass

            def call(self, action, binding=None, role=None, **fields):
                page = binding['tabs'][role]['page']
                if action == 'metadata':
                    return {'page': page, 'url': NOTE_URL, 'windowId': 7}
                if action == 'identity':
                    return {'uid': OWN_UID, 'url': NOTE_URL, 'visible': True}
                if action == 'reload':
                    return {'reloadSubmitted': True}
                if action == 'deep':
                    self.pass_num += 1
                    self.deep_calls.append(fields)
                    if self.pass_num == 1:
                        return {
                            'url': NOTE_URL,
                            'results': [{'commentId': '7' * 24, 'authorHrefRaw': f'/user/profile/{OTHER_UID}', 'text': 'Older comment'}],
                            'deepCoverage': {'rootPaginationState': 'pending', 'explicitEnd': False, 'observedRootIds': ['7' * 24]},
                        }
                    return {
                        'url': NOTE_URL,
                        'results': [{'commentId': '8' * 24, 'authorHrefRaw': f'/user/profile/{OWN_UID}', 'text': 'Synthetic top-level inquiry?'}],
                        'deepCoverage': {'rootPaginationState': 'complete', 'explicitEnd': True, 'remainingThreads': [], 'observedRootIds': ['7' * 24, '8' * 24]},
                    }
                raise AssertionError(action)

        drv = MultiPassDriver()
        out = self.root / 'cap.json'
        res = pr.capture_verification_snapshot(self.project.path, 'alpha', intent, out, max_passes=4, driver=drv, sleep=lambda _: None)
        self.assertEqual(res['results_count'], 2)
        saved = json.loads(out.read_text(encoding='utf-8'))
        cids = [r['commentId'] for r in saved['results']]
        self.assertEqual(cids, ['7' * 24, '8' * 24])
        self.assertEqual(drv.deep_calls[0]['deepReplyClicks'], 1)
        self.assertIn('threads', drv.deep_calls[0]['progress'])

    def test_pipeline_stages_filter_prioritizes_search_over_body(self):
        src = self.root / 'src2.json'
        src.write_text(json.dumps({'url': NOTE_URL}), encoding='utf-8')
        manifest = self.root / 'manifest2.json'
        manifest.write_text(json.dumps({'units': [
            {'stage': 'body', 'target': NOTE_ID, 'priority': 90, 'sources': [str(src)], 'next': 'Read body'},
            {'stage': 'search', 'target': 'Synthetic query', 'sort': 'most_commented', 'priority': 40, 'sources': [str(src)], 'next': 'Run search'},
        ]}), encoding='utf-8')
        native.import_manifest(self.project, manifest)

        default_task, _ = pipeline._pick_next_collect_task(
            self.project, self.binding, round_id=None, include_media=False, active_note=None
        )
        self.assertEqual(default_task['payload']['stage'], 'body')

        search_task, _ = pipeline._pick_next_collect_task(
            self.project, self.binding, round_id=None, include_media=False, active_note=None, stages={'search'}
        )
        self.assertEqual(search_task['payload']['stage'], 'search')
        self.assertEqual(search_task['payload']['sort'], 'most_commented')


if __name__ == '__main__':
    unittest.main()
