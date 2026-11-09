#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="${1:-$ROOT/dist/Kou.app}"
: "${KOU_NOTARY_PROFILE:?Set KOU_NOTARY_PROFILE to a notarytool keychain profile}"
codesign --verify --deep --strict "$APP"
TEMP="$(mktemp -d)"
trap 'rm -rf "$TEMP"' EXIT
ditto -c -k --keepParent "$APP" "$TEMP/Kou.zip"
xcrun notarytool submit "$TEMP/Kou.zip" --keychain-profile "$KOU_NOTARY_PROFILE" --wait
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
spctl --assess --type execute --verbose "$APP"
