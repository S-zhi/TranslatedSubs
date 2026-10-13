# H1 / R1 / G1 实施准备包（只读核对）

核对基线：`/Users/wenzhengfeng/.codex/worktrees/e307/TranslateSub`，HEAD `c5a8e40c2aabb38e01f394aa826366e0ad337a47`。本文件是准备文档，不包含业务实现。当前无法调用 GitNexus/Context7，未做图谱分析；以下是源码静态核对，不能视作 impact 结果。不得在 C0 契约冻结前猜测接口或开始相应实现。开始修改既有符号前须补 GitNexus impact；提交前须 detect_changes。若工具仍不可用，应报告阻塞并按项目要求解决，不声称已完成图谱分析。

## H1 — Registry / Manager

建议独占新增范围仍按方案：`src/plugin_system/registry.py`、`manager.py` 及各自新增测试。目录和模块当前不存在。H1 依赖 C0 的 manifest、请求/结果类型、context、错误和 session 契约，当前不能编码接口。

H1 当前没有业务调用链需要迁移；G1 将是首个消费者。宿主验收重点应锁定：重复 ID、stage/protocol 校验、工厂延迟初始化、运行时 request/result 类型校验、每次调用 session 隔离、create/validate/run/cancel 的关闭语义和取消传播。插件注册不能在导入期加载 Whisper/Demucs/Kokoro，也不能要求插件改任务数据库。

H1 若仅新建符号，GitNexus 对新符号的 impact 没有既有图谱入口；但任何改动公共导出、现有取消/错误或 runner 接线均要对被改的现有符号先做 impact。G1 验收不能替代 H1 自身针对宿主行为的单测。

## R1 — 配置解析与计划持久化

建议独占范围遵守方案：`src/service/plugin_plan.py`（新建）、`src/store/task_store.py`、`src/service/task/task_lifecycle.py` 及对应测试；暂不改 `runner.py`。R1 重点不是单独给 URL API 加字段，而是确保 URL、上传和 MCP 创建入口共用同一解析与固定计划路径。

入口现状：

- URL HTTP 路由在 `src/service/task/task_lifecycle.py:create_task_route`，再调用 `create_url_task`；后者校验翻译语言、本地模型、engine，并通过 `TaskStore.create_if_no_recent_active` 创建/复用。
- 上传 HTTP 路由 `create_upload_task_route` 调用 `create_upload_task`；它先保存和探测上传文件，再通过 `TaskStore.create` 写任务并入队。上传与 URL 的创建存储路径不同。
- MCP `start_subtitle_pipeline` 在 `src/mcp_server/tools.py` 组装请求，经 `src/mcp_server/business_client.py:create_task` 调用业务 REST API；它不是独立 TaskStore 写入点。`task_origin` 由 MCP 路由适配层传入，最后存为 `mcp`。MCP retry 同样经 REST `/api/tasks/{id}/retry`。
- `src/handler/tasks.py` 以别名重导出生命周期路由；保持旧测试 monkeypatch/导入兼容很重要。

TaskRecord 序列化/持久化的全部关键入口：`src/store/task_store.py` 中 `TaskRecord` dataclass、`_COLUMNS`（由 dataclass 字段生成）、`TaskStore._init_schema`、`create`、`create_if_no_recent_active`、`_insert`、`update`、`_row_to_record`、`to_dict`；API 输出由 `src/handler/schemas.py:to_out` 转成 `TaskOut` camelCase；`TaskOut` 不应意外暴露内部计划或凭据。所有新列需同时加入建表和旧库增量迁移，旧 SQLite 行必须能以确定默认值读回。`TaskStore.update` 只允许 `_COLUMNS` 字段，因此新增字段是否可更新也需有意决定。

去重与重试落点：URL 去重是 `create_if_no_recent_active` 内 `BEGIN IMMEDIATE` 原子查询，按 URL hash、创建时间窗口、task_origin 和 active status 匹配；上传当前走 `create`，不做此 URL 去重。重试在 `task_lifecycle.py:retry_task` 仅重置 status/progress/current_step/error 后再次入队；它更新原记录，因而固定计划应保留。重试目前没有重新校验 engine 配置或重算模型/插件计划。R1 需与 C0 明确旧记录缺少计划时的惰性/原子迁移策略、上传是否需要去重（不可自行扩大语义）、以及同一 URL 任务复用时计划来源。

