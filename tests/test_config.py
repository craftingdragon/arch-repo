"""Validate trust configuration before a build can reach master."""
from pathlib import Path
import subprocess
import tempfile
import unittest


class FingerprintConfigTests(unittest.TestCase):
    def test_requires_canonical_fingerprint(self):
        check = Path('scripts/check.py').resolve()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'aur.txt').write_text('')
            for fingerprint, valid in [('A' * 40, True), ('B' * 64, True),
                                       ('AAAA ' * 10, False), ('a' * 40, False),
                                       ('INVALID', False)]:
                (root / 'signing-fingerprint.txt').write_text(fingerprint + '\n')
                result = subprocess.run(['python', str(check)], cwd=root, text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, valid, result.stdout + result.stderr)
                if not valid:
                    self.assertIn('without spaces', result.stderr)
