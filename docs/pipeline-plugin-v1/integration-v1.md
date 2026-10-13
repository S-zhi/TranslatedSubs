# Pipeline Plugin v1：I1 接线规范

最终状态：I1 已实施，本文末尾审查指出的取消竞态和 Cookie Path 问题均已修复并通过回归；统一 E1 结果见 [验收报告](validation-report.md)。下文设计输入和阶段性审查保留为决策记录。

设计依据：`contracts-v1.md` v1.1、当前 `PluginManager.run/run_probe`、`plugin_plan.ensure_plan/parse_plugin_plan`、现有 Handler 链与 Runner/AssetResolver。I1 由 GPT-6 Luna 实现；修改既有符号前按 AGENTS.md 做 impact，高风险先报告。I1 独占 `src/service/orchestrator.py`、`runner.py`、`asset_resolver.py`、`src/handler/tasks.py`、`src/plugins/__init__.py`、`src/plugins/builtin/__init__.py` 及对应旧测试。**新增 I1 独占 `src/service/probe_batch.py`、`tests/test_probe_batch.py` 的探测注入接线**，避免启动批次继续绕过宿主；`task_lifecycle.py`、`task_store.py` 仍归 R1。

## 入口与单次调用

保留当前六个 Handler 的顺序和主任务/可选配音分支，不增加同任务阶段并行。`run_pipeline(params, on_event, *, plugin_manager: PluginManager, plugin_plan: Mapping, credential_resolver: Callable[[str], str], legacy_cache: bool = False, handler_chain=None)` 是正式注入入口；测试可传 FakeManager/FakePlugin，生产由 Runner 注入。`api_key/engine_config` 的旧测试兼容形参若暂留，只能用于旧测试适配，生产路径不能再据此直接调媒体 core。`PipelineContext` 保存 manager、只读计划、credential_resolver、legacy_cache、已发布产物/当前 invocation；每次宿主调用创建新的 `PluginContext`，`purpose` 与 Request 同为完整 canonical 槽名，`invocation_id` 为新 UUID，`task_dir` 为当前任务目录。`check_cancelled` 连接现有任务取消标记，进度按 purpose 映射 `_BANDS`，子进程登记连接 Runner 的 register/unregister，凭据只在执行时解析。Manager 调用固定为 `run(slot.plugin_id, slot.implementation_version, request, slot.config, context)`，从不从环境或当前默认重新选插件。

Runner `_run(task_id)` 在读取 TaskRecord 后先记录 `legacy_cache = rec.plugin_plan_json is None`，随后 `rec = ensure_plan(rec, _store, _engine_store, settings)` 并 `plan = parse_plugin_plan(rec.plugin_plan_json)`；计划无效或迁移失败按稳定错误码失败，不入流水线。`ensure_plan` 已保证 NULL 计划的比较并交换原子持久化；重试/恢复只用已有 JSON。Runner 不在仅下载任务上预先查询翻译引擎。凭据闭包只接受三个引用：`translation_engine:<engine_id>:api_key`、`settings:transcriber_api_key`、`settings:cookies_file`。翻译凭据从固定 engine ID 的当前记录读取**仅 API Key**；插件 ID、api_type/base_url/model 等取计划，不能被新记录覆盖。HTTP 转写可合法无 Key（ref=null）；引用存在却缺值时抛 `credential_missing`。Cookie 引用只返回当前路径字符串，不记录内容、路径或 URL 中的秘密。执行时凭据轮换可生效，删除则明确失败。

各 Handler 从 `slots` 读取该调用的 ID/版本/config，构造 `contracts-v1.md` 的强类型 Request 和宿主分配路径，调用 Manager，再将 Result 路径放入现有 `PipelineResources`。映射固定：URL 下载→`download`（上传不调用）；ASR 提取→`extract.asr`；开关启用时人声分离→`separate.asr`；转写→`transcribe`；翻译→`translate`；普通封装→`render.subtitle`；配音启用时高质量提取→`extract.dubbing`、TTS→`tts`，`lower/replace` 再→`separate.dubbing`，最后→`render.dub_mux`。`keep` 不分离。TTS 适配器只生成 `dubbed.wav`，混音/封装仅 A7 render 插件执行。仅下载任务不得初始化 ASR、翻译、TTS 或 render session。所有默认媒体执行都经 Manager；I1 删除生产链中 `download_video/extract_audio/separate_vocals/transcribe/translate_srt/generate_timeline_audio/burn_subtitles/run_ffmpeg` 的直接调用和 FFmpeg 命令，不能保留第二条生产路径。

