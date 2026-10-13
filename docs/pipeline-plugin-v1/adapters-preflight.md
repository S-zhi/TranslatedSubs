# Pipeline Plugin v1：A1–A7 适配器实施前核对

核对基线：`c5a8e40c2aabb38e01f394aa826366e0ad337a47`。本文件只记录源码与测试入口，不冻结 C0 契约，也不是实现授权。当前无可调用的 GitNexus 或 Context7；以下 impact 列表是实施前待查询符号，不代表已经完成图谱分析。没有运行测试。

## A1 下载与 probe

- 现状：`src/core/downloader.py::download_video(url, task_id, on_progress=None, *, cookies_file=None, format_selector=None, quality=None, proxy=None, socket_timeout=None) -> DownloadResult` 将文件写入任务目录固定源文件名，进度回调异常会被吞并记录。返回结果包含下载文件/标题等信息；编排器实际从 `AssetResolver.require_source(tid)` 定位源文件。`probe_video(url, *, cookies_file=None, cookies_from_browser=None, format_selector=None, ttl_sec=None, force_refresh=False, proxy=None) -> ProbeResult` 是独立只探测接口，有 TTL 缓存，不落盘。
- 测试入口：`tests/test_downloader.py`。未运行；候选命令：`uv run pytest -q tests/test_downloader.py`。
- 允许路径：新增 `src/plugins/builtin/downloader_ytdlp.py` 及适配器测试；不得改编排器接线（由 I1 负责）。
- 实施前 impact：`download_video`、`probe_video`（如需改动底层再分别查询）。
- C0 待决：Download/Probe 是否同一 manifest operation；结果的 source/title 字段和探测结果类型；cookie/proxy 等敏感或环境配置如何传入；进度与取消回调约定。不得将 URL 探测的网络缓存等同任务产物缓存。

## A2 音频提取与输出路径

- 现状：`src/core/audio_extractor.py::extract_audio(video_path, task_id, on_progress=None, *, sample_rate=None, channels=None) -> AudioResult` 验证输入及音轨，默认依据 settings 输出任务目录 `audio.wav`，格式为 PCM s16le；返回采样率、声道、估算时长、文件大小。当前没有显式输出路径参数。编排器可能为 Demucs 请求 48 kHz 立体声，并写 `audio.meta.json` sidecar；旧缓存在无 metadata 且未要求高质量时仍会复用。配音路径则在 `SubtitleBurningHandler._add_dubbed_video` 直接用 FFmpeg 写 `tts_source.wav`（48 kHz 立体声）。
- 测试入口：`tests/test_audio_extractor.py`；必要时参考 `tests/test_orchestrator.py` 的格式/缓存行为。未运行；候选命令：`uv run pytest -q tests/test_audio_extractor.py`。
- 允许路径：新增 `src/plugins/builtin/audio_ffmpeg.py`；如需底层显式输出路径，只扩展 `src/core/audio_extractor.py` 并更新其测试。不得让默认旧调用改变 `audio.wav` 输出语义。I1 才接管编排调用。
- 实施前 impact：`extract_audio`；如扩展 sidecar/缓存还需查询 `_audio_metadata_matches`、`AudioExtractionHandler.process`，但后者只能由 I1 改。
- C0 待决：AudioExtractRequest 的目标路径/用途/格式字段，结果是否含实测编码/时长；ASR `audio.wav` 与配音 `tts_source.wav` 必须独立路径、独立 invocation，不能覆盖或互作缓存。仅靠采样率/声道未必足以证明媒体格式，真实格式探测职责待定。

## A3 人声/背景分离与 stems 隔离

- 现状：`src/core/vocal_separator.py::separate_vocals(audio_path, task_id, *, cancel_check=None) -> Path` 产出 `vocal.wav`；`separate_background(audio_path, task_id, *, cancel_check=None) -> Path` 产出 `background.wav`，实现会同时要求/生成 `vocal_stem.wav`，并用 `background.meta.json` 校验源文件和 Demucs 配置。两接口共享 Demucs 底层实现及串行 semaphore。普通 ASR 分离输出 `vocal.wav`；配音 lower/replace 从 `tts_source.wav` 分离，且当前共用 `vocal_stem.wav` 文件名，背景缓存 metadata 校验输入路径但用途没有显式字段。
- 测试入口：`tests/test_vocal_separator.py`；编排缓存/分支参考 `tests/test_orchestrator.py`。未运行；候选命令：`uv run pytest -q tests/test_vocal_separator.py`。
- 允许路径：新增 `src/plugins/builtin/vocal_demucs.py`；如底层需显式 stems 输出路径/集合，只扩展 `src/core/vocal_separator.py` 与测试。不得改 `VocalSeparationHandler` 或配音调用；由 I1 接线。
- 实施前 impact：`separate_vocals`、`separate_background`；若调整共享实现再查 `_separate_vocals_impl`。现有 caller 和缓存读写需在影响结果中核实。
- C0 待决：`purpose=asr|dubbing`、requested stems 集合、结果中各 stem 的显式路径、取消类型/映射、缓存身份字段。ASR `vocal.wav` 与配音 stems 必须按输入和用途隔离；`vocal_stem.wav` 共用现状不能直接视作满足隔离。

