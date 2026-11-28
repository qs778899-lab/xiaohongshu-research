"""Offline unit tests for non-focus-stealing background observed-DOM image capture."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import xhs_bridge as bridge
import xhs_bridge_media as media
import test_bridge_media_readiness as fixtures

NOTE = fixtures.NOTE
URL = fixtures.URL
SYNTHETIC_WEBP = b'RIFF\x20\x00\x00\x00WEBPVP8 \x14\x00\x00\x00synthetic-webp-bytes'
SYNTHETIC_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRsynthetic-png-bytes'


class FakeResponse(io.BytesIO):
    def __init__(self, body, url, content_type='image/webp'):
        super().__init__(body)
        self._url = url
        self.headers = {'Content-Type': content_type, 'Content-Length': str(len(body))}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


class BackgroundCarouselDriver:
    def __init__(self, slides):
        self.slides = slides
        self.current = 0
        self.actions = []

    def call(self, action, *args, **fields):
        self.actions.append(action)
        if action == 'media_state':
            slide = self.slides[self.current]
            return {
                'index': self.current,
                'count': len(self.slides),
                'kind': slide['kind'],
                'mediaStatus': 'rendered_decoded_media',
                'screenshotReady': True,
                'mediaEvidence': slide['evidence'],
            }
        if action == 'media_select':
            self.current = fields['index']
            return {'selectionSubmitted': True}
        raise AssertionError(f'Forbidden foreground action in background mode: {action}')


class BackgroundMediaTests(unittest.TestCase):
    setUp = fixtures.MediaReadinessTests.setUp
    tearDown = fixtures.MediaReadinessTests.tearDown

    def _enable_background(self):
        self.binding['format'] = 'xhs-bridge-v1'
        self.binding['project'] = str(self.p.path.resolve())
        self.binding['background_text_collection'] = True
        self.binding['tabs']['detail']['owned'] = False
        self.binding['tabs']['search'] = dict(self.binding['tabs']['detail'])
        self.binding['manual_single_tab'] = True
        bridge.save_binding(self.p.path, 'test', self.binding)

    def _image_slides(self, count=2):
        return [
            {
                'kind': 'image',
                'evidence': {
                    'notAVisualReview': True,
                    'images': [
                        {
                            'loaded': True,
                            'visible': True,
                            'complete': True,
                            'naturalWidth': 1080,
                            'naturalHeight': 1440,
                            'src': f'https://sns-webpic-qc.xhscdn.com/202611281745/abc123def456/slide-{i}.webp',
                        },
                        {
                            'loaded': True,
                            'visible': True,
                            'complete': True,
                            'naturalWidth': 24,
                            'naturalHeight': 24,
                            'src': 'https://picasso-static.xiaohongshu.com/fe-platform/icon.png',
                        },
                    ],
                },
            }
            for i in range(count)
        ]

    def test_background_binding_acquires_full_res_assets_without_activate_or_screenshot(self):
        self._enable_background()
        driver = BackgroundCarouselDriver(self._image_slides(2))
        meta = {'page': 'p1', 'url': URL, 'visible': False}

        def fake_opener(req, timeout=30):
            body = SYNTHETIC_WEBP if 'slide-0' in req.full_url else SYNTHETIC_PNG
            mime = 'image/webp' if 'slide-0' in req.full_url else 'image/png'
            return FakeResponse(body, req.full_url, mime)

        with patch.object(bridge, 'lease', lambda *a, **k: contextlib.nullcontext(['k1'])), \
             patch.object(bridge, 'verify_tab', return_value=meta), \
             patch.object(bridge, 'save_binding'), \
             patch.object(bridge, 'save_failure'), \
             patch.object(media.time, 'sleep'):
            result = media.capture_background_assets(
                self.p.path, 'test', self.tid, [1, 2], driver=driver, opener=fake_opener
            )

        self.assertNotIn('activate', driver.actions)
        self.assertNotIn('screenshot', driver.actions)
        self.assertEqual(driver.actions.count('media_select'), 1)
        self.assertEqual(len(result['captures']), 2)
        self.assertTrue(result['captures'][0]['file'].endswith('.webp'))
        self.assertTrue(result['captures'][1]['file'].endswith('.png'))
        for rec in result['captures']:
            self.assertEqual(rec['capture_revision'], media.BACKGROUND_ASSET_CAPTURE_REVISION)
            self.assertEqual(rec['acquisition_mode'], 'background_observed_dom_asset')
            self.assertFalse(rec['reviewed'])
            self.assertTrue(rec['visual_verification_required'])
            self.assertFalse(rec['observed_context']['windowFocusRequested'])
            self.assertEqual(rec['observed_context']['account_uid'], self.binding['account_uid'])
            self.assertEqual(rec['natural_width'], 1080)
            self.assertNotIn('picasso-static', rec['source_asset_url'])
            p = Path(rec['file'])
            self.assertTrue(p.is_file())
            self.assertEqual(rec['sha256'], hashlib.sha256(p.read_bytes()).hexdigest())
            self.assertTrue(p.with_suffix('.json').is_file())

    def test_collect_inventory_routes_background_binding_and_reuses_valid_unreviewed_cache(self):
        self._enable_background()
        driver = BackgroundCarouselDriver(self._image_slides(1))
        meta = {'page': 'p1', 'url': URL, 'visible': False}

        def fake_opener(req, timeout=30):
            return FakeResponse(SYNTHETIC_WEBP, req.full_url, 'image/webp')

        orig_download = media._download_observed_cdn_image

        def patched_download(url, out_dir, position, *, opener=None):
            return orig_download(url, out_dir, position, opener=fake_opener)

        with patch.object(bridge, 'lease', lambda *a, **k: contextlib.nullcontext(['k1'])), \
             patch.object(bridge, 'verify_tab', return_value=meta), \
             patch.object(bridge, 'save_binding'), \
             patch.object(bridge, 'save_failure'), \
             patch.object(media, '_download_observed_cdn_image', side_effect=patched_download), \
             patch.object(media, 'capture', side_effect=AssertionError('Must not call foreground screenshot capture')):
            first = media.collect_inventory(self.p, 'test', self.p.task(self.tid), driver)
            self.assertEqual(len(first['media_files']), 1)
            self.assertTrue(Path(first['media_files'][0]).is_file())
            # Second call with intact file reuses valid unreviewed cache without downloading again.
            with patch.object(media, '_download_observed_cdn_image', side_effect=AssertionError('Should reuse cache')):
                second = media.collect_inventory(self.p, 'test', self.p.task(self.tid), driver)
            self.assertEqual(second['media_files'], first['media_files'])

    def test_foreground_binding_keeps_original_capture_path(self):
        # Default fixture binding has background_text_collection unset -> must NOT route to background assets.
        with patch.object(media, 'capture', return_value={'index': 0, 'count': 1, 'kind': 'image', 'mediaStatus': 'rendered_decoded_media'}) as fg, \
             patch.object(media, 'capture_background_assets', side_effect=AssertionError('Must not use background path')):
            # Mark existing item reviewed so only state probe runs.
            self.p.db.execute("UPDATE media_inventory SET state='reviewed' WHERE note_id=?", (NOTE,))
            self.p.db.commit()
            res = media.collect_inventory(self.p, 'test', self.p.task(self.tid), None)
        self.assertEqual(fg.call_count, 1)
        self.assertEqual(res['local_gap'], 'MEDIA_CONTEXT_REVIEW_REQUIRED')

    def test_cdn_461_or_html_response_triggers_fail_closed_access_stop(self):
        self._enable_background()
        for scenario in ('http_461', 'html_page', 'foreign_redirect'):
            with self.subTest(scenario=scenario):
                driver = BackgroundCarouselDriver(self._image_slides(1))
                meta = {'page': 'p1', 'url': URL, 'visible': False}

                def bad_opener(req, timeout=30):
                    if scenario == 'http_461':
                        raise HTTPError(req.full_url, 461, 'Restricted', {}, None)
                    if scenario == 'html_page':
                        return FakeResponse(b'<html><body>login required</body></html>', req.full_url, 'text/html')
                    return FakeResponse(SYNTHETIC_PNG, 'https://evil.example/a.png', 'image/png')

                expected_code = 'MEDIA_IDENTITY_UNCONFIRMED' if scenario == 'foreign_redirect' else 'ACCESS_STOP'
                with patch.object(bridge, 'lease', lambda *a, **k: contextlib.nullcontext(['k1'])), \
                     patch.object(bridge, 'verify_tab', return_value=meta), \
                     patch.object(bridge, 'save_binding'), \
                     patch.object(bridge, 'save_failure') as save_fail:
                    with self.assertRaisesRegex(bridge.BridgeError, expected_code):
                        media.capture_background_assets(
                            self.p.path, 'test', self.tid, [1], driver=driver, opener=bad_opener
                        )
                    save_fail.assert_called_once()

    def test_video_slide_never_fakes_image_or_marks_reviewed(self):
        self._enable_background()
        slides = [{
            'kind': 'video',
            'evidence': {
                'notAVisualReview': True,
                'videos': [{
                    'loaded': True,
                    'visible': True,
                    'readyState': 4,
                    'videoWidth': 720,
                    'videoHeight': 1280,
                    'src': 'https://sns-video-bd.xhscdn.com/stream/demo.mp4',
                }],
                'images': [],
            },
        }]
        driver = BackgroundCarouselDriver(slides)
        meta = {'page': 'p1', 'url': URL, 'visible': False}
        with patch.object(bridge, 'lease', lambda *a, **k: contextlib.nullcontext(['k1'])), \
             patch.object(bridge, 'verify_tab', return_value=meta), \
             patch.object(bridge, 'save_binding'), \
             patch.object(bridge, 'save_failure'):
            res = media.capture_background_assets(self.p.path, 'test', self.tid, [1], driver=driver)
        self.assertEqual(res['captures'][0]['kind'], 'video')
        self.assertEqual(res['captures'][0]['file'], '')
        self.assertFalse(res['captures'][0]['reviewed'])

    def test_background_media_can_be_explicitly_disabled_per_binding(self):
        self._enable_background()
        self.binding['background_media_collection'] = False
        bridge.save_binding(self.p.path, 'test', self.binding)
        self.assertFalse(media.background_media_allowed(bridge.load_binding(self.p.path, 'test')))
        with self.assertRaisesRegex(bridge.BridgeError, 'MEDIA_ACTIVATION_UNCONFIRMED'):
            media.capture_background_assets(self.p.path, 'test', self.tid, [1])

    def test_tampered_or_other_account_cached_background_asset_triggers_recapture(self):
        self._enable_background()
        driver = BackgroundCarouselDriver(self._image_slides(1))
        meta = {'page': 'p1', 'url': URL, 'visible': False}

        def fake_opener(req, timeout=30):
            return FakeResponse(SYNTHETIC_WEBP, req.full_url, 'image/webp')

        orig_download = media._download_observed_cdn_image

        def patched_download(url, out_dir, position, *, opener=None):
            return orig_download(url, out_dir, position, opener=fake_opener)

        with patch.object(bridge, 'lease', lambda *a, **k: contextlib.nullcontext(['k1'])), \
             patch.object(bridge, 'verify_tab', return_value=meta), \
             patch.object(bridge, 'save_binding'), \
             patch.object(bridge, 'save_failure'), \
             patch.object(media, '_download_observed_cdn_image', side_effect=patched_download) as dl:
            first = media.collect_inventory(self.p, 'test', self.p.task(self.tid), driver)
            self.assertEqual(dl.call_count, 1)
            # Tamper file bytes -> must recapture.
            Path(first['media_files'][0]).write_bytes(b'tampered')
            second = media.collect_inventory(self.p, 'test', self.p.task(self.tid), driver)
            self.assertEqual(dl.call_count, 2)
            self.assertNotEqual(second['media_files'][0], first['media_files'][0])

    def test_verify_background_fix_clears_media_activation_hold_once_and_records_history(self):
        self._enable_background()
        bridge.hold_task(self.p, self.tid, 'MEDIA_ACTIVATION_UNCONFIRMED')
        driver = BackgroundCarouselDriver(self._image_slides(1))
        meta = {'page': 'p1', 'url': URL, 'visible': False}

        def fake_opener(req, timeout=30):
            return FakeResponse(SYNTHETIC_WEBP, req.full_url, 'image/webp')

        orig_download = media._download_observed_cdn_image

        def patched_download(url, out_dir, position, *, opener=None):
            return orig_download(url, out_dir, position, opener=fake_opener)

        with patch.object(bridge, 'lease', lambda *a, **k: contextlib.nullcontext(['k1'])), \
             patch.object(bridge, 'verify_tab', return_value=meta), \
             patch.object(bridge, 'save_binding'), \
             patch.object(bridge, 'save_failure'), \
             patch.object(media, '_download_observed_cdn_image', side_effect=patched_download):
            res = media.verify_background_fix(
                self.p.path, 'test', self.tid, 'background observed-DOM asset capture enabled', driver=driver
            )
        self.assertTrue(res['repair_verified'])
        self.assertEqual(len(res['media_files']), 1)
        updated = self.p.task(self.tid)['payload']['bridge']
        self.assertNotIn('fault', updated)
        self.assertEqual(updated['resolved_faults'][0]['fault']['code'], 'MEDIA_ACTIVATION_UNCONFIRMED')
        # Re-probing on the same version after another hold is rejected (bounded one-shot per version).
        bridge.hold_task(self.p, self.tid, 'MEDIA_ACTIVATION_UNCONFIRMED')
        with self.assertRaisesRegex(bridge.BridgeError, 'REPAIR_PROBE_ALREADY_USED'):
            media.verify_background_fix(self.p.path, 'test', self.tid, 'duplicate probe', driver=driver)


if __name__ == '__main__':
    unittest.main()
