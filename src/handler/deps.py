"""路由共享依赖。"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, Query

from src.config import settings
from src.config.redis_store import ConfigUnavailable
from src.store import ProbeStore, TaskStore, TranslationEngineStore

# 存储单例（懒加载，便于测试用依赖覆盖替换）
_store: TaskStore | None = None
_probe_store: ProbeStore | None = None
_translation_engine_store: TranslationEngineStore | None = None


def get_store() -> TaskStore:
    global _store
    if _store is None:
        _store = TaskStore(settings.db_path)
    return _store


def get_probe_store() -> ProbeStore:
    global _probe_store
    if _probe_store is None:
        _probe_store = ProbeStore(settings.db_path)
    return _probe_store


def get_translation_engine_store() -> TranslationEngineStore:
    global _translation_engine_store
    if _translation_engine_store is None:
        # This store is backed by the aggregate Redis provider; db_path is a
        # compatibility constructor argument and must not trigger bootstrap
        # file access on normal request paths.
        _translation_engine_store = TranslationEngineStore()
        _translation_engine_store.ensure_local_ct2()
    return _translation_engine_store


def reset_singletons() -> None:
    """供测试在替换 monkeypatch 后重置单例。"""
    global _store, _probe_store, _translation_engine_store
    _store = None
    _probe_store = None
    _translation_engine_store = None


def require_api_token(
    authorization: Optional[str] = Header(None),
    x_api_token: Optional[str] = Header(None, alias="X-API-Token"),
    token: Optional[str] = Query(None),
    api_token: Optional[str] = Query(None, alias="api_token"),
) -> None:
    """校验 API Token（可选开启）。

    在 Redis 业务配置设置 api_token 时生效；支持 Authorization: Bearer <token>
    或 X-API-Token: <token> 头，以及 URL 查询参数 ?token=<token> 或 ?api_token=<token>。
    如果 Redis 中未设置 api_token，则不作拦截。
    """
    try:
        expected = settings.api_token
    except ConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail="Redis 配置存储不可用") from exc
    if not expected:
        return

    provided: Optional[str] = None
    if authorization:
        if authorization.startswith("Bearer "):
            provided = authorization[7:].strip()
        else:
            provided = authorization.strip()
    elif x_api_token:
        provided = x_api_token.strip()
    elif token:
        provided = token.strip()
    elif api_token:
        provided = api_token.strip()

    if not provided or provided != expected:
        raise HTTPException(
            status_code=401,
            detail="未提供有效的 API Token",
            headers={"WWW-Authenticate": "Bearer"},
        )
