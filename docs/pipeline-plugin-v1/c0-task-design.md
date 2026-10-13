# Pipeline Plugin v1：C0 决策与任务分派包

状态：**任务设计已完成；C0 契约代码、FakeContext、兼容矩阵尚未实现和冻结；M0 未通过，后续编码任务不得开工。** 本文依据附件《Pipeline Plugin v1：后端改造与 Agent 并行开发方案》和只读基线 `c5a8e40c2aabb38e01f394aa826366e0ad337a47` 编制。当前工作树是 detached HEAD。本文是分派输入，不声称已经完成 impact、测试或插件实现。

## C0：先冻结什么

C0 的代码 owner 使用 **GPT-6 Luna**，设计复核使用 GPT-6 Sol。C0 交付前须逐项给出确定的字段、类型、默认值、错误和测试；以下是待冻结决策，而不是已经生效的接口。

1. **身份与注册。** 确定七个稳定 stage ID（`download`、`audio_extract`、`vocal_separate`、`transcribe`、`translate`、`tts`、`render`）；`render` 明确 `subtitle`/`dub_mux` operation，分离明确 `asr`/`dubbing` purpose。定义 manifest 的 `plugin_id`、stage、协议版本、实现版本、operations/capabilities，重复 ID、stage 不符、协议不兼容的拒绝规则。区分实现身份 `plugin_id` 与现有翻译 `engine` 记录 ID。注册轻量工厂，不在注册时加载模型或联网。
2. **调用与生命周期。** 为每个 stage/operation 定义强类型 request/result，列清必需/可选字段、路径及类型的运行时检查。确定 `create_session(config, context) → validate(request) → run(request) → close()` 语义：每次调用独立 session；创建成功后任何结局均 close 一次；创建失败由工厂清理。跳过/缓存命中不建 session。明确结果验证何时完成、close 失败如何保留原错误。
3. **Context 与控制流。** 定义 `task_id`、`invocation_id`、工作目录、取消令牌、0–100/未知进度、子进程登记、执行时凭据解析接口；进度与取消仅由 Context 提供。取消优先于成功/普通错误，执行前后与发布结果前检查；不得承诺同步线程硬超时。Context 的用途要能区分 `extract.asr`/`extract.dubbing` 与 `render.subtitle`/`render.dub_mux`。
4. **产物与错误。** 宿主分配每次调用的目标路径，特别区分 `audio.wav`/`tts_source.wav` 和 ASR/配音 stems。结果声明必需产物与 metadata；Manager 验证存在、非空、任务目录边界和最低格式要求。定义稳定错误 code、stage、plugin_id、脱敏 message、cause、旧外部错误码映射；取消独立于普通 PluginError。首期不自动重试。
5. **计划、凭据和缓存。** 决定内部字段名（建议 `plugin_plan_json`）、schema 版本、每个调用的插件 ID/实现版本、非敏感有效配置与凭据引用。计划在创建任务并入队前持久化，去重不覆盖、重试沿用；执行时只重新解析凭据。检查 `TaskRecord.to_dict()` 及其他 API/SSE 序列化，禁止泄漏计划和密钥。定义旧任务空计划的一次性原子迁移、无法还原旧配置的边界、缺插件/版本/凭据时的失败规则。缓存指纹覆盖实现、用途、非敏感配置和输入；配置切换使下游失效，旧缓存仅走明确 legacy 路径。若合并了别的工作树的 runtime resolver，必须改为单一有效配置路径，不并行维护两套。
6. **兼容矩阵与示例。** 形成 URL/上传/仅下载、ASR 分离、单双语、hard/soft、TTS keep/lower/replace、配音降级、取消、重启/重试的旧状态、进度、产物、下载键映射。交付可运行的 FakeContext、最小假插件、契约测试和版本号。C0 owner 与设计 owner 审核后才标记“冻结”；变更需提高契约版本并广播所有下游 owner。

本期仍使用现有同步线程池和任务级串行阶段。主链故障令任务 FAILED；配音普通故障保留普通视频并设置 `tts_status=FAILED`；取消始终终止为 CANCELLED。`Manager` 不写任务数据库，插件不发任务成功事件，`AssetResolver` 管缓存/定位，`Runner` 管持久化、取消信号与子进程清理。这些职责也必须在 C0 测试示例中体现。

## 所有任务共同输入与变更规则

每个实现任务的派单必须附上：精确 HEAD/工作树状态、C0 契约版本及冻结记录、所有 `depends_on` 的已合入文件与测试证据、允许修改路径、实际底层函数签名和媒体产物语义、相关现有测试。完成回报必须含修改文件、验证命令/结果、未覆盖风险、接口变更申请。**依赖未满足时只允许只读调研或准备独立测试，不允许猜测接口编码。** 所有真实代码开发使用 GPT-6 Luna；GPT-6 Sol 负责设计和契约审查。

