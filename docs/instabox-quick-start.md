# InstaBox 账号托管的一键试用

宣传页的“快速使用”通过维护方的 **InstaBox 账号**创建一台短期独立云机器，自动启动 TranslatedSubs，再提供带本次实例访问权限的工作台入口。访客无需 InstaBox 账号或终端命令。创建只发生在点击按钮后；页面初始化和刷新只查询状态。

**适用范围：仅官网部署。** 云端试用由构建开关控制，默认开启；自托管宣传页用 `npm --prefix site run build:no-trial` 构建即可完全去掉试用 UI 与 `/api/trials` 请求，无需部署控制服务。

这项功能需要部署一个小型控制服务 `src.instabox_launcher`。宣传页仍可作为静态资源发布，视频任务在 InstaBox 云机器内处理。仅上传 `site/dist` 不能创建机器；**仅在开启试用的构建中**控制服务未配置时，页面会显示在线体验暂未开放；关闭试用的构建不含任何试用入口。

## 部署结构

```text
浏览器 ── HTTPS / ───────────→ 静态宣传页（site/dist）
       └─ /api/trials ───────→ 试用控制服务（127.0.0.1:8001）
                                  │ 服务端 InstaBox API Key
                                  ↓
                               InstaBox API
                                  ↓
                         独立机器：工作台 + API + Redis
```

控制服务使用已有 FastAPI、httpx、uvicorn 和标准库 SQLite，不加载媒体后端配置，不要求运行 Redis、FFmpeg 或模型。首版支持 **Linux/macOS 单进程**；文件锁阻止多个进程同时使用同一数据库。不要使用多个 Uvicorn workers 或复制控制服务共享数据库。

## 1. 准备维护方配置

在服务器的受限环境文件或部署平台的 Secret 设置中配置以下值。不要把真实值写进仓库、`VITE_*`、静态托管环境变量、页面或聊天记录。

| 变量 | 用途 |
| --- | --- |
| `INSTABOX_API_KEY` | 维护方 API Key；需要读取、创建、执行命令、发布端口和删除机器权限 |
| `SUBTRANS_LAUNCHER_SECRET` | 至少 32 字符的高强度随机密钥，用于会话归属和独立实例令牌派生；建议随机生成 32 字节以上 |
| `SUBTRANS_LAUNCHER_ORIGIN` | 宣传页的精确 HTTPS origin，例如 `https://subs.example.com`；不含路径 |
| `SUBTRANS_LAUNCHER_SOURCE_REVISION` | 已发布到官方仓库的完整 40 位小写提交 SHA，必须包含本次工作台令牌接收实现 |
| `SUBTRANS_LAUNCHER_DB_PATH` | 私有持久目录中的 SQLite 路径，例如 `/var/lib/translatedsubs-launcher/trials.sqlite3` |

可选配置：

| 变量 | 默认值 / 说明 |
| --- | --- |
| `SUBTRANS_LAUNCHER_TEMPLATE_ID` | `universal`，需要 Git、Bash、curl、flock、Docker daemon 和 Docker Compose |
| `SUBTRANS_LAUNCHER_SNAPSHOT_ID` | 未设置；设置后从维护方的干净快照创建，替代模板 |
| `SUBTRANS_LAUNCHER_TTL_SECONDS` | `1800`，允许 300–3600；从本地预留试用名额时起算，不因刷新或重复点击延长 |
| `SUBTRANS_LAUNCHER_MAX_ACTIVE` | `2`，允许 1–5；创建中、状态不明和回收中的机器也占名额 |
| `SUBTRANS_LAUNCHER_HOURLY_BUDGET` | `6`；滚动一小时最多预留次数，失败也计入 |
| `SUBTRANS_LAUNCHER_DAILY_BUDGET` | `12`；滚动 24 小时最多预留次数，失败也计入 |
| `SUBTRANS_LAUNCHER_BOOTSTRAP_SECONDS` | `1200`；启动时限，超过后回收，且不能超过试用期限 |

密钥和 origin 必须在控制服务重启后保持不变，否则无法恢复原会话/实例令牌。数据库保存 launcher 身份，错误变更会拒绝启动；有未回收资源时不要直接删除数据库或改密钥。API Key 可在同一 InstaBox 账号内轮换。数据库及其 `-wal`、`-shm` 和 `.lock` 放在持久私有目录，保持运行账号独占。

源码版本必须先发布。若固定 SHA 不含 `web/config.js` 的试用地址保护、`consumeTrialToken` 和主入口消费调用，启动脚本会拒绝发布工作台。不能用不含这些改动的旧 `main` 验收自动登录。

快照只能包含可公开给访客的系统和依赖，不得包含维护方 API Key、Vault 文件、浏览器会话、真实业务配置或用户数据。每次新建机器使用 `vault:false`，独立 Compose 项目、数据卷和重新派生的实例令牌。干净快照可保留构建缓存以加速，仍执行源码和鉴权校验。

## 2. 启动控制服务

现有项目环境可直接使用已有依赖：

```bash
uv run python -m src.instabox_launcher --host 127.0.0.1 --port 8001
```

