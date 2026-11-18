"""Synthetic person discovery routing and matching; no accounts or network."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_native as native
import xhs_project as project
import xhs_recall as recall
import xhs_rounds as rounds


class PersonMatchingTests(unittest.TestCase):
    def test_initial_separators_keep_original_offsets_and_boundaries(self):
        text = 'ＡＢＣ / a.b.c / a b c / a-b-c / a_b_c / abc / Xabc / abc2'
        terms = recall.compile_terms([{'id': 'p', 'text': 'A-B_C', 'mode': 'initials'}])
        hits = recall.matches(text, terms)
        self.assertEqual([h['original'] for h in hits],
                         ['ＡＢＣ', 'a.b.c', 'a b c', 'a-b-c', 'a_b_c', 'abc'])
        for hit in hits:
            self.assertEqual(text[hit['start']:hit['end']], hit['original'])
            self.assertFalse(hit['identity_confirmed'])

    def test_latin_token_excludes_long_word_and_digit_neighbors(self):
        text = 'lin ＬＩＮ lin医生 alina linlin lin2 3lin'
        terms = recall.compile_terms([{'id': 'p', 'text': 'LIN', 'mode': 'latin_token'}])
        self.assertEqual([h['original'] for h in recall.matches(text, terms)], ['lin', 'ＬＩＮ', 'lin'])

    def test_latin_phrase_is_explicit_not_character_spacing_guess(self):
        terms = recall.compile_terms([{'id': 'p', 'text': 'Lin An', 'mode': 'latin_token'}])
        self.assertEqual([h['original'] for h in recall.matches('lin an / linan / l i n a n', terms)], ['lin an'])
        for value in ['林', 'lin*', 'lin2', '', 'lin  an']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                recall.compile_terms([{'id': 'p', 'text': value, 'mode': 'latin_token'}])

    def test_legacy_literal_stays_broad_and_ambiguous(self):
        terms = recall.compile_terms([{'id': 'p', 'text': 'lin', 'ambiguities': ['synthetic ambiguity']}])
        hits = recall.matches('linlin', terms)
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(h['ambiguities'] and not h['identity_confirmed'] for h in hits))


class PersonRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.p = project.Project(self.base / 'project', create=True)
        self.addCleanup(self.p.close)
        self.source = self.base / 'scope.json'
        self.source.write_text(json.dumps({'synthetic': 'scope'}))

    def unit(self, entity='alpha', label='示例甲', query='示例机构 AB', **extra):
        return dict(stage='search', target=query, sort='general', next='Read visible candidates',
                    sources=[str(self.source)], discovery_entity_ids=[entity],
                    discovery_entity_labels={entity: label}, **extra)

    def import_units(self, *units):
        file = self.base / 'manifest.json'
        file.write_text(json.dumps({'units': list(units)}, ensure_ascii=False))
        return native.import_manifest(self.p, file)['task_ids']

    def routes(self, names, ids):
        # Isolate routing semantics from fixed-round setup and collection guards.
        with patch.object(rounds, 'round_config', return_value={'doctors': names}), \
             patch.object(rounds, 'task_ids', return_value=ids):
            return rounds.scheduler_routes(self.p, 'synthetic-round')

    def test_alias_query_routes_without_full_name_and_never_adds_attribution(self):
        tid, = self.import_units(self.unit())
        self.assertEqual(self.routes(['示例甲'], [tid]), {tid: ['示例甲']})
        payload = self.p.task(tid)['payload']
        self.assertNotIn('identity_confirmed', payload)
        self.assertNotIn('doctor', payload)
        self.assertEqual(self.p.task(tid)['status'], 'pending')

    def test_legacy_fullname_fallback_and_explicit_unknown_does_not_fallback(self):
        legacy = self.unit(query='示例甲 正畸')
        legacy.pop('discovery_entity_ids'); legacy.pop('discovery_entity_labels')
        tid, = self.import_units(legacy)
        self.assertEqual(self.routes(['示例甲'], [tid])[tid], ['示例甲'])
        other, = self.import_units(self.unit('other', '示例乙', '示例甲 比较'))
        self.assertEqual(self.routes(['示例甲'], [other])[other], [])

    def test_shared_query_unions_routes_and_keeps_checkpoint(self):
        tid, = self.import_units(self.unit())
        original = self.p.task(tid)['payload']
        original.update(bridge={'hashes': ['saved'], 'cursor': {'offset': 17},
                                'reviewed_hashes': ['saved'], 'collection_end': True},
                        latest_observation={'summary': 'Previously reviewed'}, next='Continue media separately')
        self.p.db.execute("UPDATE tasks SET payload=?,status='complete',review_status='reviewed',next_page=8,cursor='saved-cursor' WHERE id=?",
                          (project.dumps(original), tid))
        self.p.db.commit()
        source2 = self.base / 'scope-two.json'; source2.write_text('{"synthetic":"second scope"}')
        second = self.unit('beta', '示例乙'); second['sources'] = [str(source2)]
        self.assertEqual(self.import_units(second), [tid])
        now = self.p.task(tid)
        self.assertEqual(now['payload']['discovery_entity_ids'], ['alpha', 'beta'])
        self.assertEqual(now['payload']['bridge'], original['bridge'])
        self.assertEqual(now['payload']['next'], original['next'])
        self.assertEqual(len(now['payload']['provenance']), 2)
        self.assertEqual((now['status'], now['review_status'], now['next_page'], now['cursor']),
                         ('complete', 'reviewed', 8, 'saved-cursor'))
        self.assertEqual(self.routes(['示例甲', '示例乙'], [tid])[tid], ['示例乙', '示例甲'])
        self.import_units(second)
        self.assertEqual(len(self.p.task(tid)['payload']['provenance']), 2)

    def test_shared_query_in_one_batch_and_legacy_reimport_keep_entities(self):
        ids = self.import_units(self.unit(), self.unit('beta', '示例乙'))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)
        legacy = self.unit(); legacy.pop('discovery_entity_ids'); legacy.pop('discovery_entity_labels')
        self.import_units(legacy)
        self.assertEqual(self.p.task(ids[0])['payload']['discovery_entity_ids'], ['alpha', 'beta'])

    def test_shared_query_preserves_each_entity_family_history(self):
        first = self.unit(discovery_families=['initials'], discovery_entity_families={'alpha': ['initials']})
        second = self.unit('beta', '示例乙', discovery_families=['institution'],
                           discovery_entity_families={'beta': ['institution']})
        tid, = self.import_units(first)
        self.import_units(second)
        self.import_units(self.unit())
        payload = self.p.task(tid)['payload']
        self.assertEqual(payload['discovery_families'], ['initials', 'institution'])
        self.assertEqual(payload['discovery_entity_families'], {'alpha': ['initials'], 'beta': ['institution']})

    def test_label_conflict_rejected_before_any_batch_task_added(self):
        tid, = self.import_units(self.unit())
        before = self.p.db.execute('SELECT * FROM tasks ORDER BY id').fetchall()
        with self.assertRaisesRegex(ValueError, 'Conflicting discovery'):
            self.import_units(self.unit('new', '示例丙', 'new query'), self.unit('alpha', '示例乙'))
        self.assertEqual(before, self.p.db.execute('SELECT * FROM tasks ORDER BY id').fetchall())
        self.assertEqual(self.p.task(tid)['payload']['discovery_entity_labels'], {'alpha': '示例甲'})

    def test_conflict_within_batch_is_atomic(self):
        with self.assertRaisesRegex(ValueError, 'Conflicting discovery'):
            self.import_units(self.unit(), self.unit('alpha', '示例乙'))
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    def test_invalid_metadata_rejected_instead_of_silent_fullname_fallback(self):
        invalid = [dict(discovery_entity_ids=[]), dict(discovery_entity_ids=['alpha', 'alpha']),
                   dict(discovery_entity_ids='alpha'), dict(discovery_entity_labels={}),
                   dict(discovery_entity_labels={'alpha': ''}), dict(discovery_entity_labels={'beta': '示例甲'}),
                   dict(discovery_entity_labels={'alpha': '示例甲', 'extra': '示例乙'}),
                   dict(discovery_families='initials'), dict(discovery_families=['']),
                   dict(discovery_entity_families={'unknown': ['initials']}),
                   dict(discovery_entity_families={'alpha': [123]})]
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.import_units(dict(self.unit(), **overrides))
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    def test_discovery_propagates_through_sources_to_same_note_stages(self):
        search, = self.import_units(self.unit())
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',
                          (project.dumps(dict(self.p.task(search)['payload'], bridge={'hashes': ['source-hash']})), search))
        body = self.p.add_task('native', 'body', dict(stage='body', target='a'*24,
                              provenance=[{'sha256': 'source-hash'}]))
        replies = self.p.add_task('native', 'replies', dict(stage='replies', target='a'*24))
        routes = self.routes(['示例甲'], [search, body, replies])
        self.assertTrue(all(value == ['示例甲'] for value in routes.values()))
        self.assertNotIn('discovery_entity_ids', self.p.task(replies)['payload'])


if __name__ == '__main__':
    unittest.main()
