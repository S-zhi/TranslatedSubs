#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"

if [[ ! -f "${ROOT_DIR}/pyproject.toml" ]]; then
  echo "错误：无法确认工作树根目录，未清理任何内容。" >&2
  exit 1
fi

if [[ -L "${VENV_DIR}" ]]; then
  echo "检测到 .venv 是符号链接，保留不动：${VENV_DIR}"
  exit 0
fi

if [[ -d "${VENV_DIR}" ]]; then
  rm -rf -- "${VENV_DIR}"
  echo "已清理此工作树的 Python 虚拟环境：${VENV_DIR}"
else
  echo "此工作树没有本地 .venv，无需清理。"
fi
