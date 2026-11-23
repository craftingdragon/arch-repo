#!/usr/bin/env python3
"""Render the repository overview and package catalog from published artifacts."""
import html
import json
import os
from pathlib import Path
import shutil
from urllib.parse import quote


def esc(value):
    return html.escape(str(value), quote=True)


def recipe_info(path, record):
    fields = record.get('metadata', {})
    description = next(iter(fields.get('pkgdesc', [])), '')
    package_descriptions = {}
    declared = set(fields.get('pkgname', []))
    info = path / '.SRCINFO'
    if info.exists():
        current = None
        for line in info.read_text().splitlines():
            key, sep, value = line.strip().partition(' = ')
            if not sep:
                continue
            if key == 'pkgname':
                current = value
                declared.add(value)
            elif key == 'pkgdesc':
                if current:
                    package_descriptions[current] = value
                else:
                    description = value
    return description, package_descriptions, declared


def size_label(size):
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if size < 1024 or unit == 'GiB':
            return f'{size:.1f} {unit}' if unit != 'B' else f'{size} B'
        size /= 1024


def main():
    site = Path('site')
    name = os.environ['REPO_NAME']
    fingerprint = os.environ['REPO_FINGERPRINT']
    url = os.environ['PAGES_URL'].rstrip('/')
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    revision = os.environ.get('GITHUB_SHA', '')
    server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com').rstrip('/')
    repository_url = server + '/' + quote(repo, safe='/') if repo else ''
    manifest = json.loads((site / 'build-manifest.json').read_text())
    rows = []
    for recipe, record in manifest['recipes'].items():
        # Recipe paths originate in the build manifest, never output filenames.
        path = Path(recipe)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('Unsafe recipe path')
        description, package_descriptions, declared = recipe_info(path, record)
        output_names = {output['name'] for output in record['outputs']}
        for output in record['outputs']:
            filename = output['filename']
            if Path(filename).name != filename:
                raise ValueError('Unsafe package filename')
            package = output['name']
            debug = package not in declared and package.endswith('-debug') and package[:-6] in (declared | output_names)
            desc = package_descriptions.get(package, description)
            if debug:
                desc = f'Debug symbols for {package[:-6]}'
            source = (f'{repository_url}/blob/{quote(revision, safe="")}/{quote(recipe, safe="/")}/PKGBUILD'
                      if repository_url and revision else '')
            size = (site / 'x86_64' / filename).stat().st_size
            rows.append(dict(name=package, version=output['version'], architecture=output['architecture'],
                             filename=filename, size=size, description=desc, debug=debug, source=source))
    rows.sort(key=lambda row: row['name'].casefold())

    def page(title, active, content):
        navigation = ''.join(f'<a href="{href}"' + (' aria-current="page"' if label == active else '') + f'>{label}</a>'
                             for label, href in [('Overview', 'index.html'), ('Packages', 'packages.html')])
        return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · {esc(name)}</title><link rel="stylesheet" href="assets/site.css"><script src="assets/site.js" defer></script></head>
<body><a class="skip" href="#main">Skip to content</a><header><a class="brand" href="index.html">{esc(name)}<span> / Arch Linux</span></a><nav aria-label="Main">{navigation}</nav></header>
<main id="main">{content}</main><footer>Packages and repository databases are signed. Verify the key fingerprint before trusting this repository.</footer></body></html>'''

    def code(title, text, ident):
        return f'<section class="panel"><div class="panel-heading"><h3>{esc(title)}</h3><button type="button" data-copy="{ident}" hidden>Copy</button></div><pre><code id="{ident}">{esc(text)}</code></pre></section>'

    regular = sum(not row['debug'] for row in rows)
    home = f'''<p class="eyebrow">SIGNED PACKAGE REPOSITORY</p><h1>Arch packages.<br>Ready to install.</h1>
