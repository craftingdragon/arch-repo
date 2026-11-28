#!/usr/bin/env python3
"""Separate immutable R2 downloads from the small, atomic Pages deployment."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from http.client import HTTPException

from reuse import USER_AGENT


def already_public(url, key, size):
    request = Request(f'{url}/{quote(key, safe="/")}', method='HEAD',
                      headers={'User-Agent': USER_AGENT})
    try:
        with urlopen(request, timeout=5) as response:
            return response.status == 200 and response.headers.get('Content-Length') == str(size)
    except (OSError, HTTPException):
        return False


def main():
    url = os.environ['R2_PUBLIC_URL'].rstrip('/')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c.isspace() for c in url)):
        raise ValueError('R2_PUBLIC_URL must be a public HTTPS URL without credentials, query, or fragment')
    site = Path('site')
    r2 = Path('r2')
    if r2.exists():
        raise ValueError('r2/ must not exist before preparing publication')
    outputs = [output for record in json.loads((site / 'build-manifest.json').read_text())['recipes'].values()
               for output in record['outputs']]
    files = []
    seen = set()
    for output in outputs:
        name = output['filename']
        if not re.fullmatch(r'[A-Za-z0-9@._+:\-]+\.pkg\.tar\.zst', name) or name in seen:
            raise ValueError('Unsafe or duplicate package filename')
        seen.add(name)
        for suffix in ('', '.sig'):
            source = site / 'x86_64' / (name + suffix)
            if source.is_symlink() or not source.is_file():
                raise ValueError(f'Missing regular package or signature: {source}')
            with source.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if not suffix and digest != output['sha256']:
                raise ValueError(f'Package checksum mismatch: {source}')
            key = f'sha256/{digest}/{source.name}'
            files.append((source, key))
    redirects = []
    for source, key in files:
        # Retain both forms; generated clients use literal epoch colons for Pages.
        for name in dict.fromkeys((quote(source.name, safe=''), source.name)):
            redirects.append(f'/x86_64/{name} {url}/{quote(key, safe="/")} 301')
    # Cloudflare limits static redirects to 2000. Reject before moving files.
    if len(redirects) > 2000 or any(len(rule) > 1000 for rule in redirects):
        raise ValueError('Too many package redirects for Cloudflare Pages (maximum 2000)')
    index = {key: source.stat().st_size for source, key in files}
    archives = [key for key in index if key.endswith('.pkg.tar.zst')]
    omitted = set()
    if os.environ.get('R2_INCREMENTAL') == 'true':
        with ThreadPoolExecutor(max_workers=8) as pool:
            available = pool.map(lambda key: already_public(url, key, index[key]), archives)
            omitted = {key for key, present in zip(archives, available) if present}

    for source, key in files:
        if key in omitted:
            source.unlink()
        else:
            target = r2 / key
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, target)
    # libalpm also needs a signature next to the package's effective URL.
    signatures = [key for key in index if key.endswith('.sig')]
    for package, signature in zip(archives, signatures, strict=True):
        alias = package + '.sig'
        target = r2 / alias
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(r2 / signature, target)
        index[alias] = index[signature]
    r2.mkdir(exist_ok=True)
    Path('r2-index.json').write_text(json.dumps(index, sort_keys=True) + '\n')
    staged_bytes = sum(size for key, size in index.items() if key not in omitted)
    summary = f'R2 handoff: {staged_bytes:,} bytes, omitted {len(omitted)} unchanged archives'
    print(summary)
    if summary_path := os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(summary_path, 'a') as stream:
            print(summary, file=stream)
    (site / '_redirects').write_text('\n'.join(redirects) + ('\n' if redirects else ''))
    # Mutable databases stay on Pages and must not be cached across deployments.
    (site / '_headers').write_text('/x86_64/*\n  Cache-Control: no-cache\n/build-manifest.json*\n  Cache-Control: no-cache\n/repo-key.asc\n  Cache-Control: no-cache\n')
    for path in site.rglob('*'):
        if path.is_symlink():
            raise ValueError(f'Unexpected site symlink: {path}')
        if path.is_file() and path.stat().st_size >= 25 * 1024 * 1024:
            raise ValueError(f'Pages file exceeds 25 MiB: {path}')
    print(f'Prepared {len(files)} immutable R2 objects; databases and website remain on Pages')


if __name__ == '__main__':
    main()
