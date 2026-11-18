#!/usr/bin/env python3
"""Build a local Git fixture after upstream advances; verify the reviewed pin wins."""
import json
from pathlib import Path
import subprocess
import tempfile

script = Path('scripts/build.py').resolve()
with tempfile.TemporaryDirectory(prefix='crafting-vcs-smoke-') as tmp:
    root = Path(tmp)
    recipe = root / 'packages/aur/smoke-git'
    upstream = recipe / 'upstream'
    upstream.mkdir(parents=True)
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=upstream, text=True).strip()
    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'commit.gpgsign', 'false')
    (upstream / 'value').write_text('reviewed content\n')
    git('add', '.')
    git('commit', '-qm', 'Reviewed')
    reviewed = git('rev-parse', 'HEAD')
    (upstream / 'value').write_text('unreviewed newer content\n')
    git('commit', '-qam', 'Upstream advanced')
    source = 'checkout::git+file:///recipe/upstream#branch=main'
    (recipe / 'PKGBUILD').write_text('''pkgname=smoke-git
pkgver=r1
pkgrel=1
pkgdesc='Source lock smoke test'
arch=('any')
license=('MIT')
makedepends=('git')
source=(''' + repr(source) + ''')
sha256sums=('SKIP')
pkgver() {
  cd "$srcdir/checkout"
  printf 'r%s' "$(git rev-list --count HEAD)"
}
package() {
  install -Dm644 "$srcdir/checkout/value" "$pkgdir/usr/share/vcs-smoke/value"
}
''')
    (root / '.aur-locks').mkdir()
    (root / '.aur-locks/smoke-git.json').write_text(json.dumps({'vcs_sources': {source: reviewed}}))
    subprocess.run(['python', str(script)], cwd=root, check=True)
    archives = list((root / 'out').glob('*.pkg.tar.zst'))
    assert len(archives) == 1
    content = subprocess.check_output(['bsdtar', '-xOf', str(archives[0]), 'usr/share/vcs-smoke/value'], text=True)
    assert content == 'reviewed content\n', content
    manifest = json.loads((root / 'out/build-manifest.json').read_text())
    assert manifest['recipes']['packages/aur/smoke-git']['vcs_sources'][source] == reviewed
    print('Pinned Git build verified: newer unreviewed upstream commit was not used')
