# 本地快速启动

本文介绍如何运行 TranslatedSubs。业务配置保存在 Redis；机器本地的 Redis 地址、数据路径和监听参数保存在 `config.local.json`。

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

服务启动不要求本地翻译模型或可选依赖已经存在。要运行默认英语 → 简体中文翻译，再安装依赖并下载模型：

```bash
uv sync --extra local-translation
```

打开“设置 → 翻译引擎”，点击“本地 CPU 英译中”的“下载并转换”。模型就绪前，任务会提示缺少条件，不会自动切换到云端引擎。

Hugging Face Whisper 可选依赖为 `uv sync --extra hf-models`；Kokoro 配音使用 `uv sync --extra tts`，另外需要系统安装 `espeak-ng`。多个功能需要一起启用时，在同一次 `uv sync` 中列出所有 `--extra` 参数。Demucs 需单独安装，不属于 `pyproject.toml` extras；Google Drive sidecar 需要 Go 1.23 或更新版本。Node.js 仅用于运行前端测试。

验证服务：

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/health/ready
```

然后访问 <http://127.0.0.1:8000/>。MCP 配置也保存在 `config.local.json`；参见 [MCP Server 文档](./mcp-server.md)。

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
