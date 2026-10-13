# TranslatedSubs Drive Service

这是一个单用户、本机运行的 Google Drive sidecar。它不读取 Python 服务的数据库，也不导入 Python 包；只通过内部 HTTP 调用现有的 `/api/tasks/upload`。Drive 的 OAuth token、上传状态和下载 `.part` 文件都保存在 `data_dir`，进程重启后会自动恢复未完成传输。

## 首次配置

1. 在 Google Cloud Console 创建项目并启用 Google Drive API。
2. 配置 OAuth consent screen，把自己的 Google 账号加入测试用户。
3. 创建 OAuth Client，类型选择 **Desktop app**，下载 JSON 文件。
4. 复制 `config.example.json` 为 `config.local.json`，将 `google_client_config_file` 指向下载的 JSON；也可以直接填写 `google_client_id` 与 `google_client_secret`。
5. 启动服务后打开 `http://127.0.0.1:8787/api/oauth/google/start`，浏览器完成授权。

当前提交中 `google_client_id` 和 `google_client_secret` 都故意留空。若 `google_client_config_file` 也留空，服务会自动读取 `data_dir/oauth_client.json`。sidecar 每次授权都会创建一个随机端口的 `127.0.0.1` loopback 回调，这是 Desktop app 的推荐方式；不支持固定回调地址，也不需要把回调地址写死在代码里。OAuth 客户端 JSON 和 token 都是本地敏感文件，不应提交到 Git。

本地配置必须使用 **Desktop app** 类型的 OAuth Client，以支持动态 loopback 回调。

默认权限是 `https://www.googleapis.com/auth/drive.file`：sidecar 创建的 `Subtitles AI` 文件夹及其文件可被读写，不申请整个 Drive 的权限。

## 运行

从仓库根目录启动前端、Python API 和 sidecar：

```sh
./scripts/start.sh
```

只调试 sidecar 时，也可以在本目录单独运行：

```sh
cp config.example.json config.local.json
go run ./cmd/server -config ./config.local.json
```

服务默认只监听 `127.0.0.1:8787`。Python 服务默认地址为 `http://127.0.0.1:8000`；如果 Python 业务设置启用了 API Token，把该值填入 sidecar 的 `python_api_token`，导入操作会通过 `X-API-Token` 调用 `/api/tasks/upload`。Python 导入请求超时可由 `python_timeout_seconds` 配置（默认 1800 秒 / 30 分钟），与控制 Drive API 请求超时的 `request_timeout_seconds` 区分开。

## API

```text
GET    /healthz
GET    /api/oauth/status
GET    /api/oauth/google/start
POST   /api/oauth/google/disconnect
GET    /api/drive/files?parentId=&pageToken=&pageSize=  # parentId 可选，缺省为 sidecar 根目录
DELETE /api/drive/files/{fileId}                 # 移入 Drive 垃圾箱
GET    /api/drive/files/{fileId}/download        # 支持 Range/HEAD
POST   /api/drive/files/{fileId}/import          # 下载后提交 Python 流水线
POST   /api/drive/uploads                        # 创建浏览器断点上传
HEAD   /api/drive/uploads/{uploadId}              # 查询上传偏移
PATCH  /api/drive/uploads/{uploadId}              # 写入一段数据
DELETE /api/drive/uploads/{uploadId}              # 放弃未完成上传并清理暂存文件
GET    /api/drive/transfers
GET    /api/drive/transfers/{id}
POST   /api/drive/transfers/{id}/pause
POST   /api/drive/transfers/{id}/resume
POST   /api/drive/transfers/{id}/cancel
POST   /api/drive/folder-uploads                  # 创建文件夹 manifest 批次
GET    /api/drive/folder-uploads/{batchId}         # 查询批次和所有条目
POST   /api/drive/folder-uploads/{batchId}/entries/{entryId}/upload
POST   /api/drive/folder-uploads/{batchId}/entries/{entryId}/retry
POST   /api/drive/folder-uploads/{batchId}/retry
POST   /api/drive/folder-uploads/{batchId}/cancel
```

浏览器上传先发送 `X-Upload-Length`、`X-File-Name`、`X-File-Mime`：

```sh
curl -i -X POST http://127.0.0.1:8787/api/drive/uploads \
  -H 'X-Upload-Length: 1048576' -H 'X-File-Name: video.mp4' \
  -H 'X-File-Mime: video/mp4'
```

随后以 `PATCH` 搭配 `X-Upload-Offset` 上传任意大小的片段；服务端返回新的 `Upload-Offset`。客户端中断后先 `HEAD` 查询偏移再继续即可。文件收齐后，sidecar 使用 Google Drive resumable upload，默认 16 MiB（按 Drive 要求对齐到 256 KiB）的块，并将每块完成后的 offset 写入状态文件。

下载同样使用 `Range: bytes=<offset>-` 写入本地 `.part` 文件，支持重试、暂停、取消和进程重启恢复。下载完成后会校验 Drive 提供的 MD5（如果有），再把文件流式提交给 Python 的 `/api/tasks/upload`；sidecar 不在本地引入或复制 Python 业务代码。

### 文件夹批量上传

`POST /api/drive/folder-uploads` 接收以下形式的 manifest（也兼容直接传数组，以及 `files`/`path` 字段）：

```json
{
  "parentId": "optional-drive-folder-id",
  "entries": [
    {"relativePath": "season-1/episode-01.mp4", "size": 1048576, "mime": "video/mp4"}
  ]
}
```

路径必须是相对路径，不能包含 `..`、绝对路径、NUL 或控制字符；重复路径、文件夹 MIME、空文件和超过资源上限的 manifest 会被同步拒绝。`max_folder_entries`、`max_folder_bytes`、`max_manifest_bytes`、`max_folder_depth` 和 `max_folder_directories` 控制批次数量、总字节数、JSON 请求体、目录深度和目录总数；单个文件仍受 `max_upload_bytes` 限制。批次创建会按路径深度先创建父目录，再创建子目录。每个条目随后通过返回的 `uploadUrl` 使用原有 `HEAD`/`PATCH` 分片协议上传，上传完成后自动进入 Drive worker。`max_concurrent_transfers` 默认 3，限制同时运行的 Drive/Python worker 数量。

可用 `Idempotency-Key` 或 `X-Client-Request-ID` 重试批次创建；相同 key 会返回同一个批次和条目，不会重复创建 Drive 目录。状态接口返回批次聚合进度（`completed_entries`、`completed_bytes` 和 `state`）以及每个 entry 的上传/传输 ID。失败条目可通过 entry/batch `retry` 重试，整个批次可通过 `cancel` 取消。

Drive 文件夹不能下载或导入 Python 流水线；这两种请求会在创建响应体前同步返回 400。
