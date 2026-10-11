"""本地 Whisper 与通用 HTTP 转写适配器测试。"""
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from src.core import transcriber
from src.core.transcriber import HttpTranscriber, LocalWhisperTranscriber, TranscribeCancelledError, TranscribeError, TranscribeRequest, transcribe, _extract_segments, _parse_srt_segments


@pytest.fixture(autouse=True)
def isolate_task_output(monkeypatch, tmp_path):
    monkeypatch.setattr(transcriber, "ensure_task_dir", lambda _task_id: tmp_path)
    monkeypatch.setattr(transcriber, "_load_env", lambda: None)

def make_audio(tmp_path):
    path = tmp_path / "test.wav"; path.write_bytes(b"fake audio"); return path

def test_parse_and_extract_srt_formats(monkeypatch):
    standard = "1\n00:00:01,000 --> 00:00:02,500\nHello\n\n2\n00:00:03,000 --> 00:00:04,000\nWorld\n"
    compact = "1\n0:00:00.060 --> 0:00:01.000\nHello\n2\n0:00:01.000 --> 0:00:02.000\nWorld\n"
    assert len(_parse_srt_segments(standard)) == 2
    assert _parse_srt_segments(compact)[1]["text"] == "World"
    assert _extract_segments({"segments": [{"text": "a", "start": 0, "end": 1}]})[0]["text"] == "a"
    monkeypatch.setattr(transcriber.httpx, "get", lambda *_args, **_kwargs: SimpleNamespace(text=compact, raise_for_status=lambda: None))
    assert len(_extract_segments({"srt_file": "https://example.test/a.srt"})) == 2

@pytest.mark.parametrize("output", [{"segments": [{"text": "a"}]}, ["bad"]])
def test_extract_rejects_invalid_shape(output):
    with pytest.raises(TranscribeError, match="结构无法解析") as exc_info: _extract_segments(output)
    assert exc_info.value.code == "invalid_response"

def test_http_transcriber_local_file_and_metadata(monkeypatch, tmp_path):
    audio = make_audio(tmp_path); captured = {}
    class Response:
        def raise_for_status(self): return None
        def json(self): return {"language": "en", "language_probability": 0.98, "duration": 1.2, "segments": [{"text": "hello", "start": 0, "end": 1}]}
    monkeypatch.setattr(transcriber.httpx, "post", lambda url, **kwargs: (captured.update(url=url, kwargs=kwargs) or Response()))
    result = transcribe(audio, "t1", service=HttpTranscriber(url="https://stt.example.test", api_key="secret"), language="en", model_name="tiny")
    assert result.segment_count == 1 and result.language_probability == 0.98
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer secret"
    assert captured["kwargs"]["data"]["model"] == "tiny"

def test_http_transcriber_remote_url_and_timeout(monkeypatch):
    captured = {}
    class Response:
        def raise_for_status(self): return None
        def json(self): return {"segments": [{"text": "hello", "start": 0, "end": 1}]}
    monkeypatch.setattr(transcriber.httpx, "post", lambda url, **kwargs: (captured.update(url=url, kwargs=kwargs) or Response()))
    service = HttpTranscriber(url="https://stt.example.test")
    assert service.transcribe(TranscribeRequest("https://audio.test/a.wav", "t1")).output["segments"]
    assert captured["kwargs"]["json"]["audio_url"] == "https://audio.test/a.wav"
    monkeypatch.setattr(transcriber.httpx, "post", lambda *_args, **_kwargs: (_ for _ in ()).throw(httpx.ReadTimeout("timed out")))
    with pytest.raises(TranscribeError, match="超时") as exc_info: service.transcribe(TranscribeRequest("https://audio.test/a.wav", "t1"))
    assert exc_info.value.code == "network_error"

@pytest.mark.parametrize("status_code, expected", [(401, "unauthorized"), (429, "rate_limited"), (500, "upstream_error"), (422, "model_error")])
def test_http_transcriber_status_error_codes(monkeypatch, status_code, expected):
    request = httpx.Request("POST", "https://stt.example.test"); response = httpx.Response(status_code, request=request)
    def fail(*_args, **_kwargs): raise httpx.HTTPStatusError("failed", request=request, response=response)
    monkeypatch.setattr(transcriber.httpx, "post", fail)
    with pytest.raises(TranscribeError) as exc_info: HttpTranscriber(url="https://stt.example.test").transcribe(TranscribeRequest("https://audio.test/a.wav", "t1"))
    assert exc_info.value.code == expected

