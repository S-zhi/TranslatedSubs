# 本地英译中

TranslatedSubs 默认使用基于 CPU 的本地英译中，固定使用 `Helsinki-NLP/opus-mt-en-zh`，并转换为 CTranslate2 INT8 格式运行。DeepSeek 等云端翻译引擎保留为显式兼容选项。

服务启动不依赖本地翻译模型或这组可选 Python 包：即使模型尚未下载，`/api/health` 仍可正常启动和访问；只有提交需要字幕的默认任务时，才会提示先安装依赖并下载模型。

## 安装和下载

在项目根目录安装可选依赖：

```bash
uv sync --extra local-translation
```

不安装这组可选依赖也可以先启动服务。安装完成后，启动服务并打开“设置 → 翻译引擎”，在“本地 CPU 英译中”卡片上点击“下载并转换”。下载、INT8 转换和离线 CPU 烟测会在后台完成。设置页显示当前状态和本机实际文件字节数。

当前固定版本的原始权重约 312 MB；下载的模型文件和 tokenizer 合计约 315 MB。转换后的 INT8 模型实测为 84,100,908 字节（约 80.2 MiB）。最终大小可能随模型版本、转换器版本或运行平台略有变化。

模型状态为“就绪”后，创建字幕任务会默认使用“本地 CPU 英译中”。它只支持源语言 `en`、目标语言 `zh-CN` 或 `zh`，需要明确选择英语，不支持自动识别源语言或其他语对。使用该引擎无需 API Key。

模型输出可作为初稿；专有名词、语境和字幕表达仍需人工校对。

本地翻译默认每批处理 16 条字幕（云端批量仍为 8 条），可用
`SUBTRANS_LOCAL_TRANSLATE_BATCH` 调整。CPU 推理默认使用 1 个 inter-op、4 个
intra-op 线程；需要时可用 `SUBTRANS_LOCAL_CT2_INTER_THREADS` 和
`SUBTRANS_LOCAL_CT2_INTRA_THREADS` 覆盖。

## 存储和离线使用

安装目录为：

```text
<SUBTRANS_DATA_DIR>/models/translation/opus-mt-en-zh
```

首次点击“下载并转换”时需要访问固定的 Hugging Face 模型版本。此后翻译和设置页的验证烟测只从该目录加载 tokenizer 与 CTranslate2 模型，不会调用在线翻译服务。保留模型目录和可选 Python 依赖即可离线使用。

服务启动时如果模型已完整安装且可选依赖齐全，会在后台预热 tokenizer 和 CPU Translator；预热不会阻塞健康检查，也不会触发下载。预热与模型安装共用一个单 worker，安装或预热进行中不会重复提交；未安装模型或缺依赖时会跳过，直接在设置页下载并转换即可。生产环境建议使用单个 Uvicorn worker；多 worker 会各自载入一份模型，占用额外内存。

模型输入上限为 512 tokens。超过上限的字幕会以 `input_too_long` 失败；系统不会截断文本或静默改走云端翻译。请先手动拆分过长字幕再重试。

## 失败和重试

模型只有在下载、转换及短句 CPU INT8 烟测全部成功后才会发布。失败时临时文件会清理，设置页显示“失败”及错误信息，并允许重新点击“下载并转换”。
