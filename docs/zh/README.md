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

## Docker 快速启动

在仓库根目录执行：

```bash
cp .env.example .env
# 在 .env 中填写 SUBTRANS_DEEPSEEK_API_KEY
docker build -t translatedsubs:local . && docker run -d --name translatedsubs --restart unless-stopped -p 8000:8000 --env-file .env -e SUBTRANS_DATA_DIR=/data -e SUBTRANS_DB=/data/db/app.db -v translatedsubs-data:/data translatedsubs:local
```

从旧容器升级时，把 `translatedsubs-data` 换成原有数据卷名，保留任务数据库和产物。现有 `SUBTRANS_*` 环境变量继续使用。

打开 <http://localhost:8000/>。用 `curl http://127.0.0.1:8000/api/health` 检查 API 是否启动，正常响应包含 `"ok":true`；`/api/health/ready` 还会列出翻译密钥、FFmpeg、存储目录和硬字幕滤镜的就绪状态。本地开发、Linux 部署和容器升级说明见[文档目录](../README.md)。

## 能力概览

- **字幕流水线**：下载视频、提取音频、语音识别、翻译，并生成软字幕或硬字幕成品，解决视频内容快速理解和跨语言观看问题。
- **Web 工作台**：提供任务队列、实时进度、视频预览、字幕编辑和结果下载，适合直接在浏览器中处理媒体。
- **多语言界面**：可在侧栏切换简体中文、英语、印地语、西班牙语、阿拉伯语、法语、葡萄牙语和俄语。用户选择会保存在浏览器中；首次访问时，除非部署方设置了默认值，否则会跟随浏览器语言。部署方可通过 `web/config.js` 中的 `UI_LOCALE` 设置默认界面语言。
- **MCP 接入**：让 Codex、Claude Desktop 等 AI 客户端通过自然语言创建和跟踪处理任务，适合把媒体处理接入 Agent 工作流。
- **Google Drive 扩展**：按任务上传、下载和管理云端文件，适合将处理结果接入团队文件流转。
- **可替换转写后端**：支持本地 faster-whisper、Replicate 和兼容 HTTP 服务，适合在成本、速度、隐私之间选择。

## 从视频到字幕

1. 在 Web 工作台粘贴视频页面链接，或上传本地视频；也可以先用“下载测试”检查链接是否可获取。
2. 选择源语言和目标语言，以及仅译文或双语字幕、软字幕或硬字幕。默认转写后端是本地 faster-whisper；首次使用前，在设置中的“本地模型”下载并等待所选模型显示为就绪。
3. 提交任务后，在队列中查看下载、提取音频、转写、翻译和封装进度。完成后可预览视频、编辑字幕、重新封装，并下载视频和 SRT；选择“仅下载视频”时不会生成字幕。

软字幕可在播放器中开关；硬字幕写入画面，要求 FFmpeg 提供 `subtitles`（libass）滤镜。首次处理会用到外部服务或模型下载，请确认网络和磁盘空间可用。需要由 AI 客户端发起任务时，可按 [MCP Agent 指南](../mcp-agent-guide.md) 接入同一条流水线。

## 关键配置与数据

先复制 `.env.example`，再在 `.env` 填写 `SUBTRANS_DEEPSEEK_API_KEY`。完整字段和默认值以[环境变量模板](../../.env.example)为准；日常最常调整的是：

| 配置                                            | 用途                                                                        |
| ----------------------------------------------- | --------------------------------------------------------------------------- |
| `SUBTRANS_DEEPSEEK_API_KEY`                     | 字幕翻译所需的 DeepSeek 密钥；缺失时完整字幕流水线不会就绪。                |
| `SUBTRANS_DATA_DIR`、`SUBTRANS_DB`              | 视频、字幕和 SQLite 任务数据库的位置；Docker 示例把两者放在持久化数据卷中。 |
| `SUBTRANS_TRANSCRIBER_BACKEND`                  | 默认 `local_whisper`；可显式选择 `replicate` 或兼容 HTTP 服务。             |
| `SUBTRANS_COOKIES`                              | 仅在目标网站要求登录或年龄验证时配置 Cookie 文件。                          |
| `SUBTRANS_WORKERS`、`SUBTRANS_DOWNLOAD_WORKERS` | 控制流水线和下载的并发数，按机器资源调整。                                  |

升级容器时复用原数据卷；不要只迁移视频文件而丢掉 SQLite 数据库。不要提交 `.env`、Cookie、OAuth 凭据或测试生成的媒体文件。Google Drive 需要额外启动 sidecar，见[本地快速启动](../local-quick-start.md)。

## 常见问题

- API 可访问但任务无法开始：查看 `/api/health/ready` 的 `checks` 和 `capabilities`，确认密钥、FFmpeg/FFprobe、yt-dlp 与数据目录状态。
- 提示 `MODEL_NOT_READY`：先在“本地模型”下载并检查所选 Whisper 模型，再提交字幕任务。
- 硬字幕不可用：安装带 libass 的 FFmpeg，或在任务中选择软字幕；检查命令为 `ffmpeg -hide_banner -filters | grep ' subtitles '`。
- 链接下载失败：先运行“下载测试”；需要登录的网站再按[本地启动说明](../local-quick-start.md)配置 `SUBTRANS_COOKIES`。

## 文档

- [文档目录](../README.md)：按场景查找部署和扩展说明
- [本地快速启动](../local-quick-start.md)：macOS/Linux 本地运行、环境变量和 Google Drive sidecar
- [Linux 部署](../quick-start-linux.md)：Ubuntu/Debian 一键安装、systemd、反向代理和排障
- [MCP Server](../mcp-server.md)：stdio、Streamable HTTP 和工具说明
- [MCP Agent 指南](../mcp-agent-guide.md)：Agent 调用顺序、状态处理和错误处理
- [转写服务协议](../transcriber-service.md)：本地、Replicate 和 HTTP 转写后端
- [Google Drive sidecar](../../drive-service/README.md)：云端文件同步 API 与配置

## 开发

项目使用 Python 3.10–3.12、FastAPI、FFmpeg 和原生 JavaScript 前端。本地开发可运行 `uv sync` 安装依赖，再运行 `uv run uvicorn src.handler.app:app --port 8000`；前端由同一服务提供。后端测试使用 `uv run pytest -q`，前端测试在 `web/` 目录运行 `npm test`。真实云服务和下载测试需显式启用，参见 [AGENTS.md](../../AGENTS.md)。

`src/handler/` 提供 HTTP API，`src/core/` 处理下载、转写与字幕，`src/service/` 和 `src/store/` 管理任务与持久化，`src/mcp_server/` 提供 MCP 接入，`web/` 是浏览器工作台。开发规范见 [CONTRIBUTING.md](../../.github/CONTRIBUTING.md)；安全问题请按 [SECURITY.md](../../.github/SECURITY.md) 私下报告，不要提交公开 Issue。

## 许可证与合规

本项目基于 [MIT License](../../LICENSE) 发布。请仅处理你有权访问、下载、转写、翻译和再发布的内容，并遵守目标网站条款、版权限制及所在地法律。
