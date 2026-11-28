#!/usr/bin/env python3
"""Build committed recipes in dependency order, using disposable Arch containers."""
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
import shutil
import shlex
import tempfile
import time
import reuse
import vcs
from pathlib import Path
import subprocess

ROOT = Path.cwd()
OUT = ROOT / 'out'


def docker(recipe, script, *, result, cpus, memory):
    cache_root = os.environ.get('LADYBIRD_CCACHE_DIR')
    cache = cache_root and recipe == ROOT / 'packages/local/ladybird'
    limits = []
    affinity = ['taskset', '--cpu-list', ','.join(map(str, cpus))] if cpus else []
    if memory:
        limits += ['--memory', str(memory), '--memory-swap', str(memory)]
    if cache:
        cache_path = Path(cache_root).resolve()
        cache_path.mkdir(parents=True, exist_ok=True)
        limits += ['-v', f'{cache_path}:/ccache', '--env', f'BUILDER_UID={os.getuid()}']
    subprocess.run(
        ['docker', 'run', '--rm', *limits, '-v', f'{recipe}:/recipe:ro',
         '-v', f'{OUT}:/deps:ro', '-v', f'{result}:/result',
         '-v', f'{Path(vcs.__file__).resolve()}:/vcs.py:ro',
         os.environ.get('ARCH_BUILDER_IMAGE', 'archlinux:base'), *affinity, 'bash', '-euo', 'pipefail',
         '-c', script], check=True,
    )


def bootstrap():
    install = '' if os.environ.get('ARCH_BUILDER_IMAGE') else (
        'pacman -Syu --noconfirm --needed base-devel git python gnupg\n'
    )
    return 'SECONDS=0\n' + install + """
if [[ -n ${BUILDER_UID:-} && $BUILDER_UID != 0 ]]; then
  useradd -m -u "$BUILDER_UID" builder
else
  useradd -m builder
fi
"""


def report(message):
    print(message)
    if summary_path := os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(summary_path, 'a') as output:
            print(message, file=output)


RECIPE_SETUP = """
cp -a /recipe /home/builder/pkg
chown -R builder:builder /home/builder/pkg
cd /home/builder/pkg
"""


