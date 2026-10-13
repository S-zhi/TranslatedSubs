# Pipeline Plugin v1 验收报告

工作树：`/Users/wenzhengfeng/.codex/worktrees/e307/TranslateSub`。基线提交：`c5a8e40c2aabb38e01f394aa826366e0ad337a47`，detached HEAD。所有代码仍为本地未提交改动，没有创建 PR 或合并。

## 交付状态

C0/H1/R1/G1/A1–A7/I1/V1/E1 共 14 项任务完成，M0/M1/M2 通过。设计与只读设计复核由 GPT-6 Sol 完成；代码及测试实现由 GPT-6 Luna 完成。

| 范围 | 实际交付 |
|---|---|
| 宿主与契约 | 强类型 request/result、manifest、registry、单次 session 生命周期、运行时校验、错误和取消边界 |
| 任务计划 | URL/上传及 MCP 经 REST 的统一创建路径、内部计划持久化、NULL 旧记录原子迁移、重试保留快照、当前凭据解析 |
| 七项能力 | 下载/probe、提取、分离、转写、翻译、TTS、字幕/配音封装全部通过 Manager 调用 |
| 缓存和取消 | 配置与上下游来源指纹、旧产物有限迁移、取消清理未发布产物、保留已发布普通视频、终态写入串行化 |
| probe | 手动与批量探测使用同一当前下载插件选择，保留原公开结果结构 |

前端、插件发现/热替换、独立插件配置页面和第三方代码沙箱不属于本期交付。

## 统一测试

主协调在当前工作树执行以下命令，结果 **562 passed，4 warnings，8.89s**。这是受本次改动影响的相关后端范围，不是整个仓库或前端全量测试。

```sh
UV_CACHE_DIR=/private/tmp/pipeline-plugin-uv-cache \
UV_PROJECT_ENVIRONMENT=/Users/wenzhengfeng/code/computer_logic/TranslateSub/.venv \
uv run --no-sync pytest -q \
  tests/test_plugin_*.py tests/test_pipeline_plugin_*.py \
  tests/test_orchestrator.py tests/test_runner.py tests/test_asset_resolver.py \
  tests/test_task_store.py tests/test_api.py tests/test_probe_batch.py tests/test_probe_store.py \
  tests/test_mcp_tools.py tests/test_mcp_client.py tests/test_mcp_metadata.py \
  tests/test_audio_extractor.py tests/test_downloader.py tests/test_translator.py \
  tests/test_vocal_separator.py tests/test_transcriber.py tests/test_transcriber_server.py \
  tests/test_subtitle_burner.py tests/test_tts.py tests/test_ffmpeg_utils.py \
  tests/test_storage.py tests/test_artifact_store.py
```

4 条警告来自现有 FastAPI `on_event` 生命周期 API 弃用提示。没有同步、安装或修改共享 Python 环境。各 Agent 的定向结果与统一范围存在重叠，不叠加为总测试数。

重点回归覆盖 URL/上传/仅下载，hard/soft、mono/bilingual，ASR 分离计划与当前设置相反，TTS 四节点失败/取消，keep/lower/replace，插件缺失/版本不符/依赖缺失，配置快照/凭据轮换和删除，重启/重试/旧计划迁移，以及上游换插件或配置但输出字节相同的下游失效。Runner 覆盖启动注册与取消交错、计划失败不覆盖取消、插件主动取消和迟到终态；编排器另覆盖缓存元数据写入期间取消。

## 媒体 smoke 与检查

真实 FFmpeg/WAV 已验证 ASR/配音提取、hard/soft 字幕封装、三种配音混音模式、字幕轨保留及实际音频能量差异。正式 `run_pipeline` smoke 使用生产 Manager 和官方 factories，覆盖 URL/上传、默认 `auto` voice、ASR 分离、配音和第二次缓存复用；URL 的底层下载使用本地 fixture 复制，没有网络下载。

Whisper、Demucs、Kokoro 和翻译服务的重型或外部能力使用测试替身，未执行真实模型推理或云服务调用。未启用 `SUBTRANS_LIVE_TEST=1`，未运行前端测试。真实模型安装、外部服务和站点当前可用性仍需在相应部署环境单独验证。

已运行 GitNexus `detect-changes -s all -r TranslatedSubs-e307`；最终摘要为 21 个 tracked 文件、233 个符号、64 条受影响流程，风险为 **CRITICAL**，公共创建/执行/缓存/取消路径的影响已向用户报告。增量刷新曾遇到 FTS 不一致，使用 `analyze --force --index-only --skip-git` 重建成功后重新完成检查，最终索引为 5,828 nodes / 14,494 edges / 296 flows。图谱摘要只统计 tracked diff；新增插件、契约、计划和测试文件另按 `git status --untracked-files=all` 核对，不能把图谱文件数当作完整改动文件数。

`git diff --check` 通过。生产编排器已无直接下载、提取、转写、翻译、TTS、分离、字幕烧录或 FFmpeg 命令拼装；这些执行入口均在官方适配器内。探测经 `run_probe` 接线。

旧 NULL 任务只能使用迁移时配置生成计划，无法恢复历史上未记录的选择；首次迁移的相容旧产物会补写 sidecar，显式替换插件不得冒用旧产物。用法见 [配置与迁移说明](operations.md)。
