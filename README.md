<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/island-dark.gif">
    <img src="docs/media/island-light.gif" width="520" alt="Kou notch overlay: recording, transcribing, pasted">
  </picture>
</p>
<h1 align="center">Kou</h1>
<p align="center">Local dictation for macOS. Press a key, speak, and the text appears at your cursor.</p>
<p align="center"><a href="README.zh-CN.md">简体中文</a></p>

## What it does

- Press `fn` + `control` to start dictation. Press them again to stop. Kou pastes the text at the cursor.
- Speech-to-text runs on your Mac. It uses Qwen3-ASR, a speech recognition model, through MLX, Apple's machine learning framework. No cloud service is used. After setup, Kou works offline.
- Kou pastes into the app that has keyboard focus, then restores what was on your clipboard.
- The notch overlay, at the top of the screen, shows microphone level, progress, and the result. It never takes keyboard focus.
- Kou recognizes Mandarin Chinese, English, and speech that mixes both. Chinese text comes out in Simplified characters. You can add custom vocabulary terms.

## Requirements

- An Apple Silicon Mac, which is a Mac with an Apple chip such as M1 or later.
- macOS 14 Sonoma or later.
- [Homebrew](https://brew.sh), the macOS package manager.
- `uv`, a Python package manager, and `opencc`, a Chinese text converter. Install both:

```bash
brew install uv opencc
```

- About 3 GB of free disk space. The 1.7B model needs 2.3 GB, and the Python environment needs about 0.3 GB. The 0.6B model is smaller.
- An internet connection for the first setup only.

## Install

### Homebrew

1. Install the cask:

```bash
brew install --cask sypsyp97/tap/kou
```

A cask is a Homebrew package for a macOS app. The cask also installs `uv` and `opencc`, and links the `kou` command.

### Download

1. Open the [GitHub Releases page](https://github.com/sypsyp97/kou/releases). Download `Kou-<version>-arm64.zip`.
2. Unzip the file.
3. Move `Kou.app` to `/Applications` or `~/Applications`.
4. Open `Kou.app` once. macOS blocks the first launch. Use one of these methods:

- Control-click `Kou.app`, choose **Open**, and confirm.
- Or run this command in Terminal:

```bash
xattr -dr com.apple.quarantine /Applications/Kou.app
```

Kou is signed with a self-signed certificate. Apple has not notarized it. Notarization is Apple's check of an app. The project has no Apple Developer ID, so macOS does not trust the app by default. The `xattr` command removes the quarantine flag that macOS sets on downloaded files.

### Build from source

1. Install the Xcode Command Line Tools:

```bash
xcode-select --install
```

2. Install the runtime dependencies:

```bash
brew install uv opencc
```

3. Clone the repository and open it:

```bash
git clone https://github.com/sypsyp97/kou.git
cd kou
```

4. Build the app:

```bash
bash build.sh
```

The build creates `dist/Kou.app`.

#### Code signing

The build needs one stable code-signing certificate. It refuses ad-hoc signing, which means signing without a certificate. On the first build, it selects the only valid code-signing certificate. If more than one is valid, set `KOU_SIGN_IDENTITY`. The build reuses the saved identity on later builds.

macOS ties the Microphone and Accessibility permissions to the app's signature. A new signature means that you must grant the permissions again. The build rejects a changed signing requirement before it replaces an existing app. Keep the same certificate and bundle identifier to keep permissions across rebuilds.

If you do not have a certificate, create one:

1. Open Keychain Access. Choose Certificate Assistant > Create a Certificate.
2. Set Name to `Kou Local Signing`.
3. Set Identity Type to `Self-Signed Root`.
4. Set Certificate Type to `Code Signing`.
5. Build with the certificate name:

```bash
KOU_SIGN_IDENTITY="Kou Local Signing" bash build.sh
```

Move the app to `/Applications` before you grant permissions.

## First launch

1. Open Kou from `/Applications`. Kou has no Dock icon. A setup window opens with three steps.
2. Download the speech model. M1 Macs use Qwen3-ASR 0.6B (8-bit) by default. Other Apple Silicon Macs use Qwen3-ASR 1.7B (8-bit). You can change the model later in Settings > General.
3. Allow Microphone access.
4. Allow Accessibility access. Kou needs it to paste text.
5. Turn dictation on from the menu bar icon.

Setup creates a private Python 3.13 environment with locked dependencies, including `mlx-qwen3-asr` 0.4.4. It then downloads pinned snapshots, which are fixed model versions, for the speech model and for Silero VAD. Silero VAD is a voice activity detection (VAD) model. VAD finds where speech starts and stops. Downloads come from Hugging Face, a model hosting service. The Hugging Face cache is shared with other tools on this Mac. After setup, dictation runs offline.

## Use

| Action | Shortcut |
|---|---|
| Start dictation. Press again to stop and paste. | `fn` + `control` |
| Alternate shortcut, same behavior | `Option` + `Space` |

The shortcut has a 0.4 s debounce, which is a short wait that ignores repeated key events. A dictation stops automatically after 2 minutes (120 seconds).

The menu bar icon is on by default. You can hide it in Settings > General. Its menu turns dictation on or off and copies the last transcript. Kou keeps the last transcript in memory only.

Settings (⌘,) has two panes:

- **General**: speech model, permissions, launch at login (through `SMAppService`, the macOS login item API), menu bar icon, and interface language. The interface follows the system language by default.
- **Vocabulary**: one term per line. A term can contain spaces. Personal terms are never bundled with the app. You can also run `kou terms`.

## Command line

The cask links the app binary as `kou`. Run `kou help` for the full list.

- `kou`: open the app.
- `kou status`: show the status.
- `kou terms`: edit the vocabulary.
- `kou transcribe <audio>`: transcribe an audio file. The app must be set up and running. Formats other than WAV need `sox` and `ffmpeg`. Dictation does not need them.
- `kou log`: show the log.
- `kou corpus on|off`: turn corpus collection on or off.

```bash
brew install sox ffmpeg
kou transcribe clip.wav
```

A corpus is a saved set of samples for later use. Corpus collection is off by default. When it is on, Kou saves trimmed audio, transcripts, and context in `~/Library/Application Support/Kou/corpus-data`. Turning it off stops new saves. Existing data stays on disk.

## Privacy

- Audio never leaves the Mac.
- Recordings are temporary files in `~/Library/Caches/Kou`. Kou deletes them after transcription and when it quits.

Kou pastes text in these steps:

1. Save all clipboard types.
2. Write the text to the clipboard.
3. Check that the target app is still the frontmost app and that Kou still owns the clipboard. Send ⌘V to the target app only. If focus or the clipboard changed, cancel the paste.
4. After 1.2 seconds, restore the original clipboard if Kou still owns it.

macOS performs these steps separately, so a change between two steps is still possible.

Data is stored in these places:

- `~/Library/Application Support/Kou`: settings, vocabulary, the Python environment, the log, and corpus data.
- `~/Library/Caches/Kou`: temporary audio and the socket.
- `~/.cache/huggingface`: downloaded models, shared with other tools.

## Uninstall

1. Quit Kou.
2. Delete `Kou.app` from `/Applications` or `~/Applications`.
3. Delete the data and cache folders:

```bash
rm -rf ~/Library/Application\ Support/Kou ~/Library/Caches/Kou
```

4. Delete the saved preferences:

```bash
defaults delete io.github.sypsyp97.Kou
```

5. If you installed with Homebrew, remove the cask and its data:

```bash
brew uninstall --zap --cask kou
```

The Hugging Face model cache stays in `~/.cache/huggingface`. If no other tool uses the two model folders that Kou downloaded, delete them.

## Troubleshooting

**The shortcut does nothing**

Check Microphone and Accessibility in System Settings > Privacy & Security. Check that dictation is on in the menu bar menu.

**Permissions are lost after a rebuild**

The signing certificate changed. Build with the same certificate. See Code signing.

**The menu shows "Model is not ready"**

Wait for the model to load. Or run `kou status`.

**The text was not pasted**

The focus changed during dictation, so Kou cancelled the paste. The text is kept. Copy it from the menu bar menu.

**Something failed and you need details**

Run `kou log`.

## How it works

This section is for contributors.

- The app is written in Swift. It uses AppKit and SwiftUI, Apple's UI frameworks.
- The app starts and monitors a resident Python automatic speech recognition (ASR) server, `Runtime/zh-voice-daemon`. The server keeps running in the background. The app talks to it over a private Unix socket, which is a local connection between processes on the same Mac.
- The ASR server runs as a subprocess under a watchdog, a monitor that restarts a failed process. If the service is unavailable, Kou retries once after it recovers. Kou does not start a second model.
- The recorder and voice activity detection helper is `Support/zh-voice-vad.swift`. It uses the Silero VAD model in Core ML, Apple's on-device model framework.
- The process supervisor `kou-run` (`Support/kou-run.c`) stops whole process groups. A process group is a process and all of its child processes.
- `tla/` holds TLA+ models of the protocols. TLA+ is a formal specification language. The [model notes](tla/README.md) describe coverage and system limits. Run `bash tla/check.sh` for bounded protocol checks and guard mutations. A guard mutation is a deliberately broken check that the test must catch.

## Development

```bash
swift build
swift test
uv run --with-requirements Tests/runtime/requirements-dev.txt python -m pytest -q Tests
bash tla/check.sh
bash Support/render-demo.sh
```

`bash Support/render-demo.sh` regenerates the README animation. It needs `ffmpeg`, a media tool, and Screen Recording permission for your terminal. It records an off-screen window.

## Release

This section is for maintainers.

For a separate release identity, set `KOU_SIGN_IDENTITY` and a clean build directory with `KOU_BUILD_DIR`. Notarize an app that already has a Developer ID signature:

```bash
KOU_NOTARY_PROFILE=profile bash notarize.sh /path/to/Kou.app
python3 package.py --app /path/to/Kou.app --url HTTPS_FINAL_ARCHIVE_URL
```

The notarization script needs a Developer ID signature. A Developer ID is Apple's signing identity for apps distributed outside the Mac App Store. The script staples the notarization ticket, which is Apple's approval record, to the app. It then assesses the app. It does not sign the app.

Packaging creates a versioned arm64 zip and `kou.rb` beside the supplied app. The `kou.rb` cask uses the actual SHA256 checksum of the archive. The default cask path is `dist/kou.rb`. The cask depends on `uv` and `opencc`, and links the `kou` command. Upload the archive. Put the generated cask in the tap, which is a Homebrew repository of packages, as a separate step.

Signed builds include Apple's [audio-input entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.device.audio-input), which is a permission that the app declares in its signature.

## License

License: to be decided.