def main():
    OUT.mkdir(exist_ok=True)
    if any(OUT.iterdir()):
        raise SystemExit('out/ must be empty before building')
    recipes = sorted(ROOT.glob('packages/*/*/PKGBUILD'))
    records = {}
    started = time.monotonic()
    # Explicit build ABI: transport/publication fixes must not invalidate binaries.
    # Initially seeded with the implementation hash of the successful R2 build.
    implementation = Path(__file__).with_name('build-cache-version.txt').read_text().strip()
    with tempfile.TemporaryDirectory() as tmp:
        meta = Path(tmp)
        if recipes:
            subprocess.run(['docker', 'run', '--rm', '-v', f'{ROOT}:/input:ro',
                            '-v', f'{meta}:/meta', os.environ.get('ARCH_BUILDER_IMAGE', 'archlinux:base'),
                            'bash', '-euc', bootstrap() + '''
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
    report(f'Recipe metadata and dependency graph: {time.monotonic() - started:.1f}s')
    started = time.monotonic()
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
        reusable = [base for base in records if base not in dirty]
        with ThreadPoolExecutor(max_workers=4) as pool:
            restored = pool.map(lambda base: reuse.download(previous[base], url, staging), reusable)
            for base, valid in zip(reusable, restored, strict=True):
                if not valid:
                    dirty[base] = 'missing or invalid output archive'
        dirty = reuse.invalidate(records, previous, dirty)
        for base, record in records.items():
            if base not in dirty:
                record['outputs'] = previous[base]['outputs']
                for output in record['outputs']:
                    shutil.move(staging / output['filename'], OUT / output['filename'])
            print(f'{base}: ' + ('rebuild: ' + dirty[base] if base in dirty else 'reuse verified published outputs'))
    report(f'Published snapshot verification and archive restore: {time.monotonic() - started:.1f}s')
    with tempfile.TemporaryDirectory(dir=OUT.parent) as tmp:
        build_pending(records, dirty, Path(tmp))
    manifest = {'schema': 1, 'architecture': 'x86_64', 'recipes': records}
    (OUT / 'build-manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    report(f"Rebuilt: {len(dirty)}; reused: {len(records) - len(dirty)} recipes")


BUILD_SCRIPT = r'''
printf '\nMAKEFLAGS="-j%s"\n' "$(nproc)" >> /etc/makepkg.conf
runuser -u builder -- makepkg --printsrcinfo > /tmp/srcinfo
python - <<'PY'
from pathlib import Path
import re

entries = [line.strip().partition(' = ') for line in Path('/tmp/srcinfo').read_text().splitlines()]
outputs = {value for key, _, value in entries if key == 'pkgname'}
runtime_fields = {'depends', 'depends_x86_64'}
fields = runtime_fields | {'makedepends', 'checkdepends', 'makedepends_x86_64', 'checkdepends_x86_64'}
deps = []
package_section = False
for key, sep, value in entries:
    if key == 'pkgname':
        package_section = True
    if not sep or key not in fields:
        continue
    # makepkg builds every split output together. A sibling runtime dependency
    # is resolved when the resulting packages are installed.
    if package_section and key in runtime_fields and re.split(r'[<>=]', value, maxsplit=1)[0] in outputs:
        continue
    deps.append(value)
Path('/tmp/deps').write_text('\n'.join(dict.fromkeys(deps)))
PY
# Supply built packages through a temporary local repository, letting pacman
# resolve split packages, provides, and version constraints.
shopt -s nullglob
built=(/deps/*.pkg.tar.zst)
if ((${#built[@]})); then
  mkdir /tmp/repo
  ln -s "${built[@]}" /tmp/repo/
  repo-add /tmp/repo/build.db.tar.gz /tmp/repo/*.pkg.tar.zst
  build_repo=$'[build]\nSigLevel = Never\nServer = file:///tmp/repo'
  printf '\n%s\n' "$build_repo" >> /etc/pacman.conf
  # Keep official databases aligned with the prepared image's system upgrade.
  printf '[options]\nArchitecture = auto\n\n%s\n' "$build_repo" > /tmp/build-pacman.conf
  pacman -Sy --noconfirm --config /tmp/build-pacman.conf
fi
mapfile -t deps < /tmp/deps
if ((${#deps[@]})); then pacman -S --needed --noconfirm -- "${deps[@]}" || exit 42; fi
# Import declared source-verification keys, without disabling checks.
while read -r key; do
  keyfile="keys/pgp/$key.asc"
  if [[ -f "$keyfile" ]]; then
    runuser -u builder -- gpg --import -- "$keyfile"
  else
    runuser -u builder -- gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys "$key"
  fi
done < <(sed -n 's/^[[:space:]]*validpgpkeys = //p' /tmp/srcinfo)
printf 'container setup and dependencies: %ss\n' "$SECONDS" > /result/timings
build_env=(PKGEXT=.pkg.tar.zst CMAKE_BUILD_PARALLEL_LEVEL="$(nproc)" CARGO_BUILD_JOBS="$(nproc)")
if [[ -d /ccache ]]; then
  cache_owner=$(stat -c '%u:%g' /ccache)
  if [[ "$cache_owner" != "$(id -u builder):$(id -g builder)" ]]; then
    # Restore host ownership after building, including rootless UID mappings.
    trap 'chown -R "$cache_owner" /ccache' EXIT
    chown -R builder:builder /ccache
  fi
  build_env+=(CCACHE_DIR=/ccache CCACHE_MAXSIZE=512M
              CMAKE_C_COMPILER_LAUNCHER=ccache CMAKE_CXX_COMPILER_LAUNCHER=ccache)
  runuser -u builder -- env "${build_env[@]}" ccache --zero-stats
fi
runuser -u builder -- env "${build_env[@]}" makepkg --cleanbuild --noconfirm 2>&1 |
  awk -v start="$(date +%s)" '
    /Starting build\(\)/ {build = systime()}
    /Starting check\(\)/ {check = systime()}
    /Entering fakeroot/ && !package {package = systime()}
    {print; fflush()}
    END {
      printf "makepkg: %ds", systime() - start >> "/result/timings"
      if (build && (check || package))
        printf ", build: %ds", (check ? check : package) - build >> "/result/timings"
      if (check && package)
        printf ", tests: %ds", package - check >> "/result/timings"
      printf "\n" >> "/result/timings"
    }
  '
if [[ -r /sys/fs/cgroup/memory.peak ]]; then
  printf 'container peak memory: %s bytes\n' "$(</sys/fs/cgroup/memory.peak)" >> /result/timings
fi
if [[ -d /ccache ]]; then
  runuser -u builder -- env "${build_env[@]}" ccache --show-stats >> /result/timings
fi
for package in *.pkg.tar.zst; do
  cp -- "$package" /result/
  bsdtar -xOf "$package" .PKGINFO > /result/"$package".meta
done
'''


def build_one(base, record, result, cpus, memory):
    result.mkdir(exist_ok=True)
    pin = ''
    if 'vcs_sources' in record:
        pin = ('python /vcs.py ' + shlex.quote(json.dumps(record['metadata'])) + ' '
               + shlex.quote(json.dumps(record['vcs_sources'])) + ' >> PKGBUILD\n')
    started = time.monotonic()
    print(f'Building {base}')
    try:
        docker(ROOT / base, bootstrap() + RECIPE_SETUP + pin + BUILD_SCRIPT,
               result=result, cpus=cpus, memory=memory)
    except subprocess.CalledProcessError as error:
        if error.returncode == 42:
            report(f'{base}: waiting for dependencies after {time.monotonic() - started:.1f}s')
            return False
        raise
    details = result / 'timings'
    if details.exists():
        report(f'{base}: {time.monotonic() - started:.1f}s total\n\n'
               + '\n'.join(f'- {line}' for line in details.read_text().splitlines() if line.strip()) + '\n')
    return True


def merge_outputs(base, record, result):
    archives = sorted(result.glob('*.pkg.tar.zst'))
    if not archives:
        raise SystemExit(f'No package outputs: {base}')
    for archive in archives:
        target = OUT / archive.name
        if target.exists():
            raise SystemExit(f'Duplicate output package: {archive.name}')
        info = reuse.metadata(archive.with_name(archive.name + '.meta').read_text())
        record['outputs'].append({'filename': archive.name, 'sha256': reuse.sha(archive),
                                  'name': info['pkgname'][0], 'version': info['pkgver'][0],
                                  'architecture': info['arch'][0]})
        shutil.move(archive, target)


def build_pending(records, dirty, staging):
    pending = {base: staging / str(i) for i, base in enumerate(records) if base in dirty}
    if not pending:
        return
    cores = sorted(os.sched_getaffinity(0))
    gib = 1024 ** 3
    total_memory = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    parallel = len(cores) >= 4 and total_memory >= 12 * gib

    def attempt(bases):
        if len(bases) == 1:
            base, = bases
            results = [build_one(base, records[base], pending[base], (), None)]
        else:
            slots = [cores[::2], cores[1::2]]
            memory = (total_memory - 2 * gib) // len(bases)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda base, cpus: build_one(base, records[base], pending[base], cpus, memory),
                    bases, slots,
                ))
        for base, success in zip(bases, results, strict=True):
            if success:
                merge_outputs(base, records[base], pending.pop(base))
        return any(results)

    while pending:
        ready = [base for base in pending if not pending.keys() & set(records[base]['dependencies'])]
        batch = []
        if ready:
            batch = ready[:2] if parallel and 'packages/local/ladybird' not in ready[:2] else ready[:1]
            if attempt(batch):
                continue
        # The graph overapproximates possible providers. Let pacman resolve cases
        # that static readiness could not handle, including apparent cycles.
        if not any(attempt([base]) for base in tuple(pending) if base not in batch):
            raise SystemExit('Unresolved dependencies or dependency cycle: ' + ', '.join(pending))


if __name__ == '__main__':
    main()