轻量控制服务器不必安装媒体处理依赖。在独立虚拟环境中安装所需服务包，再从仓库根目录启动同一模块即可：

```bash
python3 -m venv .venv-launcher
```

```bash
.venv-launcher/bin/python -m pip install 'fastapi>=0.138.2' 'httpx>=0.28.1' 'uvicorn>=0.49.0'
```

```bash
.venv-launcher/bin/python -m src.instabox_launcher --host 127.0.0.1 --port 8001
```

用 systemd 等进程管理器长期运行，设置仓库根目录为工作目录，使用独立服务账号，`UMask=0077`，从权限 0600 的环境文件加载上述值。不要把 API Key 放在启动命令参数中。控制服务进程和 SQLite 必须保持运行/持久化，才能自动重试回收；关闭服务不会主动清空资源记录。

只检查配置是否可用，不创建机器：

```bash
curl --fail --silent http://127.0.0.1:8001/api/trials/current
```

完整配置时响应 `configured:true,status:"idle"`；缺必要配置时是 `configured:false,status:"unavailable"`。不输出密钥。实际无效 API Key、账户额度或模板问题会在创建时被检测。

### 用一键安装器部署控制服务（可选）

官网服务器可以使用仓库的一键安装器附带部署控制服务：

```bash
sudo bash install.sh --with-cloud-trial
```

它会创建 `${SERVICE_NAME}-cloud-trial.service`、权限 `0700` 的 SQLite 数据目录，并从 `/etc/<服务名>/launcher.env`（权限 0600、属主 root）读取密钥。**安装器不收集、不写入 `INSTABOX_API_KEY` 或 `SUBTRANS_LAUNCHER_SECRET`**；请先自行创建该环境文件。安装器只 `enable` 不 `start` 该服务，也不会为它跑健康检查——没有密钥它无法验证，因此不会报告虚假成功。环境文件就绪后执行：

```bash
sudo systemctl start <服务名>-cloud-trial
```

控制服务默认只监听 `127.0.0.1:8001`，公网必须按下文的同源反代暴露。未通过 `--with-cloud-trial` 或 `SUBTRANS_ENABLE_CLOUD_TRIAL=1` 显式启用时，安装器不会创建第二个单元、目录或任何试用相关配置。

## 3. 同源转发和宣传页

构建官网宣传页（默认即开启试用）：

```bash
npm --prefix site run build
```

自托管宣传页若不需要试用，改用 `npm --prefix site run build:no-trial`：产物没有试用按钮、`#cloud-start` 锚点或页脚入口，也不会请求 `/api/trials`。开关是 `SUBTRANS_ENABLE_CLOUD_TRIAL`，默认开启，只有字面量 `0` 关闭；关闭时 Vite 的 `define` 把开关内联成 `false`，Rollup 随之摇掉试用模块，构建产物中不含试用代码。

生产必须让浏览器通过宣传页 **同一 origin**访问 `/api/trials`，将这个前缀转发到控制服务，然后才应用静态页面 fallback。无需开放跨域 CORS。

Nginx 示例（放在已配置好 TLS 的 `server` 中）：

```nginx
root /srv/translatedsubs/site/dist;

location /api/trials {
    proxy_pass http://127.0.0.1:8001;
    proxy_set_header Host $host;
    proxy_set_header Origin $http_origin;
    proxy_read_timeout 45s;
    proxy_buffering off;
    client_max_body_size 1k;
    add_header Cache-Control "no-store" always;
    add_header Referrer-Policy "no-referrer" always;
}

location / {
    try_files $uri $uri/ /index.html;
}
```

`SUBTRANS_LAUNCHER_ORIGIN` 必须与浏览器的 Origin 完全一致，包括开发端口。控制服务不信任访客提交的 `X-Forwarded-For`，全局并发和滚动创建预算始终生效；若部署来源限流，在可信反向代理层配置，避免使用任意转发头绕过限制。

静态平台若只支持文件托管、不能将该路径反代到控制服务，需要增加能做同源路由的网关。Vite 开发服务器已代理 `/api/trials` 到 `http://127.0.0.1:8001`，可用仅供服务端读取的 `TRIAL_PROXY_TARGET` 修改开发目标；它不会作为前端密钥打包。

本地开发将 origin 设为 `http://127.0.0.1:5173`。仅 loopback 允许 HTTP，生产使用 HTTPS 和 `__Host-`、HttpOnly、Secure、SameSite 会话 cookie。不要在浏览器先使用 localhost、再切换 127.0.0.1，否则会建立不同会话。

## 接口和恢复行为

| 接口 | 行为 |
| --- | --- |
| `GET /api/trials/current` | 建立匿名 cookie；恢复当前会话的进度和就绪入口；不分配机器 |
| `POST /api/trials`，JSON `{}` | 为当前会话预留并创建试用，已有试用时复用 |
| `DELETE /api/trials/current`，JSON `{}` | 结束试用，撤回入口并请求删除机器与数据 |

POST/DELETE 要求有效会话、精确 Origin 和 JSON；访客不能指定命令、仓库、模板、快照或 API 地址。试用 ID 不是访问授权。响应禁用缓存和 referrer，原始提供方错误及命令日志不会回传浏览器。

