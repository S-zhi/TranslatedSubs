#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

if [[ ! -f "${ROOT_DIR}/pyproject.toml" || ! -f "${ROOT_DIR}/uv.lock" ]]; then
  echo "错误：工作树中缺少 pyproject.toml 或 uv.lock。" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "错误：找不到 uv。请先安装 uv：https://docs.astral.sh/uv/" >&2
  exit 127
fi

echo "正在为此工作树同步 Python 3.11 依赖……"
cd "${ROOT_DIR}"
UV_PROJECT_ENVIRONMENT="${ROOT_DIR}/.venv" uv sync --locked --python 3.11
echo "工作树环境已就绪：${ROOT_DIR}/.venv"
