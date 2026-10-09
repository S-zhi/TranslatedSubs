# TranslatedSubs MCP Agent 使用指南

这份文档是 MCP Resource `subtitles://agent-guide` 的内容，描述 Agent 调用字幕处理工具时必须遵循的行为。
服务级 instructions 和每个工具的 description 也包含同一套关键约束，Host 可以选择读取本 Resource 获取完整说明。

## 服务边界

MCP Server 是业务 FastAPI 的适配层，只通过业务 API 编排任务。Agent 不应尝试直接访问业务 SQLite、业务数据目录，
也不应调用底层下载、识别、翻译或烧录函数。

## 必须遵循的调用顺序

```text
check_subtitle_setup
        ↓ setup 就绪
probe_video（可选，但推荐）
        ↓ URL 可解析
start_subtitle_pipeline
        ↓ 得到 task_id
get_task_status（轮询）
        ↓ status=SUCCESS
get_task_artifacts
```

调用 `start_subtitle_pipeline` 后，任务在后台执行。保存返回的 `task_id`，根据 `status` 和 `progress` 轮询，
不要因为任务已创建就假定视频或字幕已经可以下载。

## 初始化和密钥处理

第一次调用 `check_subtitle_setup` 时，先读取返回的 `ok`、`initialized`、`config_file`、`missing`、`agent_action` 和
`restart_required`：

- `agent_action=continue`：可以继续调用后续工具。
- `agent_action=ask_user_to_configure`：不要调用处理工具。根据 `missing` 的内容安装本地翻译依赖/模型，或按 `config_file` 指向的固定位置补齐云端配置；仅修改 `.env` 后才需要重启业务 FastAPI 服务。
- `agent_action=use_soft_burn_or_install_libass`：基础流水线已就绪，但硬字幕不可用。询问用户是否改用 `burn=soft`，或提示用户安装带 libass 字幕滤镜的 FFmpeg。
- `error_code=BUSINESS_UNAVAILABLE`：业务 API 没有运行，请用户先启动业务服务。

Agent 不得：

- 要求用户把 `REPLICATE_API_TOKEN`、`SUBTRANS_DEEPSEEK_API_KEY` 或 `DEEPSEEK_API_KEY` 作为 MCP 工具参数传入；
- 在对话、工具结果或日志中打印密钥实际内容；
- 自行生成、猜测或覆盖密钥；
- 在 setup 未就绪时反复调用 `start_subtitle_pipeline`。

配置完成并重启业务服务后，再次调用 `check_subtitle_setup` 验证，不要仅根据用户口头确认继续执行。

## 工具参数默认值

`start_subtitle_pipeline` 的默认值为：

- `source_lang=en`：默认本地翻译引擎使用英语源语言；
- `target_lang=zh-CN`：翻译为简体中文；
- `mode=mono`：单语字幕；
- `burn=hard`：将字幕烧录进视频，需要 FFmpeg 的 libass；
- `model=local:tiny`：使用本地 faster-whisper tiny 语音识别模型；
- `need_subtitle=true`：执行识别和翻译。

默认翻译引擎是本地 CPU 英译中。模型未下载时不要静默切换到 DeepSeek；先提示用户安装 `uv sync --extra local-translation` 并在 Web 设置中点击“下载并转换”。

用户没有明确指定时使用这些默认值。用户明确指定 `burn=hard` 时，不要在硬字幕不可用时悄悄改成 soft，应先说明并请求确认。

## 任务状态处理

常见状态包括：`PENDING`、`DOWNLOADING`、`EXTRACTING`、`TRANSCRIBING`、`TRANSLATING`、`BURNING`、`SUCCESS`、`FAILED`。

- `PENDING` 到 `BURNING`：继续间隔轮询 `get_task_status`，可向用户报告当前阶段和进度；
- `SUCCESS`：调用 `get_task_artifacts`，再把返回的下载地址和文件类型交给用户；
- `FAILED`：展示错误信息，只有用户确认后才调用 `retry_task`；
- `RESOURCE_MISSING`：产物已经不存在，提示用户重新运行任务；
- `TASK_NOT_READY`：不要把结果当作可下载产物，继续等待或先确认任务状态。
- `TASK_ALREADY_RUNNING`：同一 URL 已有近 10 分钟内的运行中任务；复用返回的 `task_id` 调用 `get_task_status`，不要再次创建。

## 常见错误与下一步

| 错误码 | Agent 下一步 |
| --- | --- |
| `INVALID_URL` | 请求用户提供合法的 `http://` 或 `https://` 视频页面地址。 |
| `PROBE_FAILED` | 把预检查错误告知用户，请求新的 URL；不要直接启动流水线。 |
| `NOT_INITIALIZED` | 根据 `missing` 安装本地依赖/模型或配置云端环境；修改 `.env` 后再重启业务服务。 |
| `LOCAL_TRANSLATION_MODEL_NOT_READY` | 提示用户在设置中下载并转换本地模型。 |
| `LOCAL_TRANSLATION_DEPENDENCY_MISSING` | 运行 `uv sync --extra local-translation`，无需重启服务。 |
| `HARD_BURN_UNAVAILABLE` | 询问是否改用 `burn=soft`，或提示安装带 libass 的 FFmpeg。 |
| `BUSINESS_UNAVAILABLE` | 提示用户启动业务 FastAPI，并重新检查 setup。 |
| `TASK_NOT_FOUND` | 确认 task_id 是否正确，必要时用 `list_tasks` 查找。 |
| `RESOURCE_MISSING` | 提示用户重新运行任务。 |
