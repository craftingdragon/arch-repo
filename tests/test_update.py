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