## A4 Whisper 转写

- 现状：`src/core/transcriber.py::transcribe(audio_path, task_id, on_progress=None, *, language=None, model_name=None, backend=None, cancel_check=None, service=None) -> TranscribeResult` 将原文 SRT 写入任务目录 `original.srt`；支持本地 Whisper 和 HTTP/custom 服务，模型服务按配置缓存。编排器将 ASR vocal（若启用）或普通音频传入，并把 `params.model` 中已知 local 前缀解析为 backend/model。
- 测试入口：`tests/test_transcriber.py`、`tests/test_transcriber_server.py`。未运行；候选命令：`uv run pytest -q tests/test_transcriber.py tests/test_transcriber_server.py`。
- 允许路径：新增 `src/plugins/builtin/transcriber_whisper.py` 及独立测试。I1 负责从编排器移除直接调用。
- 实施前 impact：`transcribe`、`_build_transcriber_service`（若触及 backend 解析/服务选择）。
- C0 待决：request 中模型/backend/语言及有效配置来源；结果字幕字段；服务取消异常如何映射为统一取消控制流。不得将既有 engine 翻译配置语义复用到 ASR。

## A5 翻译引擎

- 现状：`src/core/translator.py::translate_srt(srt_path, task_id, source_lang, target_lang, *, mode="mono", on_progress=None, api_key=None, engine_config=None, cancel_check=None) -> TranslateResult` 读取 SRT、保持时间轴，写 `translated.srt`；`mode` 支持 mono/bilingual。编排器传入 API key 与 `engine_config`，其来源/解析链不应由适配器自行改选引擎。
- 测试入口：`tests/test_translator.py`。未运行；候选命令：`uv run pytest -q tests/test_translator.py`。
- 允许路径：新增 `src/plugins/builtin/translator_engine.py` 及测试；engine 到插件身份/配置快照映射归 C0/R1/I1，不能在适配器内部静默选别的引擎。
- 实施前 impact：`translate_srt`；仅在改配置读取时查相关 store/resolver 符号，当前基线无 `model_runtime.py`。
- C0 待决：engine 记录 ID 与 `plugin_id` 的映射、凭据引用而非明文计划字段、双语模式/语言字段、旧错误码映射。

## A6 Kokoro TTS（仅合成）

- 现状：`src/core/tts.py::generate_timeline_audio(translated_srt, original_srt, output_path, *, target_lang, voice, bilingual=False, duration=None, cancel_check=None, on_progress=None, task_id=None) -> Path` 根据两份字幕按时间轴合成语音，支持双语文本、句段切分和时长约束，直接写给定输出路径。`SubtitleBurningHandler._add_dubbed_video` 选择 auto voice（中文 `zf_001`，其他 `af_maple`）、传入视频时长和 `dubbed.wav`。此 core 函数只产生 speech audio，不混音、不封装、不改任务状态。
- 测试入口：`tests/test_tts.py`。未运行；候选命令：`uv run pytest -q tests/test_tts.py`。
- 允许路径：新增 `src/plugins/builtin/tts_kokoro.py` 及测试。不得迁移混音、FFmpeg mux、输出键或任务状态逻辑进 TTS 适配器；编排器改造由 I1。
- 实施前 impact：`generate_timeline_audio`；如改 `_pipeline` 初始化/缓存再查 `_pipeline`。真实模型 smoke 需环境具备模型依赖，普通适配器单测可 mock 底层能力。
- C0 待决：TTSRequest 的字幕/语言/音色/单双语/视频时长字段，语音输出路径分配，进度单位，运行时 key/凭据是否涉及。TTS 结果只报告 speech audio 及必要 metadata。

## A7 字幕 render 与 dub mux

