#!/usr/bin/env python3
"""Offline tests for bridge fast_mode, non_blocking_review, and Gemini actor/reviewer support."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import xhs_bridge as b
import xhs_native as native
import xhs_project as xp
import xhs_research as legacy
import xhs_rounds as rounds

UID = '1' * 24
NOTE1 = '2' * 24
NOTE2 = '3' * 24
URL1 = 'https://www.xiaohongshu.com/explore/' + NOTE1 + '?xsec_token=observed1'
URL2 = 'https://www.xiaohongshu.com/explore/' + NOTE2 + '?xsec_token=observed2'
SEARCH_URL = 'https://www.xiaohongshu.com/search_result?keyword=synthetic&type=51'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6kU8AAAAASUVORK5CYII=')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FastFakeDriver:
    def __init__(self, url, uid=UID, *, filter_closed=True, verified_capture_error=None):
        self.url = url
        self.uid = uid
        self.filter_closed = filter_closed
        self.verified_capture_error = verified_capture_error
        self.calls = []
        self.metrics = {'calls': 0, 'elapsed_seconds': 0.0}

    def close(self):
        pass

    def call(self, action, binding=None, role=None, **fields):
        self.calls.append((action, role, fields))
        self.metrics['calls'] += 1
        page = binding['tabs'][role]['page'] if binding and role else 'p1'
        if action == 'verified_capture':
            if self.verified_capture_error:
                raise b.BridgeError(self.verified_capture_error)
            if fields.get('stage') == 'search':
                return {'page': page, 'windowId': 7, 'url': self.url, 'uid': self.uid, 'visible': False,
                        'selected': ['综合'], 'cards': [{'url': URL1, 'text': 'card 1'}],
                        'position': {'scrollTop': 0, 'scrollHeight': 1200, 'viewportHeight': 800},
                        'explicitEnd': False}
            return {'page': page, 'windowId': 7, 'url': self.url, 'uid': self.uid, 'visible': False,
                    'title': 'title', 'body': 'actual synthetic body', 'media': [], 'authors': []}
        if action == 'metadata':
            return {'page': page, 'url': self.url, 'windowId': 7}
        if action == 'identity':
            return {'uid': self.uid, 'url': self.url, 'visible': False}
        if action == 'activate':
            return {'activated': page, 'windowFocusRequested': False}
        if action == 'navigate':
            self.url = fields['url']
            return {'navigationSubmitted': True}
        if action == 'capture':
            if fields.get('stage') == 'search':
                return {'url': self.url, 'uid': self.uid, 'visible': False, 'selected': ['综合'],
                        'cards': [{'url': URL1, 'text': 'card 1'}, {'url': URL2, 'text': 'card 2'}],
                        'position': {'scrollTop': 500, 'scrollHeight': 1800, 'viewportHeight': 800},
                        'explicitEnd': False}
            return {'url': self.url, 'uid': self.uid, 'visible': False,
                    'title': 'title', 'body': 'actual synthetic body', 'media': [], 'authors': []}
        if action == 'advance':
            if fields.get('dismissFilters'):
                return {'action': 'filter_already_closed' if self.filter_closed else 'filter_closed'}
            return {'action': 'scroll', 'before': 0, 'after': 500, 'anchorRestored': False}
        if action == 'deep':
            return {'results': [], 'deepCoverage': {'rootPaginationState': 'pending', 'threads': [], 'observedRootIds': []}}
        raise AssertionError(action)


class GeminiBridgeCompatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.guard = self.root / 'guards'
        self.guard.mkdir(mode=0o700)
        self.patch = patch.object(legacy, 'account_root', lambda: self.guard)
        self.patch.start()
        self.project = xp.Project(self.root / 'project', create=True)
        rounds.schema(self.project)
        self.source1 = self.root / 'src1.json'
        self.source1.write_text(json.dumps({'url': URL1}))
        self.source2 = self.root / 'src2.json'
        self.source2.write_text(json.dumps({'url': URL2}))
        self.binding = {
            'format': b.VERSION,
            'binding_name': 'test',
            'project': str(self.project.path.resolve()),
            'profile_label': 'alpha',
            'context_id': 'ctx-alpha',
            'account_uid': UID,
            'profile_guard_key': 'a' * 64,
            'session': 'xhs-test',
            'runtime': {'package_root': 'pinned', 'node': 'node', 'config_dir': 'cfg', 'cache_dir': 'cache'},
            'extension_version': '1.0.26',
            'background_text_collection': True,
            'tabs': {
                'search': {'page': 'A' * 32, 'session': 'xhs-test-search', 'owned': True, 'last_url': SEARCH_URL},
                'detail': {'page': 'B' * 32, 'session': 'xhs-test-detail', 'owned': True, 'last_url': URL1},
            },
            'state': 'ready',
        }
        b.save_binding(self.project.path, 'test', self.binding)

    def tearDown(self):
        self.project.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_fast_mode_uses_verified_capture_and_skips_redundant_filter_recapture(self):
        manifest = self.root / 'search.json'
        manifest.write_text(json.dumps({'units': [{'stage': 'search', 'target': 'synthetic', 'sort': 'general',
                                                    'sources': [str(self.source1)], 'next': 'search'}]}))
        tid = native.import_manifest(self.project, manifest)['task_ids'][0]
        driver = FastFakeDriver(SEARCH_URL, filter_closed=True)
        sleeps = []
        with patch.object(b.time, 'sleep', side_effect=sleeps.append):
            result = b.collect_one(self.project, self.binding, driver, self.project.task(tid), fast_mode=True)
        actions = [c[0] for c in driver.calls]
        self.assertEqual(actions, ['verified_capture', 'advance', 'advance', 'capture'])
        self.assertEqual(sleeps, [0.4, 0.8])
        self.assertEqual(result['observed_notes'], 2)

    def test_fast_mode_verified_capture_never_swallows_access_stop_or_account_mismatch(self):
        manifest = self.root / 'body.json'
        manifest.write_text(json.dumps({'units': [{'stage': 'body', 'target': NOTE1,
                                                    'sources': [str(self.source1)], 'next': 'body'}]}))
        tid = native.import_manifest(self.project, manifest)['task_ids'][0]
        for code in ('ACCESS_STOP', 'ACCOUNT_MISMATCH', 'SCOPE_CHANGED'):
            with self.subTest(code=code):
                driver = FastFakeDriver(URL1, verified_capture_error=code)
                with self.assertRaisesRegex(b.BridgeError, f'^{code}$'):
                    b.collect_one(self.project, self.binding, driver, self.project.task(tid), fast_mode=True)
                self.assertEqual([c[0] for c in driver.calls], ['verified_capture'])

    def test_fast_mode_falls_back_cleanly_on_target_mismatch(self):
        manifest = self.root / 'body-fb.json'
        manifest.write_text(json.dumps({'units': [{'stage': 'body', 'target': NOTE1,
                                                    'sources': [str(self.source1)], 'next': 'body'}]}))
        tid = native.import_manifest(self.project, manifest)['task_ids'][0]
        driver = FastFakeDriver(URL1, verified_capture_error='TARGET_MISMATCH')
        result = b.collect_one(self.project, self.binding, driver, self.project.task(tid), fast_mode=True)
        self.assertEqual([c[0] for c in driver.calls], ['verified_capture', 'metadata', 'identity', 'capture'])
        self.assertTrue(Path(result['snapshot']).is_file())

    def test_deep_options_forwarded_and_validated(self):
        manifest = self.root / 'roots.json'
        manifest.write_text(json.dumps({'units': [{'stage': 'roots', 'target': NOTE1,
                                                    'sources': [str(self.source1)], 'next': 'roots'}]}))
        tid = native.import_manifest(self.project, manifest)['task_ids'][0]
        driver = FastFakeDriver(URL1)
        b.collect_one(self.project, self.binding, driver, self.project.task(tid), fast_mode=True,
                      deep_options={'deepRoots': 48, 'deepReplyClicks': 4, 'deepBudgetMs': 13000})
        deep_call = [c for c in driver.calls if c[0] == 'deep'][0]
        self.assertEqual(deep_call[2]['deepRoots'], 48)
        self.assertEqual(deep_call[2]['deepReplyClicks'], 4)
        self.assertEqual(deep_call[2]['deepBudgetMs'], 13000)
        with self.assertRaisesRegex(b.BridgeError, 'DEEP_OPTIONS_INVALID'):
            b.collect_one(self.project, self.binding, driver, self.project.task(tid),
                          deep_options={'deepReplyClicks': 99})

    def test_work_non_blocking_review_backpressure_and_gemini_actor(self):
        manifest = self.root / 'two-bodies.json'
        manifest.write_text(json.dumps({'units': [
            {'stage': 'body', 'target': NOTE1, 'sources': [str(self.source1)], 'next': 'body1'},
            {'stage': 'body', 'target': NOTE2, 'sources': [str(self.source2)], 'next': 'body2'},
        ]}))
        native.import_manifest(self.project, manifest)
        fake_driver = FastFakeDriver(URL1)
        with patch.object(b, 'Driver', return_value=fake_driver), patch.object(b.time, 'sleep'):
            res = b.work(self.project.path, 'test', units=5, interval=3,
                         actor='gemini', fast_mode=True, non_blocking_review=True, max_review_backlog=1)
        self.assertEqual(res['actor'], 'gemini')
        self.assertEqual(res['event'], 'handoff')
        self.assertEqual(res['reason'], 'review_backlog_backpressure')
        self.assertEqual(res['processed_units'], 1)

    def test_work_respects_stop_event(self):
        manifest = self.root / 'one-body.json'
        manifest.write_text(json.dumps({'units': [
            {'stage': 'body', 'target': NOTE1, 'sources': [str(self.source1)], 'next': 'body1'},
        ]}))
        native.import_manifest(self.project, manifest)
        stop = threading.Event()
        stop.set()
        fake_driver = FastFakeDriver(URL1)
        with patch.object(b, 'Driver', return_value=fake_driver):
            res = b.work(self.project.path, 'test', units=5, interval=3, actor='gemini', stop_event=stop)
        self.assertEqual(res['event'], 'paused')
        self.assertEqual(res['reason'], 'stop_requested')
        self.assertEqual(res['processed_units'], 0)

    def test_gemini_reviewer_and_reader_accepted_while_automatic_rejected(self):
        # 1. Capture diagnostic accepts reviewer=gemini + inspection_method=gemini_multimodal.
        manifest = self.root / 'img.json'
        manifest.write_text(json.dumps({'units': [{'stage': 'images', 'target': NOTE1,
                                                    'sources': [str(self.source1)], 'next': 'img'}]}))
        tid = native.import_manifest(self.project, manifest)['task_ids'][0]
        d = self.project.path / 'bridge/media' / NOTE1
        d.mkdir(parents=True, exist_ok=True)
        snaps = []
        for name in ('blank', 'good'):
            png = d / (name + '.png')
            png.write_bytes(PNG + name.encode())
            cap = {'position': 1, 'count': 1, 'kind': 'image', 'file': str(png), 'url': URL1,
                   'observed_at': '2026-11-05T10:00:00+00:00', 'reviewed': False}
            png.with_suffix('.json').write_text(json.dumps(cap))
            cap['sha256'] = sha(png)
            r = b.store_capture(self.project, self.project.task(tid),
                                {'url': URL1, 'body_carousel_only': True, 'captured_positions': [1],
                                 'carousel_count': 1, 'comment_media_scan_complete': False,
                                 'media_captures': [cap], 'video_requires_segments': False})
            snaps.append((cap, Path(r['snapshot']).stem))
        self.project.db.execute('INSERT INTO media_inventory VALUES(?,?,?,?,?)',
                                (NOTE1, 'body:1', 'image', xp.dumps(snaps[1][0]), 'captured_unreviewed'))
        self.project.db.commit()
        vf = self.root / 'visual.json'
        vf.write_text(json.dumps({'target': NOTE1, 'action': 'media', 'outcome': 'ready',
                                  'observed_at': '2026-11-05T10:20:00+00:00',
                                  'summary': 'Gemini visually inspected replacement image.',
                                  'source_file': snaps[1][0]['file'], 'url': URL1,
                                  'target_verified': True, 'content_ready': True, 'view_consistent': True,
                                  'content_observed': True, 'reviewed': True, 'complete': False,
                                  'media_verified': True, 'media_reviewed_keys': ['body:1'],
                                  'positions': ['body:1'], 'bridge_snapshot_hashes': [snaps[1][1]], 'gaps': ['comments']}))
        ev = b.review(self.project, tid, vf)['event_id']
        old = snaps[0][0]
        diag = {'kind': 'failed_media_capture_diagnostic_v1', 'task_id': tid, 'target': NOTE1,
                'snapshot_hash': snaps[0][1], 'classification': 'target_image_region_blank',
                'reviewer': 'gemini', 'inspection_method': 'gemini_multimodal', 'visual_checked': True,
                'target_context_verified': True, 'media_content_readable': False,
                'visual_observation': 'Gemini vision confirmed target image region is blank.',
                'inspected_at': '2026-11-05T11:00:00+00:00', 'media_key': 'body:1', 'position': 1,
                'png_file': old['file'], 'png_sha256': old['sha256'],
                'sidecar_file': str(Path(old['file']).with_suffix('.json')),
                'sidecar_sha256': sha(Path(old['file']).with_suffix('.json')),
                'replacement_event_id': ev, 'content_observed': False, 'reviewed': False,
                'complete': False, 'comment_media_scan_complete': False}
        df = self.root / 'diag.json'
        df.write_text(json.dumps(diag))
        self.assertTrue(b.confirm_capture_diagnostic(self.project, tid, df)['diagnostic_confirmed'])
        diag['reviewer'] = 'automatic'
        df.write_text(json.dumps(diag))
        with self.assertRaises(b.BridgeError):
            b.confirm_capture_diagnostic(self.project, tid, df)

        # 2. Saved original body accepts reader=gemini and still rejects automatic.
        orig = self.root / 'orig-note.json'
        orig_data = {'operation': 'note', 'note_id': NOTE2, 'url': URL2,
                     'captured_at': '2026-11-01T00:00:00+00:00',
                     'data': {'items': [{'model_type': 'note', 'id': NOTE2, 'ignore': False,
                                         'note_card': {'note_id': NOTE2, 'type': 'normal', 'title': 'Title',
                                                       'desc': 'Complete saved description.',
                                                       'user': {'user_id': 'b' * 24},
                                                       'interact_info': {}, 'image_list': [], 'tag_list': []}}]}}
        orig.write_text(json.dumps(orig_data))
        m2 = self.root / 'body2.json'
        m2.write_text(json.dumps({'units': [{'stage': 'body', 'target': NOTE2, 'sources': [str(orig)], 'next': 'read'}]}))
        tid2 = native.import_manifest(self.project, m2)['task_ids'][0]
        rec = {'review_scope': 'saved_original_body_text_v1', 'task_id': tid2, 'target': NOTE2,
               'action': 'offline', 'outcome': 'offline', 'observed_at': '2026-11-02T00:00:00+00:00',
               'summary': 'Gemini read full archived body.', 'source_file': str(orig),
               'source_sha256': sha(orig), 'url': URL2, 'source_captured_at': '2026-11-01T00:00:00+00:00',
               'body_pointer': '/data/items/0/note_card/desc', 'title_pointer': '/data/items/0/note_card/title',
               'body_sha256': hashlib.sha256(b'Complete saved description.').hexdigest(),
               'title_sha256': hashlib.sha256(b'Title').hexdigest(),
               'reader': 'gemini', 'full_body_read': True, 'title_read': True, 'source_body_complete': True,
               'body_text_truncated': False, 'unread_body_segments': [], 'ui_observed': False,
               'content_ready': False, 'view_consistent': False, 'content_observed': True,
               'reviewed': True, 'complete': True, 'end': 'body_text_read', 'gaps': [],
               'positions': ['/data/items/0/note_card/title', '/data/items/0/note_card/desc']}
        rf = self.root / 'saved-body.json'
        rf.write_text(json.dumps(rec))
        native.record(self.project, tid2, rf)
        self.assertEqual(self.project.task(tid2)['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
