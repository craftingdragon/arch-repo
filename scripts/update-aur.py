#!/usr/bin/env python3
"""Import AUR trees through PRs without evaluating recipe code."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def digest(files):
    h = hashlib.sha256()
    for name, data in sorted(files.items()):
        if isinstance(data, tuple):
            h.update(b'symlink\0')
            data = data[1]
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
    files = {}
    for p in directory.rglob('*'):
        if p.is_symlink():
            files[str(p.relative_to(directory))] = ('symlink', os.fsencode(os.readlink(p)))
        elif p.is_file():
            files[str(p.relative_to(directory))] = p.read_bytes()
    return files


def read_aur_tree(source, revision="HEAD", prefix=""):
    files, modes = {}, {}
    entries = subprocess.check_output(['git', 'ls-tree', '-rz', revision, *(['--', prefix] if prefix else [])], cwd=source)
    for entry in entries.split(b'\0'):
        if not entry:
            continue
        meta, raw_name = entry.split(b'\t', 1)
        mode, kind, sha = meta.decode().split()
        name = os.fsdecode(raw_name)
        if prefix:
            name = name.removeprefix(prefix.rstrip('/') + '/')
        if mode not in ('100644', '100755', '120000') or kind != 'blob':
            raise ValueError(f'Unsupported AUR file: {name} (mode {mode}, type {kind})')
        modes[name] = int(mode, 8)
        files[name] = subprocess.check_output(['git', 'cat-file', 'blob', sha], cwd=source)
    return files, modes


def write_tree(target, files, modes):
    # Regular files first: never write through a just-imported symlink.
    for name, data in files.items():
        p = target / name
        if Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError(f'Unsafe AUR path: {name}')
        p.parent.mkdir(parents=True, exist_ok=True)
        if modes[name] != 0o120000:
            p.write_bytes(data)
            p.chmod(modes[name] & 0o777)
    for name, data in files.items():
        if modes[name] == 0o120000:
            destination = os.fsdecode(data)
            if Path(destination).is_absolute():
                raise ValueError(f'Absolute AUR symlink: {name}')
            (target / name).symlink_to(destination)
    for p in target.rglob('*'):
        if p.is_symlink():
            try:
                p.resolve().relative_to(target.resolve())
                try:
                    p.stat()
                except FileNotFoundError:
                    pass  # An internal dangling link is still a valid Git entry.
            except (ValueError, RuntimeError, OSError) as error:
                raise ValueError(f'AUR symlink escapes recipe or loops: {p}') from error


def lock_path(base):
    return f'.aur-locks/{base}.json'


def read_proposed_lock(checkout, base):
    paths = run('git', 'ls-tree', '--name-only', 'FETCH_HEAD', '--', lock_path(base), 'aur-lock.json', cwd=checkout).splitlines()
    if lock_path(base) in paths:
        return json.loads(run('git', 'show', f'FETCH_HEAD:{lock_path(base)}', cwd=checkout))
    if 'aur-lock.json' in paths:
        return json.loads(run('git', 'show', 'FETCH_HEAD:aur-lock.json', cwd=checkout)).get(base, {})
    raise ValueError(f'{base}: proposal has no lock metadata')


def proposal_state(checkout, remote, branch, base, commit, existing, vcs_sources=None):
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
    if not messages and not existing:
        ancestor = run('git', 'merge-base', 'origin/master', head, cwd=checkout)
        if ancestor == head:
            # A closed, already-merged proposal can supply the lease for the
            # next update without requiring deletion of its remote branch.
            return head, False
    if not messages or any(not m.startswith(f'aur-bot: {base} ') for m in messages):
        raise ValueError(f'{base}: branch contains human edits or has no bot proposal')
    changed = run('git', 'diff', '--name-only', 'origin/master...FETCH_HEAD', cwd=checkout).splitlines()
    if any(p not in ('aur-lock.json', lock_path(base)) and not p.startswith(f'packages/aur/{base}/') for p in changed):
        raise ValueError(f'{base}: branch contains unrelated changes')
    proposed = read_proposed_lock(checkout, base)
    # An unchanged AUR revision is already proposed. Advancing master alone
    # must not rewrite that commit or reset an in-progress review.
    return head, (proposed.get('commit') == commit
                  and proposed.get('vcs_sources', {}) == (vcs_sources or {}))


def snapshot_digest(files, modes):
    return digest({name: ('symlink', data) if modes[name] == 0o120000 else data
                   for name, data in files.items()})


def proposal_complete(checkout, base):
    state = read_proposed_lock(checkout, base)
    files, modes = read_aur_tree(checkout, 'FETCH_HEAD', f'packages/aur/{base}')
    return snapshot_digest(files, modes) == state.get('digest')


def stage_snapshot(checkout, base):
    path = f'packages/aur/{base}'
    # AUR can track files excluded by its own .gitignore. Import its complete
    # tracked snapshot instead of applying the source repository's ignore rules.
    run('git', 'add', '--force', '--', path, lock_path(base), cwd=checkout)
    staged = run('git', 'write-tree', cwd=checkout)
    files, modes = read_aur_tree(checkout, staged, path)
    expected = json.loads((checkout / lock_path(base)).read_text())['digest']
    if snapshot_digest(files, modes) != expected:
        raise ValueError(f'{base}: staged files differ from imported AUR snapshot')


def upstream_compare(source, old, new):
    if (old == new or not all(isinstance(sha, str) and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', sha)
                             for sha in (old, new))):
        return None
    url = source.split('::', 1)[-1].removeprefix('git+').replace('?signed', '').split('#', 1)[0]
    # Normalize SCP-style clone URLs without confusing an SSH port with a path.
    if re.match(r'^[^/@:]+@[^/:]+:', url):
        url = 'ssh://' + url.replace(':', '/', 1)
    parsed = urlsplit(url)
    if parsed.scheme not in ('https', 'http', 'git', 'ssh') or not parsed.hostname:
        return None
    hosts = json.loads(Path(__file__).resolve().parent.parent.joinpath('forge-hosts.json').read_text())
    forge = hosts.get(parsed.hostname.lower())
    path = parsed.path.rstrip('/').removesuffix('.git').strip('/')
    if parsed.query or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+', path):
        return None
    if any(part in ('.', '..') for part in path.split('/')):
        return None
    base = f'https://{parsed.hostname.lower()}/{path}'
    if forge == 'github':
        return f'{base}/compare/{old}..{new}'
    if forge == 'gitlab':
        return f'{base}/-/compare/{old}...{new}?straight=true'
    if forge in ('gitea', 'forgejo'):
        return f'{base}/compare/{old}...{new}'
    return None


def ensure_pr(repo, branch, base, commit, existing, vcs_sources=None, previous_vcs_sources=None):
    body = (f'Review all recipe and supporting-file changes before merging.\n\n'
            f'Upstream: https://aur.archlinux.org/cgit/aur.git/commit/?h={base}&id={commit}\n\n'
            'No recipe code was executed by the updater.')
    if vcs_sources:
        body += '\n\nProposed upstream Git revisions:'
        for source, sha in sorted(vcs_sources.items()):
            old = (previous_vcs_sources or {}).get(source)
            compare = upstream_compare(source, old, sha)
            body += f'\n- `{source}` → `{sha}`'
            if compare:
                body += f' ([Compare {old[:12]}..{sha[:12]}]({compare}))'
    if existing:
        run('gh', 'pr', 'edit', str(existing[0]['number']), '--repo', repo, '--body', body)
    else:
        run('gh', 'pr', 'create', '--repo', repo, '--base', 'master', '--head', branch,
            '--title', f'AUR: update {base}', '--body', body)


def main():
    import vcs
    import reuse
    root = Path.cwd()
    repo = os.environ['GITHUB_REPOSITORY']
    for base in names(Path('aur.txt').read_text()):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'
            run('git', 'clone', '--quiet', f'https://aur.archlinux.org/{base}.git', str(source))
            commit = run('git', 'rev-parse', 'HEAD', cwd=source)
            files, modes = read_aur_tree(source)
            if 'PKGBUILD' not in files or '.SRCINFO' not in files:
                raise ValueError(f'{base}: missing recipe metadata')
            state_path = root / lock_path(base)
            previous = json.loads(state_path.read_text()) if state_path.exists() else None
            vcs_sources = vcs.resolve(reuse.metadata(files['.SRCINFO'].decode())) if base.endswith('-git') else {}
            if previous and previous['commit'] == commit and previous.get('vcs_sources', {}) == vcs_sources:
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
            lease, up_to_date = proposal_state(checkout, remote, branch, base, commit, existing, vcs_sources)
            if up_to_date:
                if proposal_complete(checkout, base):
                    ensure_pr(repo, branch, base, commit, existing, vcs_sources,
                              previous.get('vcs_sources', {}) if previous else {})
                    print(f'{base}: AUR revision already proposed; preserving branch')
                    continue
                # Preserve the existing bot commit and append a repair for an
                # incomplete import, even though its AUR revision is unchanged.
                run('git', 'reset', '--hard', lease, cwd=checkout)
                print(f'{base}: repairing incomplete AUR import with an additional commit')
            target = checkout / 'packages' / 'aur' / base
            import shutil
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True)
            write_tree(target, files, modes)
            state_path = checkout / lock_path(base)
            state_path.parent.mkdir(exist_ok=True)
            state = {'commit': commit, 'digest': digest(tree(target))}
            if base.endswith('-git'):
                state['vcs_sources'] = vcs_sources
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')
            run('git', 'config', 'user.name', 'github-actions[bot]', cwd=checkout)
            run('git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com', cwd=checkout)
            stage_snapshot(checkout, base)
            run('git', 'commit', '-qm', f'aur-bot: {base} {commit}', cwd=checkout)
            run('git', 'remote', 'set-url', 'origin', f'https://github.com/{repo}.git', cwd=checkout)
            run('git', 'push', f'--force-with-lease=refs/heads/{branch}:{lease}', 'origin', branch, cwd=checkout)
            ensure_pr(repo, branch, base, commit, existing, vcs_sources,
                      previous.get('vcs_sources', {}) if previous else {})


if __name__ == '__main__':
    main()
