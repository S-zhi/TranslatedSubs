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
- **Use across devices**: Available on the web, Windows, and macOS, with mobile support planned for the future.

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

## Build from source

### macOS

To run the project locally on macOS, you need Python 3.10–3.12, `uv`, and FFmpeg built with libass support. The API also serves the web workbench, so you do not need to install Node.js separately.

Run these commands in a suitable directory:

```sh
git clone https://github.com/S-zhi/TranslatedSubs.git
cd TranslatedSubs
brew install uv
brew tap homebrew-ffmpeg/ffmpeg
brew install ffmpeg-full
uv sync
cp .env.example .env
```

You can add the cloud API keys you need to `.env`, or configure them after startup, which is the recommended approach.

Start the API and web workbench:

```sh
uv run uvicorn src.handler.app:app --port 8000
```

Open <http://127.0.0.1:8000/>. Check the service and confirm FFmpeg has the hard-subtitle filter with:

```sh
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/health/ready
ffmpeg -hide_banner -filters | grep " subtitles "
```

### Windows

On Windows 10 or 11, you can run TranslatedSubs from source in PowerShell. The project supports Python 3.10–3.12; these steps use Python 3.12 and install the versions pinned in `uv.lock`. The API serves the web workbench, so a separate Node.js installation is not needed.

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
   Copy-Item .env.example .env
   ```

   You can add the cloud API keys you need to `.env`, or configure them after startup, which is the recommended approach.

Start the API and web workbench:

```powershell
uv run --locked uvicorn src.handler.app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>. Check the service with:

```powershell
curl.exe http://127.0.0.1:8000/api/health
curl.exe http://127.0.0.1:8000/api/health/ready
```

## Docker quick start

Run this from the repository root:

```bash
cp .env.example .env
# Set SUBTRANS_DEEPSEEK_API_KEY in .env
docker build -t translatedsubs:local . && docker run -d --name translatedsubs --restart unless-stopped -p 8000:8000 --env-file .env -e SUBTRANS_DATA_DIR=/data -e SUBTRANS_DB=/data/db/app.db -v translatedsubs-data:/data translatedsubs:local
```

When upgrading an existing container, replace `translatedsubs-data` with the existing volume name to retain jobs and output files. The `SUBTRANS_*` environment variables remain supported.

Open <http://localhost:8000/>. Run `curl http://127.0.0.1:8000/api/health` to check that the API is running; a healthy response contains `"ok":true`. `/api/health/ready` also reports whether the translation key, FFmpeg, storage paths, and hard-subtitle filter are ready. See the [documentation index (Chinese)](./docs/README.md) for local development, Linux deployment, and extensions.

## Capabilities

- **Subtitle pipeline**: Download, transcribe, translate, and produce soft or hard subtitles for faster understanding and cross-language viewing.
- **Web workbench**: Queue jobs, follow progress, preview video, edit subtitles, and download results in a browser.
- **Localized interface**: Switch between Simplified Chinese, English, Hindi, Spanish, Arabic, French, Portuguese, and Russian from the sidebar. The choice is saved in the browser; on first visit, the browser language is used unless the deployment has set a default. Set the default with `UI_LOCALE` in `web/config.js`.
- **MCP integration**: Let Codex, Claude Desktop, and other AI clients create and track jobs from natural language.
- **Google Drive extension**: Upload, download, and organize task files when results need to move through a shared file workflow.
- **Replaceable transcription backends**: Choose local faster-whisper, Replicate, or a compatible HTTP service for different cost, speed, and privacy needs.

## From video to subtitles

1. Paste a video page URL into the Web workbench or upload a local video. You can use the download probe to check a URL first.
2. Choose the source and target languages, translated-only or bilingual subtitles, and soft or hard subtitles. The default transcription backend is local faster-whisper. Before the first subtitle job, download the selected model in the Local Models settings and wait until it is ready.
3. Submit the job and follow download, audio extraction, transcription, translation, and packaging in the queue. When it succeeds, preview the video, edit subtitles, package it again, and download the video and SRT. A video-only job produces no subtitles.

