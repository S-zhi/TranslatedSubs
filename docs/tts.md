# CPU 配音

任务表单可启用 Kokoro 配音，当前支持中文与英文。默认使用 Kokoro v1.1-zh：中文音色为 `zf_001`、`zm_010`，英文音色为 `af_maple`、`af_sol`。安装可选依赖：

```bash
uv sync --extra tts
```

运行机器还需安装 FFmpeg 和 `espeak-ng`。首次配音会从 Hugging Face 下载模型权重，后续使用本机缓存；推理固定使用 CPU。字幕逐条按时间轴生成，双语字幕只读取译文。语音超过字幕时长时使用 FFmpeg `atempo` 保持音高并加速；若仍无法放入时间窗，配音状态会失败，普通字幕视频仍可下载。

原始人声模式：

- `keep` 保留完整原始音轨并叠加配音。
- `lower` 使用 Demucs `htdemucs` 双 stem，降低原人声并保留背景音。
- `replace` 使用 Demucs 背景 stem 替换原人声。

配音生成失败会在任务的 `ttsStatus` / `ttsError` 单独报告，不会使字幕视频任务失败。成功后配音视频通过 `/api/tasks/{task_id}/dubbed` 下载，并保留原任务的字幕轨道。
