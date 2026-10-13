# Pipeline Plugin v1 任务看板

**C0 frozen v1.1；M0/M1/M2 已通过，14 项任务全部完成。** 统一相关后端验收为 **562 passed，4 条既有 FastAPI 弃用警告**，改动尚未提交。详见 [验收报告](validation-report.md)、[配置与迁移说明](operations.md)。冻结接口见 [contracts-v1.md](contracts-v1.md)，集中接线规范见 [integration-v1.md](integration-v1.md)。

核对工作树：`/Users/wenzhengfeng/.codex/worktrees/e307/TranslateSub`；HEAD：`c5a8e40c2aabb38e01f394aa826366e0ad337a47`，与原方案提交一致，当前为 detached HEAD。开始时工作树干净。用户确认“开始”后已进入代码实施，改动尚未提交。

## 已实际分发的准备任务

| Agent | 模型 | 已领取并完成的工作 | 交付 |
|---|---|---|---|
| `/root/c0_design` | GPT-6 Sol | C0 决策清单、14 项任务卡、四槽排程 | [契约与任务卡](c0-task-design.md) |
| `/root/host_plan_prep` | GPT-6 Luna | H1/R1/G1 源码、创建链、序列化及测试入口核对 | [宿主与计划准备包](host-plan-preflight.md) |
| `/root/adapters_prep` | GPT-6 Luna | A1–A7 底层签名、媒体产物与互操作要求核对 | [适配器准备包](adapters-preflight.md) |
| `/root` | 当前协调 Agent | 基线验证、任务汇总、文件归属冲突复核 | 本看板及[原始方案](source-plan.md) |

设计决策由 GPT-6 Sol 负责；包括契约、测试和集成在内的实际代码开发均分派 GPT-6 Luna。`host_manager` 完成 H1/G1/A2/A6/I1；`host_plan_prep` 完成 C0/R1/A3 和 E1 Runner 集成测试；`adapters_prep` 完成 A1/A4/A5/A7、正式媒体互操作及 V1；主协调完成统一 E1 验证与文档。所有 owner 共享工作树，按下述文件归属执行。

M1 放行证据：C0/H1/A5/G1 与 translator 组合曾通过 68 项；R1/A3/API 组合 178 项；A1/core 72 项、A2/core 22 项、A4/core 20 项、A6/core 21 项。最新正式 A2/A3/A6/A7 互操作及插件组合 29 项，A3 临时文件失败/取消清理补充回归 20 项，H1 probe 字段校验 25 项。以上范围有重叠，不能相加为总测试数；最终 E1 将提供一次统一范围的结果。真实 FFmpeg/WAV 已验证，Demucs/Kokoro 重型模型后端仍使用替身。

## 编码队列与依赖（已执行）

下表中的 L1/L2/L3 是实现槽位，调度时复用空闲的 Luna Agent，连同主协调 Agent 总并发不超过四个。Sol 负责契约、接线设计和最终只读复核，也占用一个子槽位。

| 批次 | L1 | L2 | L3 | 放行证据 |
|---|---|---|---|---|
| 0 | C0 契约/FakeContext 实现 | 只读准备 | Sol 设计复核 | 工具可用；契约测试、产物/状态矩阵通过，发布冻结版本 |
| 1 | H1 Registry/Manager | R1 计划与持久化 | A5 翻译适配器 | C0 已冻结 |
| 1b | G1 翻译纵向测试 | R1 收尾 | 下一批准备 | H1/A5 单测通过；不等待 R1 即可做 G1 |
| 2 | A1 下载/probe | A2 音频提取 | A3 人声/背景分离 | C0 已冻结；G1 优先完成以尽早反馈契约问题 |
| 3 | A4 转写 | A6 TTS | A7 字幕/配音封装 | C0 已冻结；A7 可与 A6 同时实现 |
| 4 | I1 集中接线 | V1 兼容测试 | A7 互操作结果复核/空闲 | I1 必须等 R1/G1/A1–A7 和配音互操作全部通过；V1 只依赖 C0，可提早使用空闲槽 |
| 5 | E1 跨模块验收与文档 | 原 owner 定点修复 | 原 owner 定点修复 | I1/V1 完成，执行分支矩阵与本地媒体 smoke |

A2/A3/A6/A7 互操作由 A7 owner 汇总，相关产物 owner 配合；它是交付 I1 前的验收条件，不限制 A7 基于 C0 契约先行开发。G1/V1 为独立测试任务，不承担业务接线。

每次真正启动编码任务，调度者必须补齐：当前 HEAD 与差异、C0 冻结版本、依赖任务的文件和实际测试证据、独占修改范围。详细输入、交付和验收见[任务卡](c0-task-design.md)。依赖未满足只可阅读或准备场景，不猜接口。

## 文件归属与验收

- C0 独占公共契约、公共导出和共享 FakeContext。H1 独占宿主；R1 独占任务计划、TaskStore、task_lifecycle 和新增计划生命周期测试。R1 可运行既有 API 测试，但不与 I1 并行修改它。
- A1–A7 各自独占适配器及对应测试；A2/A3 仅在需要时扩展各自 core 文件。I1 独占 orchestrator、runner、asset_resolver、默认注册和 REST/probe 接线。A7 不改旧 `_add_dubbed_video`，该替换由 I1 完成。
- 交付报告必须写清变更文件、实际测试命令与结果、未覆盖风险、契约修改申请。接口变化先交 Sol 设计 owner，冻结新版本后通知所有受影响任务；失败回派原文件 owner，禁止顺手跨边界修改。

工具已恢复：Context7 官方 MCP 已完成 Python 3.10 dataclass/SQLite 文档查询；GitNexus 缓存中的原生库入口已修复，当前工作树独立索引名为 `TranslatedSubs-e307`（初始 4,867 nodes / 12,397 edges / 300 flows）。CLI 位于本机 npm 缓存，FTS 扩展未安装但 query/impact 已实测可用。旧文档的工具缺失描述仅保留为准备阶段历史。

已查询 TaskRecord、创建任务入口、翻译入口及存储方法的 upstream impact。R1 检查发现 `TaskStore.update` / `_row_to_record` 为 CRITICAL，已报告并收窄实现：不修改这两个公共方法，新增专用计划 CAS 方法。其他 owner 修改既有符号仍须先 impact；提交前须 detect_changes，目前未提交。

C0 已执行 `tests/test_plugin_contracts.py`，5 passed。测试复用主工作树 Python 3.11 环境（`uv run --no-sync`，单独临时 uv cache，不安装或同步共享依赖）。先完成各任务相关单测，I1/E1 再覆盖编排器、Runner、缓存、存储、API/MCP、插件/计划兼容场景及实际改动的 core。真实云服务/下载测试未开启；未运行真实 Whisper/Demucs/Kokoro 推理，不把测试替身等同于模型验证。

最终修复包括：按固定计划决定 ASR 分离和创建时依赖校验；真实 render 配置与 `auto` voice 契约一致；Cookie 的 Path 类型兼容；上游元数据指纹传播；旧 NULL 计划迁移；取消和终态写入串行化；缓存发布窗口取消清理。详细结果与未运行范围见验收报告。
