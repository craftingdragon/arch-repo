#!/usr/bin/env python3
"""Build committed recipes in dependency order, using disposable Arch containers."""
import json
import hashlib
import os
import shutil
import shlex
import tempfile
import reuse
import vcs
from pathlib import Path
import subprocess

ROOT = Path.cwd()
OUT = ROOT / 'out'


def docker(recipe, script, extra=()):
    jobs = int(subprocess.check_output(['nproc'], text=True).strip())
    if jobs < 1:
        raise ValueError('Runner nproc must be positive')
    subprocess.run(['docker', 'run', '--rm', '--env', f'RUNNER_NPROC={jobs}', '-v', f'{recipe}:/recipe:ro',
                    '-v', f'{OUT}:/out', '-v', f'{Path(vcs.__file__).resolve()}:/vcs.py:ro', 'archlinux:base', 'bash', '-euc', script, '--', *extra], check=True)


BOOT = '''
pacman -Syu --noconfirm --needed base-devel git python gnupg
useradd -m builder
cp -a /recipe /home/builder/pkg
chown -R builder:builder /home/builder/pkg
cd /home/builder/pkg
'''


def main():
    OUT.mkdir(exist_ok=True)
    if any(OUT.iterdir()):
        raise SystemExit('out/ must be empty before building')
    recipes = sorted(ROOT.glob('packages/*/*/PKGBUILD'))
    records = {}
    # Explicit build ABI: transport/publication fixes must not invalidate binaries.
    # Initially seeded with the implementation hash of the successful R2 build.
    implementation = Path(__file__).with_name('build-cache-version.txt').read_text().strip()
    with tempfile.TemporaryDirectory() as tmp:
        meta = Path(tmp)
        if recipes:
            subprocess.run(['docker', 'run', '--rm', '-v', f'{ROOT}:/input:ro',
                            '-v', f'{meta}:/meta', 'archlinux:base', 'bash', '-euc',
                            BOOT.split('cp -a')[0] + '''
i=0
for recipe in "$@"; do
  cp -a "$recipe" /home/builder/pkg-$i
  chown -R builder:builder /home/builder/pkg-$i
  (cd /home/builder/pkg-$i; runuser -u builder -- makepkg --printsrcinfo) > /meta/$i
  i=$((i + 1))
done
''', '--', *['/input/' + str(p.parent.relative_to(ROOT)) for p in recipes]], check=True)
        for i, recipe in enumerate(recipes):
            base = str(recipe.parent.relative_to(ROOT))
            records[base] = {'fingerprint': reuse.fingerprint(recipe.parent, implementation),
                             'metadata': reuse.metadata((meta / str(i)).read_text()), 'outputs': []}
    for base, record in records.items():
        path = Path(base)
        if path.parent == Path('packages/aur') and path.name.endswith('-git'):
            state = json.loads((ROOT / '.aur-locks' / (path.name + '.json')).read_text())
            locked = state.get('vcs_sources', {})
            vcs.validate(record['metadata'], locked)
            record['vcs_sources'] = locked
            record['fingerprint'] = hashlib.sha256(json.dumps([record['fingerprint'], locked], sort_keys=True).encode()).hexdigest()
    for base, edges in reuse.graph(records).items():
        records[base]['dependencies'] = edges
    url = os.environ.get('PAGES_URL', '')
    forced = os.environ.get('FORCE_REBUILD', 'false').lower() == 'true'
    previous = {} if forced else reuse.restore(url, ROOT / 'signing-fingerprint.txt', OUT, strict=True)
    dirty = {base: 'forced rebuild' if forced else 'recipe or build inputs changed'
             for base, record in records.items()
             if forced or previous.get(base, {}).get('fingerprint') != record['fingerprint']
             or previous.get(base, {}).get('dependencies') != record['dependencies']
             or previous.get(base, {}).get('metadata') != record['metadata']}
    dirty = reuse.invalidate(records, previous, dirty)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        for base in records:
            if base not in dirty and not reuse.download(previous[base], url, staging):
                dirty[base] = 'missing or invalid output archive'
        dirty = reuse.invalidate(records, previous, dirty)
        for base, record in records.items():
            if base not in dirty:
                record['outputs'] = previous[base]['outputs']
                for output in record['outputs']:
                    shutil.move(staging / output['filename'], OUT / output['filename'])
            print(f'{base}: ' + ('rebuild: ' + dirty[base] if base in dirty else 'reuse verified published outputs'))
    pending = [p for p in recipes if str(p.parent.relative_to(ROOT)) in dirty]
    # Install dependencies through pacman from official repos plus already built
    # packages. Retry packages whose dependencies are not yet available.
    while pending:
        progressed = False
        for recipe in pending[:]:
            before = set(OUT.glob('*.pkg.tar.zst'))
            try:
                record = records[str(recipe.parent.relative_to(ROOT))]
                pin = ''
                if 'vcs_sources' in record:
                    pin = ('python /vcs.py ' + shlex.quote(json.dumps(record['metadata'])) + ' '
                           + shlex.quote(json.dumps(record['vcs_sources'])) + ' >> PKGBUILD\n')
                docker(recipe.parent, BOOT + pin + r'''
printf '\nMAKEFLAGS="-j%s"\n' "$RUNNER_NPROC" >> /etc/makepkg.conf
runuser -u builder -- makepkg --printsrcinfo > /tmp/srcinfo
python - <<'PY'
from pathlib import Path
fields = {'depends', 'makedepends', 'checkdepends', 'depends_x86_64', 'makedepends_x86_64', 'checkdepends_x86_64'}
deps = []
for line in Path('/tmp/srcinfo').read_text().splitlines():
    key, sep, value = line.strip().partition(' = ')
    if sep and key in fields:
        deps.append(value)
Path('/tmp/deps').write_text('\n'.join(dict.fromkeys(deps)))
PY
# Supply built packages through a temporary local repository, letting pacman
# resolve split packages, provides, and version constraints.
shopt -s nullglob
built=(/out/*.pkg.tar.zst)
if ((${#built[@]})); then
  mkdir /tmp/repo
  cp "${built[@]}" /tmp/repo/
  repo-add /tmp/repo/build.db.tar.gz /tmp/repo/*.pkg.tar.zst
  printf '\n[build]\nSigLevel = Never\nServer = file:///tmp/repo\n' >> /etc/pacman.conf
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
  bsdtar -xOf "$package" .PKGINFO > /out/"$package".meta
done
''')
            except subprocess.CalledProcessError as error:
                if error.returncode == 42:
                    continue
                raise
            record = records[str(recipe.parent.relative_to(ROOT))]
            for archive in sorted(set(OUT.glob('*.pkg.tar.zst')) - before):
                info_path = archive.with_name(archive.name + '.meta')
                info = reuse.metadata(info_path.read_text())
                record['outputs'].append({'filename': archive.name, 'sha256': reuse.sha(archive),
                                          'name': info['pkgname'][0], 'version': info['pkgver'][0],
                                          'architecture': info['arch'][0]})
                info_path.unlink()
            if not record['outputs']:
                raise SystemExit(f'No package outputs: {recipe.parent}')
            pending.remove(recipe)
            progressed = True
        if not progressed:
            raise SystemExit('Unresolved dependencies or dependency cycle: ' + ', '.join(str(p.parent) for p in pending))
    manifest = {'schema': 1, 'architecture': 'x86_64', 'recipes': records}
    (OUT / 'build-manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    summary = f"Rebuilt: {len(dirty)}; reused: {len(records) - len(dirty)} recipes"
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
            output.write(summary + '\n')


if __name__ == '__main__':
    main()
