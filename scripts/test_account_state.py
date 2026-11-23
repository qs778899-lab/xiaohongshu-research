import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_account_state as state
import xhs_account_identity as identity
import xhs_local as local


class AccountStateTests(unittest.TestCase):
    def test_sorted_archive_restores_exact_original_hash_only(self):
        value={'at':'2020-01-01T00:00:00Z','reason':'restriction','channel':'bridge','credential_free':True}
        original=json.dumps(value,ensure_ascii=False,indent=2).encode()
        archive=json.dumps(value,sort_keys=True).encode()
        self.assertEqual(state.original_stop_bytes(json.loads(archive),archive,state.sha(original)),original)
        with self.assertRaisesRegex(ValueError,'ARCHIVE_STOP_CHANGED'):
            state.original_stop_bytes(value,archive,'0'*64)

    def test_extra_archive_fields_cannot_be_dropped_to_match_hash(self):
        value={'at':'old','reason':'restriction','channel':'bridge','credential_free':True}
        raw=json.dumps(value,indent=2).encode();value['new_restriction']='must retain'
        with self.assertRaisesRegex(ValueError,'ARCHIVE_STOP_CHANGED'):
            state.original_stop_bytes(value,json.dumps(value).encode(),state.sha(raw))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'old'
        self.source.mkdir(mode=0o700)
        self.destination = self.root / 'persistent'
        self.repo = self.root / 'fixture-repo'
        self.repo.mkdir()
        local.initialize(self.repo)
        self.uid = '1'*24
        self.key = identity.uid_key(self.uid)
        self.profile = '2'*64
        self.raw = b'{"reason":"protect","at":"2020-01-01T00:00:00Z"}'
        self.record = {'uid':self.uid, 'uid_key':self.key, 'profile_key':self.profile,
                       'guards':{self.key:self.raw,self.profile:self.raw},
                       'binding':'synthetic', 'binding_sha256':'3'*64}
        p = patch.object(state, 'archive_guards', return_value=self.record)
        p.start();self.addCleanup(p.stop)

    def migrate(self, activate=False):
        return state.migrate(self.source,self.destination,['synthetic'],activate,self.repo)

    def test_missing_guards_are_restored_but_old_grants_are_not_activated(self):
        result=self.migrate()
        self.assertFalse(result['collection_permitted'])
        self.assertEqual((self.destination/self.key/'STOP.json').read_bytes(),self.raw)
        self.assertFalse((self.source/self.key/'STOP.json').exists())
        self.assertIsNone(state.configured_root(self.repo))

    def test_newer_stop_is_copied_without_overwriting_from_archive(self):
        folder=self.source/self.key;folder.mkdir(mode=0o700)
        newer=b'{"reason":"newer restriction"}'
        (folder/'STOP.json').write_bytes(newer)
        self.migrate()
        self.assertEqual((self.destination/self.key/'STOP.json').read_bytes(),newer)
        self.assertEqual((folder/'STOP.json').read_bytes(),newer)

    def test_identity_aliases_and_consumed_attempts_survive_migration(self):
        alias='4'*64
        (self.source/'public-identities.json').write_text(json.dumps({self.key:{'uid':self.uid,'profile_keys':[alias]}}))
        folder=self.source/'manual-tab-attempts';folder.mkdir(mode=0o700)
        (folder/'used.json').write_bytes(b'{"consumed":true}')
        self.migrate(True)
        self.assertEqual(state.configured_root(self.repo),self.destination)
        data=json.loads((self.destination/'public-identities.json').read_text())
        self.assertIn(alias,data[self.key]['profile_keys'])
        self.assertIn(self.profile,data[self.key]['profile_keys'])
        self.assertEqual((self.destination/'manual-tab-attempts/used.json').read_bytes(),b'{"consumed":true}')

    def test_activation_resumes_after_copy_only_and_is_idempotent(self):
        self.migrate()
        first=state.activate_state(self.destination,self.repo)
        second=state.activate_state(self.destination,self.repo)
        self.assertFalse(first['already_active']);self.assertTrue(second['already_active'])

    def test_source_changed_after_copy_prevents_activation(self):
        self.migrate()
        (self.source/self.key/'STOP.json').write_text('{"new":true}')
        with self.assertRaisesRegex(ValueError,'SOURCE_CHANGED'):
            state.activate_state(self.destination,self.repo)
        self.assertIsNone(state.configured_root(self.repo))

    def test_destination_tampering_prevents_activation(self):
        self.migrate()
        (self.destination/self.key/'STOP.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'DESTINATION_CHANGED'):
            state.activate_state(self.destination,self.repo)

    def test_disappeared_configured_directory_is_not_recreated(self):
        self.migrate(True)
        self.destination.rename(self.root/'removed')
        with self.assertRaisesRegex(ValueError,'DIRECTORY_MISSING'):
            state.configured_root(self.repo)
        self.assertFalse(self.destination.exists())

    def test_source_symlink_refused(self):
        (self.source/'alias.json').symlink_to(self.root/'anything')
        with self.assertRaisesRegex(ValueError,'SOURCE_SYMLINK'):
            self.migrate()
        self.assertFalse(self.destination.exists())

    def test_busy_collection_prevents_cutover(self):
        folder=self.source/self.key;folder.mkdir(mode=0o700)
        with identity.lock_file(folder/'collection.lock'):
            with self.assertRaises(Exception):self.migrate()
        self.assertFalse(self.destination.exists())

    def test_repeated_migration_cannot_reset_consumed_state(self):
        self.migrate(True)
        with self.assertRaisesRegex(ValueError,'DESTINATION_EXISTS'):
            self.migrate(True)

    def test_insecure_root_is_refused(self):
        self.source.chmod(0o755)
        with self.assertRaisesRegex(ValueError,'PERMISSIONS'):
            self.migrate()

    def test_conflicting_archives_are_refused(self):
        changed=dict(self.record,guards={self.key:b'changed'})
        with patch.object(state,'archive_guards',side_effect=[self.record,changed]):
            with self.assertRaisesRegex(ValueError,'CONFLICTING_ARCHIVES'):
                state.migrate(self.source,self.destination,['one','two'])


if __name__ == '__main__':
    unittest.main()
