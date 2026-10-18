"""Resolve configured plugin instances once before task runtime validation."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from src.config.plugin_schema import default_plugin_section
from src.config.redis_store import ConfigSnapshot
from src.plugin_system.errors import PluginError
from src.service.plugin_snapshot import PURPOSE_STAGES, validate_instance_plan


@dataclass(frozen=True)
class SelectedPlugins:
    instances: dict[str, dict[str, Any]]
    plugin_defaults: dict[str, dict[str, str]]
    config_snapshot: ConfigSnapshot
    request_model: str
    engine: str
    tts_voice: str
    revision: int


def prepare_instance_selection(
    config_snapshot: ConfigSnapshot,
    selection: dict[str, str] | None,
    *,
    request_model: str,
    engine: str,
    tts_voice: str,
    source_type: str = "url",
    need_subtitle: bool = True,
    tts_enabled: bool = False,
    original_voice_mode: str = "keep",
    manager=None,
) -> SelectedPlugins:
    from src.plugins import get_plugin_manager

    manager = manager or get_plugin_manager()
    values = copy.deepcopy(config_snapshot.values)
    section = values.get("plugins") or default_plugin_section()
    if selection is not None and not isinstance(selection, dict):
        raise ValueError("pluginInstances 必须是阶段到实例 ID 的对象")
    selected_ids = dict(section["defaults"])
    for stage, instance_id in (selection or {}).items():
        if stage not in selected_ids or not isinstance(instance_id, str) or not instance_id:
            raise ValueError("插件实例选择包含非法阶段或 ID")
        selected_ids[stage] = instance_id
    needed = {"download"} if source_type == "url" else set()
    if need_subtitle:
        needed.update({"audio_extract", "transcribe", "translate", "render"})
        if values["business"].get("vocal_separation_enabled"):
            needed.add("vocal_separate")
        if tts_enabled:
            needed.add("tts")
            if original_voice_mode != "keep":
                needed.add("vocal_separate")
    instances = {}
    identities = {}
    catalog = {item["plugin_id"]: item for item in manager.catalog()}
    for stage, instance_id in selected_ids.items():
        if stage not in needed:
            instance_id = section["defaults"][stage]
        instance = section["byId"].get(instance_id)
        if instance is None or instance.get("stage") != stage or not instance.get("enabled"):
            raise ValueError(f"插件实例不存在、已停用或阶段不匹配: {stage}")
        instances[stage] = copy.deepcopy(instance)
        identities[stage] = {key: instance[key] for key in ("plugin_id", "implementation_version")}
        if stage not in needed:
            continue
        implementation = catalog.get(instance["plugin_id"])
        if implementation is None:
            raise PluginError("plugin_unavailable", stage, instance["plugin_id"], "所选插件未注册")
        if implementation["stage"] != stage or implementation["implementation_version"] != instance["implementation_version"]:
            raise PluginError("version_mismatch", stage, instance["plugin_id"], "所选插件实现版本不可用")
        manager.validate_config(instance["plugin_id"], instance["implementation_version"], instance["config"])
        model_ref = instance.get("model_ref")
        if model_ref:
            kind = model_ref["kind"]
            block = values["models"].get(kind, {})
            card = next((item for item in block.get("models", []) if item.get("id") == model_ref["card_id"]), None)
            if card is None or not card.get("enabled", True):
                raise ValueError(f"插件绑定的模型不存在或已停用: {stage}")
            # The override is local to this immutable creation snapshot. The
            # persisted aggregate's current card is never changed by a task.
            pinned = copy.deepcopy(card)
            pinned["isCurrent"] = True
            values["models"][kind] = {**copy.deepcopy(block), **pinned,
                                      "_runtimeManaged": True, "enabled": True, "models": [pinned]}
            if kind == "whisper":
                request_model = str(pinned.get("model") or "")
            elif kind == "llm":
                engine = "deepseek"
            elif kind == "tts":
                tts_voice = str(pinned.get("voice") or tts_voice)
        if instance.get("engine_ref"):
            engine = instance["engine_ref"]["id"]
            # An explicit engine binding must bypass the compatibility rule
            # that otherwise lets the current LLM card replace 'deepseek'.
            values["models"].setdefault("llm", {})["_runtimeManaged"] = False
        if stage == "transcribe":
            config = instance["config"]
            if "backend" in config:
                backend = "http" if config["backend"] in {"http", "custom", "custom_http"} else "local_whisper"
                values["business"]["transcriber_backend"] = backend
                if backend == "http":
                    request_model = request_model.removeprefix("local:")
                    # A local active card must not force a chosen HTTP backend.
                    if not model_ref:
                        values["models"].setdefault("whisper", {})["_runtimeManaged"] = False
            if "service_url" in config:
                values["business"]["transcriber_url"] = config["service_url"]
            if "timeout_seconds" in config:
                values["business"]["transcriber_timeout"] = config["timeout_seconds"]
    pinned_snapshot = ConfigSnapshot(config_snapshot.schema, config_snapshot.revision, values,
                                     config_snapshot.updated_at, config_snapshot.updated_by)
    return SelectedPlugins(instances, identities, pinned_snapshot, request_model, engine,
                           tts_voice, config_snapshot.revision)


def build_instance_plan(
    plan_json: str, selected: SelectedPlugins, *, manager=None, runtime_snapshot: dict | None = None,
) -> str:
    from src.plugins import get_plugin_manager
    from src.service.plugin_plan import parse_plugin_plan

    manager = manager or get_plugin_manager()
    plan = parse_plugin_plan(plan_json)
    slots = {}
    for purpose, old_slot in plan["slots"].items():
        stage = PURPOSE_STAGES[purpose]
        instance = selected.instances[stage]
        config = dict(old_slot["config"])
        config.update(instance["config"])
        if stage == "transcribe":
            if str(config.get("backend")) in {"http", "custom", "custom_http"}:
                config["backend"] = "http"
                config["model"] = str(config.get("model") or selected.request_model).removeprefix("local:")
            elif instance["plugin_id"] == "builtin.whisper":
                config["backend"] = "local_whisper"
                if config.get("service_url") is not None:
                    raise ValueError("本地 Whisper 不能配置 HTTP 服务地址")
        model_ref = instance.get("model_ref")
        if model_ref is None and not instance.get("engine_ref") and runtime_snapshot:
            route_name = {"translate": "engine", "transcribe": "whisper", "tts": "tts"}.get(purpose)
            route = runtime_snapshot.get(route_name, {}) if route_name else {}
            if route.get("cardId"):
                kind = {"translate": "llm", "transcribe": "whisper", "tts": "tts"}[purpose]
                model_ref = {"kind": kind, "card_id": route["cardId"]}
        if model_ref and purpose in {"transcribe", "translate"}:
            if purpose == "translate":
                config["credential_ref"] = f"model_card:{model_ref['kind']}:{model_ref['card_id']}:api_key"
            elif config.get("backend") == "http":
                block = selected.config_snapshot.values["models"].get(model_ref["kind"], {})
                card = next((item for item in block.get("models", []) if item.get("id") == model_ref["card_id"]), {})
                config["credential_ref"] = (
                    f"model_card:{model_ref['kind']}:{model_ref['card_id']}:api_key"
                    if card.get("apiKey") or card.get("api_key") else None
                )
        config = manager.validate_config(instance["plugin_id"], instance["implementation_version"],
                                         config, purpose=purpose, partial=True)
        slots[purpose] = {
            "plugin_id": instance["plugin_id"],
            "implementation_version": instance["implementation_version"],
            "instance_id": instance["id"],
            "config_schema_version": instance["config_schema_version"],
            "config": config,
            "model_ref": copy.deepcopy(model_ref),
            "engine_ref": copy.deepcopy(instance.get("engine_ref")),
        }
    value = validate_instance_plan({"schema_version": 2, "config_revision": selected.revision, "slots": slots})
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def resolve_model_credential(ref: str, config_snapshot: ConfigSnapshot) -> str:
    """Read a rotated secret by the card ID frozen into a task, never by current."""
    parts = ref.split(":")
    if len(parts) != 4 or parts[0] != "model_card" or parts[1] not in {"llm", "whisper", "tts"} or parts[3] != "api_key":
        raise ValueError("不支持的模型凭据引用")
    kind, card_id = parts[1:3]
    block = config_snapshot.values.get("models", {}).get(kind, {})
    card = next((item for item in block.get("models", []) if item.get("id") == card_id), None)
    if card is None or not card.get("enabled", True):
        raise ValueError("任务绑定的模型卡已删除或停用")
    secret = card.get("apiKey") or card.get("api_key")
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("任务绑定的模型卡缺少凭据")
    return secret
