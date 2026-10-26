#!/usr/bin/env bash
# Private VM entrypoint. The launcher uploads this file and owns bootstrap.lock.
set +x
set -Eeuo pipefail
umask 077
# Remote exec output is a status channel, never a build log or a secret channel.
exec >/dev/null 2>&1

trial_id=${SUBTRANS_TRIAL_ID:-}
revision=${SUBTRANS_TRIAL_REVISION:-}
trial_token=${SUBTRANS_TRIAL_TOKEN:-}
workspace=${SUBTRANS_TRIAL_WORKSPACE:-/data/home}
unset SUBTRANS_TRIAL_TOKEN

[[ "$trial_id" =~ ^[0-9a-f]{32}$ ]] || exit 1
[[ "$workspace" = /* && -d "$workspace" && -w "$workspace" ]] || exit 1
trial_dir="$workspace/translatedsubs-trial-$trial_id"
[[ ! -L "$trial_dir" ]] || exit 1
mkdir -p "$trial_dir"
chmod 700 "$trial_dir"

write_status() {
  local temporary
  temporary=$(mktemp "$trial_dir/.status.XXXXXX")
  printf '%s\n' "$1" > "$temporary"
  chmod 600 "$temporary"
  mv -f "$temporary" "$trial_dir/status"
}

compose_ready=false
header_file="$trial_dir/auth-header"
finish() {
  local result=$?
  trap - EXIT
  if ((result != 0)); then
    write_status failed || true
    if [[ "$compose_ready" == true ]]; then
      "${compose[@]}" stop --timeout 10 app || true
    fi
  fi
  rm -f "$header_file"
  unset trial_token SUBTRANS_TRIAL_TOKEN
  exit "$result"
}
trap finish EXIT
trap 'exit 1' HUP INT TERM
write_status booting

log_file="$trial_dir/bootstrap.log"
[[ ! -L "$log_file" ]] || exit 1
: >> "$log_file"
chmod 600 "$log_file"
exec >>"$log_file" 2>&1

[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || exit 1
[[ "$trial_token" =~ ^[0-9a-f]{64}$ ]] || exit 1
for tool in git docker curl flock mktemp; do
  command -v "$tool" >/dev/null 2>&1 || exit 1
done
docker info >/dev/null
docker compose version >/dev/null

repo_dir="$trial_dir/repo"
project="translatedsubs-trial-$trial_id"
revision_file="$trial_dir/source-revision"
[[ ! -L "$revision_file" && ! -L "$repo_dir" ]] || exit 1
if [[ -e "$revision_file" ]]; then
  [[ "$(<"$revision_file")" == "$revision" ]] || exit 1
fi

# A standalone Compose file avoids inheriting snapshot credentials, global
# container/volume names, .env settings, or a second host port from a merge.
# Relative bind paths are resolved from this file's private trial directory.
compose_file="$trial_dir/compose.trial.yaml"
[[ ! -L "$compose_file" && ! -L "$trial_dir/http-entrypoint.py" ]] || exit 1
cat > "$compose_file" <<'COMPOSE'
services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - translatedsubs-redis:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 12
  app:
    build: ./repo
    restart: unless-stopped
    depends_on:
      redis:
        condition: service_healthy
    command: ["/opt/venv/bin/python", "/app/http-entrypoint.py"]
    ports:
      - "8000:8000"
    volumes:
      - translatedsubs-data:/data
      - ./repo/config.local.json:/app/config.local.json:ro
      - ./http-entrypoint.py:/app/http-entrypoint.py:ro
volumes:
  translatedsubs-data:
  translatedsubs-redis:
COMPOSE
chmod 600 "$compose_file"
compose=(docker compose --project-name "$project" --project-directory "$trial_dir" --env-file /dev/null --file "$compose_file")
compose_ready=true
# Reruns stop the old HTTP process before repairing its checkout or credentials.
"${compose[@]}" stop --timeout 10 app

# The only source remote is the official repository, and the only ref is the
# launcher's immutable SHA. Clean ignored files as well as ordinary leftovers.
(
  umask 022
  mkdir -p "$repo_dir"
  chmod 755 "$repo_dir"
  git -C "$repo_dir" init --quiet
  git -C "$repo_dir" fetch --no-tags --depth 1 https://github.com/S-zhi/TranslatedSubs.git "$revision"
  git -C "$repo_dir" checkout --detach --force FETCH_HEAD
  actual_revision=$(git -C "$repo_dir" rev-parse HEAD)
  if [[ "$actual_revision" != "$revision" ]]; then
    exit 1
  fi
  git -C "$repo_dir" clean -ffdx
)

# Old revisions can start successfully yet have no secure browser handoff or
# explicit initialization API. Such a trial must never be reported as ready.
git -C "$repo_dir" grep --quiet consumeTrialTokenFragment HEAD -- web/js/api-auth.js
git -C "$repo_dir" grep --quiet consumeTrialTokenFragment HEAD -- web/js/main.js
git -C "$repo_dir" grep --quiet 'trial_token' HEAD -- web/config.js
git -C "$repo_dir" grep --quiet 'def initialize_config_document' HEAD -- src/config/config_repository.py
git -C "$repo_dir" grep --quiet startup_probe_enabled HEAD -- src/config/schema.py
[[ -f "$repo_dir/Dockerfile" && -f "$repo_dir/compose.yaml" ]] || exit 1
printf '%s\n' "$revision" > "$revision_file"
chmod 600 "$revision_file"

# The image runs as uid 10001. This world-readable file contains paths only.
printf '{"redis":{"url":"redis://redis:6379/0","namespace":"%s"},"paths":{"dataDir":"/data","dbPath":"/data/db/app.db"},"http":{"host":"0.0.0.0","port":8000}}\n' "$project" > "$repo_dir/config.local.json"
chmod 644 "$repo_dir/config.local.json"

# Keep request audit records, but remove credentials from query strings before
# Uvicorn's access handler formats them. This wrapper contains no credentials.
cat > "$trial_dir/http-entrypoint.py" <<'HTTP_ENTRYPOINT'
from copy import deepcopy
import logging
from urllib.parse import unquote_plus

import uvicorn
from uvicorn.config import LOGGING_CONFIG


class RedactAccessTokens(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            arguments = list(record.args)
            path, separator, query = str(arguments[2]).partition("?")
            if separator:
                parts = []
                for part in query.split("&"):
                    key, equals, value = part.partition("=")
                    if equals and unquote_plus(key).lower() in {
                        "token", "api_token", "trial_token", "trialtoken",
                    }:
                        part = key + "=[REDACTED]"
                    parts.append(part)
                arguments[2] = path + "?" + "&".join(parts)
                record.args = tuple(arguments)
        return True


if __name__ == "__main__":
    log_config = deepcopy(LOGGING_CONFIG)
    log_config.setdefault("filters", {})["redact_trial_tokens"] = {
        "()": RedactAccessTokens,
    }
    log_config["handlers"]["access"].setdefault("filters", []).append(
        "redact_trial_tokens",
    )
    uvicorn.run(
        "src.handler.app:app", host="0.0.0.0", port=8000,
        access_log=True, log_config=log_config,
    )
HTTP_ENTRYPOINT
chmod 644 "$trial_dir/http-entrypoint.py"

"${compose[@]}" up -d redis
"${compose[@]}" build app
redis_ready=false
for ((attempt = 0; attempt < 30; attempt++)); do
  if "${compose[@]}" exec -T redis redis-cli ping >/dev/null 2>&1; then
    redis_ready=true
    break
  fi
  sleep 2
done
[[ "$redis_ready" == true ]] || exit 1

# RedisConfigProvider.mutate() reads an existing document. On a fresh volume,
# explicit initialization MUST precede the mutation and the first HTTP start.
SUBTRANS_TRIAL_TOKEN="$trial_token" "${compose[@]}" run --rm --no-deps -T -e SUBTRANS_TRIAL_TOKEN app /opt/venv/bin/python -c '
import os
from src.config.config_repository import get_config_provider, initialize_config_document

initialize_config_document()

def configure_trial(document):
    document["business"].update({
        "api_token": os.environ["SUBTRANS_TRIAL_TOKEN"],
        "startup_probe_enabled": False,
    })
    return document

get_config_provider().mutate(configure_trial)
'
# curl reads the header from a private file, never from a process argument.
[[ ! -L "$header_file" ]] || exit 1
printf 'Authorization: Bearer %s\n' "$trial_token" > "$header_file"
chmod 600 "$header_file"
unset trial_token
"${compose[@]}" up -d app
write_status checking

http_status() {
  curl --disable --silent --show-error --noproxy '*' --connect-timeout 2 --max-time 5 --output /dev/null --write-out '%{http_code}' "$@"
}

service_url=http://127.0.0.1:8000
health_ready=false
for ((attempt = 0; attempt < 90; attempt++)); do
  code=$(http_status "$service_url/api/health") || code=""
  if [[ "$code" == 200 ]]; then
    health_ready=true
    break
  fi
  sleep 2
done
[[ "$health_ready" == true ]] || exit 1
code=$(http_status "$service_url/api/settings/runtime") || exit 1
[[ "$code" == 401 ]] || exit 1
code=$(http_status --header "@$header_file" "$service_url/api/settings/runtime") || exit 1
[[ "$code" == 200 ]] || exit 1
write_status ready
