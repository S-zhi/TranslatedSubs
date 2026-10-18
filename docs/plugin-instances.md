# 插件实例与任务选择

插件实例把已注册的流水线实现、非敏感参数和模型/引擎绑定保存为可复用配置。入口是 **设置 → 插件**（`/settings?tab=plugins`）；任务高级选项可以按阶段选择实例。配置保存在 Redis 的 `plugins` 区，新建任务在提交时冻结实际选择。

当前支持管理已注册实现，不包含第三方插件安装、自动发现、沙箱或热加载。插件在服务进程内运行。

## 阶段与默认实例

每个阶段始终有一个启用的默认实例。目前 catalog 有八个内置实现，仍有七个阶段和七个初始默认实例；可选 `builtin.cobalt` 与 `builtin.ytdlp` 同属下载阶段，创建 cobalt 实例不会改变默认项。初始默认如下，内置实现版本均为字符串 `"1"`：

| 阶段 | 内置实现 | 初始实例 ID |
|---|---|---|
| 下载 `download` | `builtin.ytdlp` | `default_download` |
| 音频提取 `audio_extract` | `builtin.ffmpeg_audio` | `default_audio_extract` |
| 人声分离 `vocal_separate` | `builtin.vocal_demucs` | `default_vocal_separate` |
| 转写 `transcribe` | `builtin.whisper` | `default_transcribe` |
| 翻译 `translate` | `builtin.translation_engine` | `default_translate` |
| 配音 `tts` | `builtin.kokoro` | `default_tts` |
| 字幕/配音封装 `render` | `builtin.ffmpeg_render` | `default_render` |

设置页可创建、编辑、启停和删除实例，也可切换阶段默认项。可编辑参数由 catalog 的 `config_schema` 声明：例如下载重试次数、转写超时、翻译批量或 TTS 语速。没有可编辑参数的实现仍可管理实例身份与默认选择。读取 catalog 和管理实例不创建媒体执行 session。

停用或删除前，先把该阶段默认项切换到另一启用实例。非终态 v2 任务引用的实例也暂时不能停用或删除；编辑实例参数不会改写已经保存的任务计划。

## 模型、引擎与凭据

实例可使用 catalog 声明的绑定类型：

| 内置实例阶段 | 可选固定引用 |
|---|---|
| 转写 | `model_ref: {"kind":"whisper","card_id":"模型卡 ID"}` |
| 翻译 | `model_ref: {"kind":"llm","card_id":"模型卡 ID"}` 或 `engine_ref: {"id":"引擎 ID"}` |
| 配音 | `model_ref: {"kind":"tts","card_id":"模型卡 ID"}` |

`model_ref` 与 `engine_ref` 互斥；未绑定时沿用任务和当前模型设置。固定绑定按 ID 解析，不因切换“当前模型”而更换目标。任务表单会显示绑定来源，并锁定由绑定决定的模型、翻译引擎或音色选项。被实例引用的模型卡和翻译引擎不能删除，即使实例已停用也需先解除绑定。

插件 `config` 只接受声明的非敏感字段，不保存 API Key、Cookie、密码或带凭据的 URL。模型/引擎密钥在对应设置中维护；任务计划只保存凭据引用，执行时读取固定 ID 对应的当前密钥，因此轮换密钥仍可生效。引用失效时明确报错，不自动换到另一模型或引擎。

cobalt 实例使用 `access_mode: "anonymous"`（默认）或 `"api_key"`，实际密钥由 `PATCH /api/settings/runtime` 的 `cobalt_api_key` 保存，读取仅显示 `hasSecrets.cobalt_api_key`。v1 所有认证 cobalt 实例共用这把部署级密钥；不能在实例 config 中设置 Key 或 `credential_ref`。配置与操作示例见 [cobalt 下载指南](cobalt-download.md)。

内置 Whisper 实例可选择本地后端或 HTTP ASR。HTTP ASR 检查其服务地址与依赖，不要求本地 Whisper 已安装或模型已就绪。**绑定 Whisper 模型卡后，实例不能同时覆盖 `backend` 或 `service_url`**；后端和地址由模型卡决定，设置页会清除这两项覆盖。未绑定的本地 Whisper 实例也不能配置 HTTP 服务地址。

## 设置 API

所有接口位于 `/api/settings/plugins`，遵循项目 API Token 鉴权。配置了 Token 时使用 `Authorization: Bearer <token>` 或 `X-API-Token`。