外部事件和输出键保持：DOWNLOADING、EXTRACTING、TRANSCRIBING、TRANSLATING、BURNING、SYNTHESIZING、DUBBING，终态 SUCCESS/FAILED/CANCELLED；仅下载下载进度映射 0–100，其余沿旧 `_BANDS` 且整体单调。完成时 `outputs.video/subtitle/dubbedVideo` 与 TaskRecord 的旧文件名/下载地址一致。主链普通错误 FAILED；配音普通错误保留普通视频并置 `tts_status=FAILED`，不发布假的 dubbedVideo；任何取消都转 CANCELLED，不走配音降级。`PluginError.to_dict()` 的 code/message 可供旧错误映射，内部 `cause` 不进入 API/SSE/日志：**不能对 PluginError 使用 `logger.exception` 或输出 traceback/cause**；未知异常也仅对外给通用脱敏消息。发布 SUCCESS 或进度前检查取消，防止迟到回调覆盖 CANCELLED。

## 缓存与旧任务

I1 在 AssetResolver 增加按用途的缓存检查/sidecar 写入。sidecar 名建议为 `<purpose>.plugin-meta.json`，内容严格为 `schema_version=1, stage, operation, purpose, plugin_id, implementation_version, config_sha256, input_sha256, output_names`；规范 JSON 排序/紧凑编码，输入本地文件用流式 SHA-256，URL 等非文件输入用规范 JSON，绝不放凭据。写 sidecar 用任务目录内临时文件加原子 replace；仅在 Manager 验证结果、取消检查和产物发布成功后写入。复用条件是必需文件非空、sidecar 全字段匹配当前计划与输入、对应输出名一致。**下游 `input_sha256` 同时覆盖每个输入文件的内容 SHA-256 与其生产调用 sidecar 的规范 JSON SHA-256**，即使新插件/配置产出逐字节相同的文件，上游 provenance 变化仍使下游失效；上传 `source.*` 无插件上游 sidecar，仅使用其内容哈希和 source_type=upload。失效时删除该节点旧 sidecar，重算后再处理下游，绝不能仅凭文件名复用旧 output。上游失败或 sidecar 不匹配时不得把旧输出当成下游输入。凭据轮换不进入指纹，因为 v1 只固定凭据引用，不固定其值。

NULL 旧计划第一次 `ensure_plan` 的 `legacy_cache=True` 只允许**本次迁移运行**按现有 AssetResolver 的兼容规则复用无 sidecar 产物（含 `output.mp4` 旧名）。每复用一个旧产物，先检查旧用途/模式与现有任务字段相容、文件非空及输入文件存在，然后立即补写当前计划对应的 sidecar，记录“按迁移时配置接纳”的 provenance；后续重启 `legacy_cache=False`，只按 sidecar 匹配复用。无法追溯旧任务创建时未记录的配置，这一限制保留在迁移说明中。若旧成品与当前 mode 不符或插件选择显式被改，不能贴新 sidecar 冒用：强制重算。切换插件、实现版本、非敏感配置、输入内容或 purpose 时，该节点及所有下游重算；只改变 TTS/配音配置不破坏已发布普通视频。缓存检测与 sidecar 应覆盖 `download`、两次提取、两次分离、转写、翻译、两种 render、TTS；stage 被禁用时不写空 sidecar。

取消清理按**本次 invocation 尚未发布的产物集合**进行，且连同其临时文件/sidecar 清理；已发布普通视频及原字幕在配音失败或配音阶段取消时保留。现有 Runner `_cleanup_partial_artifacts` 无条件删普通视频、`run_pipeline` 与 Runner 都会清理，I1 必须改成统一的调用级清理入口，避免双重广域删除；既有 `AssetResolver.cleanup_cancelled_artifacts` 的按 current_step 列表也须收窄到未完成产物。取消后清子进程，结果发布前与 Runner 更新终态前再查取消；不能让迟到 SUCCESS 重写 CANCELLED。