def test_local_faster_whisper_normalizes_segments(monkeypatch, tmp_path):
    audio = make_audio(tmp_path); captured = {}
    class Segment:
        def __init__(self, text, start, end): self.text, self.start, self.end = text, start, end
    class Info: language, language_probability, duration = "en", 0.9, 2.5
    class Model:
        def transcribe(self, path, **kwargs): captured.update(path=path, kwargs=kwargs); return iter([Segment(" hello ", 0, 1.25), Segment("", 1.25, 2)]), Info()
    fake = SimpleNamespace(WhisperModel=lambda model, **kwargs: (captured.update(model=model, init=kwargs) or Model()))
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", fake)
    model_dir = tmp_path / "tiny"; model_dir.mkdir()
    monkeypatch.setattr(transcriber.model_manager, "resolve_path", lambda _name: str(model_dir)); monkeypatch.setattr(transcriber.model_manager, "is_hf", lambda _name: False)
    events = []; result = LocalWhisperTranscriber(model_name="tiny", device="cuda", compute_type="float16", beam_size=2).transcribe(TranscribeRequest(audio, "t1", language="en"), on_progress=events.append)
    assert result.output["segments"] == [{"text": "hello", "start": 0.0, "end": 1.25}]
    assert captured["init"] == {"device": "cpu", "compute_type": "int8", "local_files_only": True}
    assert captured["kwargs"] == {"language": "en", "beam_size": 2, "vad_filter": True}; assert any(event.status == "processing" for event in events)

def test_local_huggingface_whisper_contract(monkeypatch, tmp_path):
    audio = make_audio(tmp_path); model_dir = tmp_path / "external"; model_dir.mkdir(); captured = {}
    class Processor:
        tokenizer = object(); feature_extractor = object()
        @classmethod
        def from_pretrained(cls, path, **kwargs): captured["processor"] = kwargs; return cls()
    class Model:
        @classmethod
        def from_pretrained(cls, path, **kwargs): captured["model"] = kwargs; return cls()
    def pipeline(_task, **kwargs):
        captured["pipeline"] = kwargs
        return lambda _path, **request: {"text": "hello", "chunks": [{"text": " hello ", "timestamp": (0.0, 1.0)}]}
    monkeypatch.setitem(__import__("sys").modules, "transformers", SimpleNamespace(WhisperProcessor=Processor, WhisperForConditionalGeneration=Model, pipeline=pipeline))
    monkeypatch.setattr(transcriber.model_manager, "resolve_path", lambda _name: str(model_dir)); monkeypatch.setattr(transcriber.model_manager, "is_hf", lambda _name: True)
    result = LocalWhisperTranscriber(model_name="external").transcribe(TranscribeRequest(audio, "t1", language="en"))
    assert result.output["segments"] == [{"text": "hello", "start": 0.0, "end": 1.0}]; assert captured["pipeline"]["device"] == "cpu"

def test_local_whisper_rejects_remote(tmp_path):
    with pytest.raises(TranscribeError, match="只支持本地") as exc_info: LocalWhisperTranscriber().transcribe(TranscribeRequest("https://example.test/a.wav", "t1"))
    assert exc_info.value.code == "invalid_input"

def test_custom_service_raw_segments_and_cancel(tmp_path):
    audio = make_audio(tmp_path)
    class Service:
        def transcribe(self, request, *, on_progress=None, cancel_check=None): assert request.model_name == "tiny"; return [{"text": "hello", "start": 0, "end": 1}]
    assert transcribe(audio, "t1", service=Service(), model_name="tiny").segment_count == 1
    def cancel(): raise TranscribeCancelledError("cancelled")
    with pytest.raises(TranscribeCancelledError): transcribe(audio, "t2", service=Service(), cancel_check=cancel)

def test_default_backend_and_missing_http_config(monkeypatch, tmp_path):
    monkeypatch.setattr(transcriber, "settings", SimpleNamespace(transcriber_backend="local_whisper", local_whisper_model="tiny", local_whisper_device="cpu", local_whisper_compute_type="int8", local_whisper_download_root=None, local_whisper_beam_size=5))
    assert isinstance(transcriber._build_transcriber_service(), LocalWhisperTranscriber)
    monkeypatch.setattr(transcriber, "settings", SimpleNamespace(transcriber_backend="http", transcriber_url=None))
    with pytest.raises(TranscribeError, match="未配置") as exc_info: transcribe(make_audio(tmp_path), "t1")
    assert exc_info.value.code == "missing_service_config"
