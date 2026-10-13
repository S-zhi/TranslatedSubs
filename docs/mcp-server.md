# MCP Server

MCP Server 是业务 FastAPI 的适配层，只通过 HTTP 调用业务 API，不直接访问业务 SQLite、任务目录或处理流水线。

## 启动服务

先安装并启动 Redis，复制机器本地引导配置，再启动业务 API：

```bash
cp config.example.json config.local.json
uv run python -m src.cli
```

`config.local.json` 的 `redis.url` 与 `redis.namespace` 标识配置 Redis；`mcp.baseUrl` 和 `mcp.timeout` 用于 MCP 连接业务 API，`mcp.transport`、`host`、`port`、`path` 控制 MCP 监听。业务 API Token、模型密钥和其他业务设置通过 Web 工作台设置页面或配置 API 保存到 Redis；修改后即时用于新请求，无需重启。

桌面 MCP Host 通常使用 stdio：

```bash
uv run python -m src.mcp_server.server
```

切换到 Streamable HTTP 时，编辑 `config.local.json` 中的 `mcp.transport`，并设置需要的监听地址、端口和路径，然后启动：

```bash
uv run python -m src.mcp_server.server
```

`mcp.host` 只控制 MCP Server 的监听地址；`mcp.baseUrl` 始终应指向业务 FastAPI，而不是 MCP 地址。生产环境请在受控网络中暴露 MCP，并按部署环境配置认证和 TLS。

## MCP 工具

- `check_subtitle_setup`：检查业务服务、本地模型/可选依赖、兼容密钥、FFmpeg 和存储目录。
- `probe_video`：预检测 URL，不下载文件。
- `start_subtitle_pipeline`：异步启动完整流水线并返回 `task_id`。
- `get_task_status`：查询任务状态和进度。
- `get_task_artifacts`：获取成功任务的视频和字幕下载地址。
- `list_tasks`：查看最近任务。
- `retry_task`：在用户确认后重试失败任务。

常见错误码及处理方式：`BUSINESS_UNAVAILABLE` 表示业务 API 未启动，`NOT_INITIALIZED` 表示流水线条件尚未满足，`LOCAL_TRANSLATION_MODEL_NOT_READY` 表示先下载本地模型，`LOCAL_TRANSLATION_DEPENDENCY_MISSING` 表示先安装 `local-translation` 可选依赖，`INVALID_URL`/`PROBE_FAILED` 表示需要修正视频地址，`HARD_BURN_UNAVAILABLE` 表示应改用软字幕或安装带 libass 的 FFmpeg，`TASK_NOT_READY` 表示继续轮询，`RESOURCE_MISSING` 表示重新运行任务。`TASK_ALREADY_RUNNING` 返回已有任务 ID 时应复用该任务，不要重复创建。

## Agent 调用顺序

```text
check_subtitle_setup → probe_video → start_subtitle_pipeline
→ get_task_status（轮询）→ get_task_artifacts（仅 SUCCESS）
```

`check_subtitle_setup` 返回 `agent_action=ask_user_to_configure` 时，根据 `config_file` 和 `missing` 提示用户通过业务设置页面或 API 配置。模型依赖、模型文件或 API 配置完成后无需重启业务服务，再调用检查工具验证就绪状态。

Agent 不应把 API Key 作为工具参数传递，也不应直接读写业务数据库或底层流水线。完整行为规范见 [MCP Agent 指南](./mcp-agent-guide.md)。
