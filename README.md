English | [简体中文](./docs/zh/README.md) | [हिन्दी](./docs/hi/README.md) | [Español](./docs/es/README.md) | [Français](./docs/fr/README.md) | [Português](./docs/pt/README.md) | [Русский](./docs/ru/README.md)

<div align="center">
  <img src="./docs/assets/eye-subtitles-logo.svg" width="88" alt="TranslatedSubs Logo" />
  <h1>TranslatedSubs</h1>
  <p><strong>Understand both speech and on-screen text in video, from transcription and subtitle translation to handwritten board recognition.</strong></p>
  <p>Quick deployment with MCP integration.</p>
</div>

TranslatedSubs is a video and audio understanding workbench for downloading media, transcribing speech, translating subtitles, and recognizing written content in videos.

- **Video from multiple sources**: Download directly from supported video platforms, without downloading and re-uploading files by hand.
- **Flexible local deployment and model choices**: Start quickly or run a lightweight setup; configure more capable models for better matching, including community models.
- **Parallel tasks and resource visibility**: Run multiple jobs at once and review their status and resource use. Google Drive sync is also available.
- **Dedicated model fine-tuning service**: Fine-tune supported models through a separate service to improve results for your needs.
- **Use across devices**: Run the web workbench from source on Windows and macOS. The Apple Silicon desktop app is in development; no signed desktop release is available yet.

