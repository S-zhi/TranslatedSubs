# Pipeline Plugin v1 契约（设计定稿）

状态：**C0 frozen v1.1 / protocol_version=1 / plan schema_version=1**。v1.1 仅补齐配置快照清单，不改变 C0 Python 类型或计划 schema。C0 类型、manifest、Context、错误及 FakeContext 契约测试已通过；**H1 宿主及后续适配器/M0 集成尚未完成**。依据当前 `c5a8e40` 源码与 `source-plan.md`；若后续实现发现签名冲突，先走任务卡的接口变更申请，不由适配器私改契约。

## 1. 身份、调用与边界

七个 stage 为 `download | audio_extract | vocal_separate | transcribe | translate | tts | render`。流水线有八个 operation：前六个 stage 各一个默认 operation（字符串同 stage），`render` 有 `subtitle`、`dub_mux` 两个。`download.probe` 是额外的**无任务只读查询能力**，不计入八个流水线 operation、不创建 TaskRecord 或产物；同一下载插件可声明 `probe` capability。所有 stage/operation 为 `Literal` 或 Enum，不能接受任意字符串。

`PluginManifest`（冻结字段）：`plugin_id: str`（非空、稳定、全局唯一）、`stage: StageId`、`protocol_version: int = 1`、`implementation_version: str`（非空）、`operations: frozenset[str]`、`capabilities: frozenset[str] = frozenset()`。注册时拒绝重复 `plugin_id`、stage 与工厂不符、协议不等于 1、缺少该 stage 的必需 operation。`plugin_id` 表示实现，不等于任务的翻译 `engine` 配置 ID；内置翻译适配器通过 `engine` 找到固定配置快照。注册仅保存轻量 factory，不加载模型或网络依赖。

`JSONScalar = str | int | float | bool | None`。`PluginFactory.create_session(config: Mapping[str, JSONScalar], context: PluginContext) -> PluginSession`；`PluginSession.validate(request) -> None`、`run(request) -> 对应 Result`、`close() -> None`。每次实际调用独立 session；`validate` 无返回值，不执行昂贵媒体操作。Manager 在调用前检查 stage/operation、request 类及 manifest，在返回后检查 result 类和产物；不能只靠注解。缓存命中或禁用阶段由编排器/AssetResolver 决定，不创建 session。Factory 在 `create_session` 失败时清理自身已取得资源；成功创建后 Manager 在校验失败、运行失败、取消和成功时都恰好调用一次 `close()`。

## 2. 强类型请求与结果

以下类型均为冻结的 `@dataclass(frozen=True)`（实现可把共同字段抽到基类）。任务调用的每个 Request 都有 `stage: Literal[...]`、`operation: Literal[...]`、`purpose: str`，这些值由宿主填写且运行时校验；不在 Request 中重复放取消/进度回调。`Path` 是本地绝对路径。所有结果只声明产物和最小 metadata，不携带任务状态或外部响应。

