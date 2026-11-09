#!/bin/bash
# Publishes one Kou release: build, archive, GitHub release, and Homebrew cask.
# Usage: bash release.sh <version>      (for example: bash release.sh 0.1.0)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO="sypsyp97/kou"
TAP_REPO="sypsyp97/homebrew-tap"
VERSION="${1:?Usage: bash release.sh <version>}"
TAG="v$VERSION"
cd "$ROOT"

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Version must be major.minor.patch." >&2; exit 1; }
[ "$(git branch --show-current)" = main ] || { echo "Release from main." >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "Commit or stash local changes first." >&2; exit 1; }
git fetch -q origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "Push main first; HEAD differs from origin/main." >&2; exit 1; }
PLIST_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' Resources/Info.plist)"
[ "$PLIST_VERSION" = "$VERSION" ] || { echo "Resources/Info.plist has $PLIST_VERSION. Set it to $VERSION first." >&2; exit 1; }
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || git ls-remote --exit-code --tags origin "$TAG" >/dev/null; then
  echo "Tag $TAG exists." >&2; exit 1
fi

bash build.sh
URL="https://github.com/$REPO/releases/download/$TAG/Kou-$VERSION-arm64.zip"
python3 package.py --app dist/Kou.app --url "$URL"
ARCHIVE="dist/Kou-$VERSION-arm64.zip"

git tag -a "$TAG" -m "Kou $VERSION"
git push -q origin "$TAG"
gh release create "$TAG" "$ARCHIVE" --repo "$REPO" --title "Kou $VERSION" --generate-notes

TAP="$(mktemp -d)"
trap 'rm -rf "$TAP"' EXIT
gh repo clone "$TAP_REPO" "$TAP" -- -q
mkdir -p "$TAP/Casks"
cp dist/kou.rb "$TAP/Casks/kou.rb"
git -C "$TAP" add Casks/kou.rb
git -C "$TAP" -c user.name="$(git config user.name)" -c user.email="$(git config user.email)" commit -q -m "kou $VERSION"
git -C "$TAP" push -q
echo "Released Kou $VERSION: https://github.com/$REPO/releases/tag/$TAG"