Supported video platforms:
[![YouTube](https://img.shields.io/badge/YouTube-FF0033?style=plastic&logo=youtube&logoColor=white)](https://www.youtube.com/)
[![Vimeo](https://img.shields.io/badge/Vimeo-1AB7EA?style=plastic&logo=vimeo&logoColor=white)](https://vimeo.com/)
[![Dailymotion](https://img.shields.io/badge/Dailymotion-0066DC?style=plastic&logo=dailymotion&logoColor=white)](https://www.dailymotion.com/)
[![Twitch](https://img.shields.io/badge/Twitch-9146FF?style=plastic&logo=twitch&logoColor=white)](https://www.twitch.tv/)
[![TikTok](https://img.shields.io/badge/TikTok-111111?style=plastic&logo=tiktok&logoColor=white)](https://www.tiktok.com/)
[![X / Twitter](https://img.shields.io/badge/X%20%28Twitter%29-111111?style=plastic&logo=x&logoColor=white)](https://x.com/)
[![Instagram](https://img.shields.io/badge/Instagram-E4405F?style=plastic&logo=instagram&logoColor=white)](https://www.instagram.com/)
[![AcFun](https://img.shields.io/badge/AcFun-FD4C5D?style=plastic)](https://www.acfun.cn/)
[![Niconico](https://img.shields.io/badge/Niconico-252525?style=plastic&logo=niconico&logoColor=white)](https://www.nicovideo.jp/)
[![Pornhub](https://img.shields.io/badge/Pornhub-FF9900?style=plastic)](https://www.pornhub.com/)

![TranslatedSubs](./docs/assets/readme-demo-1.png)

![TranslatedSubs](./docs/assets/readme-demo-2.png)

## macOS 桌面一期（开发中）

**目前没有可下载的已签名、公证桌面正式版。** Apple Silicon 桌面版正在开发，原生窗口复用 Web 工作台；开发构建与普通用户安装包的区别见[桌面安装与开发指南](./docs/macos-desktop-install.md)。

首版目标是安装 DMG 后无需配置 Python、Redis、FFmpeg 或 API Key，首次联网自动准备模型，再用本地转写、英文到简体中文翻译和压制生成中英双语硬字幕 MP4，并可单独导出字幕。模型下载仍需要网络与等待；YouTube 公开视频下载取决于用户网络能否访问目标视频，本地视频流程独立可用。字幕由机器生成，需要人工核对。

关闭窗口继续后台处理；主动退出在有任务时提示确认，中断任务重开后可重试，**不保证断点续处理**。更新仅检查版本、展示说明并引导下载新版 DMG，由用户安装。上述流程仍需完整验收，不能把开发完成视作发行通过：M1／8GB 的 60 分钟、1080p 样本两小时处理目标、最低 macOS 支持版本、国内外网络和签名公证均未验收通过。见[发行检查表](./docs/macos-desktop-release.md)与[产品约定](./docs/macos-desktop-v1-product-brief.md)。

下面的源码、Redis 和 Docker 步骤继续适用于服务端／浏览器工作台。桌面模式使用独立 SQLite 配置，不要求普通用户运行 Redis。

## Build from source

Required: Python 3.10–3.12, `uv`, Git to clone the source, FFmpeg/FFprobe (with the `subtitles`/libass filter for hard subtitles), and a running Redis service. `uv sync` installs Python packages, including the Redis client (`redis>=5`); it does not install or start the Redis server.

Install optional dependencies only for the features you use:

- Local English → Simplified Chinese translation: `uv sync --extra local-translation` (then download and convert the model in Settings).
- Hugging Face Whisper models: `uv sync --extra hf-models`.
- Kokoro TTS: `uv sync --extra tts`, plus the system `espeak-ng` package; download models at runtime. See the [TTS guide](./docs/tts.md).
- Demucs must be installed separately. Go 1.23 or later is only needed for the Google Drive sidecar; Node.js is only needed for frontend tests.
- 可选 cobalt 下载：连接自托管或获授权的 cobalt API（可用 `ghcr.io/imputnet/cobalt:11` 部署）；`httpx` 已在基础依赖中，未配置时仍默认使用 yt-dlp。部署、健康检查、认证与任务选择见 [cobalt 下载指南](./docs/cobalt-download.md)。
- YouTube downloads: `uv sync` includes yt-dlp's default dependencies and its matching `yt-dlp-ejs` package. Also install Deno or Node.js 22 or later on the backend machine; both runtimes are enabled for downloads and probes. Node.js also runs the frontend tests.
- Demucs must be installed separately. Go 1.23 or later is only needed for the Google Drive sidecar.

If enabling multiple Python extras, include them in one `uv sync` command, for example `uv sync --extra local-translation --extra hf-models --extra tts`.

### macOS

To run the project locally on macOS, you need Python 3.10–3.12, `uv`, and FFmpeg built with libass support. The API also serves the web workbench. YouTube downloads additionally need Deno or Node.js 22 or later; see the [download checks (Chinese)](./docs/local-quick-start.md#youtube-下载检查).

Run these commands in a suitable directory:

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

The Redis check should print `PONG`. If Redis is already running elsewhere, you can use it by setting `redis.url` in `config.local.json`; you do not need to install another Redis server.

`config.local.json` stores this machine's Redis connection and filesystem/listener paths. Configure models and API credentials from Settings after startup; saved settings apply to new requests without restarting the service.

Start the API and web workbench:

```sh
uv run python -m src.cli
```

For local debugging, use the dedicated launcher to skip the fixed website
availability probe that normally runs during API startup:

```sh
uv run python -m src.cli --debug --port 8000
```

Debug mode only skips that startup probe. Explicit probes, downloads, and
other business requests still run normally and report their failures.

Open <http://127.0.0.1:8000/>. Check the service and confirm FFmpeg has the hard-subtitle filter with:

```sh
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/health/ready
ffmpeg -hide_banner -filters | grep " subtitles "
```

### Windows

On Windows 10 or 11, you can run TranslatedSubs from source in PowerShell. The project supports Python 3.10–3.12; these steps use Python 3.12 and install the versions pinned in `uv.lock`. The API serves the web workbench. For YouTube downloads, install Deno or Node.js 22 or later and make it available on the backend's `PATH`.

1. Install `uv`:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   Reopen PowerShell after installation.

2. Install FFmpeg. On the [FFmpeg download page](https://ffmpeg.org/download.html), choose a Windows build and download Gyan's full build. Extract it and add its `bin` directory (for example, `C:\ffmpeg\bin`) to `PATH`, then reopen PowerShell. Check that `ffmpeg`, `ffprobe`, and the hard-subtitle filter are available:

   ```powershell
   ffprobe -version
   ffmpeg -hide_banner -filters | findstr /i subtitles
   ```

3. Clone the project, install dependencies, and create the local configuration:

   ```powershell
   git clone https://github.com/S-zhi/TranslatedSubs.git
   cd TranslatedSubs
   uv python install 3.12
   uv sync --python 3.12 --locked
   Copy-Item config.example.json config.local.json
   ```

   Start Docker Desktop with Linux containers, then run this single PowerShell command to start Redis as a separate service:

   ```powershell
   docker run -d --name translatedsubs-redis --restart unless-stopped -p 127.0.0.1:6379:6379 -v translatedsubs-redis-data:/data redis:7-alpine redis-server --appendonly yes
   ```

   Check that Redis responds with `PONG`:

   ```powershell
   docker exec translatedsubs-redis redis-cli ping
   ```

   If you already use a Redis service elsewhere, set `redis.url` in `config.local.json` to its address instead. `uv sync` installs the Python Redis client, not the Redis server.

Start the API and web workbench:

```powershell
uv run --locked python -m src.cli --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>. Check the service with:

```powershell
curl.exe http://127.0.0.1:8000/api/health
curl.exe http://127.0.0.1:8000/api/health/ready
```

## Docker quick start

Run this from the repository root:

```bash
cp config.example.json config.local.json
docker compose up -d --build
```

Before starting Compose, edit `config.local.json` so the API container can reach the Redis service and use its persistent data volume. Replace its `redis`, `paths`, and `http` values with:

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

Compose pulls and starts Redis automatically, so the host does not need Redis, Python, `uv`, or FFmpeg installed for this deployment. The API and Redis use separate persistent volumes; Redis AOF persistence is enabled. Keep both volumes when upgrading so tasks, media, and configuration survive container replacement.

The Docker image installs the base Python dependencies only. To use local translation, Hugging Face Whisper, Kokoro TTS, or Demucs in Docker, add the corresponding optional Python packages and required system tools to the image build before rebuilding; TTS also needs `espeak-ng`. Download model files separately through the workbench as needed.

Open <http://localhost:8000/>. Run `curl http://127.0.0.1:8000/api/health` to check that the API is running; a healthy response contains `"ok":true`. `/api/health/ready` also reports whether the translation key, FFmpeg, storage paths, and hard-subtitle filter are ready. See the [documentation index (Chinese)](./docs/README.md) for local development, Linux deployment, and extensions.

## Optional one-click cloud trials

**Official-site-only capability.** The product site builds with the trial entry point by default, but self-hosted marketing pages should use `npm --prefix site run build:no-trial` (`SUBTRANS_ENABLE_CLOUD_TRIAL=0`), which removes the trial UI, the `#cloud-start` anchor, and every `/api/trials` request from the artifact. The control service is likewise opt-in at install time: `sudo bash install.sh --with-cloud-trial`.

The independent [product website](./site/README.md) can create short-lived workbenches using the maintainer's InstaBox account through `python -m src.instabox_launcher`. The control service holds the API key, reserves capacity, verifies per-instance authentication, and reclaims machines after expiry. Visitors need no InstaBox account or terminal commands. See the [deployment guide](./docs/instabox-quick-start.md) for same-origin routing, persistent SQLite, server secrets, and launch verification. A `build:no-trial` deployment shows no trial UI at all; only the trial-enabled build displays the trial as unavailable until the control service is configured. The base trial still requires model/engine preparation for subtitle processing; real-account provisioning must be verified before release.

## Capabilities

- **Subtitle pipeline**: Download, transcribe, translate, and produce soft or hard subtitles for faster understanding and cross-language viewing.
- **Web workbench**: Queue jobs, follow progress, preview video, edit subtitles, and download results in a browser.
- **Localized interface**: Switch between Simplified Chinese, English, Hindi, Spanish, Arabic, French, Portuguese, and Russian from the sidebar. The choice is saved in the browser; on first visit, the browser language is used unless the deployment has set a default. Set the default with `UI_LOCALE` in `web/config.js`.
- **MCP integration**: Let Codex, Claude Desktop, and other AI clients create and track jobs from natural language.
- **Google Drive extension**: Upload, download, and organize task files when results need to move through a shared file workflow.
- **Replaceable transcription backends**: Choose local faster-whisper or a compatible HTTP service for different cost, speed, and privacy needs.
- **插件实例**：在 `/settings?tab=plugins` 管理七类流水线阶段的实例、默认项与模型/引擎绑定；任务高级选项可指定实例，创建时预检并固定执行计划。八个内置实现包含可选 cobalt 下载与探测，初始下载默认仍为 yt-dlp；cobalt 不提供完整格式列表，回退默认关闭。详见[插件实例指南](./docs/plugin-instances.md)与 [cobalt 下载指南](./docs/cobalt-download.md)。

## From video to subtitles

1. Paste a video page URL into the Web workbench or upload a local video. You can use the download probe to check a URL first.
2. Choose the source and target languages, translated-only or bilingual subtitles, and soft or hard subtitles. The default transcription backend is local faster-whisper. Before the first subtitle job, download the selected model in the Local Models settings and wait until it is ready.
3. Submit the job and follow download, audio extraction, transcription, translation, and packaging in the queue. When it succeeds, preview the video, edit subtitles, package it again, and download the video and SRT. A video-only job produces no subtitles.

Soft subtitles can be toggled in a player. Hard subtitles are written into the picture and require FFmpeg's `subtitles` (libass) filter. The first run may need a model download and access to external services, so allow enough network bandwidth and disk space. AI clients can use the same pipeline through the [MCP Agent guide (Chinese)](./docs/mcp-agent-guide.md).

## Key configuration and data

Copy `config.example.json` to `config.local.json` and start Redis. The default translator is the local CPU English → Simplified Chinese model; install it with `uv sync --extra local-translation`, then download and convert it from Settings → Translation engines. Add model credentials and business settings in Settings or through the configuration API. They are saved to Redis and apply to new requests without a service restart. The bootstrap template lists machine-local Redis, data path, and listener defaults.

For an existing installation, prepare Redis and `config.local.json`, then run the explicit migration before the new API's first startup; the app does not import old `.env` or SQLite settings automatically. Follow the [migration steps (Chinese)](./docs/local-quick-start.md#从旧版-env-迁移).

Keep the configured data and Redis volumes when upgrading. Do not commit `config.local.json`, `.env`, cookies, OAuth credentials, or generated test media. Google Drive requires a separate sidecar; see the [local quick start (Chinese)](./docs/local-quick-start.md).

## Troubleshooting

- The API responds, but jobs cannot start: inspect `checks` and `capabilities` from `/api/health/ready` for the key, FFmpeg/FFprobe, yt-dlp, and storage status.
- `MODEL_NOT_READY`: download and verify the selected Whisper model in Local Models before creating a subtitle job.
- Hard subtitles are unavailable: install FFmpeg with libass or choose soft subtitles. Check with `ffmpeg -hide_banner -filters | grep ' subtitles '`.
- A URL fails to download, or YouTube returns HTTP 403: follow the [YouTube download checks (Chinese)](./docs/local-quick-start.md#youtube-下载检查) to verify yt-dlp, EJS, and the backend's JavaScript runtime, then inspect the service's yt-dlp warnings and proxy route. The download probe only checks whether yt-dlp can parse the page and list media formats; it does not request the media bytes, so a successful probe does not prove that the selected media stream can be downloaded. Configure a cookie file in Settings when the video requires login. For YouTube downloads that start but fail partway through, see the [yt-dlp PO Token guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) for GVS token requirements and supported provider plugins. The workbench's backend API Token only authenticates requests to this app; it cannot authorize YouTube media requests.

## Documentation

Most detailed guides below are in Chinese; the transcriber protocol is in English.

- [Documentation index](./docs/README.md)
- [Local quick start](./docs/local-quick-start.md)
- [macOS 桌面安装与开发（开发中）](./docs/macos-desktop-install.md)
- [macOS 桌面发行检查表](./docs/macos-desktop-release.md)
- [Linux deployment](./docs/quick-start-linux.md)
- [MCP Server](./docs/mcp-server.md)
- [MCP Agent guide](./docs/mcp-agent-guide.md)
- [Transcriber service protocol (English)](./docs/transcriber-service.md)
- [插件实例与任务选择](./docs/plugin-instances.md)
- [cobalt 下载、自托管与回退](./docs/cobalt-download.md)
- [Google Drive sidecar](./drive-service/README.md)

## Development

The project uses Python 3.10–3.12, FastAPI, FFmpeg, and vanilla JavaScript. For local development, run `uv sync` and then `uv run python -m src.cli`; the same service hosts the frontend. Run backend tests with `uv run pytest -q` and frontend tests with `npm test` from `web/`. Tests that use real cloud services and downloads require explicit opt-in; see [AGENTS.md (Chinese)](./AGENTS.md).

The independent product landing page lives in [`site/`](./site/README.md). It uses React and Vite, with a GhostFibers hero blending into a shared Particles background for the video and product advantages. The video heading uses React Bits SplitText with GSAP. The video is currently a locally hosted CC0 placeholder. Run `npm ci --prefix site` and `npm --prefix site run dev` to open it at <http://127.0.0.1:5173/>; verify the static build with `npm --prefix site run build`. This page needs Node.js 20.19+ or 22.12+ and does not require the Python API or Redis.

The cloud workbench controller also supports an explicit fixed-machine mode. Set `SUBTRANS_LAUNCHER_MODE=fixed` and `SUBTRANS_LAUNCHER_MACHINE_ID`; the machine starts only after a page click, while status checks remain read-only. Visitors share the existing workbench and its data. This mode never creates or deletes machines. See the [InstaBox deployment guide](docs/instabox-quick-start.md#1-准备维护方配置).

`src/handler/` serves the HTTP API; `src/core/` handles downloads, transcription, and subtitles; `src/service/` and `src/store/` manage jobs and persistence; `src/mcp_server/` provides MCP; and `web/` is the browser workbench. See [CONTRIBUTING.md (Chinese)](.github/CONTRIBUTING.md) for development guidelines. Report security issues privately through [SECURITY.md](.github/SECURITY.md), not a public issue.

## License and compliance

Released under the [MIT License](./LICENSE). Process only media you are authorized to access, download, transcribe, translate, and redistribute, and comply with the source site's terms, copyright restrictions, and applicable laws.