- 现状：`src/core/subtitle_burner.py::burn_subtitles(video_path, srt_path, task_id, on_progress=None, *, mode="hard") -> BurnResult` 写 `output.mp4`（硬烧录或软字幕内封），内部生成 FFmpeg 命令；soft 使用 `mov_text` 字幕轨，hard 需 subtitles/libass 滤镜。配音混音/mux 尚无独立 core 函数：编排器 `_add_dubbed_video` 自行调用 `run_ffmpeg`，将原声、可选背景/人声 stem、TTS 语音混音成 `dubbed_mix.wav`，再以 `output_dubbed.mp4` 输出；mux 映射为视频 `0:v:0`、配音音轨 `1:a:0`、原字幕轨 `0:s?`，视频 copy、AAC 音频、字幕 copy。
- 原声模式现状：`keep` 使用 `tts_source.wav` 全原声；`lower` 用背景 + 0.25 音量的人声 + TTS；`replace` 用背景 + TTS，不混入分离人声。配音视频基于带字幕的普通输出视频，保留原有字幕轨。命令及 filter graph 的准确行为以 `src/service/orchestrator.py::_add_dubbed_video` 为准。
- 测试入口：`tests/test_subtitle_burner.py`、`tests/test_orchestrator.py`。未运行；候选命令：`uv run pytest -q tests/test_subtitle_burner.py tests/test_orchestrator.py`。
- 允许路径：仅新增 `src/plugins/builtin/subtitle_ffmpeg.py` 及其独立测试。A7 不获准自行新增或扩展 core helper；如实现确需 core 改动，先请求设计 owner 明确文件归属与范围。不得修改/删除 `_add_dubbed_video` 或编排链，唯一由 I1 修改 `src/service/orchestrator.py`。A7 可把当前命令迁移成明确 `dub_mux` operation，并保持输入轨契约、输出文件名、hard/soft 行为及字幕轨映射。
- 实施前 impact：`burn_subtitles`；如触及 orchestrator 中配音行为，先由 I1 对 `_add_dubbed_video` 执行 impact 并独占修改。不要把“代码移动”视为无需 impact。
- C0 待决：render operation discriminator（`subtitle` / `dub_mux`）、两种请求/结果的强类型；dub mux 的显式音轨字段和原声模式 enum；字幕轨是否存在/如何映射、输出容器/文件名与 stream copy 策略。A7 可在 C0 冻结后使用 FakeContext 和约定接口并行实现。lower/replace 需要 A2、A3、A6 产物互操作验收；验收确认轨道采样率/声道/时长、stem 来源一致、混音输入顺序和预期字幕轨。互操作是交给 I1 前的完成验收门槛，不是 A7 开工依赖；不得因此串行等待 A2/A3/A6 才开始实现。

## 配音产物互操作交接（A7 交给 I1 前的验收门槛）

- A2 提供 `tts_source.wav` 的路径、采样率/声道、时长及媒体格式语义；该产物与 ASR `audio.wav` 不重名、不覆盖，且两条缓存分别按来源/配置判断。
- A3 对相同 `tts_source.wav` 明确给出 `background.wav` 和按需 vocal stem；结果清楚标出来源路径与用途，缓存不与 ASR `vocal.wav` 混用。`lower` 必须获得 vocal stem，`replace` 只需背景 stem，`keep` 不要求分离。
- A6 提供 `speech_audio` 路径及可供 FFmpeg 使用的采样率/声道/时长信息；TTS 仅生成语音，不做混音。
- 三方以小型本地媒体约定并核对长度、声道、采样率、是否允许自动重采样/补齐，以及取消后部分文件清理；这些都需 C0 明确；A7 可先依冻结接口实现，实际互操作在 A2/A3/A6 产物可用后验收。
- A7 可先用 FakeContext/测试替身独立实现和测试；交给 I1 前，互操作测试须覆盖 keep/lower/replace 的输入流集合和音量、输出音轨、原视频字幕流保留、`output_dubbed.mp4` 产物。仅 mock `run_ffmpeg` 参数不能替代至少一次可复现的本地 FFmpeg 互操作 smoke；此处尚未运行。

## 协作与待冻结项

七个适配器可独立准备，但真实实现须等待 C0 冻结强类型请求/结果、manifest/session 生命周期、Context、进度/取消、错误与产物验证、缓存身份和计划配置字段。此文件不提供可直接复制的猜测接口。适配器只能包装各自能力；默认注册、probe REST 接线、计划消费、事件映射与编排器清理由 I1 统一修改。适配器 owner 交付时应附变更路径、实际测试命令及结果、未决风险和契约修改请求。