Web 工作台在 **其他设置 → 后端连接 · API Token** 中填写 Token；收到 401 时会自动展开该入口。保存或清除会刷新页面，Token 按后端地址隔离，仅存于当前标签页的会话存储。普通 REST 与上传请求自动携带 Bearer 头，进度 SSE、媒体预览和下载链接通过后端支持的 `token` 查询参数鉴权。Google Drive 独立 sidecar 不接收此 Token。

| 方法与路径 | 用途与响应 |
|---|---|
| `GET /catalog` | 已注册实现及配置字段、绑定能力，返回 `plugins` |
| `GET /instances` | 返回 `revision`、`instances` 和 `defaults` |
| `POST /instances` | 创建实例；201，返回 `revision` 和 `instance` |
| `GET /instances/{id}` | 返回 `revision` 和 `instance` |
| `PUT /instances/{id}` | 更新实例；返回 `revision` 和 `instance` |
| `DELETE /instances/{id}` | 删除实例；返回 `revision` 和 `ok` |
| `GET /defaults` | 返回 `revision` 和阶段到实例 ID 的 `defaults` |
| `PUT /defaults` | 部分合并阶段默认项；返回 `revision` 和完整 `defaults` |

所有写请求都必须带 `If-Match: <revision>`，值为最近读取的非负整数字符串，例如 `If-Match: 12`，不加引号。revision 属于整个 Redis 配置文档，其他设置的更新也可能使它过期；收到 409 后重新读取并核对，再用新 revision 保存。

创建实例的请求示例：

```json
{
  "id": "download-retry",
  "name": "下载重试三次",
  "stage": "download",
  "plugin_id": "builtin.ytdlp",
  "implementation_version": "1",
  "config": {"retries": 3}
}
```

`id` 可省略，由服务端生成；`config_schema_version` 默认 `1`，`enabled` 默认 `true`，`config` 默认 `{}`，两类绑定默认 `null`。更新时可只提交待变更字段，但 `id`、`stage` 不可修改；显式提交 `config` 会替换整个参数覆盖对象，提交 `{}` 可清空覆盖。

切换下载默认项时，`PUT /defaults` 的请求体为：

```json
{"defaults":{"download":"download-retry"}}
```

其他六个阶段保持原值；目标必须是同阶段、已启用且实现可用的实例。停用/删除旧默认项需要在切换成功后，使用新 revision 再提交。

| 状态码 | 常见原因与处理 |
|---|---|
| `401` | 缺少或错误的 API Token |
| `404` | 实例不存在 |
| `422` | 缺少/无效 `If-Match`、配置字段或绑定错误、阶段不匹配、实现未注册或版本不可用 |
| `409` | revision 过期、实例 ID 重复、默认/在用实例保护，或模型卡/引擎被实例绑定 |
| `503` | Redis 配置存储不可用，恢复连接后重试 |

设置页遇到冲突会保留输入，刷新后可重新核对保存；422 与 503 同样保留草稿。

## 任务高级选择与创建 API

未指定的阶段在提交时跟随默认实例。上传任务不需要下载阶段；仅下载任务跳过字幕和配音阶段；TTS 仅在启用配音时使用，人声分离仅在音频增强或配音配置需要时使用。

`POST /api/tasks` 的 JSON 请求中，`pluginInstances` 是阶段到实例 ID 的**对象**，例如：

```json
{
  "url": "https://example.com/video",
  "sourceLang": "en",
  "targetLang": "zh-CN",
  "needSubtitle": true,
  "burn": "soft",
  "pluginInstances": {"download":"download-retry"}
}
```

`POST /api/tasks/upload` 使用 multipart form；其中 `pluginInstances` 是同一对象序列化后的 **JSON 字符串**，而不是嵌套表单字段。例如前端提交方式为：

```js
formData.append("pluginInstances", JSON.stringify({ transcribe: "asr-http" }));
```

示例中的 `asr-http` 需先创建为可用的转写实例。字段可省略；`{}` 表示所有所需阶段跟随默认。无效 JSON、无效阶段或所需阶段的无效实例会被拒绝，不静默回退。

`POST /api/tasks/probe` 同样接受 `pluginInstances` 对象和 `quality`，只解析下载阶段，例如 `{"url":"https://example.com/video","pluginInstances":{"download":"download-retry"},"quality":"720p"}`。省略实例时跟随下载默认项。工作台自动探测与提交使用同一下载选择；切换实例、画质或刷新到变更后的下载配置会作废旧探测结果并重新检查。缓存区分有效代理、Cookie、格式和超时配置，以及 cobalt endpoint、认证模式和回退策略，避免不同下载配置串用探测结果。