| 调用 | Request 字段（共同字段以外） | Result 字段与必需产物 |
|---|---|---|
| `download/download` | `url: str, quality: str, target_dir: Path, source_stem: str = "source"` | `source_path: Path, title: str | None, duration_seconds: float | None`；`source_path` 是 `target_dir/source.*` 的实际文件 |
| `audio_extract/audio_extract` | `source_path: Path, output_path: Path, sample_rate: int, channels: int`；`purpose="extract.asr"|"extract.dubbing"` | `audio_path: Path, sample_rate: int, channels: int, duration_seconds: float | None`；路径必须等于 `output_path` |
| `vocal_separate/vocal_separate` | `audio_path: Path, vocal_path: Path | None, background_path: Path | None`；`purpose="separate.asr"|"separate.dubbing"`，至少请求一个 stem | `vocal_path: Path | None, background_path: Path | None`；请求的每个 stem 必须存在，未请求者可为 None |
| `transcribe/transcribe` | `audio_path: Path, output_srt: Path, source_lang: str, backend: str, model: str` | `original_srt: Path, segments: int | None`；路径等于 `output_srt` |
| `translate/translate` | `original_srt: Path, output_srt: Path, source_lang: str, target_lang: str, mode: Literal["mono","bilingual"]` | `translated_srt: Path, segments: int, bilingual: bool`；路径等于 `output_srt`，时间轴与原字幕一致 |
| `tts/tts` | `translated_srt: Path, original_srt: Path, output_audio: Path, target_lang: str, voice: str, bilingual: bool, duration_seconds: float | None` | `speech_audio: Path, duration_seconds: float | None`；路径等于 `output_audio` |
| `render/subtitle` | `source_video: Path, translated_srt: Path, output_video: Path, mode: Literal["hard","soft"]`；`purpose="render.subtitle"` | `video_path: Path`；路径等于 `output_video` |
| `render/dub_mux` | `base_video: Path, source_audio: Path, speech_audio: Path, background_audio: Path | None, vocal_audio: Path | None, mixed_audio: Path, output_video: Path, original_voice_mode: Literal["keep","lower","replace"]`；`purpose="render.dub_mux"` | `mixed_audio: Path, video_path: Path`；路径分别等于请求值 |

`download.probe` 单列 `ProbeRequest(url: str, force_refresh: bool = False, cookies_from_browser: str | None = None)` → `ProbeResult`；后者精确覆盖当前 `src.core.downloader.ProbeResult` 的 `ok: bool, title: str | None, extractor: str | None, duration: float | None, formats_count: int, webpage_url: str | None, reason: str | None, detail: str | None, cached: bool, language: str | None, available_qualities: tuple[str, ...], formats: tuple[ProbeFormat, ...], thumbnail: str | None, uploader: str | None`。`ProbeFormat` 固定字段为 `formatId: str, ext: str, resolution: str, height: int | None, width: int | None, fps: float | None, vcodec: str | None, acodec: str | None, filesize: int | None, tbr: float | None, note: str | None, type: Literal["video","audio"]`；适配器从现有 `list[dict]` 转换，API 边界再转回既有 dict 形状。probe 不成为任务产物，使用后端当前下载默认映射（尚无任务计划），错误由既有探测 API 响应映射处理。

`Request.purpose` 与 `Context.purpose` **必须相等**，都采用完整调用标识：`download`、`extract.asr`、`extract.dubbing`、`separate.asr`、`separate.dubbing`、`transcribe`、`translate`、`tts`、`render.subtitle`、`render.dub_mux`。领域上的 `asr/dubbing` 可由最后一段派生，不设第二个字段或简写值。外部状态映射由 I1 保持：下载 DOWNLOADING；ASR 提取/分离 EXTRACTING；转写 TRANSCRIBING；翻译 TRANSLATING；普通封装 BURNING；配音提取/TTS SYNTHESIZING；配音分离/封装 DUBBING。阶段进度由 Context 输入 0–100 或 None，I1 按现有 `_BANDS` 和用途映射，保证总进度单调；插件不发 SSE/REST 或 SUCCESS。

## 3. 宿主路径与现有 core 的适配

宿主在调用前分配并传入明确目标路径；插件不得自选其他最终路径、覆盖其他用途产物，Manager 校验返回路径在任务工作目录内且与请求一致。当前目录名固定：ASR `audio.wav`，配音源音频 `tts_source.wav`，ASR 人声 `vocal.wav`，配音背景 `background.wav`、人声 `vocal_stem.wav`，转写 `original.srt`，翻译 `translated.srt`，TTS `dubbed.wav`，混音 `dubbed_mix.wav`，普通封装 `output_hard.mp4`/`output_soft.mp4`，配音 `output_dubbed.mp4`；下载为 `source.*`（扩展名由下载器决定）。这些名称维持现有 AssetResolver 和下载接口。`output.mp4` 只用于旧缓存读取。

