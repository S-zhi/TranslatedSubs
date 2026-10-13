# Whisper 本地模型管理迁移

模型商店移至「模型设置 → Whisper」，沿用原生 JavaScript 和已有页面样式。入口为 `/model-settings?kind=whisper`。当前模型及转写参数在模型库之前；官方目录和 Hugging Face 本地目录导入共用原有资产接口。

## 修改范围

- `web/index.html`：搬迁商店，移除旧 Tab、Whisper 云端连接表单和重复资产列表，显示当前参数与独立反馈，增加导入快捷入口。
- `web/js/ui-local-models.js`：库存卡片、当前标识、READY 选择、删除保护、操作反馈、串行刷新和可见性轮询。
- `web/js/ui-model-settings.js`：库存与保存配置融合、规范化模型 ID 复用、Whisper-only PUT、只读字段过滤、响应确认、失败回滚和未保存编辑保护。
- `web/js/api.js`：保留结构化服务器错误；目录上传保留浏览器 multipart boundary，并使用十分钟超时；示例库存、设置和任务快照遵循同一契约。
- `web/js/ui-console.js`、`web/js/task-whisper.js`：真实有效模型、托管选择锁定、READY 选项、读取失败门禁、提交前刷新。
- `web/js/router.js`、`web/js/store.js`、`web/js/ui-shell.js`、`web/js/ui-advanced-settings.js`：类型深链接、历史导航、旧 models 入口转向 Whisper，保留其他设置导航。
- `web/styles/translation-settings.css`、`web/js/locales.js`：Whisper 参数表单显示与新增文案映射。
- `web/__tests__/api.test.mjs`、`web/__tests__/ui-model-settings.test.mjs`：更新 API、示例模式和设置交互测试。
- `web/__tests__/ui-local-models.test.mjs`、`web/__tests__/router-model-settings.test.mjs`：新增轮询、请求竞态及导航回归测试。

`local-model-import.js` 与既有预检测试保持原样；本次继续运行其五项测试验证格式和过滤规则。

## 状态和保存规则

库存是安装状态来源，模型设置是当前选择来源。下载与导入只增加资产；只有 READY 模型可以设为当前。设置中缺失的当前模型保留为禁用选项并标明状态，不自动改选其他模型。

设为当前使用现有 `PUT /api/settings/models`，只发送 Whisper 配置。按规范化模型名称复用已有卡片 ID，发送唯一当前标志，并移除 `runtimeManaged`、`status`、`_runtimeManaged` 等只读字段。页面以 PUT 返回的配置确认成功；失败保留之前的当前模型。

Whisper 可见时约 2.5 秒刷新库存与配置；离开该类型、离开页面或浏览器隐藏时停止。恢复可见重新启动唯一计时器。强制刷新排队，旧响应不能覆盖保存结果；轮询不重置未保存的参数。

任务页优先读取公开 `whisper.runtimeManaged`。当前后端未提供此字段时，兼容读取真实 `/api/health/ready` 的 `model_settings.whisper`。统一管理启用时显示并锁定服务器有效模型；未启用时仅 READY 模型可选。读取失败或当前模型缺失时拒绝字幕任务提交。

## 实际验证

```sh
node --test web/__tests__/api.test.mjs web/__tests__/ui-model-settings.test.mjs web/__tests__/ui-local-models.test.mjs web/__tests__/local-model-import.test.mjs web/__tests__/router-model-settings.test.mjs
```

上述五个前端测试文件共 49 项，全部通过。

后端只运行现有两个快照集成节点：

```sh
uv run pytest -q tests/test_model_runtime_integration.py::test_http_save_then_create_persists_runtime_snapshot tests/test_model_runtime_integration.py::test_saved_settings_are_snapshotted_and_used_by_runner
```

两项通过。使用已有虚拟环境运行，没有安装或修改后端依赖。未运行全量测试或云端 live pipeline。

浏览器验收使用本分支真实 FastAPI 服务，端口 8017、隔离临时数据目录，未修改用户原有模型文件或 8000 服务：

- 商店只位于 Whisper，HTML 无重复 ID，当前参数在模型库之前；LLM/TTS、其他设置、深链接刷新与前进后退通过。
- 官方 Tiny 下载实际显示 DOWNLOADING，之后因 Hugging Face TLS 错误显示 ERROR 和原因；重试重新进入 DOWNLOADING。成功下载至 READY 的真实路径尚未通过。另以已有真实官方模型的临时副本验证 READY 卡片与设为当前。
- 实际上传官方 Hugging Face Whisper Tiny 的十个根目录文件，目录预检过滤缓存文件，后端离线加载验证成功并更新列表；未自动激活。
- 实际 PUT 保存、刷新持久化、重复选择复用卡片、唯一当前标识、当前删除提示通过。服务器对隔离任务占用测试返回 MODEL_IN_USE，页面保留原因和重试建议。
- 对临时导入副本制造无效配置后重新检查，服务器返回 MODEL_INVALID 并清理资产；设置保留缺失当前模型，任务页锁定缺失值并阻止字幕任务。
- 通过浏览器上传生成的一秒测试视频创建真实任务，任务 SUCCESS；任务页、任务记录和 Whisper 运行时快照均为 `local:whisper-tiny`。之后切换当前模型为 `local:tiny.en`，旧任务快照保持不变。

保存失败回滚、只读过滤、并发刷新、隐藏后恢复、未保存字段及示例状态转换由定向测试覆盖；未使用示例模式代替上述真实接口验收。

## 尚未完成的后端集成

本次不修改 Python 后端。该 checkout 的真实接口仍存在以下缺口：

1. GET 模型设置未提供 Whisper 的公开 `runtimeManaged`、真实 `status` 和卡片 `status`。
2. PUT 尚未执行最终 READY 校验，缺少相应 MODEL_NOT_READY 服务端保护。
3. DELETE 尚未拒绝当前配置模型，缺少 MODEL_CURRENT 服务端保护；已有 MODEL_IN_USE 已实测。
4. 官方下载成功路径受本机 Hugging Face TLS 错误阻塞，不能将其记为完成。

GitNexus 本地 CLI 已成功建立本 checkout 索引并分析主要修改符号。最终变更扫描为 HIGH，影响模型设置、任务创建及导航相关流程；API 对象方法的调用图覆盖有限，不能把工具结果当作完整调用覆盖。Context7 MCP 和本地 CLI 不可用，本次未声称完成 Context7 查询。
