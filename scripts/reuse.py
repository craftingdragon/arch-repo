"""Verified published snapshots and dependency-aware rebuild planning."""
import hashlib
from http.client import HTTPException
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.request import Request, urlopen
from urllib.parse import quote


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def fingerprint(recipe, implementation):
    entries = []
    for p in sorted(recipe.rglob('*')):
        if p.is_symlink():
            try:
                p.resolve().relative_to(recipe.resolve())
                try:
                    p.stat()
                except FileNotFoundError:
                    pass  # An internal dangling link is still a valid Git entry.
            except (ValueError, RuntimeError, OSError) as error:
                raise ValueError(f'Recipe symlink escapes recipe or loops: {p}') from error
            entries.append([str(p.relative_to(recipe)), 'symlink', str(p.readlink())])
        elif p.is_file():
            entries.append([str(p.relative_to(recipe)), bool(p.stat().st_mode & 0o111), sha(p)])
    return hashlib.sha256(json.dumps(['x86_64', implementation, entries]).encode()).hexdigest()


def metadata(text):
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(' = ')
        if sep:
            fields.setdefault(key, []).append(value)
    return fields


def graph(records):
    providers = {}
    for base, record in records.items():
        info = record['metadata']
        for field in ('pkgname', 'provides', 'provides_x86_64'):
            for value in info.get(field, []):
                name = re.split(r'[<>=]', value)[0]
                providers.setdefault(name, set()).add(base)
    result = {}
    for base, record in records.items():
        edges = set()
        for field in ('depends', 'makedepends', 'checkdepends'):
            for suffix in ('', '_x86_64'):
                for value in record['metadata'].get(field + suffix, []):
                    edges.update(providers.get(re.split(r'[<>=]', value)[0], set()))
        result[base] = sorted(edges - {base})
    return result


def invalidate(records, previous, dirty):
    dirty = dict(dirty)
    while True:
        before = len(dirty)
        for base, record in records.items():
            old = previous.get(base, {})
            edges = set(record['dependencies']) | set(old.get('dependencies', []))
            if base not in dirty and any(dep in dirty or dep not in records for dep in edges):
                dirty[base] = 'repository dependency rebuilt or removed'
        if len(dirty) == before:
            return dirty


def fetch(url, path):
    request = Request(url, headers={'User-Agent': 'crafting-repo/1.0 (+https://github.com/craftingdragon/arch-repo)'})
    with urlopen(request, timeout=30) as response, path.open('wb') as output:
        import shutil
        shutil.copyfileobj(response, output)


def restore(url, fingerprint_path, staging, *, strict=False):
    if not url:
        return {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'gnupg'
            home.mkdir(mode=0o700)
            def gpg(*args):
                return subprocess.check_output(['gpg', '--batch', '--homedir', str(home), *args], stderr=subprocess.PIPE)
            for file in ('repo-key.asc', 'build-manifest.json', 'build-manifest.json.sig'):
                fetch(url.rstrip('/') + '/' + file, root / file)
            expected = fingerprint_path.read_text().strip()
            gpg('--import', str(root / 'repo-key.asc'))
            gpg('--assert-signer', expected, '--verify', str(root / 'build-manifest.json.sig'), str(root / 'build-manifest.json'))
            data = json.loads((root / 'build-manifest.json').read_text())
            if data['schema'] != 1 or data['architecture'] != 'x86_64':
                raise ValueError('Unsupported build manifest')
            records = data['recipes']
            filenames = set()
            for record in records.values():
                if not record['outputs']:
                    raise ValueError('Incomplete outputs')
                for output in record['outputs']:
                    name = output['filename']
                    if not re.fullmatch(r'[A-Za-z0-9@._+:\-]+\.pkg\.tar\.zst', name) or name in filenames:
                        raise ValueError('Unsafe or duplicate output filename')
                    filenames.add(name)
                    if not re.fullmatch('[0-9a-f]{64}', output['sha256']):
                        raise ValueError('Invalid output hash')
            return records
    except (OSError, HTTPException, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        if strict:
            raise RuntimeError(f'Cannot verify published snapshot; refusing automatic full rebuild: {error}. '
                               'Fix access or explicitly select force_rebuild.') from error
        print(f'Published snapshot unavailable or untrusted; full rebuild: {error}')
        return {}


def download(record, url, staging):
    downloaded = []
    try:
        for output in record['outputs']:
            path = staging / output['filename']
            downloaded.append(path)
            # Pages routes literal epoch colons; encoded ones hit its HTML fallback.
            fetch(url.rstrip('/') + '/x86_64/' + quote(path.name, safe=':'), path)
            if sha(path) != output['sha256']:
                raise ValueError(f'Checksum mismatch: {path.name}')
        return True
    except (OSError, HTTPException, ValueError) as error:
        print(f'Cannot reuse package outputs: {error}')
        for path in downloaded:
            path.unlink(missing_ok=True)
        return False
