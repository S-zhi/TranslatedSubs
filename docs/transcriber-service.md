# Transcriber service protocol

The transcription stage uses the local `faster-whisper` backend by default. A compatible custom HTTP service is also supported.

## Configuration

Configure the transcriber backend, service URL, timeout, and API credential in the Web Settings page or business configuration API. These business settings are stored in Redis and apply to new requests without restarting the service. The transcriber API credential is write-only; reads expose only whether it is configured.

`local_whisper` runs the OpenAI open-source Whisper model locally through `faster-whisper`. `http`, `custom`, and `custom_http` select the custom HTTP adapter.

For the directly embedded local backend:

Choose `local_whisper` in Settings to use the local backend. Select or download a ready model in Model Store (Local).

Use **Model Settings → Whisper** to manage local models. Download an official faster-whisper model or import a separately obtained Hugging Face model. Official downloads explicitly fetch the selected model and check local CPU loading before marking it ready. For Hugging Face models, install the optional inference dependencies with `uv sync --extra hf-models`, then import the files. Import requires Whisper `config.json`, processor/tokenizer files, and either `pytorch_model.bin` or `model.safetensors` (shards of one format are supported with their index); the app checks the files and runs a short offline inference. Failed imports clean up the managed temporary copy, leaving the original files untouched. Downloads and imports only add inventory. To activate one, explicitly save it as the current Whisper card. Enabled unified settings take priority over a task's requested model; until unified settings are enabled, tasks continue using the requested model. Each created task stores its effective model, so later settings changes do not alter queued work. Deletion is blocked while a model is current or used by an active task. The `POST /api/srt/local-models/{name}/check` endpoint rechecks an imported model and removes an invalid managed copy.

Local model inference always runs on CPU. Existing CTranslate2 models use `int8` compute type; Hugging Face models use the CPU pipeline. The legacy device and compute type settings do not change this behavior. A model selected for an individual task overrides the default model setting.

The same implementation is available as a standalone HTTP service:

```bash
uv run uvicorn src.transcriber_server:app --host 0.0.0.0 --port 8010
```

For a standalone deployment, give the service and business API access to the same Redis configuration namespace. Save the credential through the business settings API and select the HTTP backend with URL `http://127.0.0.1:8010/transcribe`. The standalone service requires this Bearer key before accepting audio or fetching an `audio_url`.

## Request

The adapter sends one `POST` request to the configured transcriber service URL.

For a remote audio input, the request is JSON:

```json
{
  "audio_url": "https://example.com/audio.wav",
  "task_id": "task-123",
  "language": "en",
  "model": "small"
}
```

For a local audio input, the request is `multipart/form-data`. The audio is in the `audio` part and the same `task_id`, `language`, and `model` fields are sent as form fields. `language` can be omitted or null for automatic detection. The `Authorization: Bearer ...` header is sent when the credential is configured in business settings.

## Response

Return HTTP 2xx with a JSON object containing either timestamped `segments` or an `srt` string:

```json
{
  "language": "en",
  "language_probability": 0.99,
  "duration": 12.5,
  "segments": [
    {"text": "Hello", "start": 0.0, "end": 1.2}
  ]
}
```

Each segment must contain non-empty `text`, numeric `start`, and numeric `end`, with `end > start`. The application validates the response and writes the normalized result to `data/{task_id}/original.srt`. HTTP 401/403, 429, 5xx, and other 4xx responses are mapped to the same stable error codes used by the local and HTTP adapters.

For in-process integrations, implement `TranscriberService.transcribe()` and return a `TranscribeResponse` (or a raw output accepted by `_extract_segments`). The request object is `TranscribeRequest` and includes `audio_path`, `task_id`, `language`, and `model_name`.
