#!/usr/bin/env python3
"""Optional Docker integration check; all generated files stay in a temp directory."""
import os
from pathlib import Path
import subprocess
import tempfile

script = Path('scripts/build.py').resolve()
with tempfile.TemporaryDirectory(prefix='arch-build-smoke-') as tmp:
    root = Path(tmp)
    recipe = root / 'packages/local/smoke'
    recipe.mkdir(parents=True)
    (recipe / 'PKGBUILD').write_text('''pkgname=crafting-smoke
pkgver=1.0
pkgrel=1
pkgdesc='CI smoke fixture'
arch=('any')
license=('MIT')
package() {
  install -d "$pkgdir/usr/share/crafting-smoke"
  printf 'smoke\\n' > "$pkgdir/usr/share/crafting-smoke/test"
}
''')
    subprocess.run(['python', str(script)], cwd=root, check=True)
    assert len(list((root / 'out').glob('*.pkg.tar.zst'))) == 1
    print('Arch container build smoke test passed')