当前 `extract_audio(video_path, task_id, ...)` 固定写 `audio.wav`，A2 必须给它增加可选的 `output_path: Path | None = None`，默认仍写旧路径，并让配音调用显式传 `tts_source.wav`；不可先写 `audio.wav` 再复制/移动，否则会破坏 ASR 缓存。其他现有 core 固定写入任务目录：A1/A3/A4/A5/A7 内置适配器要先确认宿主分配的是上述兼容路径，再调用 core，返回并验证真实产物；若需要给 core 加显式目标路径，向对应文件 owner 申请。下载扩展名动态，仅验证 `target_dir/source.*` 的任务内真实文件。`dub_mux` 的 FFmpeg 混音命令由 A7 迁入插件，明确使用 `mixed_audio` 和 `output_video`；keep 用原音轨，lower 用背景加 0.25 人声，replace 仅背景，最后都混 TTS 音轨。普通成品在配音失败时保持不动。

Manager 对所有必需文件检查：规范化后的路径位于 `task_dir` 内、不等于其他用途的保留路径、存在且为**非空**普通文件；包括 SRT，不设空文件例外。现有 `write_srt([])` 默认写入 UTF-8 BOM，且转写空字幕本身报错；因此该规则与当前 core 行为兼容。结果发布前再查一次取消。写入采用适配器临时文件/底层安全方式，取消清理只删除**本次未完成**文件，不清已发布的普通视频。

## 4. Context、取消、错误与清理

`PluginContext` 冻结只读身份字段 `task_id: str`、`invocation_id: str`（每次调用唯一）、`task_dir: Path`、`purpose: str`，服务为 `check_cancelled() -> None`、`report_progress(percent: float | None) -> None`、`register_process(proc) -> None`、`unregister_process(proc) -> None`、`resolve_credential(ref: str) -> str`。进度值超出 0–100 拒绝，`None` 表示未知；终态后迟到回调丢弃。`FakeContext` 提供可切换取消标记、进度记录、进程登记记录、内存凭据表，并能断言凭据未进日志/序列化；测试不需要真实模型或网络。

`PluginCancelled` 是单独的控制流异常，不继承 `PluginError`；适配器将 `TranscribeCancelledError`/`VocalSeparationCancelledError` 等底层取消转换为它。`PluginError(code: str, stage: StageId, plugin_id: str, message: str, cause: Exception | None)` 的 message 必须脱敏；保留 `__cause__` 供内部诊断，不序列化 cause。标准 code 至少有 `invalid_input`、`plugin_unavailable`、`version_mismatch`、`credential_missing`、`dependency_missing`、`execution_error`、`invalid_result`、`cleanup_error`，I1 负责映射已有外部错误码，不覆盖旧 API 的稳定值。Manager 不做通用自动重试。

优先级固定为：**已确认取消 > 原始校验/执行/结果错误 > close 错误 > 成功**。Manager 在建 session 前、validate 前、run 前后、结果发布前查取消；一旦建成 session，`finally` close 恰好一次。close 失败若已有原始错误，只记录为附加诊断，不覆盖原始 cause；若此前成功，转为 `cleanup_error`，由主链失败或配音降级策略处理。取消与 close 同时发生，终态为 CANCELLED；普通配音错误可降级为 `tts_status=FAILED`，取消绝不降级。取消信号与子进程终止继续接现有 Runner；协作取消不是同步线程的硬超时保证。

## 5. 固定的任务计划 v1

内部 SQLite 字段固定为 `plugin_plan_json TEXT NULL`。持久化 JSON 采用以下结构，`schema_version` 固定为 1；`slots` 的键是调用用途（`download`、`extract.asr`、`extract.dubbing`、`separate.asr`、`separate.dubbing`、`transcribe`、`translate`、`tts`、`render.subtitle`、`render.dub_mux`），值为 `{plugin_id, implementation_version, config}`；禁用调用不在 slots 中。相同实现可在多个槽复用，因配置和缓存用途不同仍分别记录。上传任务无 `download` 槽，只有下载任务有；仅下载任务无字幕/配音槽；probe 不入计划。`engine` 保留在任务原字段，另在 `translate.config.engine_id` 明确记录，不允许把它当 plugin_id。