状态覆盖创建、启动、检查、发布、就绪、回收、失败与到期；页面轮询中断仅重新查询，不自动重发创建。后台任务独立于页面运行，刷新不丢失试用，同一会话重复点击不会多开机器。清除 cookie 或换浏览器会建立新会话，旧资源继续遵循原到期回收。

SQLite 先原子预留名额，再调用 InstaBox。创建请求、幂等键、固定源码版本、操作和机器 ID 会持久化。连接中断时复用原请求；过期后不再重发创建，而是按专用 metadata 找回并删除。删除操作确认失败后使用新的持久幂等键重试；仅网络结果不明时重用原键。

若创建结果不明且 metadata 暂时查不到机器，名额继续保留，控制服务会持续对账，不据此再开一台。维护者应在 InstaBox 控制台核对 `app=translatedsubs-trial`、本控制服务的 `launcher` 和 `trial` metadata；不能通过清库强行释放不明资源。不要删除其他机器。

## 启动、鉴权与回收

- 控制服务固定使用 `https://api.instabox.sh`，禁止跟随重定向。InstaBox Key 仅存在控制服务器的 HTTP 客户端中，不传入云机器。
- 机器创建设置 `vault:false`、独立 metadata、`onTimeout:"kill"` 和短期超时。提供方超时删除是控制服务以外的兜底；本地期限从名额预留起算。
- 私有脚本通过 exec stdin 上传，由远程文件锁串行启动；冷构建在后台运行，短请求轮询真实状态，不受单次 exec 600 秒上限影响。
- 脚本固定 checkout 完整 SHA；先启动 Redis、显式初始化配置、写独立实例令牌，再启动应用。
- 脚本和控制服务都检查健康 HTTP 200、无令牌受保护接口 HTTP 401、正确令牌 HTTP 200。随后由控制服务发布 8000，校验返回的机器 ID、端口和 InstaBox HTTPS 主机。
- 当前会话获得 `https://<专用主机>/#trial_token=<实例令牌>`。工作台在首次业务请求前把令牌保存在当前 tab 的 sessionStorage（受限时保留内存副本），清除 fragment，并将 API 地址锁定到该机器 origin；不会把它带到旧的外部后端设置。
- 这个 fragment 包含本次试用权限。不要分享链接。它不包含维护方的 InstaBox Key。现有 SSE/媒体请求仍会使用本实例 token query；试用应用保留访问日志，并在格式化前遮蔽这些敏感参数。
- 主动结束、启动失败或到期都会撤回入口并删除云机器；删除失败继续重试，未确认删除前不释放并发名额。试用数据不长期保留，下载需要的结果后再结束。

## 基础服务的范围

“工作台已就绪”表示 Web、API、Redis 和实例鉴权通过检查，不等于字幕模型和所有插件已准备好。

- 基础 Dockerfile 提供 Python 依赖与 FFmpeg，不预填云端 API Key或下载模型权重。
- Whisper 需要在设置中下载所选模型；本地英译中需要相应可选依赖和模型，或自行配置云端兼容翻译服务。见[本地英译中](./local-translation.md)。
- YouTube 下载还需要在后端镜像中安装 Deno / Node.js 22+；基础 Dockerfile 未包含该运行时，见 [YouTube 下载检查](./local-quick-start.md#youtube-下载检查)。
- TTS、人声分离和 Drive 按需另行配置。短期体验不要上传需要长期保留的工作文件。

## 验证与上线验收

本地相关测试不使用账号或真实云资源：

```bash
uv run pytest -q tests/test_instabox_launcher.py tests/test_instabox_start.py
```

```bash
npm --prefix site test
```

```bash
node --test web/__tests__/trial-token.test.mjs web/__tests__/api-auth.test.mjs web/__tests__/api-auth-integration.test.mjs web/__tests__/config.test.mjs
```

测试覆盖提供方契约、真实配置初始化语义、会话隔离、事务名额/预算、重复点击、未知结果、回收重试、首次请求鉴权和旧地址覆盖。浏览器验证可使用临时本地 mock provider；生产代码没有模拟开机开关或访客自定义 API 目标。

**真实 InstaBox 完整开机验收尚未通过。** 2026-10-22 的真实账号测试确认创建与删除操作均为 `succeeded`，但远程启动返回 `exec_failed`，尚未完成 Docker 构建、公开端口和工作台自动登录验收。现有日志不足以确认具体失败命令；不能仅凭模板能力列表断定缺少 Docker。发布包含令牌接收代码的固定 SHA，安全配置服务器环境后，仍需定位远程执行错误并验收自动登录、到期失效和回收。Linux/systemd 安装器新增选项目前通过了语法及参数校验测试，尚未在真实 Linux 主机上完整安装验收。

提供方契约核对日期：2026-10-19。参考[集成文档](https://instabox.sh/docs/integrate)、[公开端口](https://instabox.sh/docs/ports)和 [OpenAPI](https://api.instabox.sh/v1/openapi.json)。
