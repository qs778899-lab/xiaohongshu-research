import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import publication_check as audit
import xhs_local as local
import xhs_paths as paths
import xhs_project as project
import xhs_research as legacy


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'; self.repo.mkdir()
        local.initialize(self.repo)

    def config(self, value):
        (self.repo / '.local/config.json').write_text(json.dumps(value))

    def test_init_preserves_configuration(self):
        self.config({'format': 1, 'defaults': {}, 'projects': {'one': {'path': str(self.base/'topic-a')}}})
        before = (self.repo/'.local/config.json').read_bytes()
        self.assertFalse(local.initialize(self.repo)['created'])
        self.assertEqual(before, (self.repo/'.local/config.json').read_bytes())
        self.assertEqual((self.repo/'.local/config.json').stat().st_mode & 0o077, 0)

    def test_isolated_topics_and_explicit_override(self):
        a, b = self.base/'a', self.base/'b'
        for path in (a, b): path.mkdir()
        bp = a/'bridge/bindings/alpha.json'; bp.parent.mkdir(parents=True)
        bp.write_text(json.dumps({'project': str(a), 'binding_name': 'alpha'}))
        self.config({'format': 1, 'defaults': {}, 'projects': {'a': {'path': str(a), 'binding':'alpha'}, 'b': {'path': str(b)}}})
        self.assertEqual(local.resolve(self.repo, 'a')['binding'], 'alpha')
        self.assertNotIn('binding', local.resolve(self.repo, 'a', project=str(b)))
        self.assertTrue(local.doctor(self.repo)['ok'])
        bp.write_text(json.dumps({'project': str(b), 'binding_name':'alpha'}))
        self.assertFalse(local.doctor(self.repo)['ok'])

    def test_no_unknown_alias_or_symlink_config(self):
        with self.assertRaises(ValueError): local.resolve(self.repo, 'missing')
        target = self.base/'outside.json'; target.write_text('{}')
        (self.repo/'.local/config.json').unlink()
        (self.repo/'.local/config.json').symlink_to(target)
        with self.assertRaises(ValueError): local.initialize(self.repo)

    def test_guards_reject_repo_and_aliases(self):
        alias = self.base/'alias'; alias.symlink_to(self.repo, target_is_directory=True)
        for value in (self.repo, self.repo/'.local/data', alias/'data'):
            with self.assertRaises(ValueError): paths.external_path(value, self.repo)
        link = self.repo/'escape'; link.symlink_to(self.base/'data', target_is_directory=True)
        with self.assertRaises(ValueError): paths.external_path(link, self.repo)
        self.assertEqual(paths.external_path(self.base/'external', self.repo), (self.base/'external').resolve())

    def test_core_writers_reject_in_repository(self):
        with self.assertRaises(legacy.ResearchError): legacy.write_json(paths.REPO_ROOT/'should-not-exist.json', {})
        with self.assertRaises(legacy.ResearchError): project.Project(paths.REPO_ROOT/'should-not-exist', create=True)
        self.assertFalse((paths.REPO_ROOT/'should-not-exist').exists())

    def test_old_external_projects_preserve_rows(self):
        path = self.base/'legacy-project'
        p = project.Project(path, create=True)
        task = p.add_task('search', 'synthetic', {'query':'测试主题'})
        p.db.commit()
        p.db.close()
        p = project.Project(path)
        self.assertEqual(p.db.execute('SELECT id FROM tasks').fetchone()[0], task)
        p.db.close()

    def test_shared_account_key_across_topics(self):
        with patch.object(legacy, 'account_root', return_value=self.base/'locks'):
            (self.base/'locks').mkdir()
            a, b = self.base/'topic-one', self.base/'topic-two'
            legacy.bind_account(a, self.base/'chrome', 'Synthetic Profile')
            legacy.bind_account(b, self.base/'chrome', 'Synthetic Profile')
            self.assertEqual(legacy.account_dir(a), legacy.account_dir(b))
            (legacy.account_dir(a)/'STOP.json').write_text('{}')
            self.assertTrue((legacy.account_dir(b)/'STOP.json').exists())

    def git(self, *args):
        return subprocess.check_output(['git','-C',str(self.repo), *args], stderr=subprocess.DEVNULL)

    def seed_git(self):
        self.git('init','-b','main')
        self.git('config','user.name','Synthetic Test')
        self.git('config','user.email','test@example.invalid')
        (self.repo/'PUBLIC_FILES.txt').write_text('PUBLIC_FILES.txt\nsafe.py\n')
        (self.repo/'safe.py').write_text('value = 1\n')
        self.git('add','PUBLIC_FILES.txt','safe.py')

    def test_forced_private_file_fails_index_check(self):
        self.seed_git()
        self.git('add','.local/config.json')
        result = audit.check(self.repo, staged=True)
        self.assertFalse(result['ok'])
        self.assertIn('not_in_public_inventory', str(result))

    def test_removed_secret_still_fails_history(self):
        self.seed_git()
        self.git('commit','-m','initial')
        secret = 'gh' + 'p_' + 'X'*36
        (self.repo/'safe.py').write_text('value = "'+secret+'"\n')
        self.git('add','safe.py'); self.git('commit','-m','old secret')
        (self.repo/'safe.py').write_text('value = 1\n')
        self.git('add','safe.py'); self.git('commit','-m','remove secret')
        result = audit.check(self.repo, history=True)
        self.assertFalse(result['ok'])
        self.assertNotIn(secret, json.dumps(result))

    def test_staged_blob_and_private_deny_policy(self):
        self.seed_git()
        (self.repo/'.local/publication-policy.json').write_text(json.dumps({'forbidden_strings':['PRIVATE_FIXTURE']}))
        (self.repo/'safe.py').write_text('PRIVATE_FIXTURE')
        self.git('add','safe.py')
        (self.repo/'safe.py').write_text('clean')
        self.assertFalse(audit.check(self.repo, staged=True)['ok'])

    def test_clean_history_passes(self):
        self.seed_git(); self.git('commit','-m','synthetic fixture')
        self.assertTrue(audit.check(self.repo, staged=True, history=True)['ok'])

if __name__ == '__main__': unittest.main()
