"""Resolve floating Git sources without evaluating PKGBUILDs, then pin builds."""
import os
import re
import shlex
import subprocess


def sources(metadata):
    return {field: values for field, values in metadata.items()
            if field in ('source', 'source_x86_64')}


def floating(source):
    value = source.split('::', 1)[-1]
    if not (value.startswith('git+') or value.startswith('git://')):
        return None
    signed = '?signed' in value
    value = value.replace('?signed', '')
    url, _, fragment = value.partition('#')
    if fragment:
        kind, sep, reference = fragment.partition('=')
        if not sep or not reference:
            raise ValueError(f'Invalid Git source fragment: {source}')
        if kind in ('commit', 'tag'):
            return None  # Explicitly pinned sources retain their original semantics.
        if kind != 'branch':
            raise ValueError(f'Unsupported Git source fragment: {source}')
        reference = 'refs/heads/' + reference
    else:
        reference = 'HEAD'
    return url.removeprefix('git+'), reference, signed


def resolve(metadata):
    result = {}
    for values in sources(metadata).values():
        for source in values:
            parsed = floating(source)
            if parsed is None or source in result:
                continue
            url, reference, _ = parsed
            try:
                output = subprocess.check_output(
                    ['git', 'ls-remote', '--exit-code', '--', url, reference],
                    text=True, stderr=subprocess.PIPE, timeout=60,
                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                raise ValueError(f'Cannot resolve upstream Git source: {source}') from error
            refs = dict(line.split()[::-1] for line in output.splitlines() if line.strip())
            sha = refs.get(reference, '')
            if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', sha):
                raise ValueError(f'No exact upstream revision for {source}')
            result[source] = sha
    return result


def validate(metadata, locked):
    if not isinstance(locked, dict):
        raise ValueError('Git source lock must map source URLs to commit SHAs')
    expected = {source for values in sources(metadata).values() for source in values
                if floating(source) is not None}
    if set(locked) != expected:
        raise ValueError('Git source lock is missing or stale; run the AUR updater and merge its PR')
    if any(not isinstance(sha, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', sha)
           for sha in locked.values()):
        raise ValueError('Invalid upstream Git commit in source lock')


def pin_script(metadata, locked):
    validate(metadata, locked)
    lines = ['\n# Pin reviewed upstream revisions from the repository source lock.']
    for field, values in sources(metadata).items():
        for index, source in enumerate(values):
            if source not in locked:
                continue
            signed = floating(source)[2]
            stem = source.replace('?signed', '').split('#', 1)[0]
            pinned = stem + '#commit=' + locked[source] + ('?signed' if signed else '')
            lines.append(f'[[ ${{{field}[{index}]}} == {shlex.quote(source)} ]] || {{ echo "Git source changed before pinning" >&2; exit 1; }}')
            lines.append(f'{field}[{index}]={shlex.quote(pinned)}')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    import json
    import sys
    print(pin_script(json.loads(sys.argv[1]), json.loads(sys.argv[2])))