## probe 和配置选择

R1 的 `resolve_plugin_plan(..., plugin_defaults=...)` 已支持后端映射：显式 `plugin_defaults` 非 None 时优先且不读环境；否则每次创建读取一次 `SUBTRANS_PIPELINE_PLUGINS` JSON，对内置默认按 stage 覆盖。JSON 只允许七个 stage 键，每个值必须恰好 `{ "plugin_id": 非空字符串, "implementation_version": 非空字符串 }`；未知 stage/额外键/无效 JSON 拒绝。创建任务持久化选定 ID 与版本，执行时不再读环境。`validate_plugin_plan` 已允许 transcribe 的 ref 为 `null` 或 `settings:transcriber_api_key`、translate 的 ref 为 `null` 或 `translation_engine:<engine_id>:api_key`，cookies ref 为 `null` 或 `settings:cookies_file`。

探测无任务计划，`src/handler/tasks.py` 的兼容 `probe_video` 包装函数应读取**当前**下载 stage 映射，创建 `ProbeRequest` 和 purpose=`download` 的只读 Context，调用 `manager.run_probe(plugin_id, version, request, config, context)`，再适配为当前 core/HTTP `ProbeResult` 形状；`task_runtime.probe_and_record`、HTTP 响应及手动记录不改公开字段。下载任务创建时由同一映射选择并固定，不要求探测历史与未来任务永久绑定。R1 已公开只读 `resolve_plugin_defaults` 供 I1 获取下载身份；probe 还必须获得完整下载 config，可调用 R1 `resolve_plugin_plan` 的仅下载分支（`source_type=url, need_subtitle=False, quality=480p`，其余非字幕参数按原默认）并取 `slots.download.config`，或由 R1 抽出同一下载配置解析 helper；不能自行拼不完整 config，因为 A1 验证全部八个键。`ProbeBatchManager` 当前在 `_probe_one` 直接调用 core `probe_video`；I1 将 probe provider 作为 `start(..., probe_provider=...)` 注入并传入启动批次线程，不改其状态/记录行为。启动批次也必须走同一包装函数和默认映射；对应 `tests/test_probe_batch.py` 改由 FakeProvider 断言，而不能留生产直调分叉。探测失败的日志只写站点 ID、安全错误码，不输出可能含 Cookie/令牌的 cause。

## M1 → I1 只读复核

当前 R1 `resolve_plugin_plan` 生成的 download/audio/separation/transcribe/translate/TTS 配置键与对应适配器的必需键一致；转写凭据引用、local CPU/int8/受管目录和翻译引擎快照也一致。已知 `render` 的配置不匹配由 A7 owner 修复后须重跑其单测与真实封装互操作，**未修复前不放行 I1**。R1 retry 对旧记录保留 NULL，Runner 首次 `ensure_plan` 后才有 `legacy_cache=True` 的迁移窗口；I1 不能在 retry 入口提前写计划。A1/A4/A5 的凭据只在 Context 执行时解析，不得把 `PluginError.__cause__` 写入日志或事件。I1 还须把 Runner/AssetResolver 现有按状态广域删除逻辑改成 invocation 级未发布产物清理，否则配音取消会误删普通视频。此处其余为 I1 需实现的接线与缓存工作，不是 M1 适配器代码缺陷。

I1 至少调整 `tests/test_orchestrator.py`、`test_runner.py`、`test_asset_resolver.py`、`test_api.py`、`test_probe_batch.py` 中旧直接函数 mock 为 FakeManager/FakePlugin 注入，保留对现有状态、产物和下载键的断言；必要 MCP 创建入口也需覆盖。改完先跑这些相关文件与新插件/计划/兼容测试，再由 E1 做小媒体验收与最终 diff 检查。

## 创建时 readiness：按已选实现检查

R1 的 URL/上传/MCP 创建入口应先解析并校验非敏感计划，再看 `slots.transcribe`、`slots.translate` 的**实际 plugin_id 与 backend/api_type**决定 readiness；入库前完成这些检查，去重命中旧任务不得覆盖旧计划。仅下载任务无字幕槽，不触发 Whisper/翻译 readiness。规则如下：

