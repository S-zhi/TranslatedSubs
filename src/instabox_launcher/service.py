"""Recoverable create → bootstrap → verify → publish → delete lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import LauncherConfig
from .provider import InstaBoxClient, ProviderError
from .store import TrialStore


logger = logging.getLogger(__name__)
MESSAGES = {
    "idle": "点击后为你准备独立的云端工作台。",
    "creating": "正在创建云机器。",
    "booting": "正在安装并启动工作台，首次启动可能需要几分钟。",
    "checking": "正在检查工作台和访问权限。",
    "publishing": "检查通过，正在准备访问入口。",
    "ready": "工作台已就绪。字幕处理前请配置模型和翻译引擎。",
    "deleting": "正在结束试用并删除云端数据。",
    "failed": "本次启动未完成，云机器正在回收。请稍后重试。",
    "expired": "试用已结束，云端数据将被删除。",
    "unavailable": "在线体验暂未配置，请使用自行部署入口。",
}


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


class TrialService:
    def __init__(self, config: LauncherConfig, store: TrialStore, provider: InstaBoxClient,
                 *, clock: Callable[[], float] = time.time):
        self.config = config
        self.store = store
        self.provider = provider
        self.clock = clock
        self.tasks: dict[str, asyncio.Task] = {}
        self.sweeper: asyncio.Task | None = None
        self.script = Path(__file__).with_name("bootstrap.sh").read_text()

    def reserve(self, owner: str) -> dict:
        def payload(trial_id: str) -> dict:
            source = ({"snapshotId": self.config.snapshot_id} if self.config.snapshot_id
                      else {"templateId": self.config.template_id})
            return {
                "name": f"translatedsubs-trial-{trial_id}", **source,
                "vault": False, "timeoutSeconds": self.config.ttl_seconds, "onTimeout": "kill",
                "metadata": {"app": "translatedsubs-trial", "launcher": self.config.launcher_id,
                             "trial": trial_id},
            }
        return self.store.reserve(
            owner, now=self.clock(), ttl=self.config.ttl_seconds, max_active=self.config.max_active,
            hourly=self.config.hourly_budget, daily=self.config.daily_budget,
            revision=self.config.source_revision, payload_factory=payload,
        )

    def public(self, trial: dict | None) -> dict:
        if not self.config.configured:
            status = "unavailable"
        elif trial is None:
            status = "idle"
        elif self.clock() >= trial["expires_at"]:
            status = "expired"
        elif trial["outcome"] == "expired":
            status = "expired"
        else:
            status = trial["phase"]
        url = None
        if (status == "ready" and trial and trial["holds_capacity"] and trial["port_url"]
                and not trial["outcome"]):
            token = self.config.digest("instance-token", trial["id"])
            url = f"{trial['port_url']}#trial_token={token}"
        return {"configured": self.config.configured, "status": status,
                "trialId": trial["id"] if trial else None,
                "expiresAt": _iso(trial["expires_at"]) if trial else None,
                "url": url, "message": MESSAGES.get(status, MESSAGES["failed"])}

    def end(self, trial: dict) -> dict:
        return self.store.request_cleanup(trial["id"], "expired", self.clock())

    def _save(self, trial_id: str, **values) -> dict:
        current = self.store.get(trial_id)
        if self.clock() >= current["expires_at"] and not current["outcome"]:
            current = self.store.request_cleanup(trial_id, "expired", self.clock())
        if current["outcome"] and values.get("phase") not in {None, "deleting"}:
            values["phase"] = "deleting"
            values["port_url"] = None
        return self.store.update(trial_id, now=self.clock(), **values)

    def _cleanup(self, trial: dict, outcome: str = "failed") -> dict:
        return self.store.request_cleanup(trial["id"], trial["outcome"] or outcome, self.clock())

    async def start(self) -> None:
        self.sweeper = asyncio.create_task(self._sweep(), name="instabox-trial-sweeper")

    async def close(self) -> None:
        if self.sweeper:
            self.sweeper.cancel()
        tasks = ([self.sweeper] if self.sweeper else []) + list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def _sweep(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                logger.error("Trial scheduler temporarily unavailable; reservations retained for retry")
            await asyncio.sleep(self.config.sweep_seconds)

    def tick(self) -> None:
        for trial in self.store.due(self.clock()):
            if self.clock() >= trial["expires_at"] and not trial["outcome"]:
                trial = self._cleanup(trial, "expired")
            task = self.tasks.get(trial["id"])
            if task is None or task.done():
                self.tasks[trial["id"]] = asyncio.create_task(self.advance(trial["id"]))
        for trial_id, task in list(self.tasks.items()):
            if task.done():
                self.tasks.pop(trial_id, None)

    async def advance(self, trial_id: str) -> None:
        trial = self.store.get(trial_id)
        if not trial or not trial["holds_capacity"]:
            return
        if self.clock() >= trial["expires_at"] and trial["outcome"] is None:
            trial = self._cleanup(trial, "expired")
        if (self.clock() > trial["created_at"] + self.config.bootstrap_seconds
                and trial["phase"] not in {"ready", "deleting"}):
            trial = self._cleanup(trial)
        try:
            phase = trial["phase"]
            if phase == "deleting":
                await self._delete(trial)
            elif phase == "creating":
                await self._create(trial)
            elif phase == "booting":
                await self._boot(trial)
            elif phase == "checking":
                await self._check(trial)
            elif phase == "publishing":
                url = await self.provider.publish(trial["machine_id"])
                self._save(trial_id, phase="ready", port_url=url)
        except ProviderError as exc:
            # No upstream response, command output, URL, or credential is logged.
            logger.warning("Trial %s provider event: %s", trial_id, exc.code)
            latest = self.store.get(trial_id)
            if phase == "creating" and not latest["machine_id"]:
                if exc.uncertain or latest["create_uncertain"]:
                    self._save(trial_id, create_uncertain=1, next_attempt=self.clock() + self.config.retry_seconds)
                else:
                    # Cancellation may have arrived during a definite rejection;
                    # that cannot leave a machine to find or delete.
                    self.store.finish(trial_id, latest["outcome"] or "failed", self.clock())
            elif latest["phase"] == "deleting":
                self._save(trial_id, next_attempt=self.clock() + self.config.retry_seconds)
            elif exc.uncertain:
                self._save(trial_id, next_attempt=self.clock() + self.config.retry_seconds)
            else:
                self._cleanup(latest)
        except Exception:
            logger.error("Trial %s encountered an internal lifecycle error; retaining reservation", trial_id)
            latest = self.store.get(trial_id)
            if latest["phase"] != "deleting":
                self._cleanup(latest)
            self._save(trial_id, next_attempt=self.clock() + self.config.retry_seconds)

    async def _create(self, trial: dict) -> None:
        trial_id = trial["id"]
        if not trial["machine_id"]:
            self._save(trial_id, create_attempted=1)
            operation = await self.provider.create(json.loads(trial["payload"]), trial["create_key"])
            trial = self._save(trial_id, operation_id=operation["id"], machine_id=operation["machineId"])
        else:
            operation = await self.provider.operation(trial["operation_id"]) if trial["operation_id"] else None
            if operation and operation["machineId"] != trial["machine_id"]:
                raise ProviderError("invalid_response")
            if operation is None:
                machine = await self.provider.machine(trial["machine_id"])
                if machine is None:
                    self.store.finish(trial_id, trial["outcome"] or "failed", self.clock())
                    return
                if machine.get("state") == "running":
                    self._save(trial_id, phase="booting")
                else:
                    self._save(trial_id, next_attempt=self.clock() + self.config.retry_seconds)
                return
        if operation["state"] == "failed":
            self._cleanup(self.store.get(trial_id))
        elif operation["state"] == "succeeded":
            self._save(trial_id, phase="booting", create_uncertain=0, next_attempt=0)
        else:
            self._save(trial_id, next_attempt=self.clock() + 1)

    async def _boot(self, trial: dict) -> None:
        directory = f"/data/home/translatedsubs-trial-{trial['id']}"
        # The command is safe to repeat after an unknown exec outcome: flock owns
        # one job, and the script/status belong only to this immutable trial.
        command = f"""set -euo pipefail
