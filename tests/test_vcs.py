import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path('scripts').resolve()))
import vcs
import reuse


class VcsTests(unittest.TestCase):
    def test_local_upstream_and_shell_pinning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upstream = root / 'upstream'
            upstream.mkdir()
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=upstream, text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            git('config', 'commit.gpgsign', 'false')
            (upstream / 'file').write_text('one')
            git('add', '.')
            git('commit', '-qm', 'First')
            first = git('rev-parse', 'HEAD')
            source = 'checkout::git+' + upstream.as_uri() + '#branch=main'
            metadata = {'source': [source, 'local.patch'], 'source_x86_64': [source]}
            locked = vcs.resolve(metadata)
            self.assertEqual(locked, {source: first})
            self.assertEqual(vcs.resolve(metadata), locked)
            (upstream / 'file').write_text('two')
            git('commit', '-qam', 'Second')
            self.assertNotEqual(vcs.resolve(metadata), locked)
            # A reviewed lock continues to select the first SHA even after HEAD advances.
            recipe = 'source=(' + ' '.join(__import__('shlex').quote(s) for s in metadata['source']) + ')\n'
            recipe += 'source_x86_64=(' + __import__('shlex').quote(source) + ')\n'
            recipe += vcs.pin_script(metadata, locked)
            recipe += 'printf "%s\\n" "${source[@]}" "${source_x86_64[@]}"\n'
            output = subprocess.check_output(['bash', '-euc', recipe], text=True)
            self.assertEqual(output.splitlines(), [source.split('#')[0] + '#commit=' + first, 'local.patch', source.split('#')[0] + '#commit=' + first])
            self.assertEqual(vcs.resolve({'source': ['git+' + upstream.as_uri()]})['git+' + upstream.as_uri()], git('rev-parse', 'HEAD'))

    def test_pinned_sources_and_regular_archives_are_not_polled(self):
        self.assertEqual(vcs.resolve({'source': ['git+https://example.invalid/repo#tag=v1?signed',
                                                'git+https://example.invalid/repo#commit=' + 'a' * 40,
                                                'https://example.invalid/source.tar.gz']}), {})

    def test_signed_alias_and_stale_lock_rejection(self):
        source = 'alias::git+https://example.invalid/repo?signed#branch=main'
        info = {'source': [source]}
        with self.assertRaises(ValueError):
            vcs.pin_script(info, {})
        self.assertIn('#commit=' + 'a' * 40 + '?signed', vcs.pin_script(info, {source: 'a' * 40}))
        with self.assertRaises(ValueError):
            vcs.validate(info, {source: 'invalid'})
        with patch.object(vcs.subprocess, 'check_output', side_effect=subprocess.CalledProcessError(2, 'git')):
            with self.assertRaisesRegex(ValueError, 'Cannot resolve'):
                vcs.resolve(info)

    def test_source_only_update_changes_existing_proposal_identity(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('updater', 'scripts/update-aur.py')
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)
        head = 'a' * 40
        # Exercise the same AUR SHA with unchanged vs changed upstream source SHAs.
        def run(*args, **kwargs):
            if args[1] == 'ls-remote': return head + '\trefs/heads/branch'
            if args[1] == 'fetch': return ''
            if args[1] == 'rev-parse': return head
            if args[1] == 'log': return 'aur-bot: example-git recipe-sha'
            if args[1] == 'diff': return '.aur-locks/example-git.json'
            raise AssertionError(args)
        state = {'commit': 'recipe-sha', 'vcs_sources': {'source': 'b' * 40}}
        with patch.object(updater, 'run', side_effect=run), patch.object(updater, 'read_proposed_lock', return_value=state):
            self.assertEqual(updater.proposal_state(Path('.'), 'remote', 'branch', 'example-git', 'recipe-sha', [], state['vcs_sources']), (head, True))
            self.assertEqual(updater.proposal_state(Path('.'), 'remote', 'branch', 'example-git', 'recipe-sha', [], {'source': 'c' * 40}), (head, False))


