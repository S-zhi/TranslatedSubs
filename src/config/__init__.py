"""配置层：全局设置与存储路径策略。"""

from .config import get_runtime_settings, settings, settings_scope, update_runtime_settings
from .storage import (
    ArtifactRef,
    ArtifactStore,
    artifacts_present,
    artifact_store,
    artifact_name,
    ensure_task_dir,
    task_dir,
    SOURCE_VIDEO_STEM,
    AUDIO_FILENAME,
    ORIGINAL_SRT,
    TRANSLATED_SRT,
    OUTPUT_VIDEO,
    OUTPUT_VIDEO_HARD,
    OUTPUT_VIDEO_SOFT,
    OUTPUT_VIDEO_NAMES,
    output_video_filename,
)

__all__ = [
    "settings",
    "settings_scope",
    "ArtifactRef",
    "ArtifactStore",
    "artifact_store",
    "artifact_name",
    "get_runtime_settings",
    "update_runtime_settings",
    "artifacts_present",
    "ensure_task_dir",
    "task_dir",
    "SOURCE_VIDEO_STEM",
    "AUDIO_FILENAME",
    "ORIGINAL_SRT",
    "TRANSLATED_SRT",
    "OUTPUT_VIDEO",
    "OUTPUT_VIDEO_HARD",
    "OUTPUT_VIDEO_SOFT",
    "OUTPUT_VIDEO_NAMES",
    "output_video_filename",
]
