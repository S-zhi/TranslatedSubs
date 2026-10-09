# MCP Server

MCP Server 是业务 FastAPI 的独立适配层。它不直接访问 SQLite，也不直接调用
下载、识别、翻译和烧录函数，而是通过 HTTP 调用业务 API。

## 启动业务服务

默认本地翻译不需要 API Key。需要使用云端兼容引擎时，再在项目根目录的 `.env` 配置对应密钥：

```ini
# 可选：云端识别/翻译兼容配置
REPLICATE_API_TOKEN=your-replicate-token
SUBTRANS_DEEPSEEK_API_KEY=your-deepseek-key
```

然后启动业务服务：

```bash
uv run uvicorn src.handler.app:app --port 8000
```

业务服务可以在没有本地翻译可选依赖和模型文件时启动。要运行默认的本地英语 → 简体中文翻译，先执行 `uv sync --extra local-translation`，再在 Web 设置中点击“本地 CPU 英译中”的“下载并转换”；模型未就绪时，`check_subtitle_setup` 会报告缺少的安装条件。

可以先检查业务服务是否就绪：

```bash
curl http://127.0.0.1:8000/api/health/ready
```

## 启动 MCP Server

桌面 MCP Host 通常使用 stdio：

```bash
uv run python -m src.mcp_server.server
```

MCP Server 默认访问 `http://127.0.0.1:8000`。如果业务服务地址不同，设置：

```bash
export SUBTRANS_API_BASE_URL=http://127.0.0.1:8000
```

也可以启动 Streamable HTTP：

```bash
SUBTRANS_MCP_TRANSPORT=streamable-http \
  uv run python -m src.mcp_server.server
```

默认地址为 `http://127.0.0.1:3001/mcp`。如需让远程 Host 访问，可通过环境变量调整监听地址、端口和路径：

```bash
SUBTRANS_MCP_TRANSPORT=streamable-http \
SUBTRANS_MCP_HOST=0.0.0.0 \
SUBTRANS_MCP_PORT=3001 \
SUBTRANS_MCP_PATH=/mcp \
uv run python -m src.mcp_server.server
```

`SUBTRANS_MCP_HOST` 只控制 MCP Server 的监听地址；`SUBTRANS_API_BASE_URL` 仍必须指向业务 FastAPI，而不是 MCP 地址。生产环境请在受控网络中暴露 MCP，并按部署环境配置认证和 TLS。

## MCP 工具

- `check_subtitle_setup`：检查业务服务、本地模型/可选依赖、兼容密钥、FFmpeg 和存储目录。
- `probe_video`：预检测 URL，不下载文件。
- `start_subtitle_pipeline`：异步启动完整流水线并返回 `task_id`。
- `get_task_status`：查询任务状态和进度。
- `get_task_artifacts`：获取成功任务的视频和字幕下载地址。
- `list_tasks`：查看最近任务。
- `retry_task`：在用户确认后重试失败任务。

常见错误码及处理方式：`BUSINESS_UNAVAILABLE` 表示业务 API 未启动，`NOT_INITIALIZED` 表示流水线条件尚未满足，`LOCAL_TRANSLATION_MODEL_NOT_READY` 表示先下载本地模型，`LOCAL_TRANSLATION_DEPENDENCY_MISSING` 表示先安装 `local-translation` 可选依赖，`INVALID_URL`/`PROBE_FAILED` 表示需要修正视频地址，`HARD_BURN_UNAVAILABLE` 表示应改用软字幕或安装带 libass 的 FFmpeg，`TASK_NOT_READY` 表示继续轮询，`RESOURCE_MISSING` 表示重新运行任务。`TASK_ALREADY_RUNNING` 返回已有任务 ID 时应复用该任务，不要重复创建。

## Agent 自主发现与执行

MCP Server 在服务级 metadata 中提供了工作流 instructions，每个工具也提供了明确的用途、前置条件、参数说明和
下一步动作。完整的 Agent 行为规范通过 MCP Resource `subtitles://agent-guide` 暴露，Host 可以在连接后读取该资源。

Agent 应遵循：

```text
check_subtitle_setup → probe_video → start_subtitle_pipeline
→ get_task_status（轮询）→ get_task_artifacts（仅 SUCCESS）
```

`check_subtitle_setup` 返回的 `agent_action` 是机器可执行的决策提示：

- `continue`：可以继续处理；
- `ask_user_to_configure`：根据 `config_file` 和 `missing` 提示用户修改固定 `.env` 并重启业务服务；
- `use_soft_burn_or_install_libass`：询问是否使用 `burn=soft`，或提示安装带 libass 的 FFmpeg。

Agent 不应把 API Key 作为工具参数传递，也不应直接读写业务数据库或底层流水线。

## 初始化失败时的行为

MCP 不会要求模型传递 API Key，也不会把密钥写入 MCP 配置。本地模型条件缺失时，
`check_subtitle_setup` 会返回缺失项和安装提示；安装或下载完成后无需重启业务服务。
只有修改业务 `.env` 中的云端配置时，才按提示重启服务。
