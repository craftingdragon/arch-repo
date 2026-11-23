# crafting-repo

My Arch Linux package repository. It contains prebuilt AUR packages and a few local
recipes for software I use. Packages are built for x86_64; architecture-independent
packages are included too.

[Package list and downloads](https://arch.craftingdragon.ch/packages.html)

## Install

Download the signing key and check its fingerprint:

```sh
curl -fLO https://arch.craftingdragon.ch/repo-key.asc
gpg --show-keys --with-fingerprint repo-key.asc
```

The fingerprint should match [signing-fingerprint.txt](signing-fingerprint.txt):

```text
BFCA91C3BAB8FF7C5DF5C2F8939F4A41559A9639
```

Then import and locally sign the key:

```sh
sudo pacman-key --add repo-key.asc
sudo pacman-key --lsign-key BFCA91C3BAB8FF7C5DF5C2F8939F4A41559A9639
```

Add this below the official repositories in `/etc/pacman.conf`:

```ini
[crafting]
SigLevel = Required DatabaseRequired
Server = https://arch.craftingdragon.ch/$arch
```

Refresh and upgrade, then install what you need:

```sh
sudo pacman -Syu
sudo pacman -S crafting/qdirstat
```

## Package updates

`aur.txt` lists the AUR package bases to track. The update workflow checks them
daily at 00:00 UTC and opens pull requests when their recipes or tracked Git
sources change. It can also be run manually from GitHub Actions. Scheduled runs
may start later than the configured time.

AUR recipes live in `packages/aur/`. Each import has a commit and snapshot digest
in `.aur-locks/`; AUR `-git` packages also record the reviewed upstream revisions.
The updater reads recipe files without executing them. Changes still need review
before merging.

To add an AUR package, add its **package base** to `aur.txt`, run the update
workflow, and review the resulting import PR. Keep custom recipes in
`packages/local/`. Editing an imported AUR snapshot directly will fail the static
checks unless its lock is explicitly reconciled.

## Builds and publishing

Pushes to `master` build and publish the repository through GitHub Actions.
Recipes build in disposable Arch containers, with repository dependencies built
first. Unchanged packages are reused from the verified signed build manifest.
Packages, repository databases, and the manifest are signed before deployment.

Cloudflare Pages serves the website and databases. Package archives and signatures
are stored in R2, with redirects from the repository URL.

For a full local build, you need Python 3, Git, and a running Docker daemon:

```sh
python scripts/build.py
```

Run it from the repository root with `out/` empty or absent. Build outputs go into
`out/`; this command does not sign or publish them.

## Checks

```sh
python -m unittest discover -s tests -v
python scripts/check.py
bash -n scripts/publish.sh
find packages -name PKGBUILD -print0 | xargs -0 -r -n1 bash -n
```

The publication integration test needs GnuPG, `repo-add`, `bsdtar`, and `zstd`.
It uses a temporary signing key. Docker smoke checks are available separately in
`tests/smoke-build.py` and `tests/smoke-vcs-build.py`.
