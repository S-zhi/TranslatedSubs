"""Strongly typed contracts shared by pipeline plugins and their host."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Protocol, TypeAlias, runtime_checkable


StageId: TypeAlias = Literal[
    "download",
    "audio_extract",
    "vocal_separate",
    "transcribe",
    "translate",
    "tts",
    "render",
]
OperationId: TypeAlias = Literal[
    "download",
    "audio_extract",
    "vocal_separate",
    "transcribe",
    "translate",
    "tts",
    "subtitle",
    "dub_mux",
]
Purpose: TypeAlias = Literal[
    "download",
    "extract.asr",
    "extract.dubbing",
    "separate.asr",
    "separate.dubbing",
    "transcribe",
    "translate",
    "tts",
    "render.subtitle",
    "render.dub_mux",
]
JSONScalar: TypeAlias = str | int | float | bool | None
PluginConfig: TypeAlias = Mapping[str, JSONScalar]


@dataclass(frozen=True)
class PluginRequest:
    """Common task invocation identity; callbacks live on PluginContext."""

    stage: StageId
    operation: OperationId
    purpose: Purpose


@dataclass(frozen=True)
class DownloadRequest(PluginRequest):
    stage: Literal["download"]
    operation: Literal["download"]
    purpose: Literal["download"]
    url: str
    quality: str
    target_dir: Path
    source_stem: str = "source"


@dataclass(frozen=True)
class AudioExtractRequest(PluginRequest):
    stage: Literal["audio_extract"]
    operation: Literal["audio_extract"]
    purpose: Literal["extract.asr", "extract.dubbing"]
    source_path: Path
    output_path: Path
    sample_rate: int
    channels: int


@dataclass(frozen=True)
class VocalSeparateRequest(PluginRequest):
    stage: Literal["vocal_separate"]
    operation: Literal["vocal_separate"]
    purpose: Literal["separate.asr", "separate.dubbing"]
    audio_path: Path
    vocal_path: Path | None
    background_path: Path | None


@dataclass(frozen=True)
class TranscribeRequest(PluginRequest):
    stage: Literal["transcribe"]
    operation: Literal["transcribe"]
    purpose: Literal["transcribe"]
    audio_path: Path
    output_srt: Path
    source_lang: str
    backend: str
    model: str


@dataclass(frozen=True)
class TranslateRequest(PluginRequest):
    stage: Literal["translate"]
    operation: Literal["translate"]
    purpose: Literal["translate"]
    original_srt: Path
    output_srt: Path
    source_lang: str
    target_lang: str
    mode: Literal["mono", "bilingual"]


@dataclass(frozen=True)
class TTSRequest(PluginRequest):
    stage: Literal["tts"]
    operation: Literal["tts"]
    purpose: Literal["tts"]
    translated_srt: Path
    original_srt: Path
    output_audio: Path
    target_lang: str
    voice: str
    bilingual: bool
    duration_seconds: float | None


@dataclass(frozen=True)
class SubtitleRenderRequest(PluginRequest):
    stage: Literal["render"]
    operation: Literal["subtitle"]
    purpose: Literal["render.subtitle"]
    source_video: Path
    translated_srt: Path
    output_video: Path
    mode: Literal["hard", "soft"]


@dataclass(frozen=True)
class DubMuxRequest(PluginRequest):
    stage: Literal["render"]
    operation: Literal["dub_mux"]
    purpose: Literal["render.dub_mux"]
    base_video: Path
    source_audio: Path
    speech_audio: Path
    background_audio: Path | None
    vocal_audio: Path | None
    mixed_audio: Path
    output_video: Path
    original_voice_mode: Literal["keep", "lower", "replace"]


@dataclass(frozen=True)
class DownloadResult:
    source_path: Path
    title: str | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class AudioExtractResult:
    audio_path: Path
    sample_rate: int
    channels: int
    duration_seconds: float | None = None


@dataclass(frozen=True)
class VocalSeparateResult:
    vocal_path: Path | None = None
    background_path: Path | None = None


@dataclass(frozen=True)
class TranscribeResult:
    original_srt: Path
    segments: int | None = None


@dataclass(frozen=True)
class TranslateResult:
    translated_srt: Path
    segments: int
    bilingual: bool


@dataclass(frozen=True)
class TTSResult:
    speech_audio: Path
    duration_seconds: float | None = None


@dataclass(frozen=True)
class SubtitleRenderResult:
    video_path: Path


@dataclass(frozen=True)
class DubMuxResult:
    mixed_audio: Path
    video_path: Path


@dataclass(frozen=True)
class ProbeRequest:
    url: str
    force_refresh: bool = False
    cookies_from_browser: str | None = None


@dataclass(frozen=True)
class ProbeFormat:
    formatId: str
    ext: str
    resolution: str
    height: int | None
    width: int | None
    fps: float | None
    vcodec: str | None
    acodec: str | None
    filesize: int | None
    tbr: float | None
    note: str | None
    type: Literal["video", "audio"]


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    title: str | None = None
    extractor: str | None = None
    duration: float | None = None
    formats_count: int = 0
    webpage_url: str | None = None
    reason: str | None = None
    detail: str | None = None
    cached: bool = False
    language: str | None = None
    available_qualities: tuple[str, ...] = ()
    formats: tuple[ProbeFormat, ...] = ()
    thumbnail: str | None = None
    uploader: str | None = None


Request: TypeAlias = (
    DownloadRequest
    | AudioExtractRequest
    | VocalSeparateRequest
    | TranscribeRequest
    | TranslateRequest
    | TTSRequest
    | SubtitleRenderRequest
    | DubMuxRequest
)
Result: TypeAlias = (
    DownloadResult
    | AudioExtractResult
    | VocalSeparateResult
    | TranscribeResult
    | TranslateResult
    | TTSResult
    | SubtitleRenderResult
    | DubMuxResult
)


@runtime_checkable
class PluginSession(Protocol):
    def validate(self, request: Request) -> None: ...

    def run(self, request: Request) -> Result: ...

    def close(self) -> None: ...


@runtime_checkable
class PluginFactory(Protocol):
    stage: StageId

    def create_session(
        self, config: PluginConfig, context: "PluginContext"
    ) -> PluginSession: ...