```json
{
  "schema_version": 1,
  "slots": {
    "download": {"plugin_id": "builtin.ytdlp", "implementation_version": "1", "config": {"quality": "480p", "format_selector": "bestvideo+bestaudio/best", "proxy": null, "socket_timeout": 30, "cookies_ref": "settings:cookies_file"}},
    "extract.asr": {"plugin_id": "builtin.ffmpeg_audio", "implementation_version": "1", "config": {"sample_rate": 16000, "channels": 1}},
    "transcribe": {"plugin_id": "builtin.whisper", "implementation_version": "1", "config": {"backend": "local_whisper", "model": "tiny"}},
    "translate": {"plugin_id": "builtin.translation_engine", "implementation_version": "1", "config": {"engine_id": "local-opus-en-zh", "api_type": "local_ct2", "base_url": "", "model": "Helsinki-NLP/opus-mt-en-zh", "credential_ref": null, "batch_size": 8, "local_batch_size": 16, "timeout_seconds": 60}},
    "render.subtitle": {"plugin_id": "builtin.ffmpeg_render", "implementation_version": "1", "config": {"mode": "hard"}}
  }
}
```

示例仅展示部分槽；真正 resolver 按任务需求补全。`slots` 中的 `config` 只允许以下非敏感键，未知键拒绝而非默默持久化：下载 `quality, format_selector, proxy, socket_timeout, cookies_ref`；提取 `sample_rate, channels`；分离 `backend, model, threads, command`；转写 `backend, model, service_url`；翻译 `engine_id, api_type, base_url, model, credential_ref, batch_size, local_batch_size, timeout_seconds`；TTS `voice, target_lang, bilingual`；字幕封装 `mode`；配音封装 `original_voice_mode`。当前 `TranslationEngine` 记录的运行选择字段**正是** `id, api_type, base_url, model`，只有 `api_key` 为凭据；`name/enabled/availability/检查与时间戳` 不是翻译执行参数，不入计划。`model_name`、`api_format`、`options` 并非本基线记录字段，不能凭其他分支臆造。`batch_size` 从 `settings.translate_batch_size` 固定；`local_batch_size` 从 `settings.local_translate_batch_size` 固定；引擎记录当前没有 timeout 字段，正常引擎客户端的 `timeout_seconds` 固定为现有默认 **60**，仅兼容 `engine_config=None` 的旧直调路径才取 `settings.translate_timeout`。`api_type=local_ct2` 时只支持 `en→zh-CN|zh`、使用本地 manager，`credential_ref=null`；`openai_compatible/anthropic_compatible` 在执行时从固定 `engine_id` 取当前 API Key，组装含固定 api_type/base_url/model/timeout 的临时配置对象传给 core，不能把旧引擎记录整对象重新读取后覆盖快照。`source_lang, target_lang, mode, burn, need_subtitle, tts_enabled, tts_voice, original_voice_mode, quality, source_type` 仍由原 TaskRecord 保存；resolver 复制其**生效值**到所需 slot，Runner 只消费已持久化计划，不重读可变选择。ASR 分离开关、Demucs backend/model/threads/command、ASR 采样率/声道、转写 backend/model/service URL、翻译所有上述有效参数、下载格式/代理/超时、TTS 音色及配音模式也必须在创建时固定。基础设施二进制路径可在执行时按部署环境解析，但不得借此重选能力实现或模型。

**快照必须真正生效。** 内置适配器调用 core 时，不能让 core 再从可变全局 `settings` 重选上述配置；已有显式参数就传计划值，缺参数则由该 core 文件 owner 增加显式参数/固定配置对象，并由对应适配器传入。禁止临时修改进程全局 settings（多任务并发会互相污染）。目前下载有 quality/format/proxy/timeout 显式参数，音频有采样率/声道参数；分离的 backend/model/threads/command 和转写的 service URL 等仍需实现时核查并补齐。若无法在 owner 范围内做到，提交接口变更申请，相关任务不能宣称计划已固定。

