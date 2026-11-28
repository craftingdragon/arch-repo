"""Check the master tip before touching a publication artifact."""
import json
import os
from urllib.request import Request, urlopen

if __name__ == '__main__':
    repository = os.environ['GITHUB_REPOSITORY']
    request = Request(
        f"{os.environ['GITHUB_API_URL']}/repos/{repository}/git/ref/heads/master",
        headers={'Authorization': f"Bearer {os.environ['GH_TOKEN']}",
                 'Accept': 'application/vnd.github+json', 'Cache-Control': 'no-cache'},
    )
    with urlopen(request, timeout=20) as response:
        tip = json.load(response)['object']['sha']
    if len(tip) != 40 or any(char not in '0123456789abcdef' for char in tip):
        raise ValueError('GitHub returned an invalid master SHA')
    current = tip == os.environ['GITHUB_SHA']
    print(f"Run SHA {os.environ['GITHUB_SHA']}, master SHA {tip}")
    if not current and os.environ['GITHUB_EVENT_NAME'] == 'workflow_dispatch':
        raise SystemExit('Master advanced during the manual run. Dispatch again from the current tip.')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        print(f'current={str(current).lower()}', file=output)
