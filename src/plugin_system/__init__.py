"""Versioned contracts for pipeline plugins."""

from src.plugin_system.context import FakeContext, PluginContext
from src.plugin_system.contracts import (
    AudioExtractRequest,
    AudioExtractResult,
    DownloadRequest,
    DownloadResult,
    DubMuxRequest,
    DubMuxResult,
    JSONScalar,
    OperationId,
    PluginFactory,
    PluginRequest,
    PluginSession,
    ProbeFormat,
    ProbeRequest,
    ProbeResult,
    Purpose,
    Request,
    Result,
    StageId,
    SubtitleRenderRequest,
    SubtitleRenderResult,
    TTSRequest,
    TTSResult,
    TranscribeRequest,
    TranscribeResult,
    TranslateRequest,
    TranslateResult,
    VocalSeparateRequest,
    VocalSeparateResult,
)
from src.plugin_system.errors import PluginCancelled, PluginError
from src.plugin_system.manifest import PluginManifest, validate_registration

__all__ = [
    "AudioExtractRequest", "AudioExtractResult", "DownloadRequest", "DownloadResult",
    "DubMuxRequest", "DubMuxResult", "FakeContext", "JSONScalar", "OperationId",
    "PluginCancelled", "PluginContext", "PluginError", "PluginFactory",
    "PluginManifest",
    "PluginRequest", "PluginSession", "ProbeFormat", "ProbeRequest", "ProbeResult",
    "Purpose", "Request",
    "Result", "StageId", "SubtitleRenderRequest", "SubtitleRenderResult", "TTSRequest",
    "TTSResult", "TranscribeRequest", "TranscribeResult", "TranslateRequest", "TranslateResult",
    "VocalSeparateRequest", "VocalSeparateResult", "validate_registration",
]
