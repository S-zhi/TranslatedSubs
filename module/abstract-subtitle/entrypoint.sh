#!/bin/sh
set -eu

mkdir -p "${WHISPER_MODEL_DIR:-${WHISPER_DOWNLOAD_ROOT:-/models}}"
chown -R whisper:whisper "${WHISPER_MODEL_DIR:-${WHISPER_DOWNLOAD_ROOT:-/models}}" 2>/dev/null || true
exec su -s /bin/sh whisper -c 'exec "$@"' -- "$@"
