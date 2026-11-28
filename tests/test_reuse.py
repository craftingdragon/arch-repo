import importlib.util
import hashlib
import io
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
            with self.assertRaisesRegex(RuntimeError, 'refusing automatic full rebuild'):
                reuse.restore('https://example.invalid', Path('unused'), Path('unused'), strict=True)

    def test_epoch_download_avoids_pages_html_fallback(self):
        archives = {
            'insomnia-bin-1:13.4.0-1-x86_64.pkg.tar.zst': b'package archive',
            'insomnia-bin-debug-1:13.4.0-1-x86_64.pkg.tar.zst': b'debug archive',
        }
        prefix = 'https://example.invalid/x86_64/'

        def serve(request, **kwargs):
            # Pages serves its HTML fallback for encoded epoch paths with 200,
            # while literal-colon paths redirect to the actual package archives.
            name = request.full_url.removeprefix(prefix)
            return io.BytesIO(archives.get(name, b'<!doctype html><title>Overview</title>'))

        record = {'outputs': [{'filename': name, 'sha256': hashlib.sha256(data).hexdigest()}
                              for name, data in archives.items()]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(reuse, 'urlopen', side_effect=serve):
            stage = Path(tmp)
            self.assertTrue(reuse.download(record, 'https://example.invalid', stage))
            for name, data in archives.items():
                self.assertEqual((stage / name).read_bytes(), data)

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
            implementation = Path(build.__file__).with_name('build-cache-version.txt').read_text().strip()
            info = 'pkgname = example\npkgver = 1\npkgrel = 1\narch = any\n'
            record = {'fingerprint': reuse.fingerprint(recipe, implementation), 'metadata': reuse.metadata(info),
                      'dependencies': [], 'outputs': [{'filename': 'example.pkg.tar.zst', 'sha256': '0' * 64}]}
            def metadata_container(args, **kwargs):
                mount = next(arg for arg in args if arg.endswith(':/meta'))
                (Path(mount[:-6]) / '0').write_text(info)
            def restore_archive(record, url, staging):
                (staging / 'example.pkg.tar.zst').write_bytes(b'reused')
                return True
            def compile_package(*args, result, **kwargs):
                (result / 'example.pkg.tar.zst').write_bytes(b'new')
                (result / 'example.pkg.tar.zst.meta').write_text('pkgname = example\npkgver = 1-1\narch = any\n')
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

    def test_build_batches_independent_recipes_before_consumer(self):
        import sys
        with patch('sys.path', [str(Path('scripts').resolve()), *sys.path]):
            import build

        records = {
            'consumer': {'dependencies': ['first', 'second']},
            'first': {'dependencies': []},
            'second': {'dependencies': []},
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(build.os, 'sched_getaffinity', return_value={0, 1, 2, 3}), \
             patch.object(build.os, 'sysconf', side_effect=lambda key: {
                 'SC_PHYS_PAGES': 4_194_304, 'SC_PAGE_SIZE': 4096,
             }[key]), \
             patch.object(build, 'build_one', return_value=True) as compile_call, \
             patch.object(build, 'merge_outputs') as merge:
            build.build_pending(records, records, Path(tmp))
        calls = {call.args[0]: call.args for call in compile_call.call_args_list}
        self.assertEqual([call.args[0] for call in merge.call_args_list],
                         ['first', 'second', 'consumer'])
        self.assertNotEqual(calls['first'][2], calls['second'][2])
        self.assertEqual(set(calls['first'][3]) | set(calls['second'][3]), {0, 1, 2, 3})
        self.assertEqual(calls['consumer'][3:], ((), None))
