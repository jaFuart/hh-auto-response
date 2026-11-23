import subprocess
import shutil
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.audit_repository import audit, inspect, private_path


class RepositoryAudit(unittest.TestCase):
    def test_private_files_are_rejected_but_examples_are_publishable(self):
        for name in ('.env', '.env.production', 'hh-data/data/state.json', 'hh-session.json',
                     'candidate_profile_hh.json', 'browser-profile/Cookies', 'identity.key'):
            self.assertTrue(private_path(name), name)
        for name in ('.env.example', 'config.server.example.toml', 'candidate_profile.example.json',
                     'hh_auto/web/session.py'):
            self.assertFalse(private_path(name), name)

    def test_findings_never_contain_the_secret_value(self):
        value = b'sk-proj-' + b'a' * 50
        findings = inspect('example.py', b'API_KEY=' + value)
        self.assertEqual(findings, ['OpenAI key: example.py'])
        self.assertNotIn(value.decode(), str(findings))

    @unittest.skipUnless(shutil.which('git'), 'Git is required for the history fixture')
    def test_deleted_secret_is_still_found_in_git_history(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'GIT_CONFIG_COUNT': '0'}):
            root = Path(folder)
            def git(*args):
                subprocess.run(['git', '-C', folder, *args], check=True, capture_output=True)
            git('init', '-q')
            git('config', 'user.name', 'Maxim Shevarev')
            git('config', 'user.email', '74241026+jaFuart@users.noreply.github.com')
            (root / '.env').write_text('test fixture\n')
            git('add', '.env')
            git('commit', '-qm', 'Fixture one')
            git('rm', '.env')
            (root / 'README.md').write_text('Source only\n')
            git('add', 'README.md')
            git('commit', '-qm', 'Fixture two')
            files, commits, issues = audit(root, history=True)
            self.assertEqual((files, commits), (1, 2))
            self.assertTrue(any('private path in history' in item for item in issues))


if __name__ == '__main__':
    unittest.main()
