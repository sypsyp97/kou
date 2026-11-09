<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/island-dark.gif">
    <img src="docs/media/island-light.gif" width="680" alt="Kou 浮层：录音、转写、已粘贴">
  </picture>
</p>
<p align="center"><img src="docs/media/icon.png" width="96" alt="Kou 图标"></p>
<h1 align="center">Kou</h1>
<p align="center">macOS 本地听写。按一个键，说话，文字出现在光标处。</p>
<p align="center"><a href="README.md">English</a></p>

- 语音识别在本机运行，使用 [Qwen3-ASR](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) 和 MLX。音频不离开你的 Mac。
- 支持普通话、英文和中英混说。中文输出为简体字。
- 文字粘贴到当前应用，之后恢复原剪贴板。
- 刘海处的浮层显示录音音量和结果。浮层不抢键盘焦点。

## 系统要求

- Apple 芯片的 Mac
- macOS 14 Sonoma 或更新版本
- [Homebrew](https://brew.sh)
- 3 GB 可用磁盘空间
- 首次设置需要联网

## 安装

### Homebrew

```bash
brew install --cask sypsyp97/tap/kou
```

Homebrew 会同时安装依赖 `uv` 和 `opencc`。

### 手动安装

1. 安装依赖：
   ```bash
   brew install uv opencc
   ```
2. 从 [Releases](https://github.com/sypsyp97/kou/releases) 下载 `Kou-<版本>-arm64.zip`。
3. 解压，把 `Kou.app` 移到 `/Applications`。

### 首次打开

Kou 没有经过 Apple 公证，macOS 会拦下首次打开。运行一次以下命令即可放行：

```bash
xattr -dr com.apple.quarantine /Applications/Kou.app
```

## 设置

1. 打开 Kou，设置窗口会自动弹出。
2. 下载语音模型，最多 3 GB。
3. 允许麦克风权限。
4. 允许辅助功能权限。Kou 用它粘贴文字。
5. 在菜单栏打开听写。

设置完成后，Kou 可离线使用。M1 机型使用 0.6B 模型，其他机型使用 1.7B 模型。在设置中可更换模型。

## 使用

| 快捷键 | 作用 |
|---|---|
| `fn` `control` | 开始听写。再按一次结束并粘贴。 |
| `option` `space` | 同 `fn` `control`。 |
| `⌘` `,` | 打开设置。 |

单次听写最长 2 分钟。当前应用变化时，Kou 不粘贴，文字可在菜单栏菜单中复制。

在“设置 > 词汇”中添加人名和术语，每行一个。

### 命令行

| 命令 | 作用 |
|---|---|
| `kou status` | 显示听写、模型和服务状态。 |
| `kou terms` | 编辑词汇表。 |
| `kou transcribe <文件>` | 转写音频文件。非 WAV 格式需 `brew install sox ffmpeg`。 |
| `kou log` | 显示最近日志。 |
| `kou corpus on\|off` | 转写后保留或删除录音。默认关闭。 |

Homebrew 安装会链接 `kou` 命令。手动安装时，使用 `/Applications/Kou.app/Contents/MacOS/Kou`。

## 隐私

- 按下快捷键后，Kou 才保存音频。
- 每段录音转写后即删除。
- 只有设置期间联网。

## 卸载

```bash
brew uninstall --zap --cask kou
```

手动安装时，删除 `Kou.app`，再运行：

```bash
rm -rf ~/Library/Application\ Support/Kou ~/Library/Caches/Kou
defaults delete io.github.sypsyp97.Kou
```

模型保留在 `~/.cache/huggingface`。没有其他应用使用时可删除。

## 从源码构建

需要 Xcode Command Line Tools 和一张代码签名证书。macOS 把麦克风和辅助功能权限绑定到签名，签名变化后要重新授权。

1. 创建证书：打开“钥匙串访问”，选择“证书助理 > 创建证书”，身份类型选“自签名根证书”，证书类型选“代码签名”。
2. 构建：
   ```bash
   git clone https://github.com/sypsyp97/kou.git && cd kou
   KOU_SIGN_IDENTITY="<证书名称>" bash build.sh
   ```
3. 把 `dist.noindex/Kou.app` 移到 `/Applications`。

## 开发

```bash
swift test
uv run --with-requirements Tests/runtime/requirements-dev.txt python -m pytest -q Tests
bash tla/check.sh            # TLA+ 协议模型，见 tla/README.md
bash Support/render-demo.sh  # 录制本文顶部的动画
bash release.sh <版本>        # 发布新版本并更新 cask
```

## 许可证

[MIT](LICENSE)
