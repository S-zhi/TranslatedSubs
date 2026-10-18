# macOS 桌面安装与开发指南

状态：开发中，2026-10-17。**目前没有可下载的已签名、公证桌面正式版，也没有已验证的公开更新渠道。** 本文区分正式版预期体验和当前源码开发步骤；构建成功、单测通过都不代表已经完成发行验收。范围以[产品约定](./macos-desktop-v1-product-brief.md)和[实施契约](./macos-desktop-v1-implementation.md)为准。

## 普通用户：正式版发布后的流程

首版面向 Apple Silicon（M 系列）Mac。Intel、Windows 原生桌面版不在本次交付范围；现有服务端仍可按[本地快速启动](./local-quick-start.md)运行。最低 macOS 支持版本尚未验收：源码暂设 13.0 部署目标，这不等于所有依赖或 macOS 13 已通过兼容性测试。安装包大小和所需可用空间也须以最终发行包实测为准。

以下是正式版发布后应能完成的步骤，当前没有可供下载的发行包：

1. 从项目正式公布的下载页获得 Apple Silicon DMG，打开后把 `TranslateSub.app` 拖入“应用程序”，再从该目录启动。正式包必须完成开发者签名、公证和 Gatekeeper 验证；不要通过关闭 Gatekeeper 或删除隔离属性处理安装错误。
2. 首次打开等待应用自动准备转写、翻译模型。下载、转换、校验是不同阶段；总大小未知时只能显示已下载量和状态，不能据此估算完成百分比。准备失败后按提示检查网络、磁盘空间并重试；准备完成后已有模型不重复下载。
3. 粘贴 YouTube 公开视频链接或选择本地英文视频。默认输出英文与简体中文双语硬字幕 MP4，同时保留可单独导出的字幕文件。硬字幕已压入画面，播放器不能关闭；不配音。字幕由机器生成，专业术语和重要内容请人工核对。
4. 在任务中查看进度、预览结果，通过保存对话框导出视频或字幕。更新提示只显示新版本、更新说明和新版 DMG 下载入口，交由系统浏览器下载、用户安装，不会在应用内自动替换、重启或中断任务。

“零配置”指正式版用户无需安装 Python、Redis、FFmpeg，无需使用命令行或填写云端 API Key；不代表首次无需网络、无需等待。模型准备完成后，首版本地转写、翻译和字幕压制不依赖云端推理服务。YouTube 下载、模型准备和更新检查仍使用网络；本地推理不等于整个应用永不联网。应用不会改变用户所在网络对 YouTube 的访问条件；链接失败可重试或改用有权处理的本地视频，国内外可达性仍待分别验收。

## 窗口、退出与数据

关闭最后一个窗口按设计继续后台处理，可通过菜单栏重新打开；主动“退出”时若有活动任务，应提示确认，确认后停止任务并回收应用拥有的后端进程。重新打开保留任务记录和已完成结果，中断任务提供重试，**不保证从中断时间点或阶段续处理**。系统睡眠、异常退出及长任务后台执行还需真实设备验证，不承诺睡眠期间持续处理。

桌面数据默认位于 `~/Library/Application Support/TranslateSub`，不写入 `.app` 包。桌面配置使用 SQLite，现有服务端配置继续使用 Redis；桌面启动不要求编辑 `config.local.json`，也不自动导入原服务端配置。替换应用前先正常退出并备份数据；卸载应用包与删除任务、模型数据是两件事，不要把整个数据目录当临时文件清理。需要备份时先停止应用，再复制完整目录，避免只复制正在使用的 SQLite 主文件。

模型下载失败时记录失败阶段、应用版本和不含凭据的错误信息；转换或校验失败不能当作下载完成。磁盘不足时先释放空间再重试。更新显示“未配置更新渠道”表示当前开发构建没有渠道，不能解释为已是最新版本。后端启动失败或缺失 FFmpeg 时，开发者检查构建产物；普通用户应等待完整发行包，不需要自行寻找二进制文件。

## 开发者：从本工作树构建

以下命令在仓库根目录执行，仅面向开发者。需要 Apple Silicon 开发机、能编译 Swift 5.10 包的 Xcode／Swift 工具链、`uv`，以及 ARM64 FFmpeg/FFprobe 与所需动态库。业务源码支持 Python 3.10–3.12，当前打包驱动要求 **Python 3.11 或 3.12**，建议使用 3.12。硬字幕要求 FFmpeg 的 `subtitles`（libass）滤镜；当前打包脚本还要求 `fontconfig` 支持和经校验的中英文字体资产。开发工具和依赖须由开发者准备，不能算作正式版用户的安装步骤。

先做只读环境检查和原生单测：

```sh
uname -m
xcrun swift --version
xcrun --find notarytool
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -filters | grep ' subtitles '
swift test --package-path desktop/macos
```

原生壳位于 `desktop/macos`，后端入口为 `python -m src.desktop --session-file <absolute-path>`；发行包使用 PyInstaller onedir 收集 Python、业务依赖和 Web 资源，冻结后的入口名为 `translatedsubs-backend`。只编译 Swift 不会自动生成完整 `.app`，裸 `swift run` 缺少后端及 FFmpeg 时不能用于首次使用验收。

