<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/island-dark.gif">
    <img src="docs/media/island-light.gif" width="640" alt="Kou overlay: recording, transcribing, pasted">
  </picture>
</p>
<h1 align="center">Kou</h1>
<p align="center">Local dictation for macOS. Press a key, speak, and the text appears at your cursor.</p>
<p align="center"><a href="README.zh-CN.md">简体中文</a></p>

- Speech recognition runs on your Mac with [Qwen3-ASR](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) and MLX. No audio leaves your Mac.
- Kou recognizes Mandarin, English, and speech that mixes the two. Chinese text is in Simplified characters.
- Kou pastes the text into the active app. Then it restores your clipboard.
- An overlay at the notch shows the recording level and the result. The overlay never takes keyboard focus.

## Requirements

- A Mac with Apple silicon
- macOS 14 Sonoma or later
- [Homebrew](https://brew.sh)
- 3 GB of free disk space
- An internet connection for the first setup

## Install

### Homebrew

```bash
brew install --cask sypsyp97/tap/kou
```

Homebrew also installs the dependencies `uv` and `opencc`.

### Manual

1. Install the dependencies:
   ```bash
   brew install uv opencc
   ```
2. Download `Kou-<version>-arm64.zip` from [Releases](https://github.com/sypsyp97/kou/releases).
3. Unzip the file. Move `Kou.app` to `/Applications`.

### First open

Apple did not notarize Kou, so macOS blocks the first open. To allow Kou, run this command once:

```bash
xattr -dr com.apple.quarantine /Applications/Kou.app
```

## Set up

1. Open Kou. A setup window opens.
2. Download the speech model. The download is up to 3 GB.
3. Allow Microphone access.
4. Allow Accessibility access. Kou uses it to paste.
5. Turn on dictation in the menu bar.

After setup, Kou works offline. Macs with M1 use the 0.6B model. Other Macs use the 1.7B model. To change the model, open Settings.

## Use

| Shortcut | Action |
|---|---|
| `fn` `control` | Start dictation. Press again to stop and paste. |
| `option` `space` | Same as `fn` `control`. |
| `⌘` `,` | Open Settings. |

A dictation stops after 2 minutes. If the active app changes, Kou does not paste. The text stays available in the menu bar menu.

To add names and technical terms, open Settings > Vocabulary. Write one term on each line.

### Command line

| Command | Action |
|---|---|
| `kou status` | Show the dictation, model, and service status. |
| `kou terms` | Edit the vocabulary. |
| `kou transcribe <file>` | Transcribe an audio file. Formats other than WAV need `brew install sox ffmpeg`. |
| `kou log` | Show recent log entries. |
| `kou corpus on\|off` | Keep or delete recordings after transcription. The default is off. |

Homebrew links the `kou` command. For a manual install, use `/Applications/Kou.app/Contents/MacOS/Kou`.

## Privacy

- Kou saves audio only after you press the shortcut.
- Kou deletes each recording after transcription.
- Kou connects to the internet only during setup.

## Uninstall

```bash
brew uninstall --zap --cask kou
```

For a manual install, delete `Kou.app` and run:

```bash
rm -rf ~/Library/Application\ Support/Kou ~/Library/Caches/Kou
defaults delete io.github.sypsyp97.Kou
```

The model stays in `~/.cache/huggingface`. Delete it if no other app uses it.

## Build from source

You need the Xcode Command Line Tools and one code-signing certificate. macOS links the Microphone and Accessibility permissions to the signature. If the signature changes, you must allow the permissions again.

1. To make a certificate, open Keychain Access. Select Certificate Assistant > Create a Certificate. Set Identity Type to Self-Signed Root and Certificate Type to Code Signing.
2. Build:
   ```bash
   git clone https://github.com/sypsyp97/kou.git && cd kou
   KOU_SIGN_IDENTITY="<certificate name>" bash build.sh
   ```
3. Move `dist.noindex/Kou.app` to `/Applications`.

## Development

```bash
swift test
uv run --with-requirements Tests/runtime/requirements-dev.txt python -m pytest -q Tests
bash tla/check.sh            # TLA+ protocol models, see tla/README.md
bash Support/render-demo.sh  # record the animation in this README
bash release.sh <version>    # publish a release and update the cask
```

## License

[MIT](LICENSE)
