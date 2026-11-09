#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="${KOU_BUILD_DIR:-$ROOT/dist.noindex}"
APP="$OUT/Kou.app"
IDENTITY="${KOU_SIGN_IDENTITY:-}"
IDENTITY_FILE="$HOME/Library/Application Support/Kou/build-signing-identity"
if [ -z "$IDENTITY" ] && [ -f "$IDENTITY_FILE" ]; then
  IFS= read -r IDENTITY < "$IDENTITY_FILE"
fi
if [ -z "$IDENTITY" ]; then
  IDENTITIES="$(security find-identity -v -p codesigning)"
  COUNT="$(printf '%s\n' "$IDENTITIES" | awk '/^[[:space:]]*[0-9]+\)/ {n++} END {print n+0}')"
  if [ "$COUNT" != 1 ]; then
    echo 'Set KOU_SIGN_IDENTITY to one stable code-signing certificate. Kou will not silently use ad-hoc signing.' >&2
    exit 1
  fi
  IDENTITY="$(printf '%s\n' "$IDENTITIES" | awk '/^[[:space:]]*[0-9]+\)/ {print $2}')"
fi
if [ "$IDENTITY" = - ]; then
  echo 'Ad-hoc signing cannot preserve permissions. Use a stable code-signing certificate.' >&2
  exit 1
fi
ENTITLEMENTS="$ROOT/Resources/Audio.entitlements"
PREVIOUS_REQUIREMENT=""
if [ -d "$APP" ]; then
  PREVIOUS_REQUIREMENT="$(codesign -d -r- "$APP" 2>&1 | sed -n 's/^designated => //p')"
  if [ -z "$PREVIOUS_REQUIREMENT" ]; then
    echo 'Cannot read the existing signing requirement. The existing app was not replaced.' >&2
    exit 1
  fi
fi
mkdir -p "$OUT"
swift build --package-path "$ROOT" -c release
BIN="$(swift build --package-path "$ROOT" -c release --show-bin-path)"
STAGE="$(mktemp -d "$OUT/.kou-build.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/Kou.app/Contents/MacOS" "$STAGE/Kou.app/Contents/Resources"
RES="$STAGE/Kou.app/Contents/Resources"
# SwiftPM records the deployment target as the SDK version; AppKit then keeps
# the pre-Liquid Glass appearance. Record the SDK the binary was built with.
xcrun vtool -set-build-version macos 14.0 "$(xcrun --show-sdk-version)" -replace \
 -output "$STAGE/Kou.app/Contents/MacOS/Kou" "$BIN/Kou"
cp "$ROOT/Resources/Info.plist" "$STAGE/Kou.app/Contents/Info.plist"
cp -R "$ROOT/Resources/zh-Hans.lproj" "$RES/"
cp "$ROOT/Resources/requirements.txt" "$ROOT/Resources/models.json" "$ROOT/Resources/provision.py" "$RES/"
cp "$ROOT/Runtime/zh-voice" "$ROOT/Runtime/zh-voice-daemon" "$RES/"
xcrun swift "$ROOT/Support/icon.swift" "$STAGE/Kou.iconset"
xcrun iconutil -c icns "$STAGE/Kou.iconset" -o "$RES/Kou.icns"
xcrun swiftc -O -target arm64-apple-macos14 -framework AVFoundation -framework CoreML \
  "$ROOT/Support/zh-voice-vad.swift" -o "$RES/zh-voice-audio"
xcrun clang -O2 -Wall -Wextra -Werror -target arm64-apple-macos14 \
  "$ROOT/Support/kou-run.c" -o "$RES/kou-run"
chmod 755 "$RES/zh-voice" "$RES/zh-voice-daemon"
codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$RES/zh-voice-audio"
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$RES/kou-run"
codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$STAGE/Kou.app"
codesign --verify --deep --strict "$STAGE/Kou.app"
REQUIREMENT="$(codesign -d -r- "$STAGE/Kou.app" 2>&1 | sed -n 's/^designated => //p')"
if [ -z "$REQUIREMENT" ]; then
  echo 'Cannot read the new signing requirement. The existing app was not replaced.' >&2
  exit 1
fi
if [ -n "$PREVIOUS_REQUIREMENT" ] && [ "$REQUIREMENT" != "$PREVIOUS_REQUIREMENT" ]; then
  echo 'Signing requirement changed. Reuse the previous certificate to preserve permissions.' >&2
  echo 'Use a separate KOU_BUILD_DIR for a release with a different signing identity.' >&2
  exit 1
fi
plutil -lint "$STAGE/Kou.app/Contents/Info.plist"
if [ ! -f "$IDENTITY_FILE" ]; then
  mkdir -p "${IDENTITY_FILE%/*}"
  chmod 700 "${IDENTITY_FILE%/*}"
  (umask 077; printf '%s\n' "$IDENTITY" > "$IDENTITY_FILE.new")
  mv "$IDENTITY_FILE.new" "$IDENTITY_FILE"
fi
if [ -e "$APP" ]; then mv "$APP" "$STAGE/previous.app"; fi
mv "$STAGE/Kou.app" "$APP"
printf '%s\n' "$APP"
