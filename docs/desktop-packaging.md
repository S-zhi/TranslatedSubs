# Desktop packaging

完整的 macOS DMG 构建命令和校验步骤见：[macOS DMG 打包流程](macos-dmg-packaging.md)。

桌面包会启动本机 FastAPI，并在应用自己的 macOS 窗口中加载现有 `web/` 前端。窗口由系统 WebKit（`WKWebView`）负责，所以用户看到的是带标题栏、最小化、缩放和关闭按钮的正常应用窗口；不需要把前端重写成 React、Flutter 或原生 UI。关闭窗口后，后台 FastAPI 子进程也会退出。

## 本地验证

从仓库根目录运行：

```bash
uv run python -m desktop.launcher --no-browser
```

这条命令只启动服务，适合调试 API。源码模式调试页面时使用外部浏览器：

```bash
uv run python -m desktop.launcher --window-mode browser --port 8123
```

打包后的 `.app` 默认使用原生窗口；源码模式需要预先提供原生窗口组件，调试页面时可以显式使用 `--window-mode browser`。

数据默认写入当前系统的用户数据目录：

- Windows：`%LOCALAPPDATA%\\TranslatedSubs`
- macOS：`~/Library/Application Support/TranslatedSubs`
- Linux：`$XDG_DATA_HOME/TranslatedSubs` 或 `~/.local/share/TranslatedSubs`

## 构建 PyInstaller bundle

macOS 构建机需要安装 Xcode Command Line Tools（提供 `clang`、`xcrun` 和系统 WebKit）以及 PyInstaller。发布包建议把带 `libass` 的 `ffmpeg` 和 `ffprobe` 放在同一目录：

```bash
uv pip install pyinstaller
uv run python scripts/build-desktop.py \\
  --ffmpeg-dir /path/to/ffmpeg/bin \\
  --dmg \\
  --clean
```

Windows PowerShell：

```powershell
uv pip install pyinstaller
$env:SUBTRANS_FFMPEG_DIR = 'C:\\ffmpeg\\bin'
uv run python scripts/build-desktop.py --clean
```

macOS 构建结果位于 `dist/TranslatedSubs.app` 和 `dist/TranslatedSubs-mac-<架构>.dmg`。DMG 中带有 `Applications` 快捷方式，用户可以把应用拖入应用程序目录。Windows 构建结果位于 `dist/TranslatedSubs/TranslatedSubs.exe`。

如果 `.app` 已经存在，只重新生成 DMG：

```bash
uv run python scripts/build-desktop.py --dmg-only
```

如果没有传入 `--ffmpeg-dir`，启动器仍会构建，但会依赖用户机器的 `PATH`。这种包不能保证硬字幕功能可用。

## 资源与配置规则

- 安装目录只读，任务产物、SQLite、运行时设置和日志都放在用户数据目录。
- `.env`、Replicate/DeepSeek 等密钥不打进安装包，由用户在设置或用户目录配置。
- 启动器只监听 `127.0.0.1`，不把本地服务暴露到局域网。
- 本地 Whisper 或翻译模型不默认塞入安装包；模型缓存应放到用户数据目录，避免安装包体积失控。
- 基础桌面包会排除 `torch`、`transformers` 等可选本地模型依赖；需要离线本地模型时再单独构建扩展包。

