"""End-to-end repository publication using a disposable signing key."""
import os
import json
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
            (root / 'out/build-manifest.json').write_text(json.dumps({'schema': 1, 'architecture': 'x86_64', 'recipes': {}}))
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
            info.write_text('pkgname = smoke\npkgbase = smoke\npkgver = 1:1.0-1\npkgdesc = Smoke package\nurl = https://example.invalid\nbuilddate = 1\npackager = Test\nsize = 0\narch = any\nlicense = MIT\n')
            run('bsdtar', '--zstd', '-cf', 'out/smoke-1:1.0-1-any.pkg.tar.zst', '.PKGINFO')
            import sys
            sys.path.insert(0, str(Path('scripts').resolve()))
            import reuse
            archive = root / 'out/smoke-1:1.0-1-any.pkg.tar.zst'
            record = {'fingerprint': 'test', 'dependencies': [], 'outputs': [
                {'filename': archive.name, 'sha256': reuse.sha(archive), 'name': 'smoke', 'version': '1:1.0-1', 'architecture': 'any'}]}
            (root / 'out/build-manifest.json').write_text(json.dumps({'schema': 1, 'architecture': 'x86_64', 'recipes': {'smoke': record}}))
            run('bash', 'scripts/publish.sh')
            restored = reuse.restore((root / 'site').as_uri(), root / 'signing-fingerprint.txt', root / 'out')
            self.assertEqual(restored, {'smoke': record})
            (root / 'wrong-fingerprint').write_text('0' * 40)
            self.assertEqual(reuse.restore((root / 'site').as_uri(), root / 'wrong-fingerprint', root / 'out'), {})
            manifest = root / 'site/build-manifest.json'
            original = manifest.read_bytes()
            manifest.write_bytes(original + b' ')
            self.assertEqual(reuse.restore((root / 'site').as_uri(), root / 'signing-fingerprint.txt', root / 'out'), {})
            manifest.write_bytes(original)
            site = root / 'site/x86_64'
            self.assertFalse(any(p.is_symlink() for p in site.iterdir()))
            for name in ['testrepo.db', 'testrepo.files', 'smoke-1:1.0-1-any.pkg.tar.zst']:
                run('gpg', '--verify', str(site / (name + '.sig')), str(site / name))
            run('gpg', '--verify', str(root / 'site/build-manifest.json.sig'), str(root / 'site/build-manifest.json'))
            self.assertIn('smoke-1%3A1.0', (root / 'site/packages.html').read_text())
            self.assertIn('SigLevel = Required DatabaseRequired', (root / 'site/index.html').read_text())
            self.assertIn('smoke-1:1.0-1/desc', run('bsdtar', '-tf', str(site / 'testrepo.db')).decode())
            env['R2_PUBLIC_URL'] = 'https://packages.example.invalid'
            run('python', 'scripts/prepare-cloudflare.py')
            self.assertFalse((site / archive.name).exists())
            self.assertEqual((root / 'r2/sha256' / reuse.sha(archive) / archive.name).read_bytes(), archive.read_bytes())
            adjacent_signature = root / 'r2/sha256' / reuse.sha(archive) / (archive.name + '.sig')
            run('gpg', '--verify', str(adjacent_signature), str(archive))
            self.assertEqual(reuse.restore((root / 'site').as_uri(), root / 'signing-fingerprint.txt', root / 'out'), {'smoke': record})
            (root / 'signing-fingerprint.txt').write_text('INVALID')
            with self.assertRaises(subprocess.CalledProcessError):
                run('bash', 'scripts/publish.sh')
