"""Authenticated plugin catalog, instance CRUD, and stage-default settings."""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import RootModel

from src.handler.deps import get_store, require_api_token
from src.plugin_system.errors import PluginError
from src.plugins import get_plugin_manager
from src.store.plugin_instance_store import PluginInstanceConflict, PluginInstanceStore
from src.store.task_store import TaskStore

router = APIRouter(
    prefix="/api/settings/plugins",
    tags=["plugin-settings"],
    dependencies=[Depends(require_api_token)],
)
_TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILED", "CANCELLED"})


class PluginSettingsPayload(RootModel[dict[str, Any]]):
    """Accept fields declared by the selected plugin factory."""


def _store() -> PluginInstanceStore:
    return PluginInstanceStore(manager=get_plugin_manager())


def _expected_revision(if_match: str = Header(alias="If-Match")) -> int:
    if re.fullmatch(r"[0-9]+", if_match) is None:
        raise HTTPException(status_code=422, detail="If-Match 必须是非负整数 revision")
    try:
        return int(if_match)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="If-Match revision 超出支持范围") from exc


def _validated(operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except PluginError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="插件实例不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _read_view(store: PluginInstanceStore) -> tuple[Any, PluginInstanceStore]:
    snapshot = store._read()
    return snapshot, PluginInstanceStore(snapshot=snapshot)


def _reject_task_reference(task_store: TaskStore, instance_id: str) -> None:
    for task in task_store.list():
        if task.status in _TERMINAL_STATUSES or not task.plugin_plan_json:
            continue
        try:
            plan = json.loads(task.plugin_plan_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] != 2:
            continue
        slots = plan.get("slots")
        if isinstance(slots, dict) and any(
            isinstance(slot, dict) and slot.get("instance_id") == instance_id
            for slot in slots.values()
        ):
            raise PluginInstanceConflict("仍有未结束任务引用此插件实例，暂时无法停用或删除")


@router.get("/catalog")
def get_plugin_catalog() -> dict[str, Any]:
    return {"plugins": get_plugin_manager().catalog()}


@router.get("/instances")
def list_plugin_instances() -> dict[str, Any]:
    snapshot, view = _read_view(_store())
    return _validated(lambda: {
        "revision": snapshot.revision,
        "instances": view.list(),
        "defaults": view.defaults(),
    })


@router.get("/instances/{instance_id}")
def get_plugin_instance(instance_id: str) -> dict[str, Any]:
    snapshot, view = _read_view(_store())
    instance = _validated(lambda: view.get(instance_id))
    if instance is None:
        raise HTTPException(status_code=404, detail="插件实例不存在")
    return {"revision": snapshot.revision, "instance": instance}


@router.post("/instances", status_code=201)
def create_plugin_instance(
    body: PluginSettingsPayload,
    revision: int = Depends(_expected_revision),
) -> dict[str, Any]:
    instance, snapshot = _validated(lambda: _store().create(
        body.root, expected_revision=revision,
    ))
    return {"revision": snapshot.revision, "instance": instance}


@router.put("/instances/{instance_id}")
def update_plugin_instance(
    instance_id: str,
    body: PluginSettingsPayload,
    revision: int = Depends(_expected_revision),
    task_store: TaskStore = Depends(get_store),
) -> dict[str, Any]:
    if body.root.get("enabled") is False:
        _reject_task_reference(task_store, instance_id)
    instance, snapshot = _validated(lambda: _store().update(
        instance_id, body.root, expected_revision=revision,
    ))
    return {"revision": snapshot.revision, "instance": instance}


@router.delete("/instances/{instance_id}")
def delete_plugin_instance(
    instance_id: str,
    revision: int = Depends(_expected_revision),
    task_store: TaskStore = Depends(get_store),
) -> dict[str, Any]:
    _reject_task_reference(task_store, instance_id)
    snapshot = _validated(lambda: _store().delete(instance_id, expected_revision=revision))
    return {"revision": snapshot.revision, "ok": True}


@router.get("/defaults")
def get_plugin_defaults() -> dict[str, Any]:
    snapshot, view = _read_view(_store())
    return _validated(lambda: {"revision": snapshot.revision, "defaults": view.defaults()})


@router.put("/defaults")
def update_plugin_defaults(
    body: PluginSettingsPayload,
    revision: int = Depends(_expected_revision),
) -> dict[str, Any]:
    if set(body.root) != {"defaults"}:
        raise HTTPException(status_code=422, detail="请求必须仅包含 defaults 对象")
    snapshot = _validated(lambda: _store().set_defaults(
        body.root["defaults"], expected_revision=revision,
    ))
    defaults = PluginInstanceStore(snapshot=snapshot).defaults()
    return {"revision": snapshot.revision, "defaults": defaults}
