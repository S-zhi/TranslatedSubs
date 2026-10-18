# macOS 桌面一期实施约定

依据：[产品范围与验收目标](macos-desktop-v1-product-brief.md)。实施基线：`fe2ed74`。

本文件是开发代理之间的接口与文件所有权约定。状态为开发中，不代表已达到正式发行条件。

## 本轮交付状态（2026-10-17）

用户要求跳过剩余收尾与验证，本轮在开发版状态停止，保留全部工作区改动，未提交或发布。13 条初始工作线及后续集成修复由 `gpt-6-astra` 执行。

- 已通过的完整回归基线：Python 1214 passed、2 skipped、36 subtests passed；前端 151 passed。该基线位于最后一轮构建可搬移性修改之前。
- 原生代码直接编译及 19 项逻辑测试通过；冻结 Python 后端构建及离线原生依赖烟测通过。它们不能代替完整应用／DMG 验收。
- Browser 验证使用真实桌面 API 与受控模型状态：首启准备、错误重试、更新检查、默认参数、本地上传及测试视频预览已检查；未进行真实模型转写翻译。
- 最后构建收尾已写入显式 Swift 编译模式和 macOS 14.0 候选目标；代理报告 69 项打包测试通过，但任务在实际可搬移原生编译完成报告前停止，完整回归未对此版本重跑。
- 已修复一处阻断性问题：`LC_RPATH` 审计原先拒绝任何运行时搜索路径，而 Swift 工具链会为所有 macOS 可执行文件添加默认的 `/usr/lib/swift`，导致显式 Swift 编译模式永远无法通过。审计改为只拒绝指向系统目录之外且非包相对的搜索路径，与既有 bundle 审计规则保持一致。修复后真实编译产物已验证：`arm64`、`minos 14.0`、无非系统依赖、ad-hoc 签名，并通过 `audit_macho`；69 项打包测试与 19 项原生逻辑测试通过。
- 跳过／待续：可分发 ARM64 FFmpeg 与字体输入、最终可搬移 `.app`／DMG 构建、真实模型准备、M1／8GB 长视频性能与字幕质量、国内外网络、干净设备、签名公证及发布渠道验证。
- 独立只读审查已定位并经协调方复现确认两项缺陷，尚未修复（跨 native/runtime 与 models/admission 边界）：
  - **P1 数据目录独占仅由原生进程持有。**`DesktopRuntime.swift:41` 只由原生进程加锁，实际访问数据目录的后端不持锁；后端又通过 `posix_spawnattr_setpgroup(&attributes, 0)` 独立成组且无父进程死亡监测。原生进程被 `SIGKILL` 后旧后端继续运行，新实例可取得锁并执行任务恢复，导致同一任务被新旧两个后端同时处理。最小修复：把数据目录锁移交后端生命周期，并用父进程存活管道触发后端退出，新实例须等待旧后端退出后再恢复任务。
  - **P2 模型健康复查到期会把就绪状态短暂改写为未就绪。**`src/desktop/models.py:149` 在 TTL 到期后的首次 `status()` 必定把状态改为 `preparing`、`ready=False`，而复查线程要数百毫秒（`has_subtitles_filter` 实测约 310ms）。前端提交前刷新状态，用户在界面仍显示就绪时点击提交会被 `409 DESKTOP_MODELS_NOT_READY` 拒绝；复查完成后状态自行恢复。最小修复：把后台健康复查与安装就绪状态分离，提交时等待或复用当前复查结果再决定是否拒绝。
- 尚无完整且经过验收的 DMG；不得据此标记一期正式发行完成。

## 工程方案

- 原生 Swift / AppKit / WKWebView 桌面窗口复用现有 Web 工作台；使用 Swift Package 管理桌面源码，不引入 Node 运行时。
- 桌面窗口托管一个独立 Python 后端进程。发行包通过 PyInstaller onedir 收集 Python、业务依赖、Web 资源；首启联网下载模型。
- 桌面模式使用 SQLite 配置提供器，服务端默认继续使用 Redis。任务库沿用现有 SQLite。
- 数据位于用户 Application Support 目录，不写入应用包。关闭窗口保留后端；主动退出先确认，再停止任务和后端。
- 后端只监听 `127.0.0.1`，由操作系统分配端口；原生进程为每次启动生成随机会话令牌。WebView 使用 HttpOnly、SameSite=Strict 会话 cookie，原生请求使用同一令牌的 `X-API-Token` 头。不得把令牌放入 URL、日志或持久化业务配置。
- 默认流程：英文 → 简体中文、双语、硬字幕、本地转写、本地翻译、不配音。`base.en` 为开发候选，最终默认模型须以 M1／8GB 实测确定。
- 最低 macOS 候选构建目标为 14.0：冻结依赖审计发现多个二进制要求 macOS 14。该目标已写入构建配置，仍需干净设备验证，不代表兼容性已验收。

