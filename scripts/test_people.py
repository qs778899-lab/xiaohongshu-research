"""Synthetic, offline person-plan and source-bound coverage behavior."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import xhs_people as people
import xhs_project as xp
import xhs_native as native


class PeopleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve(); self.project = self.base / 'project'
        self.source = self.base / 'people.json'
        self.cfg = {'format': people.FORMAT,
                    'entities': [{'id': 'person-a', 'name': '示例甲', 'pinyin': ['shi', 'li', 'jia'],
                                  'surname': '示', 'surname_pinyin': 'shi', 'aliases': []}],
                    'contexts': {'institutions': ['示例机构'], 'professions': ['示例专业']},
                    'titles': ['老师'], 'experiences': ['结束', '复诊'],
                    'projects': [{'path': str(self.project), 'all_tasks': True}]}
        p = xp.Project(self.project, create=True); p.close()
        self.save()

    def save(self): self.source.write_text(json.dumps(self.cfg, ensure_ascii=False))
    def plan(self): self.save(); return people.plan(self.cfg, self.source)

    def snapshot(self, target='示例甲', sort='general', end=False, reviewed=False):
        p = xp.Project(self.project)
        raw = json.dumps({'url': 'https://www.xiaohongshu.com/search_result?keyword=' + target,
                          'cards': [{'title': 'Synthetic candidate', 'url': 'https://www.xiaohongshu.com/explore/' + 'a' * 24}]}).encode()
        h = hashlib.sha256(raw).hexdigest(); folder = self.project / 'bridge/raw'; folder.mkdir(parents=True, exist_ok=True)
        (folder / (h + '.json')).write_bytes(raw)
        payload = {'stage': 'search', 'target': target, 'sort': sort,
                   'bridge': {'hashes': [h], 'reviewed_hashes': [h] if reviewed else [], 'collection_end': end}}
        tid = p.add_task('native', xp.dumps(['search', target, sort]), payload, 30); p.db.commit(); p.close()
        return tid, h

    def states(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.rglob('*') if p.is_file()}

    def test_three_sorts_bounded_query_generation_and_no_bare_surname(self):
        b = self.plan(); queries = {q['query'] for q in b['queries']}
        self.assertTrue({'示例甲', 'shilijia', 'shi li jia', 'slj', '示例机构 slj', '示例专业 shi老师'} <= queries)
        self.assertFalse({'示', 'shi', '示老师', 'shi老师'} & queries)
        self.assertEqual(len(b['manifest']['units']), 3 * len(queries))
        self.assertNotIn('示例机构 示例专业 示例甲 结束', queries)

    def test_entity_contexts_replace_global_without_cross_routing(self):
        other = deepcopy(self.cfg['entities'][0])
        other.update(id='person-b', name='示例乙')
        self.cfg['entities'].append(other)
        self.cfg['entities'][0]['contexts'] = {'institutions': ['另一机构'], 'professions': ['另一专业']}
        b = self.plan()
        routed = {q['query']: q['entity_ids'] for q in b['queries']}
        self.assertNotIn('示例甲 示例机构', routed)
        self.assertIn('示例甲 另一机构', routed)
        self.assertIn('示例乙 示例机构', routed)
        self.assertEqual(routed['另一机构 结束'], ['person-a'])
        self.assertEqual(routed['示例机构 结束'], ['person-b'])

    def test_invalid_entity_contexts_rejected(self):
        for value in [None, [], {'institutions': ['sample']}]:
            self.cfg['entities'][0]['contexts'] = value
            with self.assertRaises(ValueError):
                self.plan()

    def test_shared_queries_union_people_without_duplicate_execution(self):
        other = deepcopy(self.cfg['entities'][0]); other.update(id='person-b', name='示例乙', pinyin=['shi', 'li', 'yi'])
        self.cfg['entities'].append(other)
        b = self.plan(); matches = [u for u in b['manifest']['units'] if u['target'] == '示例机构 结束' and u['sort'] == 'latest']
        self.assertEqual(len(matches), 1)
        self.assertEqual(set(matches[0]['discovery_entity_ids']), {'person-a', 'person-b'})
        self.assertEqual(matches[0]['discovery_entity_labels']['person-b'], '示例乙')

    def test_spelling_variants_deduplicate_queries_but_keep_alias_origins(self):
        self.cfg['entities'][0]['aliases'] = [{'text': 'SLJ', 'mode': 'initials', 'basis': 'hypothesis'},
                                             {'text': 'ＳＬＪ', 'mode': 'initials', 'basis': 'hypothesis'}]
        b = self.plan()
        q = [q for q in b['queries'] if people.key(q['query']) == 'slj']
        self.assertEqual(len(q), 1); self.assertEqual(set(q[0]['families']), {'initials', 'alias'})
        t = [t for t in b['recall_config']['terms'] if t['mode'] == 'initials']
        self.assertEqual(len(t), 1); self.assertEqual(t[0]['origins'][0]['basis'], 'hypothesis')

    def test_explicit_surname_alias_still_needs_online_context(self):
        self.cfg['entities'][0]['aliases'] = [{'text': 'SHI', 'mode': 'latin_token', 'basis': 'hypothesis'},
                                             {'text': '示老师', 'mode': 'literal', 'basis': 'hypothesis'}]
        q = {people.key(q['query']) for q in self.plan()['queries']}
        self.assertNotIn('shi', q); self.assertNotIn('示老师', q)
        self.assertIn('示例机构 shi', q)

    def test_plan_reuses_existing_query_spelling_without_resetting_checkpoint(self):
        tid, h = self.snapshot('示例机构 SLJ', end=True, reviewed=True)
        before = self.states(); bundle = people.plan(self.cfg, self.source, self.project)
        q = next(u for u in bundle['manifest']['units'] if people.key(u['target']) == '示例机构 slj' and u['sort'] == 'general')
        self.assertEqual(q['target'], '示例机构 SLJ'); self.assertEqual(self.states(), before)
        self.assertTrue(any(x['task_id'] == tid for x in bundle['existing_query_tasks_reused']))
        m = self.base / 'manifest.json'; m.write_text(json.dumps(bundle['manifest']))
        p = xp.Project(self.project); native.import_manifest(p, m)
        prior = p.task(tid)
        self.assertEqual(prior['payload']['bridge']['reviewed_hashes'], [h])
        self.assertTrue(prior['payload']['bridge']['collection_end'])
        self.assertEqual(len([r for r in p.db.execute('select payload from tasks')
                              if people.key(json.loads(r[0]).get('target', '')) == '示例机构 slj'
                              and json.loads(r[0]).get('sort') == 'general']), 1)
        p.close()

    def test_pinyin_not_inferred_when_missing_and_invalid_spelling_rejected(self):
        del self.cfg['entities'][0]['pinyin']
        self.assertFalse(any(q['families'] == ['initials'] for q in self.plan()['queries']))
        self.cfg['entities'][0]['pinyin'] = ['shi', '甲']
        with self.assertRaises(ValueError): self.plan()

    def test_observed_alias_requires_exact_preserved_source(self):
        a = {'text': '示例别称', 'mode': 'literal', 'basis': 'observed'}
        self.cfg['entities'][0]['aliases'] = [a]
        with self.assertRaises(ValueError): self.plan()
        f = self.base / 'raw.json'; f.write_text('{"text":"synthetic observed wording: 示例别称"}')
        a['sources'] = [{'file': str(f), 'sha256': hashlib.sha256(f.read_bytes()).hexdigest(), 'locator': 'text'}]
        b = self.plan(); self.assertTrue(any(t['text'] == '示例别称' for t in b['recall_config']['terms']))
        f.write_text('{}')
        with self.assertRaises(ValueError): self.plan()

    def test_observed_alias_wrong_locator_or_unmatched_text_rejected(self):
        f = self.base / 'raw.json'; f.write_text('{"one":"unrelated", "two":"示例别称"}')
        a = {'text': '示例别称', 'mode': 'literal', 'basis': 'observed',
             'sources': [{'file': str(f), 'sha256': hashlib.sha256(f.read_bytes()).hexdigest(), 'locator': 'one'}]}
        self.cfg['entities'][0]['aliases'] = [a]
        with self.assertRaises(ValueError): self.plan()
        a['sources'][0]['locator'] = 'missing'
        with self.assertRaises(ValueError): self.plan()
        a['sources'][0]['locator'] = 'two'
        self.assertTrue(self.plan())

    def test_single_syllable_pinyin_does_not_emit_bare_surname(self):
        self.cfg['entities'][0]['pinyin'] = ['shi']
        q = {people.key(q['query']) for q in self.plan()['queries']}
        self.assertNotIn('shi', q); self.assertIn('示例机构 shi', q)

    def test_multiple_legacy_spellings_reuse_one_stable_task_without_merging(self):
        ta, _ = self.snapshot('示例机构 SLJ', end=True)
        tb, _ = self.snapshot('示例机构 ＳＬＪ', end=False)
        before = self.states(); bundle = people.plan(self.cfg, self.source, self.project)
        reuse = next(r for r in bundle['existing_query_tasks_reused'] if r['sort'] == 'general'
                     and people.key(r['target']) == '示例机构 slj')
        self.assertEqual(reuse['task_id'], min(ta, tb)); self.assertEqual(self.states(), before)
        c = people.coverage(self.cfg, self.source, self.project)
        q = next(q for q in c['entities'][0]['queries'] if q['query'] == '示例机构 slj' and q['sort'] == 'general')
        self.assertEqual({t['task_id'] for t in q['tasks']}, {ta, tb})
        self.assertEqual(q['state'], 'observed')

    def test_config_must_match_manifest_source_and_stay_external(self):
        other = deepcopy(self.cfg); other['experiences'] = ['other']
        with self.assertRaises(ValueError): people.plan(other, self.source)
        with self.assertRaises(ValueError): people.load_config(Path(people.__file__))
        link = self.base / 'alias.json'; link.symlink_to(self.source)
        with self.assertRaises(ValueError): people.load_config(link)

    def test_explicit_recall_selection_is_required(self):
        self.cfg['projects'][0].pop('all_tasks')
        with self.assertRaises(ValueError): self.plan()
        self.cfg['projects'][0]['task_ids'] = []
        with self.assertRaises(ValueError): self.plan()

    def test_missing_query_is_not_zero_evaluations_and_does_not_write(self):
        before = self.states(); c = people.coverage(self.cfg, self.source, self.project)
        self.assertEqual(set(q['state'] for q in c['entities'][0]['queries']), {'missing'})
        self.assertIsNone(c['entities'][0]['attributable_evaluation_count'])
        self.assertEqual(self.states(), before)

    def test_alias_query_observation_and_review_reported_separately(self):
        tid, _ = self.snapshot('示例机构 slj')
        before = self.states(); c = people.coverage(self.cfg, self.source, self.project)
        q = next(q for q in c['entities'][0]['queries'] if q['query'] == '示例机构 slj' and q['sort'] == 'general')
        self.assertEqual(q['state'], 'observed'); self.assertEqual(q['tasks'][0]['verified_source_count'], 1)
        self.assertEqual(len(q['tasks'][0]['unreviewed_source_hashes']), 1)
        self.assertEqual(self.states(), before)

    def test_end_record_is_not_completion_certificate_or_permission(self):
        self.snapshot(end=True, reviewed=True)
        c = people.coverage(self.cfg, self.source, self.project)
        q = next(q for q in c['entities'][0]['queries'] if q['query'] == '示例甲' and q['sort'] == 'general')
        self.assertEqual(q['state'], 'ended')
        self.assertTrue(c['completion_not_revalidated']); self.assertFalse(c['network_permission'])
        self.assertEqual(q['tasks'][0]['end_basis'], 'recorded_collection_end_not_revalidated')

    def test_missing_or_changed_source_cannot_count_as_observed(self):
        _, h = self.snapshot(end=True)
        (self.project / 'bridge/raw' / (h + '.json')).write_text('{}')
        c = people.coverage(self.cfg, self.source, self.project)
        q = next(q for q in c['entities'][0]['queries'] if q['query'] == '示例甲' and q['sort'] == 'general')
        self.assertEqual(q['state'], 'blocked'); self.assertTrue(c['source_gaps'])
        self.assertEqual(q['tasks'][0]['verified_source_count'], 0)

    def test_downstream_source_routing_not_patient_attribution(self):
        _, h = self.snapshot('slj')
        p = xp.Project(self.project)
        p.add_task('native', 'body-key', {'stage': 'body', 'target': 'a' * 24,
                   'provenance': [{'sha256': h}]}, 30); p.db.commit(); p.close()
        c = people.coverage(self.cfg, self.source, self.project)
        d = c['entities'][0]['downstream']
        self.assertEqual(len(d), 1); self.assertEqual(d[0]['coverage_state'], 'pending')
        self.assertTrue(c['discovery_is_not_attribution']); self.assertIsNone(c['entities'][0]['attributable_evaluation_count'])

    def test_explicit_body_and_media_routes_without_local_search_parent(self):
        p = xp.Project(self.project)
        for stage, target in [('body', 'a' * 24), ('images', 'b' * 24)]:
            p.add_task('native', stage + '-key', {'stage': stage, 'target': target,
                       'provenance': [{'sha256': 'external-project-source'}],
                       'discovery_entity_ids': ['person-a'],
                       'discovery_entity_labels': {'person-a': '示例甲'}}, 30)
        p.db.commit(); p.close()
        before = self.states(); c = people.coverage(self.cfg, self.source, self.project)
        downstream = c['entities'][0]['downstream']
        self.assertEqual({d['stage'] for d in downstream}, {'body', 'images'})
        self.assertTrue(all(d['coverage_state'] == 'pending' for d in downstream))
        self.assertTrue(all(d['discovery_entity_ids'] == ['person-a'] for d in downstream))
        self.assertTrue(all(d['verified_source_count'] == 0 for d in downstream))
        self.assertTrue(c['discovery_is_not_attribution'])
        self.assertIsNone(c['entities'][0]['attributable_evaluation_count'])
        self.assertEqual(self.states(), before)

    def test_invalid_explicit_downstream_metadata_is_not_silently_ignored(self):
        p = xp.Project(self.project)
        p.add_task('native', 'invalid-body-key', {'stage': 'body', 'target': 'a' * 24,
                   'discovery_entity_ids': ['person-a']}, 30)
        p.db.commit(); p.close()
        before = self.states()
        with self.assertRaisesRegex(ValueError, 'Explicit discovery'):
            people.coverage(self.cfg, self.source, self.project)
        self.assertEqual(self.states(), before)

    def test_fullname_fallback_never_routes_unrelated_nonsearch_targets(self):
        p = xp.Project(self.project)
        p.add_task('native', 'legacy-identity-key', {'stage': 'identity', 'target': '示例甲'}, 30)
        p.db.commit(); p.close()
        c = people.coverage(self.cfg, self.source, self.project)
        self.assertEqual(c['entities'][0]['downstream'], [])

    def test_plan_can_import_through_existing_manifest_without_marking_read(self):
        b = self.plan(); m = self.base / 'manifest.json'; m.write_text(json.dumps(b['manifest']))
        p = xp.Project(self.project)
        result = native.import_manifest(p, m)
        self.assertEqual(len(result['task_ids']), len(b['manifest']['units']))
        row = p.task(result['task_ids'][0]); self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['payload']['discovery_entity_ids'], ['person-a']); p.close()


if __name__ == '__main__': unittest.main()
