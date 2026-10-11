#!/usr/bin/env bash

# TranslatedSubs 一键安装器（Ubuntu / Debian）。
#
# 交互模式：
#   sudo bash install.sh
#
# 非交互模式（适合 CI；注意避免把密钥写入 Shell 历史）：
#   sudo env SUBTRANS_DEEPSEEK_API_KEY=... \
#     bash install.sh --non-interactive
#
# 脚本负责：安装系统依赖、拉取仓库、交互写入密钥、安装 Python/项目依赖、
# 创建低权限 systemd 服务并启动健康检查。重复执行可用于更新依赖和服务配置。

set -Eeuo pipefail
umask 027

REPOSITORY_URL="${SUBTRANS_REPOSITORY_URL:-https://github.com/S-zhi/TranslatedSubs.git}"
DEFAULT_INSTALL_REF="main"
REPOSITORY_REF="${SUBTRANS_INSTALL_REF:-${DEFAULT_INSTALL_REF}}"
DEFAULT_INSTALL_DIR="/opt/subtitles-ai"
DEFAULT_DATA_ROOT="/var/lib/subtitles-ai"
SERVICE_NAME="${SUBTRANS_SERVICE_NAME:-subtitles-ai}"
SERVICE_USER="${SUBTRANS_SERVICE_USER:-subtitles-ai}"
API_BIND_HOST="${SUBTRANS_API_HOST:-0.0.0.0}"
API_PORT="${SUBTRANS_API_PORT:-8000}"
NON_INTERACTIVE=0
START_SERVICE=1

log() {
  printf '[TranslatedSubs] %s\n' "$*"
}

warn() {
  printf '[TranslatedSubs] 警告：%s\n' "$*" >&2
}

die() {
  printf '[TranslatedSubs] 错误：%s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
用法：sudo bash install.sh [选项]

选项：
  --non-interactive  不询问输入；密钥必须通过环境变量提供或已存在于 .env
  --no-start         安装并创建 systemd 服务，但暂不启动
  -h, --help         显示帮助

可用环境变量：
  SUBTRANS_DEEPSEEK_API_KEY       DeepSeek API Key
  SUBTRANS_INSTALL_DIR            安装目录，默认 /opt/subtitles-ai
  SUBTRANS_DATA_DIR               产物目录，默认 /var/lib/subtitles-ai/data
  SUBTRANS_DB                     SQLite 路径，默认 /var/lib/subtitles-ai/data/db/app.db
  SUBTRANS_API_HOST               监听地址，默认 0.0.0.0
  SUBTRANS_API_PORT               监听端口，默认 8000
  SUBTRANS_SERVICE_USER           systemd 用户，默认 subtitles-ai
  SUBTRANS_SERVICE_NAME           systemd 服务名，默认 subtitles-ai
  SUBTRANS_REPOSITORY_URL         Git 仓库地址
  SUBTRANS_INSTALL_REF            Git 分支或 Tag；Release 资产默认使用对应版本 Tag
EOF
}

while (($#)); do
  case "$1" in
    --non-interactive)
      NON_INTERACTIVE=1
      ;;
    --no-start)
      START_SERVICE=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "未知参数：$1"
      ;;
  esac
  shift
done