class UpdaterRoundTripTests(unittest.TestCase):
    def test_upstream_only_update_opens_pr_and_preserves_unchanged_proposal(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('updater', 'scripts/update-aur.py')
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)
        check = Path('scripts/check.py').resolve()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def init(path):
                path.mkdir()
                subprocess.run(['git', 'init', '-q', '-b', 'master', str(path)], check=True)
                for key, value in [('user.name', 'Test'), ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false')]:
                    subprocess.run(['git', 'config', key, value], cwd=path, check=True)
            def commit(path, message):
                subprocess.run(['git', 'add', '.'], cwd=path, check=True)
                subprocess.run(['git', 'commit', '-qm', message], cwd=path, check=True)
                return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=path, text=True).strip()
            upstream, aur, workspace = [root / name for name in ('upstream', 'aur', 'workspace')]
            for path in (upstream, aur, workspace):
                init(path)
            (upstream / 'value').write_text('first')
            first = commit(upstream, 'First')
            source = 'code::git+' + upstream.as_uri()
            (aur / 'PKGBUILD').write_text("pkgname=example-git\npkgver=1\npkgrel=1\narch=('any')\nsource=(" + repr(source) + ')\n')
            (aur / '.SRCINFO').write_text('pkgbase = example-git\n\tpkgver = 1\n\tsource = ' + source + '\npkgname = example-git\n')
            packaging_sha = commit(aur, 'Recipe')
            (workspace / 'aur.txt').write_text('example-git\n')
            (workspace / 'signing-fingerprint.txt').write_text('A' * 40)
            commit(workspace, 'Initial')
            remote = root / 'remote.git'
            subprocess.run(['git', 'clone', '--bare', '-q', str(workspace), str(remote)], check=True)
            subprocess.run(['git', 'remote', 'add', 'origin', str(remote)], cwd=workspace, check=True)
            subprocess.run(['git', 'fetch', '-q', 'origin'], cwd=workspace, check=True)
            original_run = updater.run
            pr_open = False
            pr_creations = []
            pr_edits = []
            def run(*args, cwd=None):
                nonlocal pr_open
                if args[0] == 'gh':
                    if args[1:3] == ('pr', 'list'):
                        head = original_run('git', '--git-dir', str(remote), 'rev-parse', 'refs/heads/aur-update/example-git') if pr_open else ''
                        return json.dumps([{'number': 1, 'headRefOid': head}] if pr_open else [])
                    if args[1:3] == ('pr', 'create'):
                        pr_open = True
                        pr_creations.append(args)
                        return 'PR created'
                    if args[1:3] == ('pr', 'edit'):
                        pr_edits.append(args)
                        return 'PR updated'
                    raise AssertionError(args)
                rewritten = [str(aur) if arg == 'https://aur.archlinux.org/example-git.git'
                             else str(remote) if arg == 'https://github.com/test/repo.git' else arg for arg in args]
                return original_run(*rewritten, cwd=cwd)
            old_cwd = Path.cwd()
            os.chdir(workspace)
            try:
                with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'test/repo', 'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'commit.gpgsign', 'GIT_CONFIG_VALUE_0': 'false'}), patch.object(updater, 'run', side_effect=run):
                    updater.main()
                    self.assertEqual(len(pr_creations), 1)
                    subprocess.run(['git', 'fetch', '-q', 'origin'], check=True)
                    subprocess.run(['git', 'merge', '--ff-only', 'origin/aur-update/example-git'], check=True, stdout=subprocess.DEVNULL)
                    subprocess.run(['git', 'push', '-q', 'origin', 'master'], check=True)
                    pr_open = False
                    (upstream / 'value').write_text('second')
                    second = commit(upstream, 'Second')
                    updater.main()
                    self.assertEqual(len(pr_creations), 2)
                    head = original_run('git', '--git-dir', str(remote), 'rev-parse', 'refs/heads/aur-update/example-git')
                    updater.main()
                    self.assertEqual(len(pr_creations), 2)
                    self.assertEqual(len(pr_edits), 1)
                    self.assertEqual(original_run('git', '--git-dir', str(remote), 'rev-parse', 'refs/heads/aur-update/example-git'), head)
                    checkout = root / 'fresh'
                    subprocess.run(['git', 'clone', '-q', '--branch', 'aur-update/example-git', str(remote), str(checkout)], check=True)
                    state = json.loads((checkout / '.aur-locks/example-git.json').read_text())
                    self.assertEqual(state['commit'], packaging_sha)
                    self.assertEqual(state['vcs_sources'], {source: second})
                    self.assertNotEqual(first, second)
                    subprocess.run(['python', str(check)], cwd=checkout, check=True)
                    self.assertEqual((checkout / 'packages/aur/example-git/PKGBUILD').read_bytes(), (aur / 'PKGBUILD').read_bytes())
            finally:
                os.chdir(old_cwd)
