# TranslatedSubs 当前架构

这张图按当前源码组织，展示入口、服务适配、任务编排、核心媒体处理、持久化和可选外部服务之间的关系。

![TranslatedSubs 当前架构图](assets/architecture.png)

```mermaid
flowchart LR
    subgraph Entry["入口层"]
        web["Web 工作台\nweb/ 原生 ES Modules"]
        mcphost["MCP Host\nCodex / Claude Desktop"]
    end

    subgraph Adapters["服务与适配层"]
        api["FastAPI App\nsrc.handler.app:app\nREST + SSE + 静态文件"]
        routes["HTTP 路由\ntasks / srt / storage / editor / settings / health"]
        mcp["MCP Server\nsrc.mcp_server.server\nstdio / Streamable HTTP"]
        driveui["Drive 视图\nweb/js/drive-api.js"]
        drive["Google Drive sidecar\ndrive-service · Go · :8787"]
        stt["可选独立转写服务\nsrc.transcriber_server:app · :8010"]
        abstract["可选 abstract-subtitle\nmodule/abstract-subtitle · HTTP"]
    end

    subgraph Application["应用服务层"]
        runner["任务执行器\nsrc/service/runner.py\nThreadPoolExecutor"]
        orchestrator["流水线编排器\nsrc/service/orchestrator.py\nPipelineHandler 责任链"]
        resolver["AssetResolver\n断点续跑 / 资源状态"]
        driveSync["DriveSyncManager\n批次、重试、取消"]
        helpers["辅助服务\nProbeBatchManager / RetentionScheduler\nRuntimeCheck / ModelManager"]
    end

    subgraph Pipeline["媒体处理流水线"]
        download["下载\nDownloadHandler + yt-dlp"]
        extract["音频提取\nAudioExtractionHandler + FFmpeg"]
        vocal["可选人声分离\nVocalSeparationHandler + Demucs/外部命令"]
        transcribe["语音识别\nTranscriptionHandler"]
        translate["字幕翻译\nTranslationHandler"]
        burn["字幕封装\nSubtitleBurningHandler + FFmpeg"]
        tts["可选配音\nKokoro/TTS + FFmpeg 混音"]
    end

    subgraph Storage["状态与产物"]
        sqlite["SQLite app.db\nTaskStore / ProbeStore / TranslationEngineStore"]
        taskdir["本地任务目录\ndata/{task_id}/\n源视频、audio.wav、SRT、output*.mp4"]
        models["模型目录\ndata/models/\nWhisper / 本地翻译模型"]
        config["配置\n.env + runtime settings\nSettings / config loader"]
    end

    subgraph Upstreams["上游与外部能力"]
        videos["视频站点\nYouTube / Vimeo / TikTok 等"]
        whisper["Whisper 后端\n本地 faster-whisper / Replicate"]
        llm["翻译引擎\n本地 CTranslate2 / OpenAI·Anthropic 兼容 API"]
        gdrive["Google Drive API\nOAuth + 断点传输"]
    end

    web -->|"REST 创建/查询、SSE 进度"| api
    mcphost -->|"MCP 调用"| mcp
    mcp -->|"HTTP 调用业务 API"| api
    api --> routes
    routes -->|"创建、重试、取消"| runner
    routes -->|"读写任务/探测/引擎"| sqlite
    routes -->|"字幕编辑、下载、清理"| taskdir
    api --> helpers

    runner -->|"提交后台任务"| orchestrator
    orchestrator --> download
    download --> extract
    extract --> vocal
    vocal --> transcribe
    transcribe --> translate
    translate --> burn
    burn --> tts

    download -->|"下载源视频"| videos
    transcribe -->|"选择识别后端"| whisper
    transcribe -.->|"HTTP backend"| stt
    transcribe -.->|"HTTP backend"| abstract
    translate -->|"选择翻译引擎"| llm

    orchestrator --> resolver
    resolver --> taskdir
    runner -->|"on_event 写状态/进度"| sqlite
    api -.->|"SSE 轮询状态"| sqlite
    transcribe --> models
    translate --> models
    config --> api
    config --> runner
    config --> orchestrator
    config --> helpers

    driveui -->|"Drive REST"| drive
    routes -->|"任务产物同步"| driveSync
    driveSync -->|"上传/下载/批次状态"| drive
    driveSync --> taskdir
    drive -->|"OAuth / resumable transfer"| gdrive
    drive -->|"导入文件时 POST /api/tasks/upload"| api
```

任务主链路是：`POST /api/tasks` 或上传接口创建 `PENDING` 记录，`runner` 把任务提交到线程池，`orchestrator` 按“下载 → 提取 →（可选）人声分离 → 转写 → 翻译 → 烧录 →（可选）配音”执行。每个阶段通过 `PipelineContext.emit()` 回写 SQLite，Web 端从 `/api/tasks/{id}/stream` 订阅 SSE；阶段产物保存在 `data/{task_id}/`，因此服务重启后可以按已有产物断点续跑。

MCP Server 只做业务 API 适配，不直接访问 SQLite 或核心流水线。Google Drive sidecar 也保持进程隔离：Python 服务负责任务和产物，Go sidecar 负责 OAuth、Google Drive 文件操作与断点传输，导入本地任务时再通过 HTTP 调用 Python 的上传接口。
