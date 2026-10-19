"""Environment-only configuration; independent of the media application's settings."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class LauncherConfig:
    api_key: str = field(default="", repr=False)
    secret: str = field(default="", repr=False)
    origin: str = ""
    source_revision: str = ""
    db_path: Path = Path("data/instabox-launcher/trials.sqlite3")
    template_id: str = "universal"
    snapshot_id: str = ""
    ttl_seconds: int = 1800
    max_active: int = 2
    hourly_budget: int = 6
    daily_budget: int = 12
    bootstrap_seconds: int = 1200
    sweep_seconds: float = 5.0
    retry_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.origin:
            parsed = urlsplit(self.origin)
            local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                    or (parsed.scheme == "http" and not local)
                    or parsed.username or parsed.password or parsed.query
                    or parsed.fragment or parsed.path not in {"", "/"}):
                raise ValueError("SUBTRANS_LAUNCHER_ORIGIN must be an HTTPS origin (HTTP loopback for development)")
            object.__setattr__(self, "origin", self.origin.rstrip("/"))
        if self.source_revision and not re.fullmatch(r"[0-9a-f]{40}", self.source_revision):
            raise ValueError("SUBTRANS_LAUNCHER_SOURCE_REVISION must be a full lowercase commit SHA")
        if self.secret and len(self.secret) < 32:
            raise ValueError("SUBTRANS_LAUNCHER_SECRET must have at least 32 characters")
        for name, value, low, high in (
            ("ttl_seconds", self.ttl_seconds, 300, 3600),
            ("max_active", self.max_active, 1, 5),
            ("hourly_budget", self.hourly_budget, 1, 100),
            ("daily_budget", self.daily_budget, 1, 500),
            ("bootstrap_seconds", self.bootstrap_seconds, 60, 1800),
        ):
            if not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
        object.__setattr__(self, "bootstrap_seconds", min(self.bootstrap_seconds, self.ttl_seconds))
        if self.sweep_seconds <= 0 or self.retry_seconds <= 0:
            raise ValueError("Worker intervals must be positive")
        for name, value in (("template", self.template_id), ("snapshot", self.snapshot_id)):
            if value and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
                raise ValueError(f"Invalid {name} identifier")
        object.__setattr__(self, "db_path", Path(self.db_path))

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.secret and self.origin and self.source_revision)

    @property
    def secure_cookie(self) -> bool:
        return self.origin.startswith("https://")

    @property
    def cookie_name(self) -> str:
        return "__Host-subtrans-trial" if self.secure_cookie else "subtrans-trial-local"

    def digest(self, purpose: str, value: str) -> str:
        return hmac.new(self.secret.encode(), f"{purpose}\0{value}".encode(), hashlib.sha256).hexdigest()

    @property
    def launcher_id(self) -> str:
        return self.digest("launcher", self.origin)[:24]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LauncherConfig":
        values = os.environ if env is None else env
        prefix = "SUBTRANS_LAUNCHER_"
        numbers = {
            "TTL_SECONDS": ("ttl_seconds", 1800),
            "MAX_ACTIVE": ("max_active", 2),
            "HOURLY_BUDGET": ("hourly_budget", 6),
            "DAILY_BUDGET": ("daily_budget", 12),
            "BOOTSTRAP_SECONDS": ("bootstrap_seconds", 1200),
        }
        kwargs = {}
        for suffix, (name, default) in numbers.items():
            try:
                kwargs[name] = int(values.get(prefix + suffix, str(default)))
            except ValueError as exc:
                raise ValueError(f"{prefix + suffix} must be an integer") from exc
        return cls(
            api_key=values.get("INSTABOX_API_KEY", "").strip(),
            secret=values.get(prefix + "SECRET", "").strip(),
            origin=values.get(prefix + "ORIGIN", "").strip(),
            source_revision=values.get(prefix + "SOURCE_REVISION", "").strip(),
            db_path=Path(values.get(prefix + "DB_PATH", "data/instabox-launcher/trials.sqlite3")),
            template_id=values.get(prefix + "TEMPLATE_ID", "universal"),
            snapshot_id=values.get(prefix + "SNAPSHOT_ID", ""),
            **kwargs,
        )
