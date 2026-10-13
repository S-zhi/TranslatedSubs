[English](../../README.md) | 简体中文 | [हिन्दी](../hi/README.md) | [Español](../es/README.md) | [Français](../fr/README.md) | [Português](../pt/README.md) | [Русский](../ru/README.md)

<div align="center">
  <img src="../assets/eye-subtitles-logo.svg" width="88" alt="TranslatedSubs Logo" />
  <h1>TranslatedSubs</h1>
  <p><strong>解析视频中的语音与画面文字，支持语音转写、字幕翻译和板书内容识别，让视频中的讲述与书写信息得到全面理解。</strong></p>
  <p>支持快速部署，MCP接入</p>
</div>

TranslatedSubs 是视频与音频字幕处理工作台，支持媒体下载、语音识别、字幕翻译、软硬字幕封装。



- **多源视频获取**：支持从多种视频平台直接下载内容，无需用户先下载文件再手动上传。

- **灵活的本地部署与模型配置**：支持一键启动和轻量运行，并可按需接入更高级的模型以提升匹配效果，也支持配置社区模型。

- **高效任务管理**：支持多任务并行处理，并提供清晰的任务状态与资源使用情况查看能力。Google Drive 同步。

- **独立模型微调服务**：为适配的模型提供独立微调服务，支持按需优化模型效果。

- **多端使用**：覆盖 Web、Windows 和 macOS，未来逐步支持在手机端运行。

