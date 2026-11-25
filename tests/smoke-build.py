#!/usr/bin/env python3
"""Optional Docker integration check; all generated files stay in a temp directory."""
from pathlib import Path
import subprocess
import tempfile

script = Path('scripts/build.py').resolve()
with tempfile.TemporaryDirectory(prefix='arch-build-smoke-') as tmp:
    root = Path(tmp)
    recipe = root / 'packages/local/smoke'
    recipe.mkdir(parents=True)
    (recipe / 'PKGBUILD').write_text('''pkgbase=crafting-smoke
pkgname=(crafting-smoke crafting-smoke-license)
pkgver=1.0
pkgrel=1
pkgdesc='CI smoke fixture'
arch=('x86_64')
license=('MIT')
package_crafting-smoke() {
  depends_x86_64=('crafting-smoke-license>=1.0' 'bash')
  install -d "$pkgdir/usr/share/crafting-smoke"
  printf 'smoke\\n' > "$pkgdir/usr/share/crafting-smoke/test"
}
package_crafting-smoke-license() {
  arch=('any')
  install -d "$pkgdir/usr/share/licenses/crafting-smoke"
  printf 'smoke license\\n' > "$pkgdir/usr/share/licenses/crafting-smoke/LICENSE"
}
''')
    consumer = root / 'packages/local/consumer'
    consumer.mkdir()
    (consumer / 'PKGBUILD').write_text('''pkgname=crafting-smoke-consumer
pkgver=1.0
pkgrel=1
pkgdesc='CI smoke consumer'
arch=('any')
license=('MIT')
makedepends=('crafting-smoke')
package() {
  test -s /usr/share/crafting-smoke/test
  install -Dm644 /usr/share/licenses/crafting-smoke/LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
''')
    subprocess.run(['python', str(script)], cwd=root, check=True)
    assert {path.name for path in (root / 'out').glob('*.pkg.tar.zst')} == {
        'crafting-smoke-1.0-1-x86_64.pkg.tar.zst',
        'crafting-smoke-license-1.0-1-any.pkg.tar.zst',
        'crafting-smoke-consumer-1.0-1-any.pkg.tar.zst',
    }
    print('Arch container build smoke test passed')