使用 cobalt 时，先在下载阶段创建 `builtin.cobalt` 实例并填写 `endpoint`，再在任务高级选项或 `pluginInstances.download` 中选择该实例 ID。探测只解析地址，返回 `formatsCount: 0`、空 `availableQualities`/`formats` 和未知时长，不提供完整格式元数据。`fallback_policy` 默认 `none`；显式设为 `ytdlp` 才会对已分类的可恢复失败回退。无效实例、配置、凭据、取消或访问限制不触发回退；`picker` 与 `local-processing` 尚未实现。服务部署、字段与边界见 [cobalt 下载指南](cobalt-download.md)。

创建时按实际执行的 slot 调用所选 factory 的 preflight，检查身份、配置和依赖。预检不创建执行 session、不加载模型权重，也不执行下载、转写、翻译或封装。未使用的阶段不会触发其依赖检查；预检失败不会入队或保存任务目录。预检通过不代表真实远程服务或模型推理已验证，上传文件后续仍有媒体格式与时长校验。

## 固定计划、重试与迁移

新建实例任务的 `plugin_plan_json` 使用 `schema_version: 2`，保存配置 revision，以及每个 slot 的插件身份、实现版本、实例 ID、配置版本、有效非敏感配置和固定模型/引擎引用。计划是内部字段，不加入公开任务响应。

修改默认项、实例或当前模型后，已排队任务和重试任务仍使用原计划。旧 v1 计划继续兼容执行和重试；NULL 历史计划进入完整 Runner 时才进行一次性兼容补全，未记录过的历史配置无法还原。已有自定义翻译计划但缺少运行时快照的旧任务，不要求补填内置翻译引擎的模型名；迁移及重试保留原插件计划。内置翻译引擎缺少模型名仍按无效快照拒绝。

当前 API 的 URL、上传和 probe 选择统一以 Redis 为准。旧 `SUBTRANS_PIPELINE_PLUGINS` 仅作为显式 legacy 迁移的导入来源，不是新任务的实时环境变量入口；已有 Redis 插件配置优先。命令与细节见[配置与迁移说明](pipeline-plugin-v1/operations.md#旧环境变量迁移)。

## 字幕保存与重烧

字幕保存和 `POST /api/tasks/{id}/subtitles/burn` 只接受 `SUCCESS`、`FAILED`、`CANCELLED` 终态。任务锁与数据库 claim/token 共同保护操作，同一任务的字幕保存、重烧、重试、删除不会并发发布；冲突时返回 409。

重烧只运行已保存的 `render.subtitle` 插槽，可选请求体 `{"mode":"hard"}` 或 `{"mode":"soft"}` 仅覆盖本次副本，不改写计划。NULL 历史计划使用 `builtin.ffmpeg_render` 版本 `"1"`，无需迁移整条流水线；已有计划损坏或缺少 render 插槽时明确失败。

输出先写临时文件，再原子替换成品。视频与缓存元数据在数据库提交前保留备份，失败时回滚；字幕保存采用同一 publication journal。服务重启会恢复中断操作，已完成提交则只清理日志；日志或备份损坏时保留文件供检查。成功重烧使旧 TTS/配音封装缓存失效，不自动重跑配音。

## 相关验证

以下为从仓库根目录运行的定向回归命令，可按改动范围选择。当前分支最终验收以实际运行结果为准，v1 的历史结果保留在[原验收报告](pipeline-plugin-v1/validation-report.md)。

```sh
# 实例配置、API、固定引用与迁移
uv run pytest -q tests/test_plugin_catalog.py tests/test_plugin_instance_store.py tests/test_plugin_settings.py tests/test_plugin_reference_protection.py tests/test_plugin_migration.py

# 创建预检、计划兼容与字幕发布
uv run pytest -q tests/test_plugin_preflight.py tests/test_plugin_selection.py tests/test_plugin_plan_lifecycle.py tests/test_plugin_runner_integration.py tests/test_subtitle_reburn.py tests/test_subtitle_editor.py tests/test_task_operations.py

# 前端设置、任务选择、API 序列化与路由
node --test web/__tests__/plugins-api.test.mjs web/__tests__/ui-plugin-settings.test.mjs web/__tests__/task-plugins.test.mjs web/__tests__/router-plugins.test.mjs web/__tests__/plugins-mock.test.mjs
```

这些契约与回归测试不等同于真实模型/云服务验收；Whisper、Demucs、Kokoro 的真实推理及云服务/网站下载仍需另行验证。真实下载测试需显式启用 `SUBTRANS_LIVE_TEST=1`。相关实现入口为 `src/handler/plugin_settings.py`、`src/service/plugin_selection.py`、`src/service/plugin_preflight.py` 和 `src/service/subtitle_reburn.py`。
