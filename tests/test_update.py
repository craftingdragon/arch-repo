import importlib.util
import unittest

spec = importlib.util.spec_from_file_location('updater', 'scripts/update-aur.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)

class UpdateTests(unittest.TestCase):
    def test_list(self):
        self.assertEqual(updater.names('# comment\nfoo-git # note\nbar\n'), ['foo-git', 'bar'])

    def test_reject_invalid_and_duplicate(self):
        for text in ['../foo', 'foo\nfoo', 'foo; true', '-option', 'foo bar']:
            with self.assertRaises(ValueError):
                updater.names(text)

    def test_snapshot_digest(self):
        self.assertEqual(updater.digest({'a': b'1', 'b': b'2'}), updater.digest({'b': b'2', 'a': b'1'}))
        self.assertNotEqual(updater.digest({'a': b'1'}), updater.digest({'a': b'2'}))


class ProposalRecoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'checkout'
        self.repo.mkdir()
        self.remote = str(self.root / 'remote.git')
        self.git('init', '-q', '-b', 'master')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'README').write_text('Initial state\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Initial state')
        self.git('clone', '--bare', '-q', str(self.repo), self.remote)
        self.git('remote', 'add', 'origin', self.remote)
        self.git('fetch', '-q', 'origin')
        self.git('checkout', '-qb', 'aur-update/example')

    def git(self, *args):
        return updater.run('git', *args, cwd=self.repo)

    def proposal(self, message='aur-bot: example upstream1'):
        import json
        package = self.repo / 'packages/aur/example'
        package.mkdir(parents=True, exist_ok=True)
        (package / 'PKGBUILD').write_text('pkgname=example\n')
        (self.repo / '.aur-locks').mkdir(exist_ok=True)
        (self.repo / '.aur-locks/example.json').write_text(json.dumps({'commit': 'upstream1'}))
        self.git('add', '.')
        self.git('commit', '-qm', message)
        self.git('push', '-q', 'origin', 'aur-update/example')
        return self.git('rev-parse', 'HEAD')

    def state(self, commit='upstream1', existing=None):
        return updater.proposal_state(self.repo, self.remote, 'aur-update/example',
                                      'example', commit, existing or [])

    def test_new_branch(self):
        self.assertEqual(self.state(), ('', False))

    def test_orphaned_branch_reused_after_pr_creation_failure(self):
        head = self.proposal()
        self.assertEqual(self.state(), (head, True))
        from unittest.mock import patch
        with patch.object(updater, 'run') as call:
            updater.ensure_pr('owner/repo', 'aur-update/example', 'example', 'upstream1', [])
        self.assertEqual(call.call_args.args[:3], ('gh', 'pr', 'create'))

    def test_orphaned_branch_updated_with_explicit_lease(self):
        head = self.proposal()
        self.assertEqual(self.state('upstream2'), (head, False))

    def test_human_edits_are_preserved(self):
        self.proposal()
        (self.repo / 'packages/aur/example/notes').write_text('human change')
        self.git('add', '.')
        self.git('commit', '-qm', 'Manual edits')
        self.git('push', '-q', 'origin', 'aur-update/example')
        with self.assertRaisesRegex(ValueError, 'human edits'):
            self.state()

    def test_concurrent_branch_change_rejected(self):
        self.proposal()
        with self.assertRaisesRegex(ValueError, 'changed during update'):
            self.state(existing=[{'headRefOid': '0' * 40}])

    def test_existing_pr_is_edited(self):
        from unittest.mock import patch
        with patch.object(updater, 'run') as call:
            updater.ensure_pr('owner/repo', 'aur-update/example', 'example', 'upstream1', [{'number': 42}])
        self.assertEqual(call.call_args.args[:4], ('gh', 'pr', 'edit', '42'))

    def test_master_advance_preserves_same_upstream_proposal(self):
        head = self.proposal()
        self.git('checkout', '-q', 'master')
        (self.repo / 'README').write_text('Updated master')
        self.git('add', 'README')
        self.git('commit', '-qm', 'Update master')
        self.git('push', '-q', 'origin', 'master')
        self.git('fetch', '-q', 'origin')
        self.assertEqual(self.state(), (head, True))
        self.assertEqual(self.state(), (head, True))
        self.assertEqual(self.git('ls-remote', '--heads', self.remote, 'aur-update/example').split()[0], head)

    def test_legacy_shared_lock_same_revision_is_preserved(self):
        import json
        self.proposal()
        self.git('rm', '.aur-locks/example.json')
        (self.repo / 'aur-lock.json').write_text(json.dumps({'example': {'commit': 'upstream1'}}))
        self.git('add', '.')
        self.git('commit', '-qm', 'aur-bot: example legacy metadata')
        self.git('push', '-q', 'origin', 'aur-update/example')
        self.assertEqual(self.state(), (self.git('rev-parse', 'HEAD'), True))

    def test_independent_package_imports_merge_without_conflicts(self):
        import json
        self.proposal()
        self.git('checkout', '-q', 'master')
        self.git('checkout', '-qb', 'aur-update/other')
        (self.repo / '.aur-locks').mkdir(exist_ok=True)
        (self.repo / '.aur-locks/other.json').write_text(json.dumps({'commit': 'other1'}))
        other = self.repo / 'packages/aur/other'
        other.mkdir(parents=True)
        (other / 'PKGBUILD').write_text('pkgname=other')
        self.git('add', '.')
        self.git('commit', '-qm', 'aur-bot: other import')
        self.git('checkout', '-q', 'master')
        self.git('merge', '--no-edit', 'aur-update/example')
        self.git('merge', '--no-edit', 'aur-update/other')
        for base in ('example', 'other'):
            self.assertTrue((self.repo / updater.lock_path(base)).exists())
            self.assertTrue((self.repo / 'packages/aur' / base / 'PKGBUILD').exists())


