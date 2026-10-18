# 本地快速启动

本文介绍如何从源码运行 TranslatedSubs 服务端与浏览器工作台。此模式的业务配置保存在 Redis；机器本地的 Redis 地址、数据路径和监听参数保存在 `config.local.json`。

Apple Silicon 原生桌面版正在开发，**目前没有可下载的已签名、公证正式版**；开发入口见[桌面安装与开发指南](./macos-desktop-install.md)，发行条件见[发行检查表](./macos-desktop-release.md)。桌面模式由原生窗口管理本机后端，使用独立 SQLite 配置，数据默认放在 `~/Library/Application Support/TranslateSub`。无需 Redis 的桌面约定不改变本文服务端步骤，也不自动迁移已有 Redis 配置。

桌面正式版的目标是无需用户安装运行依赖或填写 API Key；首次仍需联网下载模型，准备后在本机转写、翻译和压制。当前源码开发仍需开发工具，不能以本机启动成功代替干净设备验收。不要为试用桌面模式修改现有服务端的 `config.local.json`。

## 准备依赖

需要 Python 3.10–3.12、Git、[uv](https://docs.astral.sh/uv/)、带 `libass` 的 FFmpeg/FFprobe，以及一个正在运行的 Redis 服务。`uv sync` 会安装 Python Redis 客户端（`redis>=5`），不会安装或启动 Redis 服务端。macOS 可执行：

```bash
brew install uv redis
brew tap homebrew-ffmpeg/ffmpeg
brew install ffmpeg-full
brew services start redis
redis-cli ping
uv sync
cp config.example.json config.local.json
```

Redis 检查应输出 `PONG`。如使用已有或远程 Redis，请把 `config.local.json` 中的 `redis.url` 和 `redis.namespace` 改成当前部署的值；每个独立部署应使用唯一 namespace，无需再安装 Redis。Linux 用户可参考[部署文档](./quick-start-linux.md)。此文件包含机器本地连接信息，不要提交到 Git。

确认硬字幕滤镜：

```bash
ffmpeg -hide_banner -filters | grep " subtitles "
```

## 启动业务 API

如果这是从旧版本升级的部署，先确保 Redis 已启动并能连接，准备好 `config.local.json`，然后按本文末尾“从旧版 `.env` 迁移”执行显式迁移。迁移成功后再首次启动 API；应用不会在启动时自动导入旧 `.env` 或 SQLite 配置。

```bash
uv run python -m src.cli
```

本地调试时可以跳过 API 启动时的网站连通性检查：

```bash
uv run python -m src.cli --debug --port 8000
```

`config.local.json` 的 `paths.dataDir` 和 `paths.dbPath` 控制任务文件与 SQLite 数据库的落点；迁移旧数据库时，先停止 API，再将数据库及存在的 `-wal`、`-shm` 文件一起移动，并更新对应路径。业务设置和模型/API 凭据在 Web 工作台的设置页面或对应 API 中管理；保存后新请求会读取最新配置，无需重启。

如果 Redis 业务配置启用了 `api_token`，在工作台的 **其他设置 → 后端连接 · API Token** 中填写后保存；首次收到 401 会自动打开该入口。Token 按后端地址保存在当前标签页的会话存储中，保存或清除后页面自动刷新，普通请求、上传、进度订阅与媒体下载均使用该凭据。

服务启动不要求本地翻译模型或可选依赖已经存在。要运行默认英语 → 简体中文翻译，再安装依赖并下载模型：

```bash
uv sync --extra local-translation
```

打开“设置 → 翻译引擎”，点击“本地 CPU 英译中”的“下载并转换”。模型就绪前，任务会提示缺少条件，不会自动切换到云端引擎。

Hugging Face Whisper 可选依赖为 `uv sync --extra hf-models`；Kokoro 配音使用 `uv sync --extra tts`，另外需要系统安装 `espeak-ng`。多个功能需要一起启用时，在同一次 `uv sync` 中列出所有 `--extra` 参数。Demucs 需单独安装，不属于 `pyproject.toml` extras；Google Drive sidecar 需要 Go 1.23 或更新版本。Node.js 也用于运行前端测试。YouTube 下载还需要后端机器安装 Deno 或 Node.js 22 及以上版本，详见下方检查步骤。

验证服务：

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/health/ready
```

然后访问 <http://127.0.0.1:8000/>。MCP 配置也保存在 `config.local.json`；参见 [MCP Server 文档](./mcp-server.md)。

## 可选：cobalt 下载服务

默认下载器仍是 yt-dlp。需要 cobalt 时，另行部署 `ghcr.io/imputnet/cobalt:11`，或配置已获授权的服务；不提供默认公共实例。`httpx` 已由基础依赖安装，无需新增 Python extra，后续转写与 FFmpeg 依赖照常按需准备。

cobalt 的 `API_URL` 与插件 `endpoint` 必须能被 TranslatedSubs 后端访问；容器内的 `127.0.0.1` 不是宿主机。启动后从后端所在环境执行 `curl --fail http://<cobalt 地址>/`，检查 JSON 中的 `cobalt.version` 和 `cobalt.url`。该检查只验证 API 可达。

然后在 **设置 → 插件 → 下载 → 新增实例** 选择 cobalt，填写地址、`access_mode` 与 `fallback_policy`，在任务高级选项选择实例。密钥通过 `PATCH /api/settings/runtime` 的 `cobalt_api_key` 保存；GET 仅返回 `hasSecrets.cobalt_api_key`，所有认证 cobalt 实例共用这一把 Key。回退默认关闭。完整 Docker 命令、CAS revision 示例、探测限制与回退条件见 [cobalt 下载指南](./cobalt-download.md)。
## YouTube 下载检查

`uv sync` 安装 `yt-dlp[default]`，其中包含与下载器版本匹配的 `yt-dlp-ejs`。YouTube 的 JavaScript 校验还需要后端机器安装 Deno 或 Node.js 22 及以上版本，并让运行后端的进程能通过 `PATH` 找到它。下载和链接探测均启用 Deno、Node；只在浏览器所在电脑安装运行时无法帮助远程后端。

macOS 已安装 Node.js 22 及以上版本时可直接使用；否则可以安装：

```bash
brew install node
```

确认 Python 组件和运行时可用：

```bash
uv run python -c "from importlib.metadata import version; print('yt-dlp:', version('yt-dlp')); print('yt-dlp-ejs:', version('yt-dlp-ejs'))"
```

```bash
node --version
```

使用 Deno 时改为运行 `deno --version`。Linux/systemd 和 Docker 部署也需要在实际服务环境中提供上述运行时；当前 Docker 镜像不会自动安装 Node 或 Deno。安装依赖或调整服务 `PATH` 后重启后端。

遇到 `HTTP Error 403: Forbidden` 时，网页能打开或探测成功只说明能解析视频信息，不能保证所选媒体流可下载。先确认上述依赖和服务日志中的 yt-dlp 警告，再检查下载代理是否能访问媒体域名、解析与下载是否使用同一出口。只有明确提示登录、年龄或私有视频限制时，再在设置中配置 cookies 文件；浏览器 cookies 的单次探测不会自动把登录态带入下载任务。不要把 cookies 内容或带凭据的代理地址贴到日志或提交到仓库。

升级下载器后，用 `uv sync` 同步仓库锁定版本；维护者更新下载器使用 `uv lock --upgrade-package yt-dlp` 并提交对应锁文件。完整下载验证应使用自己的失败链接，避免把探测成功当成下载通过。

## 可选：Google Drive sidecar

Google Drive 凭据单独保存在 sidecar 的本地配置中：

```bash
cp drive-service/config.example.json drive-service/config.local.json
```

填写 OAuth Desktop app 信息后运行：

```bash
./scripts/start.sh
```

## 从旧版 `.env` 迁移

应用不会自动读取 `.env`。一次性迁移旧安装时，先备份 Redis 与 SQLite，再显式运行迁移命令。例如：

```bash
uv run python -m src.config.migrate_legacy \
  --legacy-env-file /path/to/old/.env \
  --legacy-db /path/to/old/data/db/app.db \
  --legacy-runtime-file /path/to/old/runtime-config.json \
  --legacy-retention-file /path/to/old/.retention.json
```

只传入实际存在的旧文件参数。迁移 CLI 是唯一读取这些旧文件的路径；Redis 中已有的自定义值和模型/引擎 ID 始终优先。已初始化但仍使用 schema 默认值的 Redis 字段默认也保留；确认要从旧文件补入这些字段时，显式追加 `--merge-defaults`。旧来源冲突时按 `runtime:audio`、runtime JSON、`.env` 的顺序确定优先级。迁移后移除旧 `.env`；不要把 API Key、Cookie 或 `config.local.json` 提交到 Git。
