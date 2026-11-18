"""End-to-end repository publication using a disposable signing key."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(all(shutil.which(x) for x in ['gpg', 'repo-add', 'bsdtar', 'zstd']), 'Arch tools required')
class PublishTests(unittest.TestCase):
    def test_signed_repository_and_empty_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree('scripts', root / 'scripts')
            (root / 'repo-name.txt').write_text('testrepo\n')
            (root / 'out').mkdir()
            home = root / 'keyring'
            home.mkdir(mode=0o700)
            env = dict(os.environ, GNUPGHOME=str(home))
            def run(*args, **kwargs):
                return subprocess.check_output(args, cwd=root, env=env, stderr=subprocess.STDOUT, **kwargs)
            run('gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase', '', '--quick-generate-key', 'Test <test@example.invalid>', 'ed25519', 'sign', '1d')
            fingerprint = run('gpg', '--with-colons', '--list-secret-keys').decode().split('fpr:::::::::')[1].split(':')[0]
            (root / 'signing-fingerprint.txt').write_text(fingerprint + '\n')
            env.update(SIGNING_KEY=run('gpg', '--armor', '--export-secret-keys').decode(), PAGES_URL='https://example.invalid/project')
            run('bash', 'scripts/publish.sh')
            self.assertIn('No packages yet', (root / 'site/index.html').read_text())
            info = root / '.PKGINFO'
            info.write_text('pkgname = smoke\npkgbase = smoke\npkgver = 1.0-1\npkgdesc = Smoke package\nurl = https://example.invalid\nbuilddate = 1\npackager = Test\nsize = 0\narch = any\nlicense = MIT\n')
            run('bsdtar', '--zstd', '-cf', 'out/smoke-1.0-1-any.pkg.tar.zst', '.PKGINFO')
            run('bash', 'scripts/publish.sh')
            site = root / 'site/x86_64'
            self.assertFalse(any(p.is_symlink() for p in site.iterdir()))
            for name in ['testrepo.db', 'testrepo.files', 'smoke-1.0-1-any.pkg.tar.zst']:
                run('gpg', '--verify', str(site / (name + '.sig')), str(site / name))
            self.assertIn('smoke-1.0', (root / 'site/index.html').read_text())
            self.assertIn('SigLevel = Required DatabaseRequired', (root / 'site/index.html').read_text())
            self.assertIn('smoke-1.0-1/desc', run('bsdtar', '-tf', str(site / 'testrepo.db')).decode())
            (root / 'signing-fingerprint.txt').write_text('INVALID')
            with self.assertRaises(subprocess.CalledProcessError):
                run('bash', 'scripts/publish.sh')
