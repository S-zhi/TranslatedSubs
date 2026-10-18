# Pipeline Plugin v1：配置与迁移说明

本文保留 v1 计划、缓存和迁移约定，并说明后续实例管理接入后的变化。v1 历史验收见[任务看板](README.md)；当前设置入口、REST API 和任务选择见[插件实例指南](../plugin-instances.md)。

## 当前插件选择

新建 URL 和上传任务统一从 Redis 配置文档的 `plugins` 区读取实例与阶段默认项。可在 `/settings?tab=plugins` 管理已注册实现的实例、非敏感参数和模型/引擎绑定，并在任务高级选项中按阶段指定实例；未指定的阶段使用提交时的默认实例。插件实现仍由后端 Registry 显式注册。

七个阶段为 `download`、`audio_extract`、`vocal_separate`、`transcribe`、`translate`、`tts`、`render`。内置实现依次是 `builtin.ytdlp`、`builtin.ffmpeg_audio`、`builtin.vocal_demucs`、`builtin.whisper`、`builtin.translation_engine`、`builtin.kokoro`、`builtin.ffmpeg_render`，实现版本均为字符串 `"1"`。

`render` 同时承担字幕封装和配音混音封装；`audio_extract`、`vocal_separate` 通过 purpose 区分 ASR 和配音调用，每次调用使用独立 session。实例管理面向已注册实现，尚不提供第三方插件安装、自动发现、沙箱或热加载。

## 旧环境变量迁移

`SUBTRANS_PIPELINE_PLUGINS` 是旧配置导入源，不是当前 API 新任务的实时配置入口。启动 API 不会自动导入 `.env`；进程环境变量的修改也不替代 Redis 中的实例选择。

若旧 `.env` 含有该字段，先准备 Redis 与 `config.local.json`，再从仓库根目录显式运行：

```sh
uv run python -m src.config.migrate_legacy --legacy-env-file /path/to/legacy.env
```

旧字段为按阶段指定 `plugin_id` 与 `implementation_version` 的 JSON 对象。迁移将其写入兼容实例，并记录一次性导入标记；重复运行不会重复覆盖。已有 Redis 插件配置优先，即使保存的选择恰好等于内置默认，也不会被旧环境变量替换。导入身份仍须已在 Registry 注册，不会触发插件下载或导入代码。其他旧配置源与迁移步骤见[本地快速开始](../local-quick-start.md#从旧版-env-迁移)。

## 任务计划与凭据

任务创建时先解析实际需要的阶段，再调用所选 factory 的 preflight 检查身份、配置和依赖。预检不创建执行 session、不加载模型权重，也不执行下载、转写、翻译或封装；仅下载任务跳过字幕及配音阶段，HTTP ASR 不检查本地 Whisper 模型。

实例任务保存 `schema_version: 2` 的内部 `plugin_plan_json`，冻结配置 revision、插件身份与实现版本、实例 ID、有效非敏感配置，以及模型卡/引擎的固定 ID。该字段不加入公开任务响应。排队后修改默认项、实例配置或当前模型，只影响之后创建的任务；重试和重启按原计划执行。已有 v1 计划仍可读取和重试，不按当前实例设置重写。

计划只保存凭据引用。执行时按冻结的模型卡 ID、翻译引擎 ID 或设置引用读取当前 Key/Cookie 路径，支持凭据轮换；缺少所需凭据或实现版本时明确失败，不自动切换到当前模型或其他插件。

## 旧任务、缓存与取消

旧数据库保留可空的计划字段。NULL 计划任务首次进入完整 Runner 时，使用任务原字段和当次配置生成兼容计划并原子写入；历史未记录的配置无法还原。第一次迁移运行可接纳相容旧产物并补写缓存元数据；显式选择其他插件时不能把旧产物标为新插件生成。

缓存元数据绑定插件身份、版本、purpose、配置、输入文件内容和上游元数据。上游更换实现后，即便产物字节相同，下游也应重新计算。配音相关变化保留普通视频。不要手工复制其他任务的产物或 sidecar 来模拟缓存。

配音普通失败保留普通视频和字幕，任务可以成功，但配音状态标为失败且不发布虚假链接。取消停止后续阶段，并清理本次未发布产物；已经发布的普通视频和字幕保留。

## 字幕编辑与重烧

字幕保存和重烧仅允许终态任务，使用任务操作锁与数据库 claim/token 互斥；进行中的字幕操作与重试、删除等状态变更不能同时发布结果。

`POST /api/tasks/{id}/subtitles/burn` 只调用任务保存的 `render.subtitle` 插槽；可选 `mode` 仅覆盖本次调用的副本。NULL 历史计划使用 `builtin.ffmpeg_render` 版本 `"1"`，不为重烧补建完整流水线计划；无效计划、缺少插槽或实现不可用时明确失败。

渲染先写临时文件，再原子替换成品；视频和缓存元数据保留备份，发布或数据库提交失败时回滚。字幕保存也使用同一套 publication journal。服务重启时通过 `.subtitle-publication.json` 恢复中断操作；已提交终态仅清理日志，不回滚新成品。日志或备份损坏时保留文件并报告恢复失败，不重投完整流水线。成功重烧更新字幕封装元数据，并使旧配音缓存失效。

## 验证边界

v1 历史测试结果见[验收报告](validation-report.md)，当前实例管理的相关测试命令见[插件实例指南](../plugin-instances.md#相关验证)。测试数量随当前分支回归变化，以最终实际运行结果为准。

本地媒体 smoke 可验证真实 FFmpeg/WAV 互操作；模型替身不代表 Whisper、Demucs、Kokoro 推理已验证。真实模型、云服务与网站下载需在相应依赖和凭据就绪后另行运行；真实云服务/下载测试遵循 `SUBTRANS_LIVE_TEST=1` 的显式启用约定。
