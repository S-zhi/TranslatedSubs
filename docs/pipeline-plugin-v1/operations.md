# Pipeline Plugin v1：配置与迁移说明

本文描述 v1 接线后的使用约定；实施与验收状态以 [任务看板](README.md) 为准。REST/MCP 请求和前端无需新增插件字段。插件注册由后端代码维护，v1 不提供发现、热替换或插件配置页面。

## 选择插件

后端环境变量 `SUBTRANS_PIPELINE_PLUGINS` 接受一个 JSON 对象，覆盖指定能力的默认插件身份。例如：

```json
{"translate":{"plugin_id":"company.translator","implementation_version":"1"}}
```

可用能力名为 `download`、`audio_extract`、`vocal_separate`、`transcribe`、`translate`、`tts`、`render`。每项必须且只能包含非空的 `plugin_id` 和 `implementation_version`；没有指定的能力沿用内置默认。替代插件必须已在后端 Registry 中显式注册；配置一个未注册的身份不会自动下载或导入插件，执行对应阶段时会明确失败。

默认实现依次是 `builtin.ytdlp`、`builtin.ffmpeg_audio`、`builtin.vocal_demucs`、`builtin.whisper`、`builtin.translation_engine`、`builtin.kokoro`、`builtin.ffmpeg_render`，实现版本均为字符串 `"1"`。`render` 同时承担普通字幕封装和配音混音封装；`audio_extract`、`vocal_separate` 通过不同 purpose 区分 ASR 和配音调用，不能共享一次调用的可变 session。

## 任务计划与凭据

URL 和上传任务在创建时把必要插件、实现版本和非敏感配置保存到内部 `plugin_plan_json`。该字段不会加入公开任务响应。排队后修改默认插件、翻译模型或服务地址，只影响之后创建的任务；重试和进程重启消费原有计划。

计划仅保存凭据引用。执行时按引用读取固定翻译引擎 ID 的当前 API Key、当前转写 Key 或 Cookie 文件路径，因此凭据轮换可以生效。所需凭据或插件版本不可用时明确失败，不自动换引擎或插件。仅下载任务不需要初始化转写、翻译和配音 session。

## 旧任务、缓存与取消

旧数据库会补充可空的计划字段。旧任务第一次进入 Runner 时，使用该任务原字段和迁移时的后端配置生成计划，并原子写入；历史上未记录的模型或插件配置无法还原。第一次迁移运行可接纳相容的旧产物并补写缓存元数据；显式选择其他插件时不能把旧产物标为新插件生成。

缓存元数据绑定插件身份、版本、purpose、配置、输入文件内容和上游元数据。上游更换实现后，即便产物字节碰巧相同，下游也应重新计算。配音相关变化保留普通视频。不要手工把一个任务的产物或 sidecar 复制到另一个任务目录来模拟缓存。

配音普通失败保留普通视频和字幕，任务可以成功，但配音状态标为失败且不发布虚假配音链接。取消会停止后续阶段，并清理本次未发布的产物；已经发布的普通视频和字幕应保留。

v1 插件在服务进程内执行。这里的生命周期和路径校验不构成不可信第三方代码沙箱。

## 验证边界

测试命令和实际结果在最终验收报告中列出。小型本地媒体 smoke 使用真实 FFmpeg/WAV；没有实际运行 Whisper、Demucs、Kokoro 模型时，测试替身仅证明契约和媒体互操作。真实云服务/下载测试仍需显式启用项目约定的 `SUBTRANS_LIVE_TEST=1`。
