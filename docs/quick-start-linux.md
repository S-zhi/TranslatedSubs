# Linux 快速启动与部署

本文面向 Ubuntu/Debian Linux，目标是先启动 TranslatedSubs 的 Web 工作台和业务 API，再按需配置 systemd 与 Nginx。Google Drive sidecar 是可选组件，不影响字幕主流程。

为了兼容已有安装，脚本默认沿用 `/opt/subtitles-ai`、`/var/lib/subtitles-ai` 和 `subtitles-ai` 服务名；这些是部署标识，不影响产品名称。

## 一键安装（推荐）

登录 Ubuntu/Debian 服务器后，只需执行：

```bash
curl -fsSL https://github.com/S-zhi/TranslatedSubs/releases/latest/download/install.sh | sudo bash
```

脚本会安装 FFmpeg、uv、Python 3.12 和锁定依赖，创建持久化目录、systemd 服务并执行健康检查。默认本地翻译不需要 API Key；需要显式使用 DeepSeek 时再配置 `.env`。

如果希望执行前先检查脚本，可以下载后再运行：

```bash
curl -fsSL https://github.com/S-zhi/TranslatedSubs/releases/latest/download/install.sh -o /tmp/translatedsubs-install.sh
less /tmp/translatedsubs-install.sh
sudo bash /tmp/translatedsubs-install.sh
```

用于 CI 的非交互模式：

```bash
sudo bash /opt/subtitles-ai/install.sh --non-interactive
```

非交互方式可能把密钥留在 Shell 历史或 CI 配置中，日常部署优先使用交互模式。安装完成后：

```bash
systemctl status subtitles-ai --no-pager
journalctl -u subtitles-ai -f
curl http://127.0.0.1:8000/api/health
```

## 1. 运行要求

- Python 3.10–3.12（推荐 3.12）
- `uv`
- FFmpeg、FFprobe；硬字幕还要求 FFmpeg 含 `subtitles`/libass 滤镜
- 首次下载本地翻译/识别模型时需要网络；服务启动本身不需要云端 API Key
- 建议至少 2 核 CPU、4 GB 内存，并为视频产物预留足够磁盘空间

安装系统依赖：

```bash
sudo apt update
sudo apt install -y curl ca-certificates git ffmpeg fontconfig fonts-noto-cjk
```

确认 FFmpeg 和硬字幕能力：

```bash
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -filters | grep ' subtitles '
```

最后一条命令有输出，才表示可以使用硬字幕。没有输出时仍可运行服务，但任务需选择软字幕；也可以改装带 libass 的 FFmpeg。

## 2. 安装项目

项目仓库是 <https://github.com/S-zhi/TranslatedSubs>。以下命令将 `main` 分支部署到兼容已有安装的 `/opt/subtitles-ai`：

```bash
sudo mkdir -p /opt/subtitles-ai
sudo chown "$USER":"$USER" /opt/subtitles-ai
git clone --branch main --single-branch https://github.com/S-zhi/TranslatedSubs.git /opt/subtitles-ai
cd /opt/subtitles-ai
```

安装 `uv`、Python 和锁定依赖：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
sudo install -m 0755 "$(command -v uv)" /usr/local/bin/uv
uv python install 3.12
uv sync --frozen --no-dev
```

这一步不会安装本地翻译可选依赖，服务可以直接启动。要使用默认的本地英语 → 简体中文翻译，再执行：

```bash
uv sync --frozen --no-dev --extra local-translation
```

启动后在设置 → 翻译引擎点击“本地 CPU 英译中”的“下载并转换”。

项目要求 Python `>=3.10,<3.13`。`uv sync --frozen` 会严格使用仓库中的 `uv.lock`，适合服务器部署。

## 3. 配置服务

复制配置模板：

```bash
cd /opt/subtitles-ai
cp .env.example .env
chmod 600 .env
```

编辑 `.env`。默认本地翻译不需要密钥；下面的 DeepSeek 配置仅在显式选择云端兼容引擎时填写：

```ini
SUBTRANS_DEEPSEEK_API_KEY=你的-deepseek-api-key
```

服务器上建议把数据和 SQLite 数据库放到独立持久化目录：

```bash
sudo mkdir -p /var/lib/subtitles-ai/data
sudo chown -R "$USER":"$USER" /var/lib/subtitles-ai
```

然后修改 `.env`：

```ini
SUBTRANS_DATA_DIR=/var/lib/subtitles-ai/data
SUBTRANS_DB=/var/lib/subtitles-ai/data/db/app.db
```

常用的资源参数如下，可先保留默认值，再根据服务器性能调整：

```ini
SUBTRANS_WORKERS=8
SUBTRANS_DOWNLOAD_WORKERS=2
SUBTRANS_DL_CONCURRENT_FRAGMENTS=4
SUBTRANS_MAX_UPLOAD_MB=2048
SUBTRANS_MAX_VIDEO_MINUTES=180
```

如果目标站点要求登录或年龄验证，可导出 Netscape 格式 cookies 文件，并在 `.env` 中写绝对路径：

```ini
SUBTRANS_COOKIES=/var/lib/subtitles-ai/cookies.txt
```

`.env`、cookies、`app.db` 和 `data/` 都包含敏感信息或任务数据，不要提交到 Git。

## 4. 首次启动与验证

先在前台启动，便于直接检查错误：

```bash
cd /opt/subtitles-ai
uv run uvicorn src.handler.app:app --host 0.0.0.0 --port 8000
```

不要在生产环境使用 `./scripts/start.sh`：该脚本面向本地开发，启用了 `--reload`，并且会强制编译和启动 Google Drive sidecar。

另开终端验证：

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/health/ready
```

