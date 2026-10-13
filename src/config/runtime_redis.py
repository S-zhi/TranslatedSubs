"""Compatibility facade for runtime settings on the aggregate config provider."""
from __future__ import annotations

from typing import Any

from .config_repository import (
    get_config_provider,
    start_config_subscription,
    stop_config_subscription,
)


def get_runtime_value(key: str) -> Any:
    return get_config_provider().read().values["business"].get(key)


def get_runtime_document() -> tuple[bool, dict[str, Any] | None]:
    return True, dict(get_config_provider().read().values["business"])


def update_runtime_values(values: dict[str, Any]) -> None:
    def apply(document: dict[str, Any]) -> dict[str, Any]:
        business = document["business"]
        for key, value in values.items():
            if value is None:
                business.pop(key, None)
            else:
                business[key] = value
        return document
    get_config_provider().mutate(apply)


def start_runtime_subscription() -> bool:
    return start_config_subscription()


def stop_runtime_subscription() -> None:
    stop_config_subscription()


def reset_runtime_store() -> None:
    """Legacy test hook; provider injection is managed by config_repository."""
    return None