1. 仅当 `slots.transcribe.plugin_id == "builtin.whisper"` 时沿用已移除 Replicate 的现有 409 校验；其中 `config.backend == "local_whisper"` 时再调用现有 `_ensure_local_model_ready` 检查本地模型，保留 `MODEL_NOT_READY`。对于未带 `local:` 前缀的模型名，要用计划中的 `config.model` 构造等价本地标识后检查，避免旧 helper 因无前缀跳过检查。HTTP backend 和替换转写插件都不做本地模型检查，依赖在对应 session `validate/run` 明确报错。不能仅凭请求 `model=local:tiny` 阻止自定义转写插件。
2. 所有需字幕的任务仍要求翻译 `engine` 记录存在，以构建固定的 `api_type/base_url/model/engine_id` 计划，保持不存在记录的 422；引擎停用的现有 422 也保持。**仅当** `slots.translate.plugin_id == "builtin.translation_engine"` 时执行内置翻译 readiness：`local_ct2` 的 en→zh 语言限制、本地模型/依赖检查（原 `LOCAL_TRANSLATION_MODEL_NOT_READY` 等 409），以及云引擎 API Key/availability 检查（原 422）。替换翻译插件仍收到固定 engine 配置，但不因内置 local_ct2 模型或云 API Key 当前缺失而在创建时失败；其自己的依赖在 session 检查。`plugin_plan.resolve_plugin_plan` 里的 local_ct2 语言限制也须按选中的翻译 plugin_id 条件化，否则表面跳过 readiness 仍会在解析阶段误拒绝自定义插件。
3. 不因本期改动延后现有请求形状、上传格式和 TTS 参数校验；这些与选用插件无关的 API 422/400 保留。执行时凭据被轮换或删除，仍按计划引用解析，不以创建时检查当成执行时保证。创建阶段验证失败不能留下任务行或触发入队。

这项调整归 R1 的 `task_lifecycle.py`/`plugin_plan.py` 及其独占测试；I1 只消费已持久化计划，不在 Runner 重新做创建时 readiness。对 `builtin.whisper` 本地、HTTP、自定义转写及 `builtin.translation_engine` local/云、自定义翻译分别测 URL 与上传；断言旧错误码和未入队行为。正式媒体互操作已报告 29 passed，A7 配置修复已通过；这不替代 readiness 场景测试。

## I1 落地后的取消竞态复核（待修）

当前生产主链七阶段均已通过 Manager，probe 包装与启动批次也走 `run_probe`；缓存已把上游 sidecar 哈希写入下游指纹，首次 NULL 计划兼容复用会补写 sidecar。以下是代码只读复核发现的具体收尾门槛，不能据已通过的媒体 smoke 与兼容测试宣布 I1 完成：

- `_run` 读取旧记录后、注册取消信号前，用户可能已取消。现 `register_cancellation_signal(..., is_cancelled=False)` 会清掉这段间隙已置位的 Event；需让读终态与注册串行，或保留已有置位信号并重新核对数据库终态。
- `ensure_plan/parse_plugin_plan` 异常分支当前直接写 `FAILED`，未获取 `_task_state_lock`、未复核已取消终态，可能覆盖并发写入的 `CANCELLED`。须与其他失败分支使用同一状态锁和终态检查。
- 插件主动抛 `PluginCancelled` 而尚无用户取消信号时，编排器会转成 `PipelineCancelledError`，Runner 通用异常分支当前记 `FAILED/execution_error`。应按取消控制流落 `CANCELLED`，覆盖此场景；检查取消标记与写终态必须在锁内串行。

另需保证 `settings.cookies_file` 的 `Path` 值可由普通执行与 probe 凭据解析器转换成路径字符串。`PipelineContext.invoke` 完成取消检查到写 sidecar 之间还存在极窄发布窗口；若要强保证取消后不发布该次 invocation，应在发布临界区增加最终取消复核或同锁串行。以上建议用确定性线程事件测试验证，不以睡眠碰运气。
