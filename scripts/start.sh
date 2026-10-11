#!/usr/bin/env bash

# 同时启动 Python 业务服务和 Google Drive sidecar。
# OAuth Client 与 Refresh Token 只从本地 config.local.json / drive-data 读取。

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRIVE_CONFIG="${DRIVE_CONFIG:-${ROOT_DIR}/drive-service/config.local.json}"
API_PORT="${API_PORT:-8000}"

if [[ "${DRIVE_CONFIG}" != /* ]]; then
  DRIVE_CONFIG="${ROOT_DIR}/${DRIVE_CONFIG}"
fi

if [[ ! -f "${DRIVE_CONFIG}" ]]; then
  echo "缺少 Google Drive 配置：${DRIVE_CONFIG}" >&2
  echo "请先执行：cp drive-service/config.example.json drive-service/config.local.json" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "找不到 uv，请先安装 https://docs.astral.sh/uv/" >&2
  exit 1
fi
if ! command -v go >/dev/null 2>&1; then
  echo "找不到 Go，请先安装 Go 1.23 或更高版本" >&2
  exit 1
fi

api_pid=""
drive_pid=""

get_descendant_pids() {
  local pid="$1"
  local children
  children="$(pgrep -P "${pid}" 2>/dev/null || true)"
  for child in ${children}; do
    get_descendant_pids "${child}"
    echo "${child}"
  done
}

stop_process() {
  local pid="$1"
  [[ -z "${pid}" ]] && return 0
  if kill -0 "${pid}" 2>/dev/null; then
    local pids_to_kill=()
    if command -v pgrep >/dev/null 2>&1; then
      # 先递归收集所有子孙进程 PID，防止父进程退出后子进程变为孤儿
      local descendants
      descendants="$(get_descendant_pids "${pid}")"
      if [[ -n "${descendants}" ]]; then
        pids_to_kill=(${descendants})
      fi
    fi
    pids_to_kill+=("${pid}")

    for p in "${pids_to_kill[@]}"; do
      kill "${p}" 2>/dev/null || true
    done

    # 给进程平滑退出的时间，超时后强制清理
    sleep 0.5
    for p in "${pids_to_kill[@]}"; do
      if kill -0 "${p}" 2>/dev/null; then
        kill -9 "${p}" 2>/dev/null || true
      fi
    done
  fi
}

cleanup() {
  stop_process "${api_pid}"
  stop_process "${drive_pid}"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "编译 Google Drive sidecar..."
(
  cd "${ROOT_DIR}/drive-service"
  go build -o ./bin/drive-server ./cmd/server
)

echo "TranslatedSubs API    → http://127.0.0.1:${API_PORT}"
echo "Google Drive sidecar  → ${DRIVE_CONFIG}"
echo "按 Ctrl-C 同时停止两个服务。"

(
  cd "${ROOT_DIR}/drive-service"
  exec ./bin/drive-server -config "${DRIVE_CONFIG}"
) &
drive_pid=$!

(
  cd "${ROOT_DIR}"
  exec uv run python -m uvicorn src.handler.app:app --reload --port "${API_PORT}"
) &
api_pid=$!

# macOS 默认 Bash 没有 wait -n；轮询两个已知 PID 可保持脚本兼容性。
while kill -0 "${api_pid}" 2>/dev/null && kill -0 "${drive_pid}" 2>/dev/null; do
  sleep 1
done

if ! kill -0 "${api_pid}" 2>/dev/null; then
  wait "${api_pid}" || true
else
  wait "${drive_pid}" || true
fi