class SymlinkImportTests(unittest.TestCase):
    def test_git_symlink_import_lock_and_build_fingerprint(self):
        import tempfile
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path('scripts').resolve()))
        import reuse
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'
            source.mkdir()
            updater.run('git', 'init', '-q', cwd=source)
            updater.run('git', 'config', 'user.name', 'Test', cwd=source)
            updater.run('git', 'config', 'user.email', 'test@example.invalid', cwd=source)
            (source / 'LICENSE').write_text('license')
            (source / 'LICENSES').mkdir()
            (source / 'LICENSES/0BSD.txt').symlink_to('../LICENSE')
            (source / 'broken').symlink_to('missing')
            (source / 'script with spaces').write_text('#!/bin/sh\n')
            (source / 'script with spaces').chmod(0o755)
            updater.run('git', 'add', '.', cwd=source)
            updater.run('git', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture', cwd=source)
            files, modes = updater.read_aur_tree(source)
            target = Path(tmp) / 'target'
            target.mkdir()
            updater.write_tree(target, files, modes)
            self.assertEqual((target / 'LICENSES/0BSD.txt').readlink(), Path('../LICENSE'))
            self.assertEqual((target / 'LICENSES/0BSD.txt').read_text(), 'license')
            self.assertTrue((target / 'script with spaces').stat().st_mode & 0o111)
            first = updater.digest(updater.tree(target))
            first_build = reuse.fingerprint(target, 'build')
            (target / 'broken').unlink()
            (target / 'broken').symlink_to('another-missing')
            self.assertNotEqual(first, updater.digest(updater.tree(target)))
            self.assertNotEqual(first_build, reuse.fingerprint(target, 'build'))

    def test_unsafe_symlinks_are_rejected(self):
        import tempfile
        from pathlib import Path
        for links in ({'link': b'/etc/passwd'}, {'link': b'../outside'},
                      {'a': b'b', 'b': b'a'}):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    updater.write_tree(Path(tmp), links, {name: 0o120000 for name in links})
