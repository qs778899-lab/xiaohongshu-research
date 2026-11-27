"""Synthetic repositories only: all-ref inventory and disclosure boundaries."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import publication_check as audit


class PublicationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Synthetic Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'tag.gpgsign', 'false')
        self.write('PUBLIC_FILES.txt', 'PUBLIC_FILES.txt\nsafe.py\n')
        self.write('safe.py', 'value = 1\n')
        self.commit('initial public files')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL)

    def write(self, name, value):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes): path.write_bytes(value)
        else: path.write_text(value)

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-m', message)
        return self.git('rev-parse', 'HEAD').decode().strip()

    def feature(self, name='feat/extra-docs'):
        self.git('checkout', '-b', name)

    def register_extra(self, value='Generic public documentation.\n'):
        self.write('PUBLIC_FILES.txt', 'PUBLIC_FILES.txt\nsafe.py\nFEATURE.md\n')
        self.write('FEATURE.md', value)

    def check(self):
        return audit.check(self.repo, history=True)

    def test_other_branch_uses_its_own_valid_inventory(self):
        self.feature(); self.register_extra(); self.commit('add alternate public document')
        self.git('checkout', 'main')
        self.assertNotIn('FEATURE.md', (self.repo/'PUBLIC_FILES.txt').read_text())
        self.assertTrue(self.check()['ok'])

    def test_remote_tracking_ref_is_included(self):
        self.feature(); self.register_extra(); tip=self.commit('alternate branch')
        self.git('update-ref', 'refs/remotes/example/alternate', tip)
        self.git('checkout', 'main'); self.git('branch', '-D', 'feat/extra-docs')
        self.assertTrue(self.check()['ok'])

    def test_later_inventory_cannot_authorize_earlier_unlisted_path(self):
        self.feature(); self.write('FEATURE.md', 'A public-looking unregistered file.\n')
        bad=self.commit('unregistered path in older history')
        self.register_extra(); self.commit('register only in a later commit')
        self.git('checkout', 'main')
        result=self.check(); self.assertFalse(result['ok'])
        self.assertIn((bad[:12], 'FEATURE.md', 'not_in_public_inventory'), result['errors'])

    def test_inventory_of_one_branch_does_not_authorize_another_branch(self):
        self.feature(); self.register_extra(); self.commit('registered alternate document')
        self.git('checkout', 'main'); self.feature('feat/unregistered')
        self.write('FEATURE.md', 'Same name, different branch, no declaration.\n')
        bad=self.commit('unregistered alternate document'); self.git('checkout', 'main')
        result=self.check(); self.assertFalse(result['ok'])
        self.assertIn((bad[:12], 'FEATURE.md', 'not_in_public_inventory'), result['errors'])

    def test_removed_secret_on_another_branch_still_blocks(self):
        self.feature(); secret='gh'+'p_'+'X'*36
        self.register_extra(secret); self.commit('old sensitive content')
        self.register_extra('Generic replacement.\n'); self.commit('remove sensitive content')
        self.git('checkout', 'main')
        result=self.check(); self.assertFalse(result['ok'])
        self.assertIn('github_credential', str(result)); self.assertNotIn(secret, json.dumps(result))

    def test_deleted_alternate_file_still_scanned(self):
        self.feature(); secret='gh'+'p_'+'Y'*36
        self.register_extra(secret); self.commit('sensitive alternate file')
        (self.repo/'FEATURE.md').unlink()
        self.write('PUBLIC_FILES.txt', 'PUBLIC_FILES.txt\nsafe.py\n')
        self.commit('delete alternate file and inventory entry'); self.git('checkout', 'main')
        result=self.check(); self.assertFalse(result['ok']); self.assertIn('github_credential', str(result))

    def test_tag_only_history_and_tag_message_both_scanned(self):
        self.feature(); self.register_extra(); self.commit('tagged alternate document')
        token='gh'+'p_'+'Z'*36
        self.git('tag', '-a', 'synthetic-release', '-m', token)
        self.git('checkout', 'main'); self.git('branch', '-D', 'feat/extra-docs')
        result=self.check(); self.assertFalse(result['ok'])
        self.assertIn(('tag', '<tag-message>', 'github_credential'), result['errors'])
        self.assertNotIn(token, json.dumps(result))

    def test_commit_message_is_scanned_with_version_inventory(self):
        self.feature(); self.register_extra(); token='gh'+'p_'+'M'*36
        self.commit(token); self.git('checkout', 'main')
        result=self.check(); self.assertFalse(result['ok']); self.assertIn('<commit-message>', str(result))
        self.assertNotIn(token, json.dumps(result))

    def test_private_policy_applies_to_other_branch(self):
        self.feature(); self.register_extra('PRIVATE_FIXTURE_VALUE\n'); self.commit('alternate content')
        self.git('checkout', 'main')
        self.write('.local/publication-policy.json', json.dumps({'forbidden_strings':['PRIVATE_FIXTURE_VALUE']}))
        result=self.check(); self.assertFalse(result['ok']); self.assertIn('private_policy_match', str(result))

    def test_missing_historical_inventory_blocks(self):
        self.feature(); (self.repo/'PUBLIC_FILES.txt').unlink(); bad=self.commit('remove inventory')
        self.git('checkout', 'main'); result=self.check(); self.assertFalse(result['ok'])
        self.assertIn((bad[:12], 'PUBLIC_FILES.txt', 'history_inventory_missing'), result['errors'])

    def test_invalid_historical_inventory_never_grants_extra_paths(self):
        self.feature()
        for value in ['PUBLIC_FILES.txt\n../outside.md\n', 'PUBLIC_FILES.txt\n.local/private.json\n', 'PUBLIC_FILES.txt\nimage.png\n', 'safe.py\n', b'\xff']:
            self.write('PUBLIC_FILES.txt', value); self.commit('invalid historical inventory fixture')
        self.git('checkout', 'main'); result=self.check(); self.assertFalse(result['ok'])
        self.assertEqual(sum(e[2]=='history_inventory_invalid' for e in result['errors']), 5)

    def test_symlink_historical_inventory_blocks(self):
        self.feature(); (self.repo/'PUBLIC_FILES.txt').unlink()
        self.write('alternate.txt', 'PUBLIC_FILES.txt\nsafe.py\nalternate.txt\n')
        (self.repo/'PUBLIC_FILES.txt').symlink_to('alternate.txt')
        bad=self.commit('non-regular historical inventory'); self.git('checkout', 'main')
        result=self.check(); self.assertFalse(result['ok'])
        self.assertIn((bad[:12], 'PUBLIC_FILES.txt', 'history_inventory_non_regular'), result['errors'])

    def test_current_inventory_legacy_allowance_remains(self):
        self.write('legacy.md', 'Legacy public text.\n'); self.commit('legacy public path')
        (self.repo/'legacy.md').unlink()
        self.write('PUBLIC_FILES.txt', 'PUBLIC_FILES.txt\nsafe.py\nlegacy.md\n')
        self.commit('declare retained legacy path')
        self.assertTrue(self.check()['ok'])

    def test_unregistered_blob_is_also_checked_for_secrets(self):
        self.feature(); token='gh'+'p_'+'U'*36
        self.write('unregistered.md', token); self.commit('unregistered sensitive content')
        self.git('checkout', 'main'); result=self.check(); self.assertFalse(result['ok'])
        self.assertIn('not_in_public_inventory', str(result)); self.assertIn('github_credential', str(result))

    def test_index_does_not_inherit_alternate_branch_inventory(self):
        self.feature(); self.register_extra(); self.commit('registered alternate file')
        self.git('checkout', 'main'); self.write('FEATURE.md', 'New unstated index file.\n'); self.git('add', 'FEATURE.md')
        result=audit.check(self.repo, staged=True); self.assertFalse(result['ok'])
        self.assertIn(('index', 'FEATURE.md', 'not_in_public_inventory'), result['errors'])


if __name__=='__main__':unittest.main()