[[ "${EUID}" -eq 0 ]] || die "请使用 sudo 运行：sudo bash install.sh"
[[ "${API_PORT}" =~ ^[0-9]+$ ]] || die "SUBTRANS_API_PORT 必须是数字"
((API_PORT >= 1 && API_PORT <= 65535)) || die "SUBTRANS_API_PORT 必须在 1–65535 之间"
[[ "${SERVICE_USER}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || die "SUBTRANS_SERVICE_USER 不是有效的 Linux 用户名"
[[ "${SERVICE_NAME}" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "SUBTRANS_SERVICE_NAME 不是有效的 systemd 服务名"
[[ "${API_BIND_HOST}" =~ ^[A-Za-z0-9._:-]+$ ]] || die "SUBTRANS_API_HOST 包含无效字符"

assert_safe_recursive_target() {
  local path="$1"
  [[ "${path}" =~ ^/[^/]+/[^/]+(/.*)?$ ]] || die "拒绝递归修改过于宽泛的目录：${path}"
  case "${path}" in
    /opt|/var|/var/lib|/home|/usr|/etc|/root|/tmp|/srv)
      die "拒绝递归修改系统目录：${path}"
      ;;
  esac
}

assert_safe_managed_dir() {
  local path="$1"
  [[ "${path}" =~ ^/[^/]+/[^/]+/[^/]+(/.*)?$ ]] || die "数据目录层级过浅，拒绝修改其属主：${path}"
}

detect_project_dir() {
  local script_path candidate
  script_path="${BASH_SOURCE[0]:-}"
  if [[ -n "${script_path}" && -f "${script_path}" ]]; then
    candidate="$(cd "$(dirname "${script_path}")/.." 2>/dev/null && pwd || true)"
    if [[ -n "${candidate}" && -f "${candidate}/pyproject.toml" ]]; then
      printf '%s\n' "${candidate}"
      return
    fi
  fi
  printf '%s\n' "${SUBTRANS_INSTALL_DIR:-${DEFAULT_INSTALL_DIR}}"
}

PROJECT_DIR="$(detect_project_dir)"
[[ "${PROJECT_DIR}" = /* ]] || die "安装目录必须是绝对路径：${PROJECT_DIR}"
[[ "${PROJECT_DIR}" != *[[:space:]]* ]] || die "安装目录不能包含空格：${PROJECT_DIR}"
[[ "${PROJECT_DIR}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "安装目录包含不支持的字符：${PROJECT_DIR}"
assert_safe_recursive_target "${PROJECT_DIR}"

if [[ ! -f /etc/os-release ]]; then
  die "无法识别 Linux 发行版；当前脚本仅支持 Ubuntu / Debian"
fi

# shellcheck disable=SC1091
source /etc/os-release
DISTRO_FAMILY="${ID:-} ${ID_LIKE:-}"
if [[ "${DISTRO_FAMILY}" != *ubuntu* && "${DISTRO_FAMILY}" != *debian* ]]; then
  die "当前系统不是 Ubuntu / Debian：${PRETTY_NAME:-unknown}"
fi
[[ -d /run/systemd/system ]] || die "当前环境未运行 systemd；请在 Ubuntu/Debian 主机上执行安装器"

log "安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  ffmpeg \
  fontconfig \
  fonts-noto-cjk \
  git

if ! command -v uv >/dev/null 2>&1; then
  log "安装 uv 到 /usr/local/bin"
  UV_INSTALLER="$(mktemp /tmp/subtitles-ai-uv-installer.XXXXXX)"
  trap 'rm -f "${UV_INSTALLER:-}"' EXIT
  curl -LsSf https://astral.sh/uv/install.sh -o "${UV_INSTALLER}"
  UV_INSTALL_DIR=/usr/local/bin sh "${UV_INSTALLER}"
  rm -f "${UV_INSTALLER}"
  trap - EXIT
fi

if [[ ! -f "${PROJECT_DIR}/pyproject.toml" ]]; then
  if [[ -e "${PROJECT_DIR}" && -n "$(find "${PROJECT_DIR}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    die "安装目录已存在且不是 TranslatedSubs 仓库：${PROJECT_DIR}"
  fi
  log "克隆代码到 ${PROJECT_DIR}"
  mkdir -p "$(dirname "${PROJECT_DIR}")"
  git clone --branch "${REPOSITORY_REF}" --single-branch "${REPOSITORY_URL}" "${PROJECT_DIR}"
fi
[[ -f "${PROJECT_DIR}/src/handler/app.py" ]] || die "安装目录不是有效的 TranslatedSubs 仓库：${PROJECT_DIR}"

ENV_FILE="${PROJECT_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${PROJECT_DIR}/.env.example" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
fi

read_env_value() {
  local key="$1" line
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" == "${key}="* ]]; then
      printf '%s' "${line#*=}"
      return 0
    fi
  done < "${ENV_FILE}"
  return 0
}

prompt_secret() {
  local variable_name="$1" label="$2" supplied existing entered resolved
  supplied="${!variable_name-}"
  existing="$(read_env_value "${variable_name}")"
  resolved=""

  if [[ -n "${supplied}" ]]; then
    resolved="${supplied}"
  elif ((NON_INTERACTIVE)); then
    resolved="${existing}"
  else
    [[ -r /dev/tty ]] || die "当前没有交互终端；请使用环境变量注入密钥并添加 --non-interactive"
    if [[ -n "${existing}" ]]; then
      printf '%s（留空则保留现有值）：' "${label}" > /dev/tty
    else
      printf '%s：' "${label}" > /dev/tty
    fi
    IFS= read -r -s entered < /dev/tty
    printf '\n' > /dev/tty
    resolved="${entered:-${existing}}"
  fi

  [[ -n "${resolved}" ]] || die "缺少 ${variable_name}"
  [[ "${resolved}" != *$'\n'* && "${resolved}" != *$'\r'* ]] || die "${variable_name} 不能包含换行"
  printf -v "${variable_name}" '%s' "${resolved}"
}

update_env_key() {
  local key="$1" value="$2" line found=0 temporary
  temporary="$(mktemp "${PROJECT_DIR}/.env.tmp.XXXXXX")"
  chmod 600 "${temporary}"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" == "${key}="* ]]; then
      printf '%s=%s\n' "${key}" "${value}" >> "${temporary}"
      found=1
    else
      printf '%s\n' "${line}" >> "${temporary}"
    fi
  done < "${ENV_FILE}"
  if ((found == 0)); then
    printf '\n%s=%s\n' "${key}" "${value}" >> "${temporary}"
  fi
  mv "${temporary}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
}

prompt_secret SUBTRANS_DEEPSEEK_API_KEY "请输入 DeepSeek API Key（输入不会回显）"

EXISTING_DATA_DIR="$(read_env_value SUBTRANS_DATA_DIR)"
EXISTING_DB_PATH="$(read_env_value SUBTRANS_DB)"
if [[ -n "${SUBTRANS_DATA_DIR:-}" ]]; then
  DATA_DIR="${SUBTRANS_DATA_DIR}"
elif [[ "${EXISTING_DATA_DIR}" = /* ]]; then
  DATA_DIR="${EXISTING_DATA_DIR}"
else
  DATA_DIR="${DEFAULT_DATA_ROOT}/data"
fi
if [[ -n "${SUBTRANS_DB:-}" ]]; then
  DB_PATH="${SUBTRANS_DB}"
elif [[ "${EXISTING_DB_PATH}" = /* ]]; then
  DB_PATH="${EXISTING_DB_PATH}"
else
  DB_PATH="${DATA_DIR}/db/app.db"
fi
[[ "${DATA_DIR}" = /* ]] || die "SUBTRANS_DATA_DIR 必须是绝对路径"
[[ "${DB_PATH}" = /* ]] || die "SUBTRANS_DB 必须是绝对路径"
[[ "${DATA_DIR}" != *[[:space:]]* ]] || die "SUBTRANS_DATA_DIR 不能包含空格"
[[ "${DB_PATH}" != *[[:space:]]* ]] || die "SUBTRANS_DB 不能包含空格"
[[ "${DATA_DIR}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "SUBTRANS_DATA_DIR 包含不支持的字符"
[[ "${DB_PATH}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "SUBTRANS_DB 包含不支持的字符"
assert_safe_managed_dir "${DATA_DIR}"
assert_safe_managed_dir "$(dirname "${DB_PATH}")"

update_env_key SUBTRANS_DEEPSEEK_API_KEY "${SUBTRANS_DEEPSEEK_API_KEY}"
update_env_key SUBTRANS_DATA_DIR "${DATA_DIR}"
update_env_key SUBTRANS_DB "${DB_PATH}"
unset SUBTRANS_DEEPSEEK_API_KEY

log "安装 Python 3.12 和锁定依赖"
PYTHON_INSTALL_DIR="${PROJECT_DIR}/.uv-python"
UV_PYTHON_INSTALL_DIR="${PYTHON_INSTALL_DIR}" uv python install 3.12 --no-bin
PYTHON_BIN="$(UV_PYTHON_INSTALL_DIR="${PYTHON_INSTALL_DIR}" uv python find 3.12)"
uv sync \
  --directory "${PROJECT_DIR}" \
  --python "${PYTHON_BIN}" \
  --frozen \
  --no-dev \
  --compile-bytecode

if ! getent group "${SERVICE_USER}" >/dev/null 2>&1; then
  log "创建系统用户组 ${SERVICE_USER}"
  groupadd --system "${SERVICE_USER}"
fi

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  log "创建系统用户 ${SERVICE_USER}"
  useradd --system --gid "${SERVICE_USER}" --home-dir "${PROJECT_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${DATA_DIR}" "$(dirname "${DB_PATH}")"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${PROJECT_DIR}"
if [[ -e "${DB_PATH}" ]]; then
  chown "${SERVICE_USER}:${SERVICE_USER}" "${DB_PATH}"
fi
chmod 600 "${ENV_FILE}"

UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
log "写入 systemd 服务 ${UNIT_FILE}"
cat > "${UNIT_FILE}" <<EOF
[Unit]
Description=TranslatedSubs FastAPI Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/.venv/bin/uvicorn src.handler.app:app --host ${API_BIND_HOST} --port ${API_PORT}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0027
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "${UNIT_FILE}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

if ((START_SERVICE)); then
  log "启动 ${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}.service"

  HEALTH_HOST="${API_BIND_HOST}"
  if [[ "${HEALTH_HOST}" == "0.0.0.0" || "${HEALTH_HOST}" == "::" ]]; then
    HEALTH_HOST="127.0.0.1"
  elif [[ "${HEALTH_HOST}" == *:* ]]; then
    HEALTH_HOST="[${HEALTH_HOST}]"
  fi
  HEALTH_OK=0
  for _ in {1..20}; do
    if curl -fsS "http://${HEALTH_HOST}:${API_PORT}/api/health" >/dev/null 2>&1; then
      HEALTH_OK=1
      break
    fi
    sleep 1
  done

  if ((HEALTH_OK)); then
    log "安装完成，健康检查通过"
  else
    systemctl status "${SERVICE_NAME}.service" --no-pager || true
    die "服务未通过健康检查；查看日志：journalctl -u ${SERVICE_NAME} -n 200 --no-pager"
  fi
else
  log "安装完成，服务尚未启动。运行：systemctl start ${SERVICE_NAME}"
fi

if ffmpeg -hide_banner -filters 2>/dev/null | grep -q ' subtitles '; then
  log "FFmpeg 硬字幕滤镜可用"
else
  warn "FFmpeg 缺少 subtitles/libass 滤镜，请使用软字幕或安装带 libass 的 FFmpeg"
fi

if [[ "${API_BIND_HOST}" == "0.0.0.0" ]]; then
  log "Web 地址：http://<服务器IP>:${API_PORT}"
  warn "请只向可信来源开放 TCP ${API_PORT}；公网部署建议使用 Nginx/Caddy 的 80/443 反向代理"
else
  log "本机地址：http://127.0.0.1:${API_PORT}"
fi
log "服务状态：systemctl status ${SERVICE_NAME} --no-pager"
log "实时日志：journalctl -u ${SERVICE_NAME} -f"
