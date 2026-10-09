"""API 请求 / 响应模型。

字段用 camelCase，直接对齐前端契约（web/app.js 的 RealApi），
这样前端切真实后端时无需改字段。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from src.store import (
    DEFAULT_TRANSLATION_ENGINE_ID,
    RESOURCE_STATUS_AVAILABLE,
    RESOURCE_STATUS_MISSING,
    ProbeRecord,
    TaskRecord,
)


def _probe_record_to_out(rec: ProbeRecord) -> "ProbeRecordOut":
    """ProbeRecord(snake_case) -> ProbeRecordOut(camelCase)。"""
    return ProbeRecordOut(
        id=rec.id,
        url=rec.url,
        ok=bool(rec.ok),
        title=rec.title,
        extractor=rec.extractor,
        duration=rec.duration,
        formatsCount=rec.formats_count,
        webpageUrl=rec.webpage_url,
        reason=rec.reason,
        detail=rec.detail,
        createdAt=rec.created_at,
        language=rec.language,
        availableQualities=rec.available_qualities,
        formats=rec.parsed_formats,
        thumbnail=rec.thumbnail,
        uploader=rec.uploader,
    )


class TaskCreate(BaseModel):
    """POST /api/tasks 的请求体。"""

    url: str
    sourceLang: str = Field(default="en", min_length=1)
    targetLang: str = Field(default="zh-CN", min_length=1)
    mode: Literal["mono", "bilingual"] = "mono"
    burn: Literal["hard", "soft"] = "hard"
    model: str = Field(default="local:tiny", min_length=1)
    # 配置实例 ID；保留 deepseek 以兼容旧版环境变量配置。
    engine: str = Field(default=DEFAULT_TRANSLATION_ENGINE_ID, min_length=1)
    needSubtitle: bool = True  # False = 仅下载视频，跳过识别/翻译/烧录
    quality: Optional[str] = Field(default=None, description="下载画质策略：best/1080p/720p/480p/360p/audio_only")
    ttsEnabled: bool = False
    ttsVoice: str = "auto"
    originalVoiceMode: Literal["keep", "lower", "replace"] = "keep"

    @model_validator(mode="after")
    def validate_tts_options(self):
        if self.ttsEnabled and not self.needSubtitle:
            raise ValueError("配音需要启用字幕翻译")
        if self.ttsEnabled and not self.targetLang.lower().startswith(("zh", "en")):
            raise ValueError("Kokoro 配音目前支持中文和英文目标语")
        if self.ttsEnabled and self.ttsVoice not in {"auto", "zf_001", "zm_010", "af_maple", "af_sol"}:
            raise ValueError("不支持的 Kokoro 音色")
        if self.ttsEnabled and self.ttsVoice in {"zf_001", "zm_010"} and not self.targetLang.lower().startswith("zh"):
            raise ValueError("中文音色只能用于中文目标语")
        if self.ttsEnabled and self.ttsVoice in {"af_maple", "af_sol"} and not self.targetLang.lower().startswith("en"):
            raise ValueError("英文音色只能用于英文目标语")
        return self


class ErrorDetail(BaseModel):
    """机器可读错误详情，同时保留可直接展示的中文信息与操作建议。"""

    code: str
    message: str
    limits: Optional[dict[str, Any]] = None
    suggestion: Optional[str] = None


class TaskProbeIn(BaseModel):
    """POST /api/tasks/probe 的请求体。"""

    url: str = Field(min_length=1)
    cookiesFromBrowser: Optional[
        Literal[
            "chrome",
            "chromium",
            "edge",
            "firefox",
            "brave",
            "vivaldi",
            "opera",
            "safari",
        ]
    ] = None


class TaskProbeOut(BaseModel):
    """视频链接探针的响应体。"""

    ok: bool
    title: Optional[str] = None
    extractor: Optional[str] = None
    duration: Optional[float] = None
    formatsCount: int = 0
    webpageUrl: Optional[str] = None
    reason: Optional[str] = None
    detail: Optional[str] = None
    cached: bool = False
    language: Optional[str] = None
    availableQualities: list[str] = Field(default_factory=list)
    formats: list[dict[str, Any]] = Field(default_factory=list)
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None


class ProbeRecordOut(BaseModel):
    """单条下载测试历史记录（数据库行 -> 前端契约）。"""

    id: str
    url: str
    ok: bool
    title: Optional[str] = None
    extractor: Optional[str] = None
    duration: Optional[float] = None
    formatsCount: int = 0
    webpageUrl: Optional[str] = None
    reason: Optional[str] = None
    detail: Optional[str] = None
    createdAt: int
    language: Optional[str] = None
    availableQualities: list[str] = Field(default_factory=list)
    formats: list[dict[str, Any]] = Field(default_factory=list)
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None


class YtDlpEnvInfo(BaseModel):
    """yt-dlp 运行时环境与提取器诊断信息。"""

    version: Optional[str] = None
    extractorsCount: int = 0
    proxyConfigured: bool = False
    proxyMasked: Optional[str] = None
    cookiesConfigured: bool = False
    browserCookiesEnabled: bool = False
    cacheTtlSec: float = 0.0


class ProbeBatchSiteOut(BaseModel):
    """固定站点在当前启动批次中的状态。"""

    id: str
    name: str
    domain: str
    url: str
    status: Literal["pending", "testing", "ok", "fail"]
    source: Optional[Literal["startup", "manual"]] = None
    updatedAt: Optional[int] = None
    result: Optional[dict[str, Any]] = None


class ProbeBatchStatusOut(BaseModel):
    """固定十站点启动探测批次的实时状态。"""

    runId: Optional[str] = None
    state: Literal["idle", "disabled", "running", "completed"]
    total: int = 0
    completed: int = 0
    successful: int = 0
    failed: int = 0
    runStartedAt: Optional[int] = None
    updatedAt: Optional[int] = None
    sites: list[ProbeBatchSiteOut] = Field(default_factory=list)


class ProbeRecordsClearOut(BaseModel):
    """清空历史记录后的响应，便于前端 toast 显示删了多少条。"""

    deleted: int


class TaskOut(BaseModel):
    """任务对象的响应形态。"""

    id: str
    url: str
    title: Optional[str]
    sourceLang: str
    targetLang: str
    mode: str
    burn: str
    model: str
    engine: str
    sourceType: str
    taskOrigin: str = "web"
    needSubtitle: bool
    status: str
    progress: int
    currentStep: Optional[str]
    error: Optional[str]
    errorCode: Optional[str] = None
    outputs: Optional[dict]
    resourceStatus: str  # AVAILABLE | MISSING — 任务产物文件是否在盘
    downgradeReason: Optional[str] = None
    downgradeErrno: Optional[int] = None
    downgradedAt: Optional[int] = None
    quality: Optional[str] = None
    ttsEnabled: bool = False
    ttsVoice: str = "auto"
    originalVoiceMode: str = "keep"
    ttsStatus: str = "DISABLED"
    ttsError: Optional[str] = None
    createdAt: int
    updatedAt: int


def to_out(rec: TaskRecord) -> TaskOut:
    """TaskRecord(snake_case) -> TaskOut(camelCase)。

    outputs 暴露规则：
    - 任务不是 SUCCESS：None（流水线还在跑 / 失败了都不会给下载链接）
    - SUCCESS 但 resource_status == MISSING：None（产物已被清理，不再暴露失效链接）
    - SUCCESS + AVAILABLE：按 need_subtitle 拼出 video / subtitle 链接
    """
    need_subtitle = bool(rec.need_subtitle)
    resource_status = rec.resource_status or RESOURCE_STATUS_AVAILABLE
    outputs = None
    if rec.status == "SUCCESS" and resource_status == RESOURCE_STATUS_AVAILABLE:
        outputs = {"video": f"/api/tasks/{rec.id}/download"}
        if need_subtitle:
            outputs["subtitle"] = f"/api/tasks/{rec.id}/subtitle"
        if bool(getattr(rec, "tts_enabled", 0)) and getattr(rec, "tts_status", "") == "SUCCESS":
            outputs["dubbedVideo"] = f"/api/tasks/{rec.id}/dubbed"
    return TaskOut(
        id=rec.id,
        url=rec.url,
        title=rec.title,
        sourceLang=rec.source_lang,
        targetLang=rec.target_lang,
        mode=rec.mode,
        burn=rec.burn,
        model=rec.model,
        engine=rec.engine,
        sourceType=rec.source_type,
        taskOrigin=getattr(rec, "task_origin", "web"),
        needSubtitle=need_subtitle,
        status=rec.status,
        progress=rec.progress,
        currentStep=rec.current_step,
        error=rec.error,
        errorCode=rec.error_code,
        outputs=outputs,
        resourceStatus=resource_status,
        downgradeReason=rec.downgrade_reason,
        downgradeErrno=rec.downgrade_errno,
        downgradedAt=rec.downgraded_at,
        quality=getattr(rec, "quality", None),
        ttsEnabled=bool(getattr(rec, "tts_enabled", 0)),
        ttsVoice=getattr(rec, "tts_voice", "auto"),
        originalVoiceMode=getattr(rec, "original_voice_mode", "keep"),
        ttsStatus=getattr(rec, "tts_status", "DISABLED"),
        ttsError=getattr(rec, "tts_error", None),
        createdAt=rec.created_at,
        updatedAt=rec.updated_at,
    )


# 暴露给前端 / 文档的状态字面量，避免散落字符串
RESOURCE_STATUS_VALUES = (RESOURCE_STATUS_AVAILABLE, RESOURCE_STATUS_MISSING)