<p class="lede">The {esc(name)} repository provides reviewed packages for x86_64, including architecture-independent packages.</p>
<div class="hero-actions"><a class="primary" href="packages.html">Browse {regular} packages <span aria-hidden="true">→</span></a>{f'<a href="{esc(repository_url)}">View repository ↗</a>' if repository_url else ''}</div>
<section class="key panel"><h2>Repository signing key</h2><p>Verify this fingerprint independently before importing the key.</p><code class="fingerprint" id="fingerprint">{esc(fingerprint)}</code><div class="key-actions"><button type="button" data-copy="fingerprint" hidden>Copy fingerprint</button><a href="repo-key.asc">Download public key</a></div></section>
<h2>Install this repository</h2><p>Run these commands in order, then install packages with pacman.</p>'''
    home += code('1. Import and trust the signing key', f'curl -fLO {url}/repo-key.asc\nsudo pacman-key --add repo-key.asc\nsudo pacman-key --lsign-key {fingerprint}', 'import-key')
    home += code('2. Add to /etc/pacman.conf', f'[{name}]\nSigLevel = Required DatabaseRequired\nServer = {url}/$arch', 'pacman-config')
    home += code('3. Refresh and upgrade', 'sudo pacman -Syu', 'upgrade')
    if not rows:
        home += '<p class="empty">No packages yet. Packages will appear after the first successful package build.</p>'
    home += '<p role="status" id="copy-status" class="copy-status" aria-live="polite"></p>'
    (site / 'index.html').write_text(page('Overview', 'Overview', home))

    catalog = '''<p class="eyebrow">PACKAGE CATALOG</p><h1>Find your package.</h1><p class="lede">Browse available versions, download signed archives, or inspect the reviewed build recipes.</p>
<form id="catalog-controls" class="controls" hidden><label>Search packages<input type="search" id="search" placeholder="Name or description…"></label><label>Sort by<select id="sort"><option value="name">Name: A–Z</option><option value="size">Size: smallest first</option><option value="size-desc">Size: largest first</option></select></label><label class="checkbox"><input type="checkbox" id="show-debug"> Show debug packages</label></form>'''
    catalog += f'<p id="package-count" role="status">{len(rows)} packages</p><div class="table-wrap"><table><caption class="sr-only">Available packages</caption><thead><tr><th scope="col">Package</th><th scope="col">Version</th><th scope="col">Architecture</th><th scope="col">Size</th><th scope="col">Actions</th></tr></thead><tbody id="packages">'
    for row in rows:
        # Keep epoch colons literal so Pages resolves the package redirect.
        href = 'x86_64/' + quote(row['filename'], safe=':')
        source_link = f'<a href="{esc(row["source"])}">PKGBUILD</a>' if row['source'] else '<span class="muted">Source unavailable</span>'
        signature = f'<a href="{esc(href)}.sig">Signature</a>' if (site / 'x86_64' / (row['filename'] + '.sig')).exists() else '<span class="muted">Signature unavailable</span>'
        catalog += f'''<tr data-name="{esc(row['name'])}" data-search="{esc(row['name'] + ' ' + row['description'])}" data-size="{row['size']}" data-debug="{str(row['debug']).lower()}"><th scope="row"><span class="package-name">{esc(row['name'])}</span>{'<span class="badge">debug</span>' if row['debug'] else ''}<p class="description">{esc(row['description'])}</p></th><td class="version">{esc(row['version'])}</td><td><code>{esc(row['architecture'])}</code></td><td class="size">{size_label(row['size'])}</td><td><div class="row-actions"><a href="{esc(href)}" aria-label="Download {esc(row['name'])}">Download</a>{signature}{source_link}</div></td></tr>'''
    catalog += '</tbody></table></div><p id="no-results" class="empty" hidden>No packages match your filters. Try another search or enable debug packages.</p>'
    if not rows:
        catalog += '<p class="empty">No packages yet.</p>'
    catalog += '<details class="panel files"><summary>Repository files</summary><p>Pacman databases, detached signatures, and build manifests.</p><ul>'
    for file in sorted((site / 'x86_64').iterdir()):
        if '.pkg.tar.' not in file.name:
            catalog += f'<li><a href="x86_64/{quote(file.name, safe=":")}">{esc(file.name)}</a></li>'
    for filename in ('build-manifest.json', 'build-manifest.json.sig', 'repo-key.asc'):
        if (site / filename).exists():
            catalog += f'<li><a href="{filename}">{filename}</a></li>'
    catalog += '</ul></details><noscript><p>All packages, including debug packages, are shown. Enable JavaScript for search and sorting.</p></noscript>'
    (site / 'packages.html').write_text(page('Packages', 'Packages', catalog))
    shutil.copytree(Path(__file__).with_name('site-assets'), site / 'assets', dirs_exist_ok=True)


if __name__ == '__main__':
    main()