Runner 当前仍负责取得翻译配置：`src/service/runner.py:_run` 读取 `_store` 的 TaskRecord，再由 `_engine_store.get(rec.engine)` 获取 engine_config，并传给 `run_pipeline(..., engine_config=...)`（用签名检查兼容旧 runner 测试替身）。R1 按既定边界不改 Runner；新计划的消费接线属于后续 I1。计划若含配置快照，必须遵循 C0 对敏感凭据“仅引用/执行时解析”或脱敏的决定；`plugin_plan_json`/任务计划快照不得写入 key/API token，也不得通过 API 输出或日志泄漏。此约束针对插件计划快照，不改变现有 `TranslationEngineStore` 在服务端存储翻译引擎凭据的职责。

R1 修改既有符号前必须先 impact：`TaskStore`（类，涉及建表/CRUD）、`TaskStore._init_schema`、`TaskStore._insert`、`TaskStore.create_if_no_recent_active`、`TaskStore._row_to_record`、`create_url_task`、`create_upload_task`、`retry_task`、`create_task_route`、`create_upload_task_route`。若修改 `TaskRecord` 字段还需对 `TaskRecord` 做 impact。GitNexus 不可用时这些均为待分析，不得把本静态调用链当作图谱 blast radius。

## G1 — 宿主 + 翻译插件纵向联通

建议新增 `tests/test_plugin_vertical_slice.py`，只依赖已冻结的 C0 契约、H1 完成证据和 A5 翻译适配器。方案指定不提前改编排器：G1 单独执行真实宿主与翻译插件，底层翻译函数用 fake/mock；覆盖成功、校验/执行异常、取消时资源清理和错误/取消保真。不要为了完成纵向测试修改 `orchestrator.py`、`runner.py` 或 TaskStore。

A5 当前实现的真实语义位于 `src/core/translator.py` / `src/core/translation_engines.py`，执行入口由 `src/service/orchestrator.py` 的翻译 handler 调用 `translate_srt`；但 G1 应只核对 A5 包装真实签名和测试，不改 A5 文件。接口、fake context 与取消机制完全取决于 C0，冻结前只可准备测试案例清单。

## 测试定位（均未运行）

R1 独占新增 `tests/test_plugin_plan_lifecycle.py`，覆盖 URL/上传/MCP 创建路径的计划解析、计划持久化/旧记录迁移、重试沿用计划及敏感字段脱敏。回归候选：`uv run pytest -q tests/test_task_store.py`、`uv run pytest -q tests/test_api.py`；可运行 `tests/test_api.py` 验证现有 API 行为，但不要修改该文件（归 I1 所有）。若改 MCP 创建适配（当前方案不要求）再定位 `tests/test_mcp_tools.py`、`tests/test_mcp_client.py`；MCP 实际新建最终仍走 API，需验证 task_origin 兼容。

H1：新增 manager/registry 单测文件（C0 冻结后由 owner 确定），不运行现有测试来替代。

G1：`uv run pytest -q tests/test_plugin_vertical_slice.py`（文件待 H1/A5 完成后新增）。

以上均为建议命令，未运行；当前任务明确只读准备。

## C0 冻结前的缺口与决策门

1. 确认 `plugin_plan` 的序列化版本、计划字段及 stage/operation 标识，并决定 TaskRecord 用新列还是版本化 JSON；定义空计划旧任务迁移和原子写回方式。不要凭方案中模糊快照描述自造字段。
2. 冻结凭据策略：engine 配置 ID、非敏感参数快照、敏感 key 的执行时解析边界，及任务排队后凭据轮换的预期。
3. 冻结重复 URL 返回既有任务时采用既有计划；确定计划落库必须与新任务插入同一事务，避免已创建但无计划的窗口。上传沿用现有不去重行为，不扩大为上传去重。
4. 冻结重试语义：保持原计划；旧历史任务无计划时如何重建，若重建则哪些字段/当前配置可用、何时写回，以及失败是否阻止 retry enqueue。
5. 冻结 H1 session 生命周期和错误分类，尤其工厂部分失败清理、close 失败叠加原异常、取消优先级；G1 才能写确定断言。
6. 冻结翻译请求/结果类型及取消上下文，以便 A5 与 G1 一致；保留 `engine` 配置记录 ID 与 `plugin_id` 实现身份的区分。

边界结论：H1 可作为 C0 后纯新增模块先行；R1 会触及任务存储与三类创建/重试生命周期，风险高且必须等计划 schema、迁移与凭据决策；G1 可在 H1/A5 完成后独立新增测试，不承担架构接线。当前三个任务均未实施，所有相关测试未运行。
