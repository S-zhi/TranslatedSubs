"""独立本地 Whisper HTTP 服务协议测试。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from src import transcriber_server
from src.core.transcriber import TranscribeResponse


def test_transcriber_server_accepts_authenticated_multipart(monkeypatch):
    captured = {}

    class FakeService:
        def transcribe(self, request):
            assert request.audio_path.read_bytes() == b"audio"
            captured["request"] = request
            return TranscribeResponse(
                output={"segments": [{"text": "hello", "start": 0.0, "end": 1.0}]},
                language="en",
                language_probability=0.9,
                duration=1.0,
            )

    monkeypatch.setattr(transcriber_server, "settings", SimpleNamespace(
        transcriber_api_key="local-secret",
        local_whisper_model="small",
        local_whisper_device="cpu",
        local_whisper_compute_type="int8",
        local_whisper_download_root=None,
        local_whisper_beam_size=5,
    ))
    monkeypatch.setattr(transcriber_server, "_service", FakeService())

    with TestClient(transcriber_server.app) as client:
        denied = client.post("/transcribe", files={"audio": ("audio.wav", b"audio", "audio/wav")})
        assert denied.status_code == 401

        response = client.post(
            "/transcribe",
            headers={"Authorization": "Bearer local-secret"},
            files={"audio": ("audio.wav", b"audio", "audio/wav")},
            data={"task_id": "task-1", "language": "en", "model": "tiny"},
        )

    assert response.status_code == 200
    assert response.json()["segments"][0]["text"] == "hello"
    assert response.json()["language_probability"] == 0.9
    request = captured["request"]
    assert request.task_id == "task-1"
    assert request.language == "en"
    assert request.model_name == "tiny"
    assert not request.audio_path.exists()
