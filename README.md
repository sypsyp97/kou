# Kou

Kou is a native macOS dictation app for Apple Silicon. It records speech from a global shortcut, transcribes it locally, and pastes the result at the cursor. It requires macOS 14 or later and does not depend on Hammerspoon.

## Build and setup

Install the Xcode command line tools, then install the runtime dependencies:

```bash
brew install uv opencc
bash build.sh
```

The build creates `dist/Kou.app`. It reuses a saved signing identity. On the first build, it selects the only valid code-signing certificate, or requires `KOU_SIGN_IDENTITY` if the choice is ambiguous. It rejects a changed signing requirement before replacing an existing app. Keep the same certificate and bundle identifier to preserve permissions across rebuilds.

Move the app to Applications before granting permissions or enabling launch at login. On first launch, Kou opens a setup window with three steps: download the speech model, allow Microphone access, and allow Accessibility access. Setup creates a dedicated Python 3.13 environment with locked dependencies, including `mlx-qwen3-asr` 0.4.4. It downloads pinned ASR and Silero VAD snapshots. Hugging Face caches are shared with other tools on this Mac. Runtime inference runs offline.

M1 defaults to Qwen3-ASR 0.6B (8-bit). Other Apple Silicon defaults to Qwen3-ASR 1.7B (8-bit). You can change the model in Settings > General. The interface follows the system language: Simplified Chinese or English.

If the legacy voice-input heartbeat is active, pause that system before enabling Kou. Both systems use the same primary shortcut.

## Dictation

Press `fn` + `control` to start recording. Press again to stop and paste. `Option` + `Space` is an alternate shortcut. The recording toggle has a 0.4 s debounce; recording stops automatically after 120 seconds.

The click-through notch overlay shows microphone levels, transcription, and completion without taking keyboard focus. Kou has no Dock icon. The menu bar icon is on by default; its menu turns dictation on or off and copies the last transcript. Settings (⌘,) has two panes: General and Vocabulary. Settings controls launch at login through `SMAppService`.

Kou saves all materialized clipboard types and restores them after 1.2 seconds while it still owns the clipboard. It checks ownership and the target app before sending paste keys directly to that app. A detected change cancels paste. The menu bar menu keeps the last transcript in memory for manual copying. macOS performs these operations separately, so changes between system calls remain possible.

## Terms and recordings

Edit vocabulary in Settings > Vocabulary, or run `kou terms`. Personal terms are never bundled.

Corpus collection is off by default; turn it on or off with `kou corpus on|off`. Enabling it saves trimmed audio, transcripts, and context locally. Disabling it stops new collection and keeps existing data. Other temporary audio is deleted after use and at shutdown.

User data lives in `~/Library/Application Support/Kou`; temporary files live in `~/Library/Caches/Kou`. The app owns its ASR server, which runs as a subprocess under a watchdog and uses a private socket. An unavailable service triggers one retry after recovery without starting a second model.

## Command line

The cask links the app binary as `kou`. Run `kou help` for the commands: `status`, `terms`, `transcribe <audio>`, `log`, and `corpus on|off`. With no command, `kou` opens the app.

## File transcription and checks

With the configured app running:

```bash
brew install sox ffmpeg
kou transcribe clip.wav
swift test
uv run --with pytest python -m pytest -q Tests
```

Arbitrary file formats require `sox` and `ffmpeg`. The app's microphone path does not need them.

Run `bash tla/check.sh` for bounded protocol checks and guard mutations. The [model notes](tla/README.md) describe coverage and system limits.

## Distribution

For a separate release identity, set `KOU_SIGN_IDENTITY` and a clean `KOU_BUILD_DIR`. Notarize an already Developer ID-signed app:

```bash
KOU_NOTARY_PROFILE=profile bash notarize.sh /path/to/Kou.app
python3 package.py --app /path/to/Kou.app --url HTTPS_FINAL_ARCHIVE_URL
```

The notarization script staples and assesses the app; it does not sign it. Packaging creates a versioned arm64 zip and `kou.rb` beside the supplied app, with the archive's actual SHA256. The default cask path is `dist/kou.rb`. It depends on `uv` and `opencc` and links the `kou` command. Upload the archive and put the generated cask in the tap separately.

Kou is not yet publicly published or notarized. Signed builds include Apple's [audio-input entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.device.audio-input).
