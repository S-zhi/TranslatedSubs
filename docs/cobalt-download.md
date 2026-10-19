# cobalt 下载插件

`builtin.cobalt` 是可选的下载与探测实现，版本为字符串 `"1"`。它与 `builtin.ytdlp` 共用现有插件实例、任务选择和固定计划机制：catalog 共八个实现，仍有七个阶段与七个初始默认实例，下载默认仍为 yt-dlp。创建 cobalt 实例不会自动切换默认项。

仅连接自托管或明确获准使用的 cobalt 实例；项目不提供默认公共实例地址。

## 可选自托管与可用性检查

先按[本地快速启动](local-quick-start.md)准备基础环境。cobalt 是独立可选服务，HTTP 客户端 `httpx` 已包含在项目基础依赖中，无需新增 Python extra。它不替代后续音频提取、转写或封装所需的工具。cobalt 使用冻结的运行时 `download_proxy`，仅支持 HTTP(S) 代理，SOCKS 会被预检拒绝；yt-dlp 原有代理支持不变。

当 TranslatedSubs 后端直接运行在宿主机时，可用 Docker 启动只监听本机的实例：

```bash
docker run -d --name translatedsubs-cobalt --init --read-only --restart unless-stopped -p 127.0.0.1:9000:9000 -e API_URL=http://127.0.0.1:9000/ ghcr.io/imputnet/cobalt:11
```

```bash
curl --fail --silent --show-error http://127.0.0.1:9000/
```

`GET /` 应返回含 `cobalt.version`、`cobalt.url` 和 `cobalt.services` 的 JSON。请从 **TranslatedSubs 后端所在环境**检查地址可达；服务信息正常只说明 API 可达，不能证明某网站或媒体下载成功。

`API_URL` 决定 cobalt 返回的 tunnel 地址，必须能被 TranslatedSubs 后端访问，并与插件 `endpoint` 的部署方式一致。后端也在容器中时，`127.0.0.1` 指向该容器自身；应将两者接入同一网络并使用例如 `http://cobalt:9000/`，或使用后端可达的 HTTPS 反向代理地址。浏览器能访问地址不等于后端能下载 tunnel。

