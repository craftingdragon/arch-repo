#!/usr/bin/env python3
import html
import os
from pathlib import Path
from urllib.parse import quote

name = os.environ['REPO_NAME']
fingerprint = os.environ['REPO_FINGERPRINT']
url = os.environ['PAGES_URL'].rstrip('/')
lines = ['<!doctype html><html lang="en"><meta charset="utf-8">',
         f'<title>{html.escape(name)} Arch repository</title>',
         f'<h1>{html.escape(name)} Arch Linux repository</h1>',
         '<p>Architecture: x86_64 (including architecture-independent packages).</p>',
         '<p><a href="repo-key.asc">Repository public key</a></p>',
         f'<p>Verify this fingerprint independently: <code>{fingerprint}</code></p>',
         '<pre>' + html.escape(f'curl -fLO {url}/repo-key.asc\nsudo pacman-key --add repo-key.asc\nsudo pacman-key --lsign-key {fingerprint}\n\n# /etc/pacman.conf\n[{name}]\nSigLevel = Required DatabaseRequired\nServer = {url}/$arch\n\n# Refresh and upgrade\nsudo pacman -Syu') + '</pre>', '<ul>']
for file in sorted(Path('site/x86_64').iterdir()):
    lines.append(f'<li><a href="x86_64/{quote(file.name)}">{html.escape(file.name)}</a></li>')
lines.extend(['</ul>', '<p>No packages yet.</p>' if not list(Path('site/x86_64').glob('*.pkg.tar.zst')) else '', '</html>'])
Path('site/index.html').write_text('\n'.join(lines))
