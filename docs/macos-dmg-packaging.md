# macOS DMG 打包流程

本文记录当前 TranslatedSubs 的 macOS 打包方式。构建结果是一个包含原生 Cocoa/WebKit 窗口的 `.app`，再把 `.app` 放进可拖拽安装的 DMG；打开应用后不会调用 Chrome 或 Safari。

## 构建环境

必须在 macOS 上构建，并安装以下工具：

- Xcode Command Line Tools，提供 `clang`、`xcrun` 和系统 WebKit SDK。
- Python 3.10–3.12、`uv`、FFmpeg。
- Python 构建依赖：`PyInstaller`。

```bash
xcode-select --install  # 如果机器尚未安装 Command Line Tools
uv pip install pyinstaller
```

FFmpeg 目录必须同时包含 `ffmpeg` 和 `ffprobe`。发布前确认该版本带有 `libass`，否则硬字幕功能不可用：

```bash
/absolute/path/to/ffmpeg/bin/ffmpeg -filters | grep -E 'ass|subtitles'
```

## 一次构建 `.app` 和 DMG

在仓库根目录执行：

```bash
uv run python scripts/build-desktop.py \
  --ffmpeg-dir /absolute/path/to/ffmpeg/bin \
  --dmg \
  --clean
```

构建脚本会自动完成以下步骤：

1. 用系统 `clang` 编译 `desktop/native_window.m`，生成原生 `WKWebView` 窗口组件。
2. 用 `desktop/translatedsubs.spec` 调用 PyInstaller，把 FastAPI、`web/`、资源和 FFmpeg 打进 `.app`。
3. 在 `build/dmg-staging` 创建 `.app` 和 `Applications` 快捷方式。
4. 用 `hdiutil makehybrid` 和 `hdiutil convert -format UDZO` 生成压缩 DMG。

产物位于：

```text
dist/TranslatedSubs.app
dist/TranslatedSubs-mac-<架构>.dmg
```

`<架构>`由构建机决定：Apple Silicon 通常是 `arm64`，Intel Mac 通常是 `x86_64`。需要同时支持两种架构时，应分别构建后再制作通用包。

## 只重新生成 DMG

如果 `.app` 已经构建完成、只修改了 DMG 内容，可以执行：

```bash
uv run python scripts/build-desktop.py --dmg-only
```

该命令要求 `dist/TranslatedSubs.app` 已存在，不会重新编译或重新打包应用。

## 校验产物

```bash
codesign --verify --deep --strict dist/TranslatedSubs.app
DMG="dist/TranslatedSubs-mac-$(uname -m).dmg"
hdiutil checksum -type CRC32 "$DMG"
tail -c 512 "$DMG" | strings | grep koly
```

也可以直接启动 `.app` 验证窗口行为：

```bash
open dist/TranslatedSubs.app
```

打开后应看到标准 macOS 标题栏、最小化、缩放和关闭按钮；关闭窗口后本地 FastAPI 子进程会退出。当前构建使用 ad-hoc 签名，跨机器正式分发还需要 Apple Developer ID 签名和 notarization。