第一条应返回 `{"ok":true}`。第二条中：

- `initialized: true`：下载、识别和默认本地翻译主流程已就绪；模型或可选依赖未安装时服务仍可启动，`missing` 会给出安装提示；
- `capabilities.hard_burn: true`：硬字幕可用；
- `missing`：仍缺少的密钥、命令或目录权限。

浏览器访问 `http://服务器IP:8000/` 即可打开 Web 工作台，接口文档位于 `http://服务器IP:8000/docs`。如果访问失败，检查云厂商安全组和服务器防火墙是否允许 TCP 8000；不要把 8000 端口直接暴露到不受信任的公网。

## 5. 使用 systemd 后台运行

服务采用进程内任务队列和 SQLite，建议只运行 **1 个 Uvicorn 进程**，不要添加 `--workers 2` 等多进程参数。任务并发由 `.env` 中的 `SUBTRANS_WORKERS` 控制。

创建专用系统用户并移交目录权限：

```bash
sudo useradd --system --home /opt/subtitles-ai --shell /usr/sbin/nologin subtitles-ai 2>/dev/null || true
sudo chown -R subtitles-ai:subtitles-ai /opt/subtitles-ai /var/lib/subtitles-ai
sudo chmod 600 /opt/subtitles-ai/.env
```

创建 `/etc/systemd/system/subtitles-ai.service`：

```ini
[Unit]
Description=TranslatedSubs FastAPI Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=subtitles-ai
Group=subtitles-ai
WorkingDirectory=/opt/subtitles-ai
ExecStart=/opt/subtitles-ai/.venv/bin/uvicorn src.handler.app:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0027
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

加载并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now subtitles-ai
sudo systemctl status subtitles-ai --no-pager
```

查看实时日志：

```bash
sudo journalctl -u subtitles-ai -f
```

重启与停止：

```bash
sudo systemctl restart subtitles-ai
sudo systemctl stop subtitles-ai
```

## 6. 公网访问建议

正式对外提供服务时，建议让 Uvicorn 只监听 `127.0.0.1:8000`，再由 Nginx/Caddy 提供 HTTPS、访问认证和上传限制。将 systemd 中的启动参数改为：

```ini
ExecStart=/opt/subtitles-ai/.venv/bin/uvicorn src.handler.app:app --host 127.0.0.1 --port 8000
```

Nginx 反向代理的最小示例：

```nginx
server {
    listen 80;
    server_name subtitles.example.com;

    client_max_body_size 2048m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 7200s;
        proxy_send_timeout 7200s;
        proxy_request_buffering off;
    }
}
```

配置域名证书，并在 Nginx/Caddy、VPN 或零信任网关层增加身份认证。项目支持通过 `SUBTRANS_API_TOKEN` 保护全量 API（包括任务列表、详情、媒体/字幕下载、SSE 进度流与数据变更等，支持 HTTP Header `Authorization`/`X-API-Token` 或 URL 参数 `?token=`/`?api_token=`），但当前 Web 工作台默认未集成 Token 自动携带逻辑；如果主要通过浏览器访问 UI，建议通过网关/VPN 进行接入防护，或在客户端请求中携带 Token。

## 7. 更新、备份与故障排查

更新前至少备份 SQLite 和任务产物：

```bash
sudo systemctl stop subtitles-ai
sudo cp /var/lib/subtitles-ai/data/db/app.db /var/lib/subtitles-ai/data/db/app.db.bak
cd /opt/subtitles-ai
sudo -u subtitles-ai git pull --ff-only
sudo -u subtitles-ai /usr/local/bin/uv sync --frozen --no-dev
sudo systemctl start subtitles-ai
```

更稳妥的生产方式是以新目录发布新版本，验证后再切换服务路径；`/var/lib/subtitles-ai` 应始终独立保留。

常见问题：

| 现象 | 检查方式 |
| --- | --- |
| 服务启动后立即退出 | `journalctl -u subtitles-ai -n 200 --no-pager` |
| readiness 提示缺密钥 | 检查 `/opt/subtitles-ai/.env`，修改后重启服务 |
| `data_directory`/`database_directory` 不可写 | 检查 `/var/lib/subtitles-ai` 的属主和权限 |
| 硬字幕不可用 | 运行 `ffmpeg -hide_banner -filters \| grep ' subtitles '`，或任务改选软字幕 |
| 视频 URL 下载失败 | 更新依赖中的 yt-dlp，必要时配置 `SUBTRANS_COOKIES` |
| 浏览器无法访问 | 检查 `systemctl status`、监听地址、安全组和防火墙 |
| 接口返回 401 | 检查是否设置了 `SUBTRANS_API_TOKEN`，以及客户端是否发送 Bearer Header、`X-API-Token` Header 或 `?token=`/`?api_token=` URL 参数 |

## 8. 可选：Google Drive sidecar

字幕主流程不依赖 Google Drive。确实需要云盘页面时，再安装 Go 1.23 或更高版本，并参考 [`drive-service/README.md`](../drive-service/README.md) 配置 `drive-service/config.local.json`。

远程 Linux 服务器上的 Google Desktop OAuth 使用动态 `127.0.0.1` 回调，授权流程比本机部署更复杂。建议先完成主服务部署，再通过安全隧道或受控桌面环境单独处理 Drive 授权；不要为了启动主服务而创建空的 Drive 配置。
