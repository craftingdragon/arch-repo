#!/usr/bin/env python3
"""Build committed recipes in dependency order, using disposable Arch containers."""
import json
from pathlib import Path
import subprocess

ROOT = Path.cwd()
OUT = ROOT / 'out'


def docker(recipe, script, extra=()):
    subprocess.run(['docker', 'run', '--rm', '-v', f'{recipe}:/recipe:ro',
                    '-v', f'{OUT}:/out', 'archlinux:base', 'bash', '-euc', script, '--', *extra], check=True)


BOOT = '''
pacman -Syu --noconfirm --needed base-devel git python gnupg
useradd -m builder
cp -a /recipe /home/builder/pkg
chown -R builder:builder /home/builder/pkg
cd /home/builder/pkg
'''


def main():
    OUT.mkdir(exist_ok=True)
    pending = sorted(ROOT.glob('packages/*/*/PKGBUILD'))
    # Install dependencies through pacman from official repos plus already built
    # packages. Retry packages whose dependencies are not yet available.
    while pending:
        progressed = False
        for recipe in pending[:]:
            try:
                docker(recipe.parent, BOOT + '''
runuser -u builder -- makepkg --printsrcinfo > /tmp/srcinfo
python - <<'PY'
from pathlib import Path
fields = {'depends', 'makedepends', 'checkdepends', 'depends_x86_64', 'makedepends_x86_64', 'checkdepends_x86_64'}
deps = []
for line in Path('/tmp/srcinfo').read_text().splitlines():
    key, sep, value = line.strip().partition(' = ')
    if sep and key in fields:
        deps.append(value)
Path('/tmp/deps').write_text('\\n'.join(dict.fromkeys(deps)))
PY
# Supply built packages through a temporary local repository, letting pacman
# resolve split packages, provides, and version constraints.
shopt -s nullglob
built=(/out/*.pkg.tar.zst)
if ((${#built[@]})); then
  mkdir /tmp/repo
  cp "${built[@]}" /tmp/repo/
  repo-add /tmp/repo/build.db.tar.gz /tmp/repo/*.pkg.tar.zst
  printf '\\n[build]\\nSigLevel = Never\\nServer = file:///tmp/repo\\n' >> /etc/pacman.conf
  pacman -Sy --noconfirm
fi
mapfile -t deps < /tmp/deps
if ((${#deps[@]})); then pacman -S --needed --noconfirm -- "${deps[@]}" || exit 42; fi
# Import declared source-verification keys, without disabling checks.
while read -r key; do
  runuser -u builder -- gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys "$key"
done < <(sed -n 's/^[[:space:]]*validpgpkeys = //p' /tmp/srcinfo)
runuser -u builder -- env PKGEXT=.pkg.tar.zst makepkg --cleanbuild --noconfirm
for package in *.pkg.tar.zst; do
  [[ ! -e /out/"$package" ]] || { echo "Duplicate output package: $package"; exit 1; }
  cp -- "$package" /out/
done
''')
            except subprocess.CalledProcessError as error:
                if error.returncode == 42:
                    continue
                raise
            pending.remove(recipe)
            progressed = True
        if not progressed:
            raise SystemExit('Unresolved dependencies or dependency cycle: ' + ', '.join(str(p.parent) for p in pending))
    (OUT / 'build.json').write_text(json.dumps({'recipes': len(list(ROOT.glob('packages/*/*/PKGBUILD')))}))


if __name__ == '__main__':
    main()
