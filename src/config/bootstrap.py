"""Machine-local bootstrap options, separate from shared business config."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_BOOTSTRAP: dict[str, Any] = {
    "redis": {"url": "redis://127.0.0.1:6379/0", "namespace": "translatedsubs-local"},
    "paths": {"dataDir": "data", "dbPath": "data/db/app.db"},
    "mcp": {
        "baseUrl": "http://127.0.0.1:8000", "timeout": 15,
        "transport": "stdio", "host": "127.0.0.1", "port": 3001,
        "path": "/mcp", "logLevel": "INFO",
    },
    "http": {"host": "127.0.0.1", "port": 8000},
}


def load_bootstrap(path: Path | str | None = None) -> dict[str, Any]:
    source = Path(path) if path else Path(__file__).resolve().parents[2] / "config.local.json"
    result = json.loads(json.dumps(DEFAULT_BOOTSTRAP))
    try:
        supplied = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return result
    if not isinstance(supplied, dict):
        raise ValueError("config.local.json must contain a JSON object")
    for section in ("redis", "paths", "mcp", "http"):
        values = supplied.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(f"config.local.json {section} must be an object")
        result[section].update(values)
    return result
