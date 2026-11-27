#!/usr/bin/env python3
"""Offline end-to-end synthetic tests for xhs_gemini_pipeline (async, hybrid, backpressure, STOP)."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import xhs_account_identity as identity
import xhs_bridge as bridge
import xhs_gemini_client as gc
import xhs_gemini_pipeline as gp
import xhs_native as native
import xhs_project as xp
import xhs_research as legacy
import xhs_rounds as rounds

UID = '1' * 24
AUTHOR_UID = '9' * 24
NOTE1 = '2' * 24
NOTE2 = '3' * 24
CID1 = '4' * 24
URL1 = f'https://www.xiaohongshu.com/explore/{NOTE1}?xsec_token=obs1'
URL2 = f'https://www.xiaohongshu.com/explore/{NOTE2}?xsec_token=obs2'
SEARCH_URL = 'https://www.xiaohongshu.com/search_result?keyword=synthetic&type=51'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6kU8AAAAASUVORK5CYII=')


def make_gemini_responder(*, slow_delay=0.0):
    def responder(url, payload, headers, timeout):
        if slow_delay:
            time.sleep(slow_delay)
        prompt_text = payload['contents'][0]['parts'][0]['text']
        if 'Evaluate EVERY card below' in prompt_text:
            body_json = json.loads(prompt_text.split(gc.UNTRUSTED_OPEN + '\n', 1)[1].split('\n' + gc.UNTRUSTED_CLOSE, 1)[0])
            decisions = []
            for c in body_json['cards']:
                decisions.append({
                    'note_id': c['note_id'],
                    'locator': c['locator'],
                    'decision': 'include' if c['note_id'] == NOTE1 else 'exclude',
                    'reason': 'Relevant review note' if c['note_id'] == NOTE1 else 'Off-topic card',
                    'commercial_signal': False,
                    'comment_check_worthwhile': c['note_id'] == NOTE1,
                    'priority': 25,
                })
            out = {'summary': f'Triaged {len(decisions)} cards.', 'decisions': decisions,
                   'discovered_aliases': ['别名测试'], 'discovered_queries': ['别名测试 体验']}
        elif 'Read this Xiaohongshu note body carefully' in prompt_text:
            out = {
                'summary': 'Patient shared authentic waiting time and communication details.',
                'relevant': True,
                'should_collect_comments': True,
                'commercial_signals': [],
                'findings': [{
                    'quote': '等了两个月才面诊，态度还不错',
                    'attribution': 'explicit',
                    'missing_confirmation': '',
                    'experience_summary': 'Two-month wait, good attitude',
                }],
                'discovered_aliases': ['医生简称'],
                'discovered_authors': [AUTHOR_UID],
                'gaps': [],
            }
        elif 'Review EVERY comment entity below' in prompt_text:
            body_json = json.loads(prompt_text.split(gc.UNTRUSTED_OPEN + '\n', 1)[1].split('\n' + gc.UNTRUSTED_CLOSE, 1)[0])
            eids = [i['entity_id'] for i in body_json['items']]
            out = {
                'summary': f'Reviewed {len(eids)} comment entities.',
                'reviewed_entity_ids': eids,
                'findings': [{
                    'entity_id': eids[0],
                    'quote': '我也是等了很久但是复诊很细致',
                    'attribution': 'context_supported',
                    'missing_confirmation': 'Commenter did not restate full name',
                    'experience_summary': 'Long wait, careful follow-up',
                    'commercial_signal': False,
                    'counter_or_followup': 'Positive follow-up noted',
                }] if eids else [],
                'discovered_aliases': [],
                'discovered_authors': [AUTHOR_UID],
                'gaps': [],
            }
        elif 'Inspect ONLY this single captured image' in prompt_text:
            out = {
                'readable': True,
                'blank_or_unloaded': False,
                'observation': 'Shows clinic receipt screenshot dated November.',
                'ocr_key_quotes': ['挂号单'],
                'commercial_signals': [],
                'discovered_aliases': [],
                'gaps': [],
            }
        else:
            return {'candidates': [{'content': {'parts': [{'text': '# Gemini Pro 综合报告\n\n核心发现已核对。'}]}}]}
        return {'candidates': [{'content': {'parts': [{'text': json.dumps(out, ensure_ascii=False)}]}}]}
    return responder


class SyntheticDriver:
    def __init__(self, *, trigger_stop_on_stage=None):
        self.urls = {'search': SEARCH_URL, 'detail': URL1}
        self.selected_sort = '综合'
        self.calls = 0
        self.trigger_stop_on_stage = trigger_stop_on_stage
        self.metrics = {'calls': 0, 'elapsed_seconds': 0.0}

    def close(self):
        pass

    def call(self, action, binding=None, role=None, **fields):
        self.calls += 1
        self.metrics['calls'] += 1
        if self.trigger_stop_on_stage and fields.get('stage') == self.trigger_stop_on_stage:
            raise bridge.BridgeError('ACCESS_STOP')
        page = binding['tabs'][role]['page']
        cur_url = self.urls.get(role, SEARCH_URL)
        if action == 'metadata':
            return {'page': page, 'url': cur_url, 'windowId': 7}
        if action == 'identity':
            return {'uid': UID, 'url': cur_url, 'visible': True}
        if action == 'activate':
            return {'activated': page, 'windowFocusRequested': False}
        if action == 'navigate':
            self.urls[role] = fields['url']
            return {'navigationSubmitted': True}
        if action in {'verified_capture', 'capture'}:
            if fields.get('stage') == 'search':
                return {
                    'page': page, 'windowId': 7, 'url': cur_url, 'uid': UID, 'visible': False,
                    'selected': [self.selected_sort],
                    'cards': [{'url': URL1, 'text': '真实体验记录'}, {'url': URL2, 'text': '无关风景'}],
                    'position': {'scrollTop': 600, 'scrollHeight': 1200, 'viewportHeight': 800},
                    'explicitEnd': True, 'endSignals': ['- THE END -'],
                }
            return {
                'page': page, 'windowId': 7, 'url': cur_url, 'uid': UID, 'visible': False,
                'title': '面诊真实记录',
                'body': '等了两个月才面诊，态度还不错，后续继续更新。',
                'authors': [{'url': f'https://www.xiaohongshu.com/user/profile/{AUTHOR_UID}', 'name': '作者甲'}],
                'media': [],
            }
        if action == 'advance':
            if fields.get('sort'):
                self.selected_sort = {'general': '综合', 'latest': '最新', 'popular': '最多点赞'}[fields['sort']]
                return {'action': 'sort_clicked', 'sort': fields['sort']}
            if fields.get('dismissFilters'):
                return {'action': 'filter_already_closed'}
            return {'action': 'scroll', 'before': 0, 'after': 600, 'anchorRestored': False}
        if action == 'deep':
            return {
                'results': [{
                    'commentId': CID1, 'text': '我也是等了很久但是复诊很细致',
                    'author': '评论者', 'authorId': AUTHOR_UID,
                    'authorHrefRaw': f'/user/profile/{AUTHOR_UID}', 'is_reply': False,
                }],
                'deepCoverage': {
                    'rootPaginationState': 'complete',
                    'explicitEnd': True,
                    'observedRootIds': [CID1],
                    'threads': [{
                        'rootKey': 'id:' + CID1, 'commentId': CID1, 'state': 'visible_replies_read',
                        'contentCaptured': True, 'replyContentCaptured': True,
                        'replyIdentityComplete': True, 'replyBodyGaps': [],
                    }],
                    'remainingThreads': [],
                    'unresolvedRootKeys': [],
                },
            }
        raise AssertionError(action)


class GeminiPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.guard = self.root / 'guards'
        self.guard.mkdir(mode=0o700)
        self.patch = patch.object(legacy, 'account_root', lambda: self.guard)
        self.patch.start()
        self.project = xp.Project(self.root / 'project', create=True)
        rounds.schema(self.project)
        self.src = self.root / 'seed.json'
        self.src.write_text(json.dumps({'url': SEARCH_URL}))
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'units': [{
            'stage': 'search', 'target': 'synthetic', 'sort': 'general',
            'sources': [str(self.src)], 'next': 'start search',
        }]}))
        self.search_tid = native.import_manifest(self.project, manifest)['task_ids'][0]
        self.binding = {
            'format': bridge.VERSION, 'binding_name': 'alpha',
            'project': str(self.project.path.resolve()), 'profile_label': 'alpha',
            'context_id': 'ctx-alpha', 'account_uid': UID, 'profile_guard_key': 'a' * 64,
            'session': 'xhs-alpha',
            'runtime': {'package_root': 'pinned', 'node': 'node', 'config_dir': 'cfg', 'cache_dir': 'cache'},
            'extension_version': '1.0.26', 'background_text_collection': True,
            'tabs': {
                'search': {'page': 'A' * 32, 'session': 'xhs-alpha-search', 'owned': True, 'last_url': SEARCH_URL},
                'detail': {'page': 'B' * 32, 'session': 'xhs-alpha-detail', 'owned': True, 'last_url': URL1},
            },
            'state': 'ready',
        }
        bridge.save_binding(self.project.path, 'alpha', self.binding)
        self.project.close()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def make_client(self, slow_delay=0.0):
        cfg = gc.GeminiConfig(api_key='synthetic-key')
        return gc.GeminiClient(cfg, transport=make_gemini_responder(slow_delay=slow_delay), sleep=lambda _s: None)

    def make_clock(self):
        now = [0.0]

        def fake_clock():
            return now[0]

        def fake_sleep(seconds):
            now[0] += max(0.0, float(seconds))
            time.sleep(0.002)

        return fake_clock, fake_sleep

    def test_end_to_end_api_async_collect_triage_enqueue_comments_and_synthesize(self):
        driver = SyntheticDriver()
        client = self.make_client()
        clk, slp = self.make_clock()
        summary = gp.run_pipeline(
            self.root / 'project', 'alpha', research_goal='验证测试目标真实体验',
            mode='api-async', units=10, max_seconds=600, interval=3.0, jitter=0.0,
            review_workers=3, max_review_backlog=10, auto_enqueue=True,
            client=client, driver=driver, clock=clk, sleep=slp,
        )
        self.assertEqual(summary['review_errors'], 0)
        self.assertGreaterEqual(summary['units_collected'], 3)  # search + body + roots/replies
        self.assertGreaterEqual(summary['reviews_committed'], 3)
        self.assertGreaterEqual(summary['findings_extracted'], 2)
        self.assertFalse(summary['access_stopped'])

        st = gp.pipeline_status(self.root / 'project')
        self.assertEqual(st['stages']['search']['complete'], 1)
        self.assertEqual(st['stages']['body']['complete'], 1)
        self.assertEqual(st['stages']['roots']['complete'], 1)
        self.assertGreaterEqual(st['findings_count'], 2)
        self.assertGreaterEqual(st['discovered_counts']['aliases'], 1)

        out_md = self.root / 'report.md'
        rep = gp.synthesize(self.root / 'project', '验证测试目标真实体验', out_md, client=client)
        text = out_md.read_text(encoding='utf-8')
        self.assertIn('Gemini Pro 综合报告', text)
        self.assertIn('覆盖范围与未完成缺口台账', text)
        self.assertEqual(rep['model'], 'gemini-2.5-pro')

    def test_access_stop_halts_collection_writes_shared_stop_and_finishes_offline_reviews(self):
        # Stop when body collection runs after search completes and enqueues body.
        driver = SyntheticDriver(trigger_stop_on_stage='body')
        client = self.make_client()
        clk, slp = self.make_clock()
        summary = gp.run_pipeline(
            self.root / 'project', 'alpha', research_goal='测试熔断',
            mode='api-async', units=10, max_seconds=300, interval=3.0, jitter=0.0,
            review_workers=2, auto_enqueue=True, client=client, driver=driver, clock=clk, sleep=slp,
        )
        self.assertTrue(summary['access_stopped'])
        self.assertEqual(summary['stop_reason'], 'SHARED_STOP')
        binding_after = bridge.load_binding(self.root / 'project', 'alpha')
        self.assertEqual(binding_after['last_error']['code'], 'ACCESS_STOP')
        # Shared STOP.json must exist and never be cleared.
        keys = identity.related_keys('a' * 64, UID)
        self.assertTrue(any((identity.guard_directory(k) / 'STOP.json').is_file() for k in keys))
        # Doctor surfaces the active shared stop.
        doc = gp.doctor(self.root / 'project', 'alpha', config=gc.GeminiConfig(api_key=''))
        self.assertTrue(doc['shared_account_stops'])
        self.assertEqual(doc['recommended_mode'], 'agent-hybrid')

    def test_collect_only_enforces_backpressure_threshold(self):
        driver = SyntheticDriver()
        clk, slp = self.make_clock()
        # Add a second pending search task so collect-only would otherwise continue.
        db = xp.Project(self.root / 'project')
        try:
            m = self.root / 'search2.json'
            m.write_text(json.dumps({'units': [{
                'stage': 'search', 'target': 'synthetic', 'sort': 'latest',
                'sources': [str(self.src)], 'next': 'second search',
            }]}))
            native.import_manifest(db, m)
        finally:
            db.close()
        summary = gp.run_pipeline(
            self.root / 'project', 'alpha', mode='collect-only',
            units=5, max_seconds=300, interval=3.0, jitter=0.0,
            max_review_backlog=1, driver=driver, clock=clk, sleep=slp,
        )
        self.assertEqual(summary['units_collected'], 1)
        self.assertEqual(summary['stop_reason'], 'review_backlog_backpressure')

    def test_hybrid_collect_export_and_commit_round_trip(self):
        driver = SyntheticDriver()
        summary = gp.run_pipeline(
            self.root / 'project', 'alpha', mode='collect-only',
            units=1, max_seconds=30, interval=3.0, jitter=0.0,
            driver=driver, sleep=lambda _s: None,
        )
        self.assertEqual(summary['units_collected'], 1)

        batch_file = self.root / 'batch.json'
        exported = gp.export_review_batch(self.root / 'project', batch_file, limit=5)
        self.assertEqual(exported['task_count'], 1)
        batch = json.loads(batch_file.read_text(encoding='utf-8'))
        task_entry = batch['tasks'][0]
        self.assertEqual(task_entry['stage'], 'search')

        results_file = self.root / 'batch-results.json'
        results_file.write_text(json.dumps({
            'results': [{
                'task_id': task_entry['task_id'],
                'packet_id': task_entry['packet_id'],
                'model': 'antigravity-flash',
                'review': {
                    'summary': 'Hybrid agent triaged search cards.',
                    'decisions': [{
                        'note_id': c['note_id'], 'locator': c['locator'],
                        'decision': 'include' if c['note_id'] == NOTE1 else 'exclude',
                        'reason': 'Hybrid relevance check', 'commercial_signal': False,
                        'comment_check_worthwhile': c['note_id'] == NOTE1, 'priority': 25,
                    } for c in task_entry['cards']],
                    'discovered_aliases': ['混合模式别名'],
                    'discovered_queries': [],
                },
            }],
        }, ensure_ascii=False))
        committed = gp.commit_review_batch(self.root / 'project', results_file, auto_enqueue=True)
        self.assertEqual(committed['committed'], 1)
        self.assertEqual(committed['errors'], [])
        self.assertEqual(len(committed['items'][0]['enqueued_tasks']), 1)

    def test_offline_multimodal_image_review_binds_sha_and_does_not_mark_complete(self):
        db = xp.Project(self.root / 'project')
        rounds.schema(db)
        try:
            src_note = self.root / 'note1.json'
            src_note.write_text(json.dumps({'url': URL1}))
            m = self.root / 'img-manifest.json'
            m.write_text(json.dumps({'units': [{'stage': 'images', 'target': NOTE1, 'sources': [str(src_note)], 'next': 'img'}]}))
            img_tid = native.import_manifest(db, m)['task_ids'][0]
            mdir = self.root / 'project' / 'bridge' / 'media' / NOTE1
            mdir.mkdir(parents=True, exist_ok=True)
            png_path = mdir / '001.png'
            png_path.write_bytes(PNG)
            cap = {'position': 1, 'count': 1, 'kind': 'image', 'file': str(png_path), 'url': URL1,
                   'observed_at': '2026-11-05T10:00:00+00:00', 'sha256': hashlib.sha256(PNG).hexdigest()}
            db.db.execute('INSERT INTO media_inventory VALUES(?,?,?,?,?)',
                          (NOTE1, 'body:1', 'image', xp.dumps(cap), 'captured_unreviewed'))
            db.db.commit()
            bridge.store_capture(db, db.task(img_tid), {
                'url': URL1, 'body_carousel_only': True, 'captured_positions': [1],
                'carousel_count': 1, 'comment_media_scan_complete': False,
                'media_captures': [cap], 'video_requires_segments': False,
            })
        finally:
            db.close()

        summary = gp.run_pipeline(
            self.root / 'project', 'alpha', research_goal='检查图片内容',
            mode='review-only', include_media=True, client=self.make_client(), sleep=lambda _s: None,
        )
        self.assertEqual(summary['reviews_committed'], 1)
        self.assertEqual(summary['review_errors'], 0)
        db = xp.Project(self.root / 'project')
        try:
            state = db.db.execute("SELECT state FROM media_inventory WHERE note_id=? AND media_key='body:1'", (NOTE1,)).fetchone()[0]
            self.assertEqual(state, 'reviewed')
            # Comment attachments remain separate so task must stay pending.
            self.assertEqual(db.task(img_tid)['status'], 'pending')
        finally:
            db.close()

    def test_pipeline_does_not_recollect_already_captured_body_task(self):
        db = xp.Project(self.root / 'project')
        try:
            src_note = self.root / 'note1-body.json'
            src_note.write_text(json.dumps({'url': URL1}))
            m = self.root / 'body-only.json'
            m.write_text(json.dumps({'units': [{'stage': 'body', 'target': NOTE1, 'sources': [str(src_note)], 'next': 'body'}]}))
            body_tid = native.import_manifest(db, m)['task_ids'][0]
            # Simulate a body task already captured and reviewed once, still pending due to a noted gap.
            stored = bridge.store_capture(db, db.task(body_tid), {
                'url': URL1, 'title': 't', 'body': 'b', 'authors': [], 'media': [], 'visible': True,
            })
            h = Path(stored['snapshot']).stem
            task = db.task(body_tid)
            task['payload']['bridge']['reviewed_hashes'] = [h]
            task['payload']['bridge']['awaiting_review'] = False
            db.db.execute('UPDATE tasks SET payload=? WHERE id=?', (xp.dumps(task['payload']), body_tid))
            db.db.commit()
            picked, _ = gp._pick_next_collect_task(db, self.binding, round_id=None, include_media=False, active_note=NOTE1)
            self.assertIsNotNone(picked)
            self.assertNotEqual(picked['id'], body_tid)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
