#!/usr/bin/env bash

# TranslatedSubs 一键安装器（Ubuntu / Debian）。
#
# 脚本负责：安装系统依赖（含 Redis）、拉取仓库、写入机器本地引导配置、安装 Python/项目依赖、
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
START_SERVICE=1
# 云端试用只服务官网部署，默认不安装。--with-cloud-trial 或
# SUBTRANS_ENABLE_CLOUD_TRIAL=1 才会写入第二个 systemd 单元。
CLOUD_TRIAL_SERVICE="${SUBTRANS_ENABLE_CLOUD_TRIAL:-0}"
LAUNCHER_BIND_HOST="${SUBTRANS_LAUNCHER_HOST:-127.0.0.1}"
LAUNCHER_PORT="${SUBTRANS_LAUNCHER_PORT:-8001}"
LAUNCHER_DATA_DIR="${SUBTRANS_LAUNCHER_DATA_DIR:-${DEFAULT_DATA_ROOT}/launcher}"
LAUNCHER_ENV_FILE="${SUBTRANS_LAUNCHER_ENV_FILE:-/etc/${SERVICE_NAME}/launcher.env}"

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
  --no-start         安装并创建 systemd 服务，但暂不启动
  --with-cloud-trial 额外部署可选的 InstaBox 云端试用控制服务（仅官网部署需要）
  -h, --help         显示帮助

可用环境变量：
  SUBTRANS_INSTALL_DIR            安装目录，默认 /opt/subtitles-ai
  SUBTRANS_DATA_DIR               产物目录，默认 /var/lib/subtitles-ai/data
  SUBTRANS_DB                     SQLite 路径，默认 /var/lib/subtitles-ai/data/db/app.db
  SUBTRANS_API_HOST               监听地址，默认 0.0.0.0
  SUBTRANS_API_PORT               监听端口，默认 8000
  SUBTRANS_SERVICE_USER           systemd 用户，默认 subtitles-ai
  SUBTRANS_SERVICE_NAME           systemd 服务名，默认 subtitles-ai
  SUBTRANS_REPOSITORY_URL         Git 仓库地址
  SUBTRANS_INSTALL_REF            Git 分支或 Tag；Release 资产默认使用对应版本 Tag
  SUBTRANS_ENABLE_CLOUD_TRIAL     设为 1 等同于 --with-cloud-trial
  SUBTRANS_LAUNCHER_HOST          试用控制服务监听地址，默认 127.0.0.1
  SUBTRANS_LAUNCHER_PORT          试用控制服务端口，默认 8001
  SUBTRANS_LAUNCHER_DATA_DIR      SQLite 数据目录，默认 /var/lib/subtitles-ai/launcher
  SUBTRANS_LAUNCHER_ENV_FILE      密钥环境文件，默认 /etc/<服务名>/launcher.env

安装器不收集、不写入 InstaBox API Key 或 SUBTRANS_LAUNCHER_SECRET。
需要试用控制服务时，请先自行创建权限 0600、属主 root 的环境文件，
配置方法见 docs/instabox-quick-start.md。
EOF
}

while (($#)); do
  case "$1" in
    --no-start)
      START_SERVICE=0
      ;;
    --with-cloud-trial)
      CLOUD_TRIAL_SERVICE=1
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

if [[ "${CLOUD_TRIAL_SERVICE}" != "0" && "${CLOUD_TRIAL_SERVICE}" != "1" ]]; then
  die "SUBTRANS_ENABLE_CLOUD_TRIAL 只能是 0 或 1"
fi
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