跨 owner 修改共享符号一律先提交接口变更申请：说明现有/建议签名与行为、调用方、兼容和迁移影响、所需测试、受影响 owner；由 C0 设计 owner 决定是否修订契约并通知所有下游。申请通过后由该文件 owner 改动。不得让适配器 owner 顺手修改 Runner/编排器/任务数据库。`task_lifecycle.py`、`task_store.py` 归 R1；`orchestrator.py`、`runner.py`、`asset_resolver.py`、`tasks.py` 的接线归 I1。`src/plugin_system/__init__.py` 的公共导出归 C0；H1 如需新增导出先申请，期间可直接从所拥有模块导入。A7 不修改旧 `_add_dubbed_video`，I1 负责替换。共享 FakeContext/fixture 仅 C0 owner 修改；其他 owner 自建局部 fixture。

仓库 AGENTS.md 要求**编辑任何既有符号前**运行 GitNexus `impact(..., direction="upstream")` 并报告直接调用者、流程和风险；HIGH/CRITICAL 须先告知用户；提交前运行 `detect_changes()`。当前无可调用 GitNexus、无仓库 `.claude/skills/gitnexus`，因此这些是后续编码与提交的**未满足门槛**，本轮没有执行或伪称执行。仓库还要求生成代码、库/API 文档、配置步骤使用 Context7；当前无可调用 Context7，后续涉及这些活动须先恢复工具。本轮仅制作文档并只读核对基线。

## 分派表

路径是建议的**独占写入范围**，新目录当前未建立。跨范围需求按上面的申请流程处理。测试文件也由对应 owner 独占；最终 E1 只补跨模块场景或将失败回派原 owner。

| 任务 / 依赖证据 | 独占写入范围与测试建议 | 交付及验收 |
|---|---|---|
| **C0** / 基线与附件已核对；无前置实现 | `src/plugin_system/__init__.py`（最小公共导出）、`contracts.py`、`manifest.py`、`context.py`、`errors.py`；`tests/test_plugin_contracts.py`、`tests/plugin_fakes.py`；本设计文档的后续契约版本记录 | 强类型七阶段、manifest/context/error、FakeContext、最小假插件、行为/产物兼容矩阵；契约测试通过且设计 owner 正式冻结，才放行 H1/R1/A1–A7/V1。当前仅设计文档完成。 |
| **H1** / C0 冻结版本与契约测试 | `src/plugin_system/registry.py`、`manager.py`；`tests/test_plugin_registry.py`、`tests/test_plugin_manager.py` | 重复注册/版本/类型拒绝，延迟加载，隔离 session，close 恰好一次，异常 cause 与取消不吞；对应单测通过。 |
| **R1** / C0 计划与脱敏契约 | `src/service/plugin_plan.py`、`src/store/task_store.py`、`src/service/task/task_lifecycle.py`；`tests/test_plugin_plan.py`、`tests/test_plugin_plan_lifecycle.py`、`tests/test_task_store.py` | URL/上传/MCP 入口经同一 resolver 固定计划；字段迁移、去重、重试、旧任务原子补写及 API 序列化脱敏经测试；`tests/test_api.py` 只做回归运行，修改归 I1。R1 不改 Runner；MCP 如需改入口，向 I1 申请接线。 |
| **A1** / C0 download/probe 契约、现有下载及探测函数签名 | `src/plugins/builtin/downloader_ytdlp.py`；`tests/test_plugin_downloader.py` | download/probe 共用选择逻辑，保留清晰度、标题、source 路径、进度/取消；mock 单测和现有底层签名核对。探测端点接线归 I1。 |
| **A2** / C0 audio_extract 目标路径契约、`extract_audio` 现有语义 | `src/plugins/builtin/audio_ffmpeg.py`、确需扩展时 `src/core/audio_extractor.py`；`tests/test_plugin_audio.py`、`tests/test_audio_extractor.py` | 默认 `audio.wav` 兼容，显式 `tts_source.wav` 不覆盖 ASR 音频，采样率/声道/时长回报正确；相关测试与小媒体检查。 |
| **A3** / C0 separation purpose/stems 契约、现有分离函数签名 | `src/plugins/builtin/vocal_demucs.py`、确需扩展时 `src/core/vocal_separator.py`；`tests/test_plugin_vocal.py`、`tests/test_vocal_separator.py` | ASR 人声与配音背景/人声按用途产生，缓存用途区分，取消正确映射；对应测试通过。 |
| **A4** / C0 transcribe 契约、当前 backend/model 配置与输出语义 | `src/plugins/builtin/transcriber_whisper.py`；`tests/test_plugin_transcriber.py` | 保留现有 local/model/backend 选择和原文 SRT 产物；取消、错误、空产物测试通过。 |
| **A5** / C0 translate 契约、现有 TranslationEngineStore 配置语义 | `src/plugins/builtin/translator_engine.py`；`tests/test_plugin_translator.py` | `engine` 映射内置实现但不改记录身份，语言对及单双语结果兼容；不自行切换引擎；成功/异常/取消单测通过。 |
| **A6** / C0 TTS 契约、现有时间轴与音色函数签名 | `src/plugins/builtin/tts_kokoro.py`；`tests/test_plugin_tts.py` | auto 音色、单双语去重、时间轴与 speech 音频正确；只合成，不混音/写任务状态；对应测试通过。 |
| **A7** / C0 render 两 operation 契约；最终互操作验收另需 A2/A3/A6 产物证据 | `src/plugins/builtin/subtitle_ffmpeg.py`；`tests/test_plugin_render.py` | hard/soft 与 keep/lower/replace，字幕轨、文件名和混音结果兼容；插件单测及 A2/A3/A6 产物互操作检查通过；不改编排器。 |
| **V1** / C0 兼容矩阵和 FakeContext | `tests/test_pipeline_plugin_compat.py` 与自身局部 fixture | 先写 FakePlugin 的 URL/上传/仅下载、字幕、配音降级、取消、缓存与计划固定场景；接口可导入，最终接线后由 E1 跑通，不通过则回派归属 owner。 |
| **G1** / H1 与 A5 已通过各自单测 | `tests/test_plugin_vertical_slice.py` | 真实宿主 + 翻译适配器 + 假底层翻译函数，成功/失败/取消均完成清理；发现契约缺陷先回 C0 修订，不私改 H1/A5。 |
| **I1** / R1、G1、A1–A7 单测及 A2/A3/A6/A7 互操作证据 | `src/service/orchestrator.py`、`runner.py`、`asset_resolver.py`、`src/handler/tasks.py`、`src/plugins/__init__.py`（必要的最小包初始化）、`src/plugins/builtin/__init__.py`（内置注册）；其 owner 管理的 `tests/test_orchestrator.py`、`tests/test_runner.py`、`tests/test_asset_resolver.py`、`tests/test_api.py` 及必要 MCP 接线测试 | 默认注册/注入宿主，Runner 消费固定计划，编排器经宿主执行七能力，缓存/事件/probe 接线，旧响应与下载键兼容；不残留直接媒体能力调用；相关既有测试通过。 |
| **E1** / I1 与 V1 已合入并通过各自门槛 | `tests/test_pipeline_plugin_e2e.py`、`docs/pipeline-plugin-v1/acceptance.md`、必要用户/开发文档；原 owner 文件仅经回派修改 | 用小媒体覆盖 FFmpeg 提取、hard/soft、配音混音，运行兼容矩阵及实际受影响测试，报告具体命令/结果、未执行 live/模型 smoke 的条件、残余风险；最后检查 `detect_changes()` 与精确 diff。 |

