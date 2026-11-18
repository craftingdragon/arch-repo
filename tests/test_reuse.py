import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('reuse', 'scripts/reuse.py')
reuse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reuse)


class ReuseTests(unittest.TestCase):
    def test_files_modes_and_implementation_invalidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            recipe = Path(tmp)
            file = recipe / 'PKGBUILD'
            file.write_text('one')
            first = reuse.fingerprint(recipe, 'implementation1')
            self.assertEqual(first, reuse.fingerprint(recipe, 'implementation1'))
            self.assertNotEqual(first, reuse.fingerprint(recipe, 'implementation2'))
            file.chmod(0o755)
            self.assertNotEqual(first, reuse.fingerprint(recipe, 'implementation1'))
            file.chmod(0o644)
            (recipe / 'fix.patch').write_text('patch')
            self.assertNotEqual(first, reuse.fingerprint(recipe, 'implementation1'))

    def test_graph_split_provides_and_transitive_invalidation(self):
        records = {
            'a': {'metadata': reuse.metadata('pkgname = a\npkgname = a-cli\nprovides = virtual=1')},
            'b': {'metadata': reuse.metadata('pkgname = b\nmakedepends_x86_64 = virtual>=1')},
            'c': {'metadata': reuse.metadata('pkgname = c\ncheckdepends = b')},
            'd': {'metadata': reuse.metadata('pkgname = d')},
        }
        for base, edges in reuse.graph(records).items():
            records[base]['dependencies'] = edges
        self.assertEqual(records['b']['dependencies'], ['a'])
        self.assertEqual(set(reuse.invalidate(records, records, {'a': 'changed'})), {'a', 'b', 'c'})
        old = dict(records)
        del records['a']
        self.assertEqual(set(reuse.invalidate(records, old, {})), {'b', 'c'})

    def test_missing_and_corrupt_split_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / 'remote/x86_64'
            remote.mkdir(parents=True)
            stage = root / 'stage'
            stage.mkdir()
            file = remote / 'a.pkg.tar.zst'
            file.write_bytes(b'archive')
            record = {'outputs': [{'filename': file.name, 'sha256': reuse.sha(file)}]}
            self.assertTrue(reuse.download(record, (root / 'remote').as_uri(), stage))
            record['outputs'].append({'filename': 'missing.pkg.tar.zst', 'sha256': '0' * 64})
            self.assertFalse(reuse.download(record, (root / 'remote').as_uri(), stage))
            self.assertFalse(list(stage.iterdir()))
            record['outputs'] = [{'filename': file.name, 'sha256': '0' * 64}]
            self.assertFalse(reuse.download(record, (root / 'remote').as_uri(), stage))

    def test_absent_snapshot_falls_back(self):
        self.assertEqual(reuse.restore('', Path('unused'), Path('unused')), {})
        with patch.object(reuse, 'fetch', side_effect=OSError('unreachable')):
            self.assertEqual(reuse.restore('https://example.invalid', Path('unused'), Path('unused')), {})

    def test_unchanged_run_does_not_compile_and_force_does(self):
        import os
        import hashlib
        import sys
        sys.path.insert(0, str(Path('scripts').resolve()))
        import build
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe = root / 'packages/local/example'
            recipe.mkdir(parents=True)
            (recipe / 'PKGBUILD').write_text('pkgname=example')
            out = root / 'out'
            implementation = hashlib.sha256(Path(build.__file__).read_bytes() + Path(build.reuse.__file__).read_bytes()).hexdigest()
            info = 'pkgname = example\npkgver = 1\npkgrel = 1\narch = any\n'
            record = {'fingerprint': reuse.fingerprint(recipe, implementation), 'metadata': reuse.metadata(info),
                      'dependencies': [], 'outputs': [{'filename': 'example.pkg.tar.zst', 'sha256': '0' * 64}]}
            def metadata_container(args, **kwargs):
                mount = next(arg for arg in args if arg.endswith(':/meta'))
                (Path(mount[:-6]) / '0').write_text(info)
            def restore_archive(record, url, staging):
                (staging / 'example.pkg.tar.zst').write_bytes(b'reused')
                return True
            def compile_package(*args):
                (out / 'example.pkg.tar.zst').write_bytes(b'new')
                (out / 'example.pkg.tar.zst.meta').write_text('pkgname = example\npkgver = 1-1\narch = any\n')
            with patch.object(build, 'ROOT', root), patch.object(build, 'OUT', out), \
                 patch.object(build.subprocess, 'run', side_effect=metadata_container), \
                 patch.object(build.reuse, 'restore', return_value={'packages/local/example': record}), \
                 patch.object(build.reuse, 'download', side_effect=restore_archive), \
                 patch.object(build, 'docker', side_effect=compile_package) as compile_call, \
                 patch.dict(os.environ, {'FORCE_REBUILD': 'false', 'PAGES_URL': 'https://example.invalid'}):
                build.main()
                compile_call.assert_not_called()
                self.assertEqual((out / 'example.pkg.tar.zst').read_bytes(), b'reused')
                for file in out.iterdir():
                    file.unlink()
                os.environ['FORCE_REBUILD'] = 'true'
                build.main()
                compile_call.assert_called_once()
                self.assertEqual((out / 'example.pkg.tar.zst').read_bytes(), b'new')
