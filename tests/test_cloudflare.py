"""Verify the Pages/R2 split, including epoch filenames and signed metadata."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('cloudflare', 'scripts/prepare-cloudflare.py')
cloudflare = importlib.util.module_from_spec(spec)
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
