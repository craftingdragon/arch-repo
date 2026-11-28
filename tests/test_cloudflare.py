"""Verify the Pages/R2 split, including epoch filenames and signed metadata."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('cloudflare', 'scripts/prepare-cloudflare.py')
cloudflare = importlib.util.module_from_spec(spec)
with patch('sys.path', [str(Path('scripts').resolve()), *sys.path]):
    spec.loader.exec_module(cloudflare)


class CloudflareTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        previous = Path.cwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, previous)
        self.env = patch.dict(os.environ, R2_PUBLIC_URL='https://packages.example.invalid')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.arch = Path('site/x86_64')
        self.arch.mkdir(parents=True)
        self.name = 'example-1:2.0-1-x86_64.pkg.tar.zst'
        self.package = self.arch / self.name
        self.package.write_bytes(b'package')
        self.signature = self.arch / (self.name + '.sig')
        self.signature.write_bytes(b'signature')
        self.digest = hashlib.sha256(b'package').hexdigest()
        Path('site/build-manifest.json').write_text(json.dumps({'recipes': {'example': {
            'outputs': [{'filename': self.name, 'sha256': self.digest}]}}}))
        (self.arch / 'crafting.db').write_bytes(b'database')
        (self.arch / 'crafting.db.sig').write_bytes(b'database signature')
        Path('site/build-manifest.json.sig').write_bytes(b'manifest signature')

    def test_immutable_redirects_preserve_epoch_and_metadata(self):
        manifest = Path('site/build-manifest.json').read_bytes()
        cloudflare.main()
        self.assertFalse(self.package.exists())
        self.assertFalse(self.signature.exists())
        self.assertEqual((Path('r2/sha256') / self.digest / self.name).read_bytes(), b'package')
        signature_hash = hashlib.sha256(b'signature').hexdigest()
        self.assertEqual((Path('r2/sha256') / signature_hash / (self.name + '.sig')).read_bytes(), b'signature')
        # Pacman uses the final package URL + '.sig', rather than the catalog's
        # independent signature redirect. Both paths must return the signature.
        package = Path('r2/sha256') / self.digest / self.name
        self.assertEqual(Path(str(package) + '.sig').read_bytes(), b'signature')
        redirects = Path('site/_redirects').read_text().splitlines()
        self.assertEqual(len(redirects), 4)
        self.assertIn(f'/x86_64/{self.name} https://packages.example.invalid/sha256/{self.digest}/example-1%3A2.0-1-x86_64.pkg.tar.zst 301', redirects)
        self.assertIn(f'/x86_64/example-1%3A2.0-1-x86_64.pkg.tar.zst https://packages.example.invalid/sha256/{self.digest}/example-1%3A2.0-1-x86_64.pkg.tar.zst 301', redirects)
        self.assertEqual((self.arch / 'crafting.db').read_bytes(), b'database')
        self.assertEqual(Path('site/build-manifest.json').read_bytes(), manifest)
        self.assertTrue(Path('site/build-manifest.json.sig').exists())
        self.assertIn('Cache-Control: no-cache', Path('site/_headers').read_text())

    def test_reject_corrupt_archive_before_moving(self):
        self.package.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            cloudflare.main()
        self.assertTrue(self.package.exists())
        self.assertFalse(Path('r2').exists())

    def test_reject_bad_public_url(self):
        for url in ('http://example.invalid', 'https://user:password@example.invalid',
                    'https://example.invalid/?query=1', 'https://example.invalid/\ninjection'):
            with self.subTest(url=url), patch.dict(os.environ, R2_PUBLIC_URL=url):
                with self.assertRaises(ValueError):
                    cloudflare.main()


    def test_incremental_handoff_keeps_signatures_and_archive_index(self):
        with patch.dict(os.environ, R2_INCREMENTAL='true'), patch.object(
            cloudflare, 'already_public', return_value=True
        ) as lookup:
            cloudflare.main()
        key = f'sha256/{self.digest}/{self.name}'
        lookup.assert_called_once_with('https://packages.example.invalid', key, 7)
        self.assertFalse((Path('r2') / key).exists())
        self.assertEqual((Path('r2') / (key + '.sig')).read_bytes(), b'signature')
        self.assertEqual(json.loads(Path('r2-index.json').read_text())[key], 7)


class R2UploadTests(unittest.TestCase):
    def setUp(self):
        scripts = Path('scripts').resolve()
        with patch('sys.path', [str(scripts), *sys.path]):
            spec = importlib.util.spec_from_file_location('r2_upload', scripts / 'upload-r2.py')
            self.upload = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.upload)

    def test_exact_key_checks_skip_existing_and_fail_on_unexpected_responses(self):
        existing = 'existing'
        missing = 'missing'

        def head(args, **kwargs):
            if args[args.index('--key') + 1] in (existing, existing + '.sig'):
                return SimpleNamespace(returncode=0, stdout='7\n', stderr='')
            return SimpleNamespace(returncode=254, stdout='', stderr='(404) HeadObject')

        with patch.object(self.upload.subprocess, 'run', side_effect=head):
            self.assertEqual(self.upload.upload_plan(['aws'], 'bucket', {existing: 7, missing: 7}),
                             [missing])
            self.assertEqual(self.upload.upload_plan(['aws'], 'bucket',
                                                     {existing: 7, existing + '.sig': 8}),
                             [existing + '.sig'])
            with self.assertRaisesRegex(RuntimeError, 'Unexpected size'):
                self.upload.upload_plan(['aws'], 'bucket', {existing: 8})
        with patch.object(self.upload.subprocess, 'run',
                          return_value=SimpleNamespace(returncode=254, stdout='', stderr='(403) HeadObject')):
            with self.assertRaisesRegex(RuntimeError, 'Cannot check'):
                self.upload.upload_plan(['aws'], 'bucket', {existing: 7})

    def test_omitted_epoch_archive_recovers_only_if_digest_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            name = 'example-1:2.0-1-x86_64.pkg.tar.zst'
            source = root / name
            source.write_bytes(b'package')
            artifact = root / 'packages.tar'
            with tarfile.open(artifact, 'w') as tar:
                tar.add(source, arcname=f'./{name}')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            good = f'sha256/{digest}/{name}'
            bad = f'sha256/{"0" * 64}/{name}'
            with patch.object(self.upload, 'STAGING', root / 'staged'):
                self.upload.recover_archives([good], artifact, {good: 7})
                self.assertEqual((self.upload.STAGING / good).read_bytes(), b'package')
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    self.upload.recover_archives([bad], artifact, {bad: 7})
