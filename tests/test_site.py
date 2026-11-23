import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('site_generator', 'scripts/site.py')
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


def fixture(root):
    target = root / 'site'
    (target / 'x86_64').mkdir(parents=True)
    recipe = root / 'packages/aur/example'
    recipe.mkdir(parents=True)
    (recipe / '.SRCINFO').write_text('pkgbase = example\n\tpkgdesc = Base description\npkgname = example\n\tpkgdesc = <script>alert("test")</script>\npkgname = example-cli\n\tpkgdesc = Command line tools\npkgname = example-debug\n\tpkgdesc = Deliberately named debug package\n')
    outputs = []
    for i, name in enumerate(('example', 'example-cli', 'example-debug', 'example-cli-debug')):
        filename = f'{name}-1.2.3-1-any.pkg.tar.zst'
        (target / 'x86_64' / filename).write_bytes(b'a' * (i + 1) * 1024)
        (target / 'x86_64' / (filename + '.sig')).write_text('signature')
        outputs.append({'name': name, 'filename': filename, 'version': '1.2.3-1', 'architecture': 'any'})
    (target / 'repo-key.asc').write_text('public key')
    (target / 'x86_64/crafting.db').write_text('database')
    manifest = {'recipes': {'packages/aur/example': {'metadata': {'pkgname': ['example', 'example-cli', 'example-debug'], 'pkgdesc': ['Base description']}, 'outputs': outputs}}}
    (target / 'build-manifest.json').write_text(json.dumps(manifest))
    return target


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = fixture(self.root)
        cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        env = patch.dict(os.environ, REPO_NAME='crafting', REPO_FINGERPRINT='A' * 40,
                         PAGES_URL='https://example.invalid/project', GITHUB_REPOSITORY='owner/repo',
                         GITHUB_SHA='b' * 40, GITHUB_SERVER_URL='https://github.com')
        env.start()
        self.addCleanup(env.stop)

    def test_catalog_and_navigation(self):
        site.main()
        home = (self.target / 'index.html').read_text()
        catalog = (self.target / 'packages.html').read_text()
        self.assertIn('href="packages.html"', home)
        self.assertIn('Server = https://example.invalid/project/$arch', home)
        self.assertIn('href="index.html"', catalog)
        self.assertIn('https://github.com/owner/repo/blob/' + 'b' * 40 + '/packages/aur/example/PKGBUILD', catalog)
        self.assertIn('Command line tools', catalog)
        self.assertIn('&lt;script&gt;', catalog)
        self.assertNotIn('<script>alert', catalog)
        self.assertEqual(catalog.count('data-debug="true"'), 1)
        self.assertEqual(catalog.count('>PKGBUILD</a>'), 4)
        self.assertIn('4.0 KiB', catalog)
        self.assertIn('x86_64/crafting.db', catalog)
        self.assertIn('example-cli-1.2.3-1-any.pkg.tar.zst.sig', catalog)
        for file in ('site.css', 'site.js'):
            self.assertTrue((self.target / 'assets' / file).exists())

    def test_source_unavailable_and_empty(self):
        with patch.dict(os.environ, GITHUB_SHA=''):
            site.main()
        self.assertIn('Source unavailable', (self.target / 'packages.html').read_text())
        (self.target / 'build-manifest.json').write_text('{"recipes": {}}')
        site.main()
        for page in ('index.html', 'packages.html'):
            self.assertIn('No packages yet', (self.target / page).read_text())

    def test_epoch_download_and_signature_links_use_literal_colons(self):
        path = self.target / 'build-manifest.json'
        manifest = json.loads(path.read_text())
        output = manifest['recipes']['packages/aur/example']['outputs'][0]
        old = output['filename']
        name = 'example-1:1.2.3-1-any.pkg.tar.zst'
        for suffix in ('', '.sig'):
            (self.target / 'x86_64' / (old + suffix)).rename(self.target / 'x86_64' / (name + suffix))
        output.update(filename=name, version='1:1.2.3-1')
        path.write_text(json.dumps(manifest))
        site.main()
        catalog = (self.target / 'packages.html').read_text()
        self.assertIn(f'href="x86_64/{name}"', catalog)
        self.assertIn(f'href="x86_64/{name}.sig"', catalog)
        self.assertNotIn('%3A', catalog)