交付任务预期入口为 `scripts/desktop/build.sh` 和 `scripts/desktop/sign-and-notarize.sh`。当前工作树实际实现为统一驱动 [`scripts/desktop/build.py`](../scripts/desktop/build.py)，分别通过 `--mode dev` 与 `--mode release` 构建开发包和签名公证包；两个 `.sh` 文件当前不存在，属于待集成协调的文件名差异，不应复制不存在的命令或使用其他 checkout 的旧脚本。

安装锁定的开发依赖后先查看帮助；若 `--locked` 报锁文件与依赖声明不一致，应交由打包工作线修正，不要通过自动更新锁文件掩盖差异：

```sh
uv sync --locked --python 3.12 --group desktop-build --extra local-translation --extra hf-models
.venv/bin/python scripts/desktop/build.py --help
.venv/bin/python scripts/desktop/benchmark.py --help
```

构建需要事先准备并审核 FFmpeg 和字体分发目录及其原始归档。每个目录的 `inventory.json` 使用 `schema: 1`，声明 `kind`（`ffmpeg`／`fonts`）、真实 `source_url`、归档 `source_sha256`、`license`，以及每个文件的 `path`、`sha256`、`role`；字体还须声明 `languages` 包含 `en` 和 `zh-CN`。FFmpeg 目录须有 `bin/ffmpeg`、`bin/ffprobe`、必要的 `lib/` 和许可证；字体目录须含字体及其许可证。校验逻辑见 `scripts/desktop/build.py`，显式下载与解包辅助工具为 `scripts/desktop/prepare_assets.py`。本项目尚未确定可直接使用的发行资产来源，不提供占位地址，也不把 Homebrew 安装结果视为可直接分发的资产包。

由开发者将下列变量设为已审核的本地绝对路径；输出目录应是新的目录。具备上述资产后，开发包命令为：

```sh
.venv/bin/python scripts/desktop/build.py --mode dev \
  --ffmpeg-dir "$DESKTOP_FFMPEG_DIR" --ffmpeg-archive "$DESKTOP_FFMPEG_ARCHIVE" \
  --font-dir "$DESKTOP_FONT_DIR" --font-archive "$DESKTOP_FONT_ARCHIVE" \
  --output "$DESKTOP_OUTPUT_DIR"
```

脚本生成 `TranslateSub.app`、`TranslateSub-<version>-macos-arm64-dev.dmg` 和对应 `.sha256` 文件，默认输出目录为 `dist/desktop`。开发模式也会审核动态库是否泄漏开发机路径；失败应修复依赖闭包，不跳过审核。它不会自动安装依赖、下载模型或上传发行资产。`--dev-ffmpeg`／`--dev-ffprobe` 是显式开发工具替代参数，不能用于 release；即使使用它们仍需字体资产，并可能因外部动态库依赖而失败。

完整 `.app` 的资源布局须与原生启动器一致：`Contents/Resources/backend/translatedsubs-backend`、`Contents/Resources/bin/ffmpeg`、`Contents/Resources/bin/ffprobe`。Python 原生扩展、FFmpeg 动态库、Web 资源和许可证也必须收集到发行包中，不能依赖开发机的 Homebrew 路径。开发包的 ad-hoc 签名不能代替 Developer ID 签名及 Apple 公证。[PyInstaller 官方说明](https://pyinstaller.org/en/stable/feature-notes.html#macos-binary-code-signing)介绍了两种签名模式；最终验收按[发行检查表](./macos-desktop-release.md)执行。

## 联调契约与验证边界

原生进程设置 `SUBTRANS_DESKTOP=1`，为每次启动生成会话令牌，并把后端限制在 `127.0.0.1` 随机端口；不要套用服务端固定 `8000` 端口的联调方式。会话文件只记录端口、进程 ID 和实例 ID，不含令牌。WebView 使用 HttpOnly、SameSite=Strict cookie，原生请求使用 `X-API-Token`；不要把令牌放入 URL、日志、文档或业务配置。

| 开发参数 | 用途与约束 |
| --- | --- |
| `SUBTRANS_DESKTOP_HOME` | 应用数据根目录的绝对路径；独立测试目录须由开发者明确选择，避免污染已有任务 |
| `SUBTRANS_DESKTOP_FFMPEG` / `SUBTRANS_DESKTOP_FFPROBE` | 由启动器传入的包内工具绝对路径 |
| `SUBTRANS_DESKTOP_VERSION` | 打包版本，须与应用及更新信息一致 |
| `SUBTRANS_DESKTOP_MODEL_ENDPOINT` | 可选 HTTPS 模型源；默认官方来源，未经验证不能声称国内可用 |
| `SUBTRANS_DESKTOP_UPDATE_URL` | 可选 HTTPS 更新 manifest；未配置时显示渠道未配置，不填虚构下载地址 |

完整变量与 HTTP 接口见[实施契约](./macos-desktop-v1-implementation.md)。开发候选是 `local:base.en` 转写和 `local-opus-en-zh` 翻译，最终默认模型须由质量与性能验收决定。[`scripts/desktop/benchmark.py`](../scripts/desktop/benchmark.py) 是已存在的验收脚本入口，使用前检查其帮助和[桌面验收说明](./desktop-acceptance.md)；本指南不自动运行媒体任务或下载模型。

目前未宣称通过 M1／8GB、60 分钟 1080p 样本两小时内处理、最低 macOS、干净安装、国内外网络或真实应用生命周期验收。两小时目标仅计模型已就绪且视频已在本地后的转写、翻译和硬字幕压制，不含模型或视频下载。16GB 开发机的结果不能代替 8GB 目标机验收。