if [[ "${CLOUD_TRIAL_SERVICE}" == "1" ]]; then
  [[ "${LAUNCHER_PORT}" =~ ^[0-9]+$ ]] || die "SUBTRANS_LAUNCHER_PORT 必须是数字"
  ((LAUNCHER_PORT >= 1 && LAUNCHER_PORT <= 65535)) || die "SUBTRANS_LAUNCHER_PORT 必须在 1–65535 之间"
  ((LAUNCHER_PORT != API_PORT)) || die "SUBTRANS_LAUNCHER_PORT 不能与 SUBTRANS_API_PORT 相同"
  [[ "${LAUNCHER_BIND_HOST}" =~ ^[A-Za-z0-9._:-]+$ ]] || die "SUBTRANS_LAUNCHER_HOST 包含无效字符"
  [[ "${LAUNCHER_DATA_DIR}" = /* ]] || die "SUBTRANS_LAUNCHER_DATA_DIR 必须是绝对路径"
  [[ "${LAUNCHER_DATA_DIR}" != *[[:space:]]* ]] || die "SUBTRANS_LAUNCHER_DATA_DIR 不能包含空格"
  [[ "${LAUNCHER_DATA_DIR}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "SUBTRANS_LAUNCHER_DATA_DIR 包含不支持的字符"
  assert_safe_managed_dir "${LAUNCHER_DATA_DIR}"
  [[ "${LAUNCHER_ENV_FILE}" = /* ]] || die "SUBTRANS_LAUNCHER_ENV_FILE 必须是绝对路径"
  [[ "${LAUNCHER_ENV_FILE}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "SUBTRANS_LAUNCHER_ENV_FILE 包含不支持的字符"
fi

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
  git \
  redis-server

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
if [[ "${CLOUD_TRIAL_SERVICE}" == "1" && ! -f "${PROJECT_DIR}/src/instabox_launcher/app.py" ]]; then
  die "安装目录缺少 src/instabox_launcher；请使用包含云端试用控制服务的版本"
fi

DATA_DIR="${SUBTRANS_DATA_DIR:-${DEFAULT_DATA_ROOT}/data}"
DB_PATH="${SUBTRANS_DB:-${DATA_DIR}/db/app.db}"
[[ "${DATA_DIR}" = /* ]] || die "SUBTRANS_DATA_DIR 必须是绝对路径"
[[ "${DB_PATH}" = /* ]] || die "SUBTRANS_DB 必须是绝对路径"
[[ "${DATA_DIR}" != *[[:space:]]* ]] || die "SUBTRANS_DATA_DIR 不能包含空格"
[[ "${DB_PATH}" != *[[:space:]]* ]] || die "SUBTRANS_DB 不能包含空格"
[[ "${DATA_DIR}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "SUBTRANS_DATA_DIR 包含不支持的字符"
[[ "${DB_PATH}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "SUBTRANS_DB 包含不支持的字符"
assert_safe_managed_dir "${DATA_DIR}"
assert_safe_managed_dir "$(dirname "${DB_PATH}")"

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

log "准备 Redis 服务和机器本地配置"
systemctl enable --now redis-server.service
if [[ ! -f "${PROJECT_DIR}/config.local.json" ]]; then
  cat > "${PROJECT_DIR}/config.local.json" <<EOF
{
  "redis": {"url": "redis://127.0.0.1:6379/0", "namespace": "translatedsubs-local"},
  "paths": {"dataDir": "${DATA_DIR}", "dbPath": "${DB_PATH}"},
  "http": {"host": "${API_BIND_HOST}", "port": ${API_PORT}}
}
EOF
fi
chmod 640 "${PROJECT_DIR}/config.local.json"

install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${DATA_DIR}" "$(dirname "${DB_PATH}")"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${PROJECT_DIR}"
if [[ -e "${DB_PATH}" ]]; then
  chown "${SERVICE_USER}:${SERVICE_USER}" "${DB_PATH}"
fi

UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
log "写入 systemd 服务 ${UNIT_FILE}"
cat > "${UNIT_FILE}" <<EOF
[Unit]
Description=TranslatedSubs FastAPI Service
After=network-online.target
Wants=network-online.target
After=redis-server.service
Wants=redis-server.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/.venv/bin/python -m src.cli
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

if [[ "${CLOUD_TRIAL_SERVICE}" == "1" ]]; then
  LAUNCHER_UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}-cloud-trial.service"
  log "写入云端试用控制服务 ${LAUNCHER_UNIT_FILE}"
  install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 "${LAUNCHER_DATA_DIR}"
  cat > "${LAUNCHER_UNIT_FILE}" <<EOF
[Unit]
Description=TranslatedSubs Optional InstaBox Trial Launcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/.venv/bin/python -m src.instabox_launcher --host ${LAUNCHER_BIND_HOST} --port ${LAUNCHER_PORT}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
Environment=PYTHONUNBUFFERED=1
Environment=SUBTRANS_LAUNCHER_DB_PATH=${LAUNCHER_DATA_DIR}/trials.sqlite3
EnvironmentFile=-${LAUNCHER_ENV_FILE}

[Install]
WantedBy=multi-user.target
EOF
  chmod 644 "${LAUNCHER_UNIT_FILE}"
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}-cloud-trial.service"
  if [[ -f "${LAUNCHER_ENV_FILE}" ]]; then
    log "云端试用控制服务使用环境文件 ${LAUNCHER_ENV_FILE}"
  else
    warn "未找到 ${LAUNCHER_ENV_FILE}；安装器不收集任何密钥。请按 docs/instabox-quick-start.md 创建该文件（权限 0600、属主 root）后执行：systemctl start ${SERVICE_NAME}-cloud-trial"
  fi
fi

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
log "本机引导配置：${PROJECT_DIR}/config.local.json"
log "业务模型/API 设置请在 Web 工作台或配置 API 中管理；保存后无需重启服务"

if [[ "${CLOUD_TRIAL_SERVICE}" == "1" ]]; then
  log "云端试用控制服务：systemctl status ${SERVICE_NAME}-cloud-trial --no-pager"
  log "云端试用控制服务日志：journalctl -u ${SERVICE_NAME}-cloud-trial -f"
  log "云端试用数据目录：${LAUNCHER_DATA_DIR}（SQLite 与锁文件，保持 0700）"
  warn "控制服务默认只监听 ${LAUNCHER_BIND_HOST}:${LAUNCHER_PORT}；公网必须经反向代理按同源 /api/trials 转发并启用 TLS"
  warn "官网宣传页需带 SUBTRANS_ENABLE_CLOUD_TRIAL 未设为 0 的构建（默认开启）；自托管宣传页用 npm --prefix site run build:no-trial 关闭试用入口"
fi
