#!/usr/bin/env python3
"""Static validation only: never source a PKGBUILD."""
import importlib.util
import json
from pathlib import Path
import re

spec = importlib.util.spec_from_file_location('updater', Path(__file__).with_name('update-aur.py'))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)
updater.names(Path('aur.txt').read_text())
seen = set()
for recipe in Path('packages').glob('*/*/PKGBUILD'):
    base = recipe.parent.name
    if base in seen:
        raise SystemExit(f'Duplicate local/AUR package base: {base}')
    seen.add(base)
    if recipe.parent.parent.name == 'aur':
        lock = Path(updater.lock_path(base))
        state = json.loads(lock.read_text()) if lock.exists() else None
        if not state or not re.fullmatch('[0-9a-f]{40}', state['commit']):
            raise SystemExit(f'{base}: missing or invalid AUR lock')
        if updater.digest(updater.tree(recipe.parent)) != state['digest']:
            raise SystemExit(f'{base}: imported files differ from AUR snapshot; reconcile the lock explicitly')
        info = recipe.with_name('.SRCINFO')
        if not info.exists() or f'pkgbase = {base}' not in info.read_text().splitlines():
            raise SystemExit(f'{base}: missing or inconsistent .SRCINFO')
print('Static package configuration checks passed')
