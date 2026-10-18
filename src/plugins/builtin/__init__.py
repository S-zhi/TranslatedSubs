"""Built-in pipeline plugin factories."""

from src.plugins.builtin.audio_ffmpeg import AudioFFmpegFactory
from src.plugins.builtin.downloader_ytdlp import YtDlpFactory
from src.plugins.builtin.subtitle_ffmpeg import FFmpegRenderFactory
from src.plugins.builtin.transcriber_whisper import WhisperFactory
from src.plugins.builtin.translator_engine import TranslatorEngineFactory
from src.plugins.builtin.tts_kokoro import KokoroFactory
from src.plugins.builtin.vocal_demucs import DemucsFactory


def factories():
    """Return lightweight factories; sessions load runtime capabilities on demand."""
    from src.plugins.builtin.downloader_cobalt import CobaltFactory

    return (
        YtDlpFactory(),
        AudioFFmpegFactory(),
        DemucsFactory(),
        WhisperFactory(),
        TranslatorEngineFactory(),
        KokoroFactory(),
        FFmpegRenderFactory(),
        CobaltFactory(),
    )


__all__ = ["factories"]