A5 已获准独占 `src/core/translator.py` 与 `tests/test_translator.py` 的最小扩展，为批大小/本地批大小提供显式参数并保留现有默认；编辑既有符号前仍须按 AGENTS.md 做 impact 并报告。A4 若需要给 core 转写增加配置透传，须另行审查；两者都不能吞掉取消信号。

## 6. C0 类型对照与冻结检查

当前 Luna 已建立 `src/plugin_system/{contracts,context,errors,manifest,__init__}.py` 和 `tests/test_plugin_contracts.py`/`tests/plugin_fakes.py`。实际公开类型名为 `VocalSeparateRequest/Result`、`TranscribeRequest/Result`、`TranslateRequest/Result`；另有 `DownloadRequest/Result`、`AudioExtractRequest/Result`、`TTSRequest/Result`、`SubtitleRenderRequest/Result`、`DubMuxRequest/Result`、`ProbeRequest/ProbeFormat/ProbeResult`。`PluginRequest` 与具体请求是 frozen dataclass；`PluginContext`、`PluginFactory`、`PluginSession` 是 Protocol；`PluginManifest` 固定协议版本 1。下游按这些**真实类型名**导入，不用计划稿中的 `SeparationRequest` 等简称。

设计审阅结论：上述类型布局、canonical purpose、最小 FakeFactory/FakeSession、错误与 manifest 骨架均与 v1 设计一致。FakeContext 已使用 UUID 生成默认 invocation ID，并拒绝布尔值、NaN 与越界进度。设计 owner 在补丁后重跑 `UV_CACHE_DIR=/private/tmp/pipeline-plugin-uv-cache UV_PROJECT_ENVIRONMENT=/Users/wenzhengfeng/code/computer_logic/TranslateSub/.venv uv run --no-sync pytest -q tests/test_plugin_contracts.py`，结果 **5 passed**；C0 契约可冻结为 v1。H1 的运行时 request/result/产物检查、session 生命周期、cancel/close 优先级属于 H1 实现，不作为 C0 当前缺漏。后续接口变更需提出申请并同步所有下游 owner。

## 7. v1.1 配置快照修订（覆盖第 5 节的旧白名单）

`plugin_plan_json.schema_version` **仍为 1**；仅扩大每个 slot 的允许配置键并要求 R1 把生效值固定。以下清单覆盖第 5 节较短的白名单；未列键继续拒绝，密钥内容继续只在执行时解析。

内置 `plugin_id` 同步固定为：`download=builtin.ytdlp`、`audio_extract=builtin.ffmpeg_audio`、`vocal_separate=builtin.vocal_demucs`、`transcribe=builtin.whisper`、`translate=builtin.translation_engine`、`tts=builtin.kokoro`、`render=builtin.ffmpeg_render`；这些内置实现的 `implementation_version` 均为字符串 `"1"`。R1 按用途槽填入对应 ID/版本，不用 stage 名充当 ID。

| slot | v1.1 允许的 `config` 键 | 创建时取值与执行要求 |
|---|---|---|
| `download` | `quality, format_selector, proxy, socket_timeout, cookies_ref, merge_output_format, retries, concurrent_fragments` | 从请求与当前下载设置解析实际值；`proxy=null` 表示明确不用代理，不能回退后续全局设置。A1 获准独占 `src/core/downloader.py` 及其测试的必要显式 kwargs 扩展，保持旧调用默认兼容；还须覆盖 `merge_output_format/retries/concurrent_fragments` 等直接读取全局设置的参数。编辑既有符号前先 impact。 |
| `separate.asr` / `separate.dubbing` | `backend, model, threads, command, timeout_seconds` | `timeout_seconds` 固定当前 `settings.vocal_separation_timeout` 的有效值；A3 透传给 core，不能执行时重读 settings。 |
| `transcribe` | `backend, model, service_url, credential_ref, timeout_seconds, device, compute_type, download_root, beam_size` | HTTP/custom 时固定 URL 与超时，`credential_ref="settings:transcriber_api_key"` 或 null；Local 时固定 model/device/compute_type/download_root/beam_size，ref=null。A4 优先构造 `HttpTranscriber(url, api_key, timeout)` 或 `LocalWhisperTranscriber(model_name, device, compute_type, download_root, beam_size, require_model_ready=True)`，通过现有 `transcribe(service=...)` 注入，无需为 HTTP 配置改 core。 |
| `translate` | `engine_id, api_type, base_url, model, credential_ref, batch_size, local_batch_size, timeout_seconds` | 与第 5 节一致；A5 已获准对 `src/core/translator.py` 做显式批大小的最小兼容扩展。 |
| 其他槽 | 沿用第 5 节对应键 | `extract.asr/extract.dubbing` 保留 `sample_rate,channels`；TTS、render 不变。 |