umask 077
mkdir -p '{directory}'
exec 8> '{directory}/dispatch.lock'
flock -w 5 8
if [[ -f '{directory}/status' ]]; then
    phase=$(< '{directory}/status')
    if [[ "$phase" == ready || "$phase" == failed ]]; then printf '%s\\n' "$phase"; exit 0; fi
    if [[ "$phase" == checking ]] && ! flock -n '{directory}/bootstrap.lock' true; then printf 'checking\\n'; exit 0; fi
fi
exec 9> '{directory}/bootstrap.lock'
if ! flock -n 9; then printf 'booting\\n'; exit 0; fi
cat > '{directory}/bootstrap.sh.tmp'
mv '{directory}/bootstrap.sh.tmp' '{directory}/bootstrap.sh'
# The child inherits the held job lock, but closes the dispatch lock.
nohup bash '{directory}/bootstrap.sh' 8>&- > '{directory}/bootstrap.log' 2>&1 < /dev/null &
printf 'booting\\n'
"""
        status = await self.provider.execute(
            trial["machine_id"], command, stdin=self.script, timeout=20,
            envs={"SUBTRANS_TRIAL_ID": trial["id"], "SUBTRANS_TRIAL_REVISION": trial["source_revision"],
                  "SUBTRANS_TRIAL_TOKEN": self.config.digest("instance-token", trial["id"])},
        )
        if status == "ready":
            self._save(trial["id"], phase="checking", next_attempt=0)
        elif status == "failed":
            self._cleanup(self.store.get(trial["id"]))
        elif status in {"booting", "checking"}:
            # The next call observes the lock/status before considering a restart.
            self._save(trial["id"], next_attempt=self.clock() + self.config.sweep_seconds)
        else:
            raise ProviderError("invalid_bootstrap_status")

    async def _check(self, trial: dict) -> None:
        # No credentials are included in command arguments, stdout or access URLs.
        command = """set -euo pipefail