## 四槽排程与放行点

四槽包含协调/设计 owner，不能把七个适配器同时派给七个 Agent。**当前只完成了准备任务；C0 实现/冻结及后续编码都未开始。** 仅可准备 C0 实现输入和只读检查签名/测试；恢复工具、完成 C0 契约、FakeContext、矩阵并经审阅后才进入 M0。

| 波次 | 槽 0：设计/协调 | 槽 1 | 槽 2 | 槽 3 | 放行条件 |
|---|---|---|---|---|---|
| 0 | C0 设计复核、冻结决议 | C0 Luna 实现 | 只读底层签名清单 | 只读测试清单 | C0 测试、兼容矩阵、工具门槛通过；发布契约版本 |
| 1 | 契约变更仲裁 | H1 | R1 | A5 | H1/A5 各自单测通过 |
| 1b | G1 设计验收 | G1（Luna 实现） | R1 收尾或只读准备 | 下一波只读准备 | G1 通过后推进集成前置任务 |
| 2 | G1 问题回派 | A1 | A2 | A3 | 各插件单测独立交付 |
| 3 | V1 兼容场景审核 | A4 | A6 | A7 | A7 完成后，与 A2/A3/A6 做配音产物互操作检查 |
| 4 | 集成审查 | I1（单 owner 接线） | V1 补场景 | 只读集成评审 | R1/G1/A1–A7/互操作均通过才开始 I1 |
| 5 | 验收审查 | E1 | 原 owner 定点修复 | 原 owner 定点修复 | I1/V1 通过，完成实际测试、文档、diff 与图谱检查 |

若 G1 或互操作检查揭示契约缺陷，暂停受影响任务，由 C0 发布修订版本和迁移说明后重跑相关测试。M1 是宿主/适配器独立可用，M2 是 I1/V1/E1 旧行为验收完成；这两个门槛目前均未满足。真实云服务与下载用例仍只在具备环境时显式执行 `SUBTRANS_LIVE_TEST=1`。