`resolve_credential()` v1.1 允许三种命名引用：`translation_engine:<engine_id>:api_key`、`settings:transcriber_api_key`、`settings:cookies_file`；仅从固定引用取执行时当前值。R1 不得把 API Key、Cookie 文件内容或携带 userinfo/token query 的 URL 写进 JSON。HTTP 转写未配置密钥时 `credential_ref=null` 可合法执行；引用存在但值丢失时返回 `credential_missing`，不可默默切换服务。

本基线 `LocalWhisperTranscriber` 构造器虽接收 `device/compute_type/download_root`，`_get_model()` 当前仍硬编码 `cpu/int8` 并通过受管 model manager 解析路径；v1.1 对**当前有效行为**固定 `device="cpu"`、`compute_type="int8"`、`download_root=null`，本期不扩展其他设备或自定义下载目录。A4 只需显式固定 model/beam_size 并经 `service=...` 注入；取消异常仍须原样透传。下载 `proxy=None` 以及 cookies 缺省也有 core 回退全局设置的问题，A1 扩展时用显式未提供哨兵区分“旧默认”与“计划明确无代理/无 Cookie”，避免并发任务被全局状态污染。

`credential_ref` 仅允许 `translation_engine:<engine_id>:api_key`；`cookies_ref` 仅允许 `settings:cookies_file`，两者都是命名引用，不是密钥或 Cookie 文件内容。执行时取**该引用当前值**，轮换可生效，删除/缺失则明确 `credential_missing`；不得回退其他 engine/插件。URL 型配置（`base_url, service_url, proxy`）不得包含 userinfo、令牌型 query/fragment；有此形式必须拒绝持久化并要求改用凭据引用。`api_key`、Authorization、Cookie 内容、环境变量值、任意 `**kwargs` 都不能进入计划、普通日志、API/SSE。R1 新增字段时必须修改 `TaskRecord.to_dict()` 或对外 DTO 的明确白名单，因当前 `asdict(self)` 会输出新字段；测试同时覆盖详情、列表、SSE/响应路径。

URL/上传/MCP 创建均调用同一个 resolver，在任务行插入事务内写入计划、成功持久化后才能入队；URL 去重命中旧任务原样返回，不覆写其计划。重试与中断恢复沿用原计划；旧记录为 NULL 时，用旧字段和**迁移时**能读取的当前默认配置只解析一次并原子写入，不能假装还原创建时配置。计划指向不存在或版本不匹配的插件时失败，不静默替换。未来若其他分支引入 `model_runtime.resolve_runtime`，应升级 schema 并合并为一个有效配置解析路径。

缓存 sidecar 的 v1 最小元数据为 `schema_version, stage, operation, purpose, plugin_id, implementation_version, config_sha256, input_sha256, output_names`；`config_sha256` 来自排序、紧凑编码的非敏感配置 JSON；`input_sha256` 来自输入文件的流式 SHA-256 与非文件输入的规范 JSON，不含凭据值。fingerprint 变动使该产物及依赖它的下游缓存失效。无 sidecar 的旧产物只在 NULL 计划迁移的明确 legacy 路径按现有规则复用；新计划或显式换插件不能仅凭文件名复用。AssetResolver 负责判断，Manager 仅验证本次新产物。
