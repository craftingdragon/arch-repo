#!/usr/bin/env python3
"""Import AUR trees through PRs without evaluating recipe code."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def digest(files):
    h = hashlib.sha256()
    for name, data in sorted(files.items()):
        h.update(name.encode() + b'\0' + data + b'\0')
    return h.hexdigest()


def names(text):
    result = []
    for line in text.splitlines():
        name = line.split('#', 1)[0].strip()
        if not name:
            continue
        if not re.fullmatch(r'[a-z0-9][a-z0-9@._+\-]*', name):
            raise ValueError(f'Invalid package base: {name}')
        if name in result:
            raise ValueError(f'Duplicate package base: {name}')
        result.append(name)
    return result


def tree(directory):
    return {str(p.relative_to(directory)): p.read_bytes()
            for p in directory.rglob('*') if p.is_file()}


def lock_path(base):
    return f'.aur-locks/{base}.json'


def read_proposed_lock(checkout, base):
    paths = run('git', 'ls-tree', '--name-only', 'FETCH_HEAD', '--', lock_path(base), 'aur-lock.json', cwd=checkout).splitlines()
    if lock_path(base) in paths:
        return json.loads(run('git', 'show', f'FETCH_HEAD:{lock_path(base)}', cwd=checkout))
    if 'aur-lock.json' in paths:
        return json.loads(run('git', 'show', 'FETCH_HEAD:aur-lock.json', cwd=checkout)).get(base, {})
    raise ValueError(f'{base}: proposal has no lock metadata')


def proposal_state(checkout, remote, branch, base, commit, existing):
    """Find the branch independently of PR state; return lease and up-to-date flag."""
    ref = f'refs/heads/{branch}'
    advertised = run('git', 'ls-remote', '--heads', remote, ref, cwd=checkout)
    if not advertised:
        if existing:
            raise ValueError(f'{base}: PR branch disappeared during update')
        return '', False
    head = advertised.split()[0]
    if existing and existing[0]['headRefOid'] != head:
        raise ValueError(f'{base}: branch changed during update')
    run('git', 'fetch', '--quiet', remote, ref, cwd=checkout)
    if run('git', 'rev-parse', 'FETCH_HEAD', cwd=checkout) != head:
        raise ValueError(f'{base}: branch changed during update')
    messages = run('git', 'log', '--format=%s', 'origin/master..FETCH_HEAD', cwd=checkout).splitlines()
    if not messages or any(not m.startswith(f'aur-bot: {base} ') for m in messages):
        raise ValueError(f'{base}: branch contains human edits or has no bot proposal')
    changed = run('git', 'diff', '--name-only', 'origin/master...FETCH_HEAD', cwd=checkout).splitlines()
    if any(p not in ('aur-lock.json', lock_path(base)) and not p.startswith(f'packages/aur/{base}/') for p in changed):
        raise ValueError(f'{base}: branch contains unrelated changes')
    proposed = read_proposed_lock(checkout, base)
    # An unchanged AUR revision is already proposed. Advancing master alone
    # must not rewrite that commit or reset an in-progress review.
    return head, proposed.get('commit') == commit


def ensure_pr(repo, branch, base, commit, existing):
    body = (f'Review all recipe and supporting-file changes before merging.\n\n'
            f'Upstream: https://aur.archlinux.org/cgit/aur.git/commit/?h={base}&id={commit}\n\n'
            'No recipe code was executed by the updater.')
    if existing:
        run('gh', 'pr', 'edit', str(existing[0]['number']), '--repo', repo, '--body', body)
    else:
        run('gh', 'pr', 'create', '--repo', repo, '--base', 'master', '--head', branch,
            '--title', f'AUR: update {base}', '--body', body)


def main():
    root = Path.cwd()
    repo = os.environ['GITHUB_REPOSITORY']
    for base in names(Path('aur.txt').read_text()):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'
            run('git', 'clone', '--quiet', f'https://aur.archlinux.org/{base}.git', str(source))
            commit = run('git', 'rev-parse', 'HEAD', cwd=source)
            files = {}
            modes = {}
            for entry in run('git', 'ls-tree', '-r', 'HEAD', cwd=source).splitlines():
                meta, name = entry.split('\t', 1)
                mode, kind, sha = meta.split()
                if mode not in ('100644', '100755') or kind != 'blob':
                    raise ValueError(f'Unsupported AUR file: {name}')
                modes[name] = int(mode, 8) & 0o777
                files[name] = subprocess.check_output(['git', 'show', f'HEAD:{name}'], cwd=source)
            if 'PKGBUILD' not in files or '.SRCINFO' not in files:
                raise ValueError(f'{base}: missing recipe metadata')
            state_path = root / lock_path(base)
            previous = json.loads(state_path.read_text()) if state_path.exists() else None
            if previous and previous['commit'] == commit:
                continue
            dest = root / 'packages' / 'aur' / base
            current = tree(dest) if dest.exists() else {}
            if current and (not previous or digest(current) != previous['digest']):
                raise ValueError(f'{base}: local edits require manual reconciliation')
            branch = f'aur-update/{base}'
            existing = json.loads(run('gh', 'pr', 'list', '--repo', repo, '--state', 'open', '--head', branch,
                                      '--json', 'number,headRefOid'))
            checkout = Path(tmp) / 'checkout'
            run('git', 'clone', '--quiet', '--no-hardlinks', str(root), str(checkout))
            run('git', 'checkout', '-B', branch, 'origin/master', cwd=checkout)
            remote = f'https://github.com/{repo}.git'
            lease, up_to_date = proposal_state(checkout, remote, branch, base, commit, existing)
            if up_to_date:
                if not existing:
                    ensure_pr(repo, branch, base, commit, existing)
                print(f'{base}: AUR revision already proposed; preserving branch')
                continue
            target = checkout / 'packages' / 'aur' / base
            import shutil
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True)
            for name, data in files.items():
                p = target / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                p.chmod(modes[name])
            state_path = checkout / lock_path(base)
            state_path.parent.mkdir(exist_ok=True)
            state_path.write_text(json.dumps({'commit': commit, 'digest': digest(files)}, indent=2, sort_keys=True) + '\n')
            run('git', 'config', 'user.name', 'github-actions[bot]', cwd=checkout)
            run('git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com', cwd=checkout)
            run('git', 'add', f'packages/aur/{base}', lock_path(base), cwd=checkout)
            run('git', 'commit', '-qm', f'aur-bot: {base} {commit}', cwd=checkout)
            run('git', 'remote', 'set-url', 'origin', f'https://github.com/{repo}.git', cwd=checkout)
            run('git', 'push', f'--force-with-lease=refs/heads/{branch}:{lease}', 'origin', branch, cwd=checkout)
            ensure_pr(repo, branch, base, commit, existing)


if __name__ == '__main__':
    main()