umask 077
headers=$(mktemp)
trap 'rm -f "$headers"' EXIT
printf 'Authorization: Bearer %s\\n' "$SUBTRANS_TRIAL_TOKEN" > "$headers"
health=$(curl --disable --noproxy '*' -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/health)
challenge=$(curl --disable --noproxy '*' -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/settings/runtime)
authorized=$(curl --disable --noproxy '*' -sS --max-time 5 -o /dev/null -w '%{http_code}' -H @"$headers" http://127.0.0.1:8000/api/settings/runtime)
[[ "$health" == 200 && "$challenge" == 401 && "$authorized" == 200 ]]
printf 'verified\\n'
"""
        status = await self.provider.execute(
            trial["machine_id"], command, timeout=20,
            envs={"SUBTRANS_TRIAL_TOKEN": self.config.digest("instance-token", trial["id"])},
        )
        if status != "verified":
            raise ProviderError("readiness_failed")
        self._save(trial["id"], phase="publishing", next_attempt=0)

    async def _delete(self, trial: dict) -> None:
        if not trial["machine_id"]:
            if not trial["create_attempted"]:
                self.store.finish(trial["id"], trial["outcome"] or "expired", self.clock())
                return
            # A lost create response may still complete. Never release its slot
            # based solely on an empty list, nor issue a fresh create after expiry.
            machine = await self.provider.find_trial(self.config.launcher_id, trial["id"])
            if machine is None:
                self._save(trial["id"], next_attempt=self.clock() + self.config.retry_seconds)
                return
            trial = self._save(trial["id"], machine_id=machine["id"])
        if trial["delete_operation_id"]:
            operation = await self.provider.operation(trial["delete_operation_id"])
            if operation and operation["machineId"] != trial["machine_id"]:
                raise ProviderError("invalid_response")
            if operation and operation["state"] in {"queued", "running"}:
                self._save(trial["id"], next_attempt=self.clock() + 1)
                return
            machine = await self.provider.machine(trial["machine_id"])
            if machine is None or machine.get("state") == "deleted":
                self.store.finish(trial["id"], trial["outcome"] or "expired", self.clock())
                return
            if operation and operation["state"] == "failed":
                # A confirmed failed operation needs a new durable key; reusing
                # the old key would return that same failure indefinitely.
                self._save(trial["id"], delete_operation_id=None,
                           delete_key=f"trial-delete-{self.config.digest('delete-retry', trial['delete_key'])}",
                           next_attempt=self.clock() + self.config.retry_seconds)
                return
        operation = await self.provider.delete(trial["machine_id"], trial["delete_key"])
        if operation is None:
            self.store.finish(trial["id"], trial["outcome"] or "expired", self.clock())
        else:
            self._save(trial["id"], delete_operation_id=operation["id"],
                       next_attempt=self.clock() + 1)
