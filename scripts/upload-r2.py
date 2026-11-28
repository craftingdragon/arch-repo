"""Check exact R2 keys and recover package bytes when the small artifact omits them."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

from reuse import sha

STAGING = Path('r2')
INDEX = Path('r2-index.json')
PENDING = Path('r2-pending.json')
ARCHIVE = '.pkg.tar.zst'


def upload_plan(aws, bucket, index):
    def needs_upload(entry):
        key, size = entry
        result = subprocess.run(
            [*aws, 's3api', 'head-object', '--bucket', bucket, '--key', key,
             '--query', 'ContentLength', '--output', 'text'],
            capture_output=True, text=True,
        )
        if result.returncode:
            if '(404)' in result.stderr and 'HeadObject' in result.stderr:
                return True
            raise RuntimeError(f'Cannot check R2 object {key}: {result.stderr.strip()}')
        if result.stdout.strip() != str(size):
            # The adjacent pacman signature is keyed by its package digest.
            # A fresh signature from the same key can have a different size.
            if key.endswith('.sig') and key.removesuffix('.sig') in index:
                return True
            raise RuntimeError(f'Unexpected size for R2 object {key}')
        return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [key for key, missing in zip(index, pool.map(needs_upload, index.items()))
                if missing]


def recover_archives(keys, archive_path, index):
    if not keys:
        return
    with tarfile.open(archive_path, mode='r:') as artifact:
        for key in keys:
            _, digest, name = key.split('/')
            member = artifact.getmember(f'./{name}')
            if not member.isfile() or member.size != index[key]:
                raise ValueError(f'Invalid recovery archive for {key}')
            path = STAGING / key
            path.parent.mkdir(parents=True, exist_ok=True)
            with artifact.extractfile(member) as source, path.open('xb') as target:
                shutil.copyfileobj(source, target)
            if sha(path) != digest:
                raise ValueError(f'Recovery archive checksum mismatch for {key}')


def validate_index(index):
    for key, size in index.items():
        if (not re.fullmatch(r'sha256/[0-9a-f]{64}/[A-Za-z0-9@._+:\-]+\.pkg\.tar\.zst(?:\.sig)?', key)
                or type(size) is not int or size <= 0):
            raise ValueError(f'Invalid R2 object in index: {key}')


def main(mode):
    endpoint = f"https://{os.environ['CLOUDFLARE_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    aws = ['aws', '--endpoint-url', endpoint]
    bucket = os.environ['R2_BUCKET']
    if not bucket or not os.environ['CLOUDFLARE_ACCOUNT_ID']:
        raise ValueError('R2 account and bucket are required')
    index = json.loads(INDEX.read_text())
    validate_index(index)
    if mode == 'plan':
        pending = upload_plan(aws, bucket, index)
        PENDING.write_text(json.dumps(pending))
        recovery = any(key.endswith(ARCHIVE) and not (STAGING / key).is_file()
                       for key in pending)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            print(f'recovery={str(recovery).lower()}', file=stream)
        return
    if mode != 'upload':
        raise ValueError('Expected plan or upload')
    pending = json.loads(PENDING.read_text())
    recover_archives([key for key in pending if key.endswith(ARCHIVE)
                      and not (STAGING / key).is_file()], 'recovery/packages.tar', index)
    for key in pending:
        path = STAGING / key
        if path.is_symlink() or not path.is_file() or path.stat().st_size != index[key]:
            raise ValueError(f'Missing or invalid staged R2 object {key}')

    def upload(key):
        subprocess.run(
            [*aws, 's3', 'cp', str(STAGING / key), f's3://{bucket}/{key}',
             '--cache-control', 'public,max-age=31536000,immutable', '--no-progress'],
            check=True,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(upload, pending))
    archives = [key for key in pending if key.endswith(ARCHIVE)]
    summary = (f'R2 uploaded {len(pending)}/{len(index)} objects, '
               f'{sum(index[key] for key in archives):,} bytes of package archives')
    print(summary)
    if summary_path := os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(summary_path, 'a') as stream:
            print(summary, file=stream)


if __name__ == '__main__':
    main(sys.argv[1])