Soft subtitles can be toggled in a player. Hard subtitles are written into the picture and require FFmpeg's `subtitles` (libass) filter. The first run may need a model download and access to external services, so allow enough network bandwidth and disk space. AI clients can use the same pipeline through the [MCP Agent guide (Chinese)](./docs/mcp-agent-guide.md).

## Key configuration and data

Copy `.env.example` and set `SUBTRANS_DEEPSEEK_API_KEY` in `.env`. The [environment template](.env.example) lists every setting and its default. Common settings are:

| Setting | Purpose |
| --- | --- |
| `SUBTRANS_DEEPSEEK_API_KEY` | DeepSeek key for subtitle translation; the full subtitle pipeline is not ready without it. |
| `SUBTRANS_DATA_DIR`, `SUBTRANS_DB` | Locations of video and subtitle files and the SQLite job database; the Docker example stores both in a persistent volume. |
| `SUBTRANS_TRANSCRIBER_BACKEND` | Defaults to `local_whisper`; select `replicate` or a compatible HTTP service explicitly. |
| `SUBTRANS_COOKIES` | Cookie file for sites that require login or age verification. |
| `SUBTRANS_WORKERS`, `SUBTRANS_DOWNLOAD_WORKERS` | Pipeline and download concurrency limits; adjust for available resources. |

Reuse the existing data volume when upgrading a container. Keep the SQLite database along with the output files. Do not commit `.env`, cookies, OAuth credentials, or generated test media. Google Drive requires a separate sidecar; see the [local quick start (Chinese)](./docs/local-quick-start.md).

## Troubleshooting

- The API responds, but jobs cannot start: inspect `checks` and `capabilities` from `/api/health/ready` for the key, FFmpeg/FFprobe, yt-dlp, and storage status.
- `MODEL_NOT_READY`: download and verify the selected Whisper model in Local Models before creating a subtitle job.
- Hard subtitles are unavailable: install FFmpeg with libass or choose soft subtitles. Check with `ffmpeg -hide_banner -filters | grep ' subtitles '`.
- A URL fails to download: run the download probe first. If the site requires login, configure `SUBTRANS_COOKIES` as described in the [local setup guide (Chinese)](./docs/local-quick-start.md).

## Documentation

Most detailed guides below are in Chinese; the transcriber protocol is in English.

- [Documentation index](./docs/README.md)
- [Local quick start](./docs/local-quick-start.md)
- [Linux deployment](./docs/quick-start-linux.md)
- [MCP Server](./docs/mcp-server.md)
- [MCP Agent guide](./docs/mcp-agent-guide.md)
- [Transcriber service protocol (English)](./docs/transcriber-service.md)
- [Google Drive sidecar](./drive-service/README.md)

## Development

The project uses Python 3.10–3.12, FastAPI, FFmpeg, and vanilla JavaScript. For local development, run `uv sync` and then `uv run uvicorn src.handler.app:app --port 8000`; the same service hosts the frontend. Run backend tests with `uv run pytest -q` and frontend tests with `npm test` from `web/`. Tests that use real cloud services and downloads require explicit opt-in; see [AGENTS.md (Chinese)](./AGENTS.md).

`src/handler/` serves the HTTP API; `src/core/` handles downloads, transcription, and subtitles; `src/service/` and `src/store/` manage jobs and persistence; `src/mcp_server/` provides MCP; and `web/` is the browser workbench. See [CONTRIBUTING.md (Chinese)](.github/CONTRIBUTING.md) for development guidelines. Report security issues privately through [SECURITY.md](.github/SECURITY.md), not a public issue.

## License and compliance

Released under the [MIT License](./LICENSE). Process only media you are authorized to access, download, transcribe, translate, and redistribute, and comply with the source site's terms, copyright restrictions, and applicable laws.