已经适配支持的网站列表：[![YouTube](https://img.shields.io/badge/YouTube-FF0033?style=plastic&logo=youtube&logoColor=white)](https://www.youtube.com/)
[![Vimeo](https://img.shields.io/badge/Vimeo-1AB7EA?style=plastic&logo=vimeo&logoColor=white)](https://vimeo.com/)
[![Dailymotion](https://img.shields.io/badge/Dailymotion-0066DC?style=plastic&logo=dailymotion&logoColor=white)](https://www.dailymotion.com/)
[![Twitch](https://img.shields.io/badge/Twitch-9146FF?style=plastic&logo=twitch&logoColor=white)](https://www.twitch.tv/)
[![TikTok](https://img.shields.io/badge/TikTok-111111?style=plastic&logo=tiktok&logoColor=white)](https://www.tiktok.com/)
[![X / Twitter](https://img.shields.io/badge/X%20%28Twitter%29-111111?style=plastic&logo=x&logoColor=white)](https://x.com/)
[![Instagram](https://img.shields.io/badge/Instagram-E4405F?style=plastic&logo=instagram&logoColor=white)](https://www.instagram.com/)
[![AcFun](https://img.shields.io/badge/AcFun-FD4C5D?style=plastic)](https://www.acfun.cn/)
[![Niconico](https://img.shields.io/badge/Niconico-252525?style=plastic&logo=niconico&logoColor=white)](https://www.nicovideo.jp/)
[![Pornhub](https://img.shields.io/badge/Pornhub-FF9900?style=plastic)](https://www.pornhub.com/)

![TranslatedSubs](../assets/readme-demo-1.png)

![TranslatedSubs](../assets/readme-demo-2.png)

## 从源码构建

必需依赖：Python 3.10–3.12、`uv`、用于克隆源码的 Git、FFmpeg/FFprobe（硬字幕需要含 `subtitles`/libass 滤镜）以及正在运行的 Redis 服务。`uv sync` 会安装 Python 库，包括 Redis 客户端（`redis>=5`），但不会安装或启动 Redis 服务端。

仅为需要使用的功能安装可选依赖：

- 本地英译中：`uv sync --extra local-translation`（之后在设置中下载并转换模型）。
- Hugging Face Whisper 模型：`uv sync --extra hf-models`。
- Kokoro 配音：`uv sync --extra tts`，并安装系统包 `espeak-ng`；模型在运行时下载。系统依赖见[配音指南](../tts.md)。
- Demucs 需单独安装。Google Drive sidecar 才需要 Go 1.23 或更新版本；运行前端测试时才需要 Node.js。

如果要启用多个 Python extras，请在同一条 `uv sync` 命令中列出，例如 `uv sync --extra local-translation --extra hf-models --extra tts`。

### macOS 构建

macOS 本地运行需要 Python 3.10–3.12、`uv` 和启用 libass 的 FFmpeg。网页工作台由 API 一并提供，不需要单独安装 Node.js。

在合适目录执行：

```sh
git clone https://github.com/S-zhi/TranslatedSubs.git
cd TranslatedSubs

brew install uv
brew tap homebrew-ffmpeg/ffmpeg
brew install ffmpeg-full
brew install redis
brew services start redis
redis-cli ping
uv sync

cp config.example.json config.local.json
```

Redis 检查应输出 `PONG`。如果已有其他 Redis 服务正在运行，可将其地址写入 `config.local.json` 的 `redis.url`，无需另装 Redis。

云端模型凭据和业务设置在服务启动后通过 Web 工作台或配置 API 管理，保存到 Redis 后无需重启即可生效。

启动业务 API 和网页工作台：

```sh
uv run python -m src.cli
```

然后打开 <http://127.0.0.1:8000/>。可用下面的命令检查服务状态，以及 FFmpeg 是否支持硬字幕：

```sh
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/health/ready
ffmpeg -hide_banner -filters | grep " subtitles "
```

### Windows 构建

在 Windows 10/11 上，可使用 PowerShell 从源码启动 TranslatedSubs。项目使用 Python 3.10–3.12；以下步骤固定使用 Python 3.12，并通过 uv.lock 安装已锁定的依赖。网页工作台由 API 服务提供，不需要单独安装 Node.js。

1. 安装 `uv`：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装完成后，重新打开 PowerShell。

2. 安装 FFmpeg：在 [FFmpeg 下载页](https://ffmpeg.org/download.html)选择 Windows builds，下载 Gyan 的 full build。解压后将其中的 `bin` 目录（例如 `C:\ffmpeg\bin`）加入 Windows `PATH`，然后重新打开 PowerShell。检查 `ffmpeg`、`ffprobe` 是否可用，以及 FFmpeg 是否包含硬字幕所需的 `subtitles` 滤镜：

```powershell
ffprobe -version
ffmpeg -hide_banner -filters | findstr /i subtitles
```

3. 在项目根目录安装依赖并创建本地配置：

```powershell
git clone https://github.com/S-zhi/TranslatedSubs.git
cd TranslatedSubs
uv python install 3.12
uv sync --python 3.12 --locked
Copy-Item config.example.json config.local.json
```

请先启动启用 Linux 容器的 Docker Desktop，再在 PowerShell 中用这一行启动独立 Redis 服务：

```powershell
docker run -d --name translatedsubs-redis --restart unless-stopped -p 127.0.0.1:6379:6379 -v translatedsubs-redis-data:/data redis:7-alpine redis-server --appendonly yes
```

检查 Redis 返回 `PONG`：

```powershell
docker exec translatedsubs-redis redis-cli ping
```

如果已有其他 Redis 服务，可将 `config.local.json` 的 `redis.url` 改为对应地址。`uv sync` 安装的是 Python Redis 客户端，不是 Redis 服务端。云端模型凭据和业务设置在服务启动后通过 Web 工作台或配置 API 管理。

启动 API 和网页工作台：

```powershell
uv run --locked python -m src.cli --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/> 使用工作台。可通过以下命令检查服务：

```powershell
curl.exe http://127.0.0.1:8000/api/health
curl.exe http://127.0.0.1:8000/api/health/ready
```

## Docker 快速启动

在仓库根目录执行：

```bash
cp config.example.json config.local.json
docker compose up -d --build
```

启动 Compose 前，先编辑 `config.local.json`，让 API 容器连接 Compose Redis 并使用持久化数据卷。将其中的 `redis`、`paths` 和 `http` 值改为：

```json
{
  "redis": {
    "url": "redis://redis:6379/0",
    "namespace": "translatedsubs-local"
  },
  "paths": {
    "dataDir": "/data",
    "dbPath": "/data/db/app.db"
  },
  "http": {
    "host": "0.0.0.0",
    "port": 8000
  }
}
```

Compose 会自动拉取并启动 Redis，因此宿主机部署 Compose 时无需安装 Redis、Python、`uv` 或 FFmpeg。API 和 Redis 使用独立持久化卷，Redis 已启用 AOF 持久化。升级时保留这两个卷。

Docker 镜像只安装基础 Python 依赖。若要在容器中使用本地翻译、Hugging Face Whisper、Kokoro 配音或 Demucs，需要在 Docker 镜像中添加对应可选依赖和系统工具后重新构建；配音还需要 `espeak-ng`。模型文件仍需在工作台中单独下载。

打开 <http://localhost:8000/>。用 `curl http://127.0.0.1:8000/api/health` 检查 API 是否启动，正常响应包含 `"ok":true`；`/api/health/ready` 还会列出翻译密钥、FFmpeg、存储目录和硬字幕滤镜的就绪状态。本地开发、Linux 部署和容器升级说明见[文档目录](../README.md)。

## 能力概览

- **字幕流水线**：下载视频、提取音频、语音识别、翻译，并生成软字幕或硬字幕成品，解决视频内容快速理解和跨语言观看问题。
- **Web 工作台**：提供任务队列、实时进度、视频预览、字幕编辑和结果下载，适合直接在浏览器中处理媒体。
- **多语言界面**：可在侧栏切换简体中文、英语、印地语、西班牙语、阿拉伯语、法语、葡萄牙语和俄语。用户选择会保存在浏览器中；首次访问时，除非部署方设置了默认值，否则会跟随浏览器语言。部署方可通过 `web/config.js` 中的 `UI_LOCALE` 设置默认界面语言。
- **MCP 接入**：让 Codex、Claude Desktop 等 AI 客户端通过自然语言创建和跟踪处理任务，适合把媒体处理接入 Agent 工作流。
- **Google Drive 扩展**：按任务上传、下载和管理云端文件，适合将处理结果接入团队文件流转。
- **可替换转写后端**：支持本地 faster-whisper 和兼容 HTTP 服务，适合在成本、速度、隐私之间选择。

## 从视频到字幕

1. 在 Web 工作台粘贴视频页面链接，或上传本地视频；也可以先用“下载测试”检查链接是否可获取。
2. 选择源语言和目标语言，以及仅译文或双语字幕、软字幕或硬字幕。默认转写后端是本地 faster-whisper；首次使用前，在设置中的“本地模型”下载并等待所选模型显示为就绪。
3. 提交任务后，在队列中查看下载、提取音频、转写、翻译和封装进度。完成后可预览视频、编辑字幕、重新封装，并下载视频和 SRT；选择“仅下载视频”时不会生成字幕。

软字幕可在播放器中开关；硬字幕写入画面，要求 FFmpeg 提供 `subtitles`（libass）滤镜。首次处理会用到外部服务或模型下载，请确认网络和磁盘空间可用。需要由 AI 客户端发起任务时，可按 [MCP Agent 指南](../mcp-agent-guide.md) 接入同一条流水线。

## 关键配置与数据

将 `config.example.json` 复制为 `config.local.json` 并启动 Redis。模型凭据和业务设置通过 Web 工作台的设置页面或配置 API 管理，保存到 Redis 后会立即用于新请求，无需重启服务。

引导文件只保存本机 Redis、数据路径和监听设置。升级时保留数据卷与 Redis 卷；不要提交 `config.local.json`、旧版 `.env`、Cookie、OAuth 凭据或测试媒体。Google Drive 需要单独启动 sidecar，见[本地快速启动](../local-quick-start.md)。

已有部署首次启动新版 API 前，应先启动并检查 Redis、准备好 `config.local.json`，再执行显式迁移；应用不会自动导入旧版 `.env` 或 SQLite 配置。请按[迁移步骤](../local-quick-start.md#从旧版-env-迁移)操作。

## 常见问题

- API 可访问但任务无法开始：查看 `/api/health/ready` 的 `checks` 和 `capabilities`，确认密钥、FFmpeg/FFprobe、yt-dlp 与数据目录状态。
- 提示 `MODEL_NOT_READY`：先在“本地模型”下载并检查所选 Whisper 模型，再提交字幕任务。
- 硬字幕不可用：安装带 libass 的 FFmpeg，或在任务中选择软字幕；检查命令为 `ffmpeg -hide_banner -filters | grep ' subtitles '`。
- 链接下载失败：先运行“下载测试”；需要登录的网站再按[本地启动说明](../local-quick-start.md)配置 cookie file setting。

## 文档

- [文档目录](../README.md)：按场景查找部署和扩展说明
- [本地快速启动](../local-quick-start.md)：macOS/Linux 本地运行、Redis/机器引导配置和 Google Drive sidecar
- [Linux 部署](../quick-start-linux.md)：Ubuntu/Debian 一键安装、systemd、反向代理和排障
- [MCP Server](../mcp-server.md)：stdio、Streamable HTTP 和工具说明
- [MCP Agent 指南](../mcp-agent-guide.md)：Agent 调用顺序、状态处理和错误处理
- [转写服务协议](../transcriber-service.md)：本地和 HTTP 转写后端
- [Google Drive sidecar](../../drive-service/README.md)：云端文件同步 API 与配置

## 开发

项目使用 Python 3.10–3.12、FastAPI、FFmpeg 和原生 JavaScript 前端。本地开发可运行 `uv sync` 安装依赖，再运行 `uv run python -m src.cli`；前端由同一服务提供。后端测试使用 `uv run pytest -q`，前端测试在 `web/` 目录运行 `npm test`。真实云服务和下载测试需显式启用，参见 [AGENTS.md](../../AGENTS.md)。

`src/handler/` 提供 HTTP API，`src/core/` 处理下载、转写与字幕，`src/service/` 和 `src/store/` 管理任务与持久化，`src/mcp_server/` 提供 MCP 接入，`web/` 是浏览器工作台。开发规范见 [CONTRIBUTING.md](../../.github/CONTRIBUTING.md)；安全问题请按 [SECURITY.md](../../.github/SECURITY.md) 私下报告，不要提交公开 Issue。

## 许可证与合规

本项目基于 [MIT License](../../LICENSE) 发布。请仅处理你有权访问、下载、转写、翻译和再发布的内容，并遵守目标网站条款、版权限制及所在地法律。
