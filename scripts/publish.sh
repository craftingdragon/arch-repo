#!/usr/bin/env bash
set -euo pipefail
name=$(cat repo-name.txt)
[[ $name =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || { echo 'Invalid repository name'; exit 1; }
mkdir -p site/x86_64
export GNUPGHOME
GNUPGHOME=$(mktemp -d)
trap 'rm -rf "$GNUPGHOME"' EXIT
chmod 700 "$GNUPGHOME"
printf '%s' "$SIGNING_KEY" | gpg --batch --import
fingerprint=$(gpg --batch --with-colons --list-secret-keys | awk -F: '$1=="fpr" {print $10; exit}')
[[ -n $fingerprint ]]
expected=$(tr -d '[:space:]' < signing-fingerprint.txt)
[[ "$fingerprint" == "$expected" ]] || { echo 'Signing key fingerprint mismatch'; exit 1; }
sign() {
  printf '%s' "${SIGNING_PASSPHRASE:-}" | gpg --batch --yes --pinentry-mode loopback --passphrase-fd 0 --local-user "$fingerprint" --detach-sign "$1"
  gpg --batch --verify "$1.sig" "$1"
}
gpg --armor --export "$fingerprint" > site/repo-key.asc
shopt -s nullglob
packages=(out/*.pkg.tar.zst)
for package in "${packages[@]}"; do
  cp "$package" site/x86_64/
  sign "site/x86_64/${package##*/}"
done
if ((${#packages[@]})); then
  repo-add "site/x86_64/$name.db.tar.gz" site/x86_64/*.pkg.tar.zst
  for kind in db files; do
    archive="site/x86_64/$name.$kind.tar.gz"
    sign "$archive"
    rm -f "site/x86_64/$name.$kind"
    cp "$archive" "site/x86_64/$name.$kind"
    cp "$archive.sig" "site/x86_64/$name.$kind.sig"
  done
fi
export REPO_NAME="$name" REPO_FINGERPRINT="$fingerprint"
python scripts/site.py
[[ -z $(find site -type l -print -quit) ]]
[[ $(du -sb site | cut -f1) -lt 1000000000 ]] || { echo 'Pages site exceeds 1 GB'; exit 1; }