需要认证时，在 cobalt 端配置 `API_AUTH_REQUIRED=1`、`API_KEY_URL=file:///keys.json`，并只读挂载密钥文件；格式与部署细节见上游[实例保护指南](https://github.com/imputnet/cobalt/blob/main/docs/protect-an-instance.md)及[自托管指南](https://github.com/imputnet/cobalt/blob/main/docs/run-an-instance.md)。客户端凭据按下文保存。

## 创建实例与选择下载器

在 **其他设置 → 插件管理**（`/settings?tab=plugins`）的下载阶段点击“新增实例”，选择 **Cobalt 下载**，填写实例 ID、名称和服务地址。表单根据 catalog 自动显示以下非敏感字段：

| `config` 字段 | 允许值与默认值 |
|---|---|
| `endpoint` | 必填 HTTP(S) API 根地址；不允许 URL 用户名/密码、查询参数、片段、空白或无效端口；保存时补齐结尾 `/` |
| `socket_timeout` | 整数 1–300 秒；未覆盖时沿用冻结的下载超时，系统默认 30 秒 |
| `access_mode` | `anonymous`（默认）或 `api_key`；仅表示认证模式，不是密钥 |
| `fallback_policy` | `none`（默认）或 `ytdlp`；仅对下文列出的失败启用回退 |
| `filename_style` | `classic`、`pretty`、`basic`（默认）、`nerdy` |
| `audio_format` | `best`（默认）、`mp3`、`ogg`、`wav`、`opus`；用于仅音频下载 |

保存后，在任务表单 **高级插件选择 → 下载** 选择该实例。只想用于当前任务时保留原阶段默认；需要以后所有未指定实例的 URL 任务都使用 cobalt 时，再在设置中点击“设为默认”。上传任务没有下载阶段。

REST 方式先读取全局 revision，再用它创建实例。以下 `12` 仅为示例，应替换为本次 GET 返回的 revision；启用了项目 API Token 时，所有请求另带 `Authorization: Bearer <项目 Token>`。

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/api/settings/plugins/instances
```

```bash
curl --fail --silent --show-error -X POST http://127.0.0.1:8000/api/settings/plugins/instances -H 'Content-Type: application/json' -H 'If-Match: 12' --data '{"id":"cobalt-local","name":"Local cobalt","stage":"download","plugin_id":"builtin.cobalt","implementation_version":"1","config_schema_version":1,"enabled":true,"config":{"endpoint":"http://127.0.0.1:9000/","access_mode":"anonymous","fallback_policy":"none"}}'
```

创建响应含 `revision` 和 `instance`。实例写操作必须带不加引号的十进制 `If-Match`；它是整个配置文档的 CAS revision，模型、运行时设置等更新也会使它过期。收到 `409` 后重新读取、核对再提交。更新实例使用 `PUT /api/settings/plugins/instances/{id}`，提交 `config` 会替换整个覆盖对象，须保留 `endpoint`；切换默认使用 `PUT /api/settings/plugins/defaults`，请求体为 `{"defaults":{"download":"cobalt-local"}}`。每次写入都使用最新 revision。完整保护规则见[插件实例指南](plugin-instances.md)。

## API Key 的保存与范围

需要密钥的实例选择 `access_mode: "api_key"`，密钥通过现有运行时设置 API 保存，不写入插件 `config`。项目 API Token 与 cobalt API Key 是两个独立凭据。

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/api/settings/runtime
```

用上一步返回的 revision 替换 `13`，将 `COBALT_API_KEY` 替换为该 cobalt 实例发放的密钥：

```bash
curl --fail --silent --show-error -X PATCH http://127.0.0.1:8000/api/settings/runtime -H 'Content-Type: application/json' -H 'If-Match: 13' --data '{"cobalt_api_key":"COBALT_API_KEY"}'
```

再次读取以确认密钥存在标记：

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/api/settings/runtime
```

GET 和 PATCH 响应均不在 `values` 中返回 `cobalt_api_key`，只通过 `hasSecrets.cobalt_api_key: true` 表示已设置。PATCH 请求体直接是字段对象，不包 `values`；传 `{"cobalt_api_key":null}` 清除密钥。运行时 API 的 `If-Match` 可选，建议像示例一样使用 CAS；实例 API 则必须提供。

**v1 只有一把部署级共享 cobalt 密钥**：所有 `access_mode: "api_key"` 的 cobalt 实例都会使用它，不能分别绑定不同实例的 Key。任务计划只保存内部引用 `settings:cobalt_api_key`，执行和探测时读取当前密钥，轮换对已冻结计划也生效。密钥缺失或鉴权失败会明确报错，不触发 yt-dlp 回退。`anonymous` 不发送密钥。

密钥仅随 cobalt 解析 POST 以 `Authorization: Api-Key <key>` 发送；媒体 GET 与重定向请求不携带它。不会把 TranslatedSubs 的 Cookie 文件或浏览器 Cookie 直接转发给 cobalt；`cookiesFromBrowser` 探测选项不受支持。若服务需要站点 Cookie，应由 cobalt 管理员按其部署配置维护。密钥与 Cookie 文件不要提交到仓库。

## 单任务 API、画质与探测

以下 JSON 可作为 `POST /api/tasks` 的请求体，仅为该任务指定 cobalt：

```json
{
  "url": "https://example.com/video",
  "sourceLang": "en",
  "targetLang": "zh-CN",
  "needSubtitle": false,
  "quality": "720p",
  "pluginInstances": {"download": "cobalt-local"}
}
```

`pluginInstances` 是阶段到实例 ID 的对象；省略下载选择时跟随当前下载默认项。任务创建会冻结 endpoint、认证模式、回退策略及有效下载参数，后续编辑实例不改写已排队或重试任务。

`quality: "best"` 对应 cobalt `videoQuality: "max"`；支持的高度为 `4320`、`2160`、`1440`、`1080`、`720`、`480`、`360`、`240`、`144`，可使用带 `p` 的形式。`quality: "audio_only"` 对应 `downloadMode: "audio"`，其他画质使用 `downloadMode: "auto"`。不支持的画质会被拒绝，不会悄悄改成其他值。`filename_style` 控制上游命名策略，实际任务文件始终写为 `source.<扩展名>`。

`POST /api/tasks/probe` 使用同一选择和画质：

```json
{"url":"https://example.com/video","quality":"720p","pluginInstances":{"download":"cobalt-local"}}
```

cobalt 探测仅解析下载地址，不下载媒体文件；真正下载时重新解析，避免复用过期链接。成功不代表拥有完整格式信息：cobalt 路径返回 `extractor: "cobalt"`、`duration: null`、`formatsCount: 0`、`availableQualities: []`、`formats: []`，标题仅由文件名推导。工作台仍可在探测成功后提交任务，切换实例或更新 endpoint／认证模式／回退策略会使旧探测指纹失效。若显式启用回退且探测实际改用 yt-dlp，结果会标明回退来源。

## 回退与 v1 边界

`fallback_policy` 默认 `none`。设为 `ytdlp` 后，只有 cobalt 已分类为可恢复的失败才会切换到现有 yt-dlp 插件：连接/超时、HTTP `429` 或 `5xx`、已知服务/链接/格式不支持，以及暂不支持的 `picker`、`local-processing` 响应。日志标明切换来源；两者均失败时报告失败，不发布半成品。

以下情况**不回退**：取消、配置或实例选择无效、缺少密钥、HTTP `401/403`、DRM/付费/私有/年龄/地区限制、畸形或未知响应、非法媒体路径、空或无效媒体、程序异常。未知上游错误按失败处理，不承诺“所有错误都重试 yt-dlp”。

回退使用创建时冻结的 `quality`、`format_selector`、`proxy`、`socket_timeout`、`cookies_ref`、`merge_output_format`、`retries`、`concurrent_fragments`；Cookie 引用仍在执行时解析。启用回退会预检 yt-dlp、FFmpeg/FFprobe 依赖，并要求运行时 `download_retries` 至少为 `1`（系统默认 `3`）；`none` 策略允许保留 `0`。cobalt 本身只支持单文件 `tunnel`/`redirect` 结果，请求固定发送 `localProcessing: "disabled"`，不支持 picker 多项选择或客户端合并/转码流程。

本页给出配置与验证方法，不代表已完成真实 cobalt 实例或第三方网站验收。离线契约测试、mock UI 与本地 API 仿真不能替代实际服务测试。