## 共享接口

### 运行环境

- `SUBTRANS_DESKTOP=1`：明确启用桌面模式。
- `SUBTRANS_DESKTOP_HOME`：应用数据根目录的绝对路径，默认 `~/Library/Application Support/TranslateSub`。
- `SUBTRANS_DESKTOP_SESSION_TOKEN`：原生进程生成的会话密钥，后端启动必需，不能输出。
- `SUBTRANS_DESKTOP_FFMPEG`、`SUBTRANS_DESKTOP_FFPROBE`：发行包中工具的绝对路径，由原生启动器传入。
- `SUBTRANS_DESKTOP_VERSION`：应用版本，由打包配置传入。
- `SUBTRANS_DESKTOP_UPDATE_URL`：可选的 HTTPS 更新 manifest 地址；未配置时明确显示未配置更新渠道，不伪造可用链接。
- `SUBTRANS_DESKTOP_MODEL_ENDPOINT`：可选 HTTPS 模型源；默认官方来源，不能未经验证宣称国内可用。

### 后端启动

入口 `python -m src.desktop --session-file <absolute-path>`；打包可执行文件名 `translatedsubs-backend`。

启动时先设置桌面环境，再导入配置与业务模块。绑定 loopback 的随机空闲端口，以已绑定 socket 启动 uvicorn，避免端口探测竞态。完成服务启动后原子写入权限 0600 的会话描述文件，内容至少包含 `port`、`pid`、`instance_id`，不得包含令牌。原生端读取后，带令牌探测 `/api/health`，成功才加载 WebView。退出清理本次会话文件。

桌面 API 在静态文件 catch-all 之前挂载，仅桌面模式启用。桌面包装层保护所有业务 API、媒体和 Web 内容；校验会话令牌、Host 与跨源修改请求。不得降低已有服务器模式认证要求。

### 模型管理器的增量能力

转写 `LocalModelManager`：

- 保留 `download(name)` 和既有返回字段。
- 增加可选关键字参数 `revision=None, endpoint=None`，桌面默认模型使用已核实的固定 revision。
- 库存状态增加 `downloadedBytes`、`totalBytes`、`revision`；未知总量用 null，不能伪造百分比。
- 暴露 `OFFICIAL_MODEL_REVISIONS`，至少包含开发候选 `base.en` 的已核实提交。在线核实失败时显式记录阻塞，不编造 revision。
- 下载完成前执行完整性与本地加载验证，验证完成后原子发布；失败可重试。

翻译 `TranslationModelManager`：

- 保持现有固定 revision、转换和离线烟测；保留已有 API。
- 状态增加 `downloaded_bytes`、`total_bytes`、`revision`、`phase`；现有 `model_status`、`installed_bytes`、`model_error` 保留。
- 支持配置下载 endpoint，下载、转换、校验阶段独立显示，不把转换误计为下载进度。

协调服务 `src/desktop/models.py`：

- `get_desktop_model_manager()` 返回单例；构造时可注入两个模型管理器用于测试。
- `status()` 返回下述模型准备结构。
- `start_prepare()` 幂等启动准备，返回状态；失败后调用相同接口重试。后台自动顺序准备两个模型，减少内存峰值。
- 状态为 `not_started / preparing / ready / failed`。每个模型为 `not_installed / downloading / converting / checking / ready / failed`。
- 返回 `{state, ready, models: [{id, name, status, phase, downloaded_bytes, total_bytes, error}], error}`。
- `ready` 必须同时要求转写和翻译模型、必要运行依赖可用。已准备好的模型不重复下载。
- `desktop_task_defaults()` 返回 `{sourceLang:'en', targetLang:'zh-CN', mode:'bilingual', burn:'hard', model:'local:base.en', engine:'local-opus-en-zh', needSubtitle:true, ttsEnabled:false}`。

### 生命周期服务

`src/desktop/lifecycle.py` 提供：

- `active_task_count()`：返回活动任务数量，测试可注入 store。
- `recover_desktop_tasks()`：启动时把遗留非终态任务标为可重试的失败状态并给出中断原因，保留历史与已完成结果；不自动重新入队。服务端既有恢复行为保留。
- `shutdown_desktop_tasks()`：停止新任务接收后取消当前活动任务、终止其关联进程，返回结果。原生端超时后回收其拥有的后端进程组；不能杀死不属于本应用的服务。
- 模型准备的退出由后端进程所有者处理，启动后必须重新检查残留状态。

### 更新服务

`src/desktop/updates.py` 提供 `get_update_service()`，返回对象 `check(force=False)`。

返回 `{state, current_version, latest_version, release_notes, download_url, error}`，state 为 `disabled / checking / current / available / failed`。

