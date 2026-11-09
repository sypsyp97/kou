<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/island-dark.gif">
    <img src="docs/media/island-light.gif" width="520" alt="Kou notch overlay: recording, transcribing, pasted">
  </picture>
</p>
<h1 align="center">Kou</h1>
<p align="center">macOS 本地语音听写。按一个键，开口说话，文字会出现在光标处。</p>
<p align="center"><a href="README.md">English</a></p>

## 功能

- 按 `fn` + `control` 开始听写。再按一次停止。Kou 会把文字粘贴到光标处。
- 语音转文字在你的 Mac 上运行。它使用语音识别模型 Qwen3-ASR，通过 Apple 的机器学习框架 MLX 运行。不使用云服务。完成设置后，Kou 可以离线工作。
- 文字粘贴到当前有键盘焦点的应用，之后恢复你原来的剪贴板内容。
- 屏幕顶部刘海区域的覆盖层会显示麦克风音量、进度和结果。它不会抢走键盘焦点。
- Kou 识别普通话、英文和中英混说，中文输出为简体字。你可以添加自定义词汇。

## 系统要求

- Apple Silicon Mac，即搭载 Apple 芯片的 Mac，例如 M1 或更新的型号。
- macOS 14 Sonoma 或更新版本。
- [Homebrew](https://brew.sh)，macOS 的软件包管理器。
- `uv`（Python 包管理工具）和 `opencc`（中文繁简转换工具）。两者都需要安装：

```bash
brew install uv opencc
```

- 约 3 GB 可用磁盘空间。1.7B 模型需要 2.3 GB，Python 环境约需 0.3 GB。0.6B 模型更小。
- 只有首次设置需要联网。

## 安装

### Homebrew

1. 安装 cask：

```bash
brew install --cask sypsyp97/tap/kou
```

cask 是 Homebrew 中用于安装 macOS 应用的软件包。它还会安装 `uv` 和 `opencc`，并链接 `kou` 命令。

### 下载安装包

1. 打开 [GitHub Releases 页面](https://github.com/sypsyp97/kou/releases)，下载 `Kou-<version>-arm64.zip`。
2. 解压该文件。
3. 把 `Kou.app` 移到 `/Applications` 或 `~/Applications`。
4. 打开一次 `Kou.app`。macOS 会阻止首次启动。请使用以下方法之一：

- 按住 Control 键点按 `Kou.app`，选择**打开**，然后确认。
- 或在终端中运行以下命令：

```bash
xattr -dr com.apple.quarantine /Applications/Kou.app
```

Kou 使用自签名证书签名。Apple 没有对它做公证。公证是 Apple 对应用的检查和批准。本项目没有 Apple Developer ID，因此 macOS 默认不信任该应用。`xattr` 命令会删除 macOS 为下载文件设置的隔离标记（quarantine）。

### 从源码构建

1. 安装 Xcode Command Line Tools：

```bash
xcode-select --install
```

2. 安装运行依赖：

```bash
brew install uv opencc
```

3. 克隆仓库并进入目录：

```bash
git clone https://github.com/sypsyp97/kou.git
cd kou
```

4. 构建应用：

```bash
bash build.sh
```

构建结果是 `dist/Kou.app`。

#### 代码签名

构建需要一个稳定的代码签名证书。构建脚本拒绝 ad-hoc 签名，即不使用证书的签名。第一次构建时，脚本会选择唯一有效的代码签名证书。如果有多个有效证书，必须设置 `KOU_SIGN_IDENTITY`。之后的构建会复用已保存的身份。

macOS 把麦克风和辅助功能权限绑定到应用的签名上。签名变了，就必须重新授权。构建脚本在替换已有应用前，会拒绝签名要求发生变化的构建。要在重新构建后保留权限，请保持同一证书和同一 bundle identifier（应用的唯一标识符）。

如果没有证书，请创建一个：

1. 打开「钥匙串访问」（Keychain Access）。选择「证书助理」（Certificate Assistant）>「创建证书」。
2. 将「名称」设为 `Kou Local Signing`。
3. 将「身份类型」设为 `Self-Signed Root`。
4. 将「证书类型」设为 `Code Signing`。
5. 使用证书名称构建：

```bash
KOU_SIGN_IDENTITY="Kou Local Signing" bash build.sh
```

授权权限前，请先把应用移到 `/Applications`。

## 首次启动

1. 从 `/Applications` 打开 Kou。Kou 没有 Dock 图标。设置窗口会打开，共三个步骤。
2. 下载语音模型。M1 Mac 默认使用 Qwen3-ASR 0.6B（8-bit）。其他 Apple Silicon Mac 默认使用 Qwen3-ASR 1.7B（8-bit）。之后可以在「设置」>「通用」中更改模型。
3. 允许麦克风访问。
4. 允许辅助功能访问。Kou 需要它来粘贴文字。
5. 从菜单栏图标开启听写。

设置会创建一个独立的 Python 3.13 环境，并安装锁定版本的依赖，包括 `mlx-qwen3-asr` 0.4.4。然后，它会从 Hugging Face（模型托管服务）下载固定版本的快照（即固定版本的模型文件），包括语音模型和 Silero VAD。Silero VAD 是语音活动检测（VAD）模型，用来判断语音的开始和结束。Hugging Face 缓存与这台 Mac 上的其他工具共用。设置完成后，听写离线运行。

## 使用

| 操作 | 快捷键 |
|---|---|
| 开始听写。再按一次停止并粘贴。 | `fn` + `control` |
| 备用快捷键，行为相同 | `Option` + `Space` |

快捷键有 0.4 秒的 debounce（防抖，即忽略短时间内的重复按键事件）。单次听写最长 2 分钟（120 秒），到时自动停止。

菜单栏图标默认显示。你可以在「设置」>「通用」中隐藏它。菜单可以开启或关闭听写，也可以复制上一次的转写结果。Kou 只在内存中保留上一次的转写结果。

「设置」（⌘,）有两个面板：

- **通用**：语音模型、权限、登录时启动（通过 macOS 登录项接口 `SMAppService`）、菜单栏图标和界面语言。界面默认跟随系统语言。
- **词汇**：每行一个词。词条可以包含空格。个人词汇不会随应用一起发布。你也可以运行 `kou terms`。

## 命令行

cask 会把应用的可执行文件链接为 `kou` 命令。运行 `kou help` 查看完整列表。

- `kou`：打开应用。
- `kou status`：查看状态。
- `kou terms`：编辑词汇。
- `kou transcribe <audio>`：转写一个音频文件。应用必须已完成设置且正在运行。WAV 以外的格式需要 `sox` 和 `ffmpeg`。听写功能不需要它们。
- `kou log`：查看日志。
- `kou corpus on|off`：开启或关闭语料收集。

```bash
brew install sox ffmpeg
kou transcribe clip.wav
```

语料（corpus）是为后续使用而保存的一组样本。语料收集默认关闭。开启后，Kou 会把裁剪后的音频、转写文本和上下文保存在 `~/Library/Application Support/Kou/corpus-data`。关闭后，Kou 停止保存新数据，已有数据仍保留在磁盘上。

## 隐私

- 音频不会离开这台 Mac。
- 录音是临时文件，保存在 `~/Library/Caches/Kou`。转写完成后，以及 Kou 退出时，Kou 会删除它们。

Kou 按以下步骤粘贴文字：

1. 保存所有剪贴板类型。
2. 把文字写入剪贴板。
3. 检查目标应用仍是最前面的应用，并且 Kou 仍拥有剪贴板。只把 ⌘V 发送到目标应用。如果焦点或剪贴板发生变化，取消粘贴。
4. 1.2 秒后，如果 Kou 仍拥有剪贴板，就恢复原来的剪贴板内容。

macOS 会分别执行这些操作，因此两步之间仍可能发生变化。

数据保存在以下位置：

- `~/Library/Application Support/Kou`：设置、词汇、Python 环境、日志和语料数据。
- `~/Library/Caches/Kou`：临时音频和 socket。
- `~/.cache/huggingface`：下载的模型，与其他工具共用。

## 卸载

1. 退出 Kou。
2. 从 `/Applications` 或 `~/Applications` 删除 `Kou.app`。
3. 删除数据目录和缓存目录：

```bash
rm -rf ~/Library/Application\ Support/Kou ~/Library/Caches/Kou
```

4. 删除已保存的偏好设置：

```bash
defaults delete io.github.sypsyp97.Kou
```

5. 如果你通过 Homebrew 安装，请移除 cask 及其数据：

```bash
brew uninstall --zap --cask kou
```

Hugging Face 模型缓存会保留在 `~/.cache/huggingface`。如果其他工具不使用 Kou 下载的两个模型目录，请删除它们。

## 故障排查

**快捷键没有反应**

请在「系统设置」>「隐私与安全性」中检查「麦克风」和「辅助功能」。同时检查菜单栏菜单中听写是否已开启。

**重新构建后权限丢失**

代码签名证书已经改变。请使用同一证书构建。参见「代码签名」。

**显示 `Model is not ready`**

等待模型加载完成，或运行 `kou status`。

**文字没有被粘贴**

听写期间焦点发生了变化，因此 Kou 取消了粘贴。文字仍然保留。请从菜单栏菜单中复制。

**出现故障，需要详细信息**

运行 `kou log`。

## 工作原理

本节面向贡献者。

- 应用使用 Swift 编写，并使用 Apple 的界面框架 AppKit 和 SwiftUI。
- 应用会启动并监控一个常驻的 Python 自动语音识别（ASR）服务，即 `Runtime/zh-voice-daemon`。该服务在后台持续运行。应用通过私有 Unix socket（同一台 Mac 上进程之间的本地连接）与它通信。
- ASR 服务作为子进程运行，并由 watchdog（监控进程，负责在失败时重启）管理。如果服务不可用，Kou 会在服务恢复后重试一次。Kou 不会启动第二个模型。
- 录音和语音活动检测辅助程序是 `Support/zh-voice-vad.swift`。它使用 Core ML（Apple 的设备端模型框架）格式的 Silero VAD 模型。
- 进程监管程序 `kou-run`（`Support/kou-run.c`）会停止整个进程组。进程组是一个进程及其所有子进程。
- `tla/` 目录包含协议的 TLA+ 模型。TLA+ 是一种形式化规约语言。[模型说明](tla/README.md)描述了覆盖范围和系统限制。运行 `bash tla/check.sh` 可执行有界协议检查和防护变异测试。防护变异是故意破坏某项防护条件，用来确认测试能发现它。

## 开发

```bash
swift build
swift test
uv run --with-requirements Tests/runtime/requirements-dev.txt python -m pytest -q Tests
bash tla/check.sh
bash Support/render-demo.sh
```

`bash Support/render-demo.sh` 会重新生成 README 中的动图。它需要 `ffmpeg`（音视频处理工具），并需要为终端授予屏幕录制权限。它录制的是屏幕外的窗口。

## 发布

本节面向维护者。

如需使用单独的发布身份，请设置 `KOU_SIGN_IDENTITY`，并用 `KOU_BUILD_DIR` 指定一个干净的构建目录。对已具有 Developer ID 签名的应用进行公证：

```bash
KOU_NOTARY_PROFILE=profile bash notarize.sh /path/to/Kou.app
python3 package.py --app /path/to/Kou.app --url HTTPS_FINAL_ARCHIVE_URL
```

公证脚本需要 Developer ID 签名。Developer ID 是 Apple 为在 Mac App Store 之外分发应用而提供的签名身份。脚本会把公证凭证（即 Apple 的批准记录）附加到应用上，然后评估应用状态。它不会对应用签名。

打包脚本会在提供的应用旁边生成带版本号的 arm64 zip 文件和 `kou.rb`。`kou.rb` 使用归档文件的真实 SHA256 校验值。默认 cask 路径是 `dist/kou.rb`。cask 依赖 `uv` 和 `opencc`，并链接 `kou` 命令。请上传归档文件，并把生成的 cask 单独放入 tap（Homebrew 软件包仓库）。

签名构建包含 Apple 的 [audio-input entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.device.audio-input)（音频输入权限声明），即应用在签名中声明的一项权限。

## 许可证

许可证：待定。
