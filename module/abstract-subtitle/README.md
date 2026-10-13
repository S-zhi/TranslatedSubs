# Abstract Subtitle Whisper Service

独立的 OpenAI Whisper 本地字幕提取服务，与主应用通过 HTTP 对接。

```bash
docker build -t abstract-subtitle-whisper .
docker run --rm -p 8010:8010 -v whisper-models:/models \
  -e ABSTRACT_SUBTITLE_API_KEY=change-me \
  -e WHISPER_MODEL=small abstract-subtitle-whisper
```

生产环境必须设置 `ABSTRACT_SUBTITLE_API_KEY`，并只允许可信调用方访问该端口。镜像默认要求 API Key；本地开发如需关闭要求，可设置 `ABSTRACT_SUBTITLE_REQUIRE_API_KEY=0`。自定义微调 checkpoint 应放在 `/models` 下，例如：

```bash
docker run --rm -p 8010:8010 -v "$PWD/models:/models" \
  -e ABSTRACT_SUBTITLE_API_KEY=change-me \
  -e WHISPER_MODEL=/models/my-finetuned.pt abstract-subtitle-whisper
```

发送 `multipart/form-data`：

```bash
curl -H 'Authorization: Bearer change-me' \
  -F audio=@sample.mp3 -F model=medium -F language=en \
  -F task=transcribe http://127.0.0.1:8010/transcribe
```

`model` 支持 `tiny`、`base`、`small`、`medium`、`large`、`turbo` 等 OpenAI Whisper 模型名，也支持 `/models` 下的自定义 `.pt` checkpoint。`task=translate` 只能使用 Whisper 原生能力翻译成英语；主应用的 HTTP 适配器默认请求的是转写，其他目标语言仍由主应用的翻译阶段处理。首次使用会下载模型，建议持久化 `/models`。