HTTPS JSON manifest：`{version, release_notes, published_at, platforms: {'macos-arm64': {url, sha256, minimum_macos}}}`。只接受有效稳定版本和 HTTPS 下载 URL；合理超时、缓存与失败回退，不安装或重启应用。不得在没有实际分发渠道时伪造更新地址。

### 桌面 HTTP 契约

- `GET /api/desktop/status` → `{version, platform:'macos-arm64', models:<准备状态>, active_tasks:<int>, defaults:<默认参数>, ready:<bool>}`。
- `POST /api/desktop/models/prepare` 与 `POST /api/desktop/models/retry` → 模型准备状态。
- `GET /api/desktop/updates?force=false` → 更新服务结果。
- `GET /api/desktop/tasks/active` → `{count:<int>}`。
- `POST /api/desktop/shutdown` → 停止接受新任务，发起停止与后端退出，返回确认。
- 桌面模式的 `/config.js` 在既有配置基础上设置 `window.SUBTRANS_DESKTOP={enabled:true,version:...}` 并固定 API origin，禁止旧 localStorage 覆盖指向其他后端。
- 模型准备 UI 通过状态轮询观察；应用启动自动发起准备，重复调用幂等。视频任务复用既有 API。

### 原生桥接

`window.webkit.messageHandlers.desktop.postMessage(...)` 支持：

- `{action:'export', taskId:<id>, kind:'video'|'subtitle'}`：原生保存对话框，带会话认证从本机固定产物端点下载；不得接受任意目标 URL 或任意本机读取路径。
- `{action:'openExternal', url:<https-url>}`：用系统浏览器打开已验证 HTTPS 地址，不在应用 WebView 加载远程内容。

原生端导航仅允许当前 loopback origin；文件选择委托使用系统文件选择器。关闭最后窗口不退出；菜单栏提供重新打开与退出。检查活动任务后才提示主动退出。原生端不注入访问密钥到 JavaScript。

## 并行文件所有权

| 工作线 | 唯一可写范围 |
| --- | --- |
| config | `src/config/bootstrap.py`、`src/config/config_repository.py`、新增 `src/config/sqlite_config.py`、`tests/test_desktop_config.py` |
| lifecycle | `src/desktop/lifecycle.py`、`tests/test_desktop_lifecycle.py` |
| runtime | `src/desktop/__init__.py`、`src/desktop/__main__.py`、`src/desktop/runtime.py`、`src/desktop/api.py`、`src/desktop/security.py`、`src/handler/app.py`、`tests/test_desktop_runtime.py` |
| whisper | `src/service/model_manager.py`、`tests/test_desktop_whisper_models.py` |
| translation | `src/service/translation_model_manager.py`、`tests/test_desktop_translation_models.py` |
| models | `src/desktop/models.py`、`tests/test_desktop_models.py` |
| native | `desktop/macos/Package.swift`、`desktop/macos/Sources/**`、`desktop/macos/Tests/**` |
| updates | `src/desktop/updates.py`、`tests/test_desktop_updates.py` |
| frontend | `web/js/desktop*.js`、`web/styles/desktop.css`、`web/js/main.js`、`web/js/ui-console.js`、`web/js/ui-preview.js`、`web/index.html`、`web/__tests__/desktop*.test.mjs` |
| packaging | `desktop/packaging/**`、`scripts/desktop/**`（不含 benchmark）、`.github/workflows/macos-desktop.yml`、`pyproject.toml`、`uv.lock`、`.gitignore`、`tests/test_desktop_packaging.py` |
| acceptance | `scripts/desktop/benchmark.py`、`tests/test_desktop_benchmark.py`、`docs/desktop-acceptance.md` |
| assets | `web/vendor/phosphor/**` |
| docs | `docs/macos-desktop-install.md`、`docs/macos-desktop-release.md`、`README.md`、`docs/local-quick-start.md` |

任何跨所有权修改由集成代理完成；工作线只报告需要的接口变化。代理不得提交或推送，不得更改其他代理的文件，不得启动未批准的对外发布或购买行为。

## 验证与当前外部条件

- 每条工作线完成相关单测和静态检查，并列明实际范围；共享配置、API 与完整集成后扩大回归范围。
- Web 可见改动通过 Browser preview 工具验证；原生源码需编译，真实应用生命周期和媒体能力另测。
- 本机为 ARM64、16GB 内存，不能代替产品要求的 M1／8GB 性能验收。
- 当前没有已选定的 60 分钟验收样本、两种地区网络记录、正式发行证书／公证授权或已部署的更新渠道。脚本和测试可就绪，相关发行验收状态保持未验证。
- 正式公开发行仍受原产品约定的签名、公证、性能、质量、国内外下载等门槛约束。
