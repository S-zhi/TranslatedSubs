# TranslatedSubs 文档

根目录 [README（英文）](../README.md) 包含产品概览、快速启动、基本使用和关键配置；[中文版 README](./zh/README.md) 提供相同内容。更详细的说明请按使用场景阅读以下文档：

- [产品宣传页开发](../site/README.md)：独立 React 宣传页、连续背景、视频与核心优势、本地预览及静态构建。
- [InstaBox 一键云端试用](./instabox-quick-start.md)：用维护方账号自动创建独立工作台，控制服务配置、同源转发、鉴权与到期回收。
- [本地快速启动](./local-quick-start.md)：macOS/Linux 本地运行、Redis/bootstrap 配置和 Google Drive sidecar。
- [Linux 部署](./quick-start-linux.md)：Ubuntu/Debian 一键安装、systemd、反向代理、更新和排障。
- [MCP Server](./mcp-server.md)：启动 MCP、stdio/Streamable HTTP 配置和工具列表。
- [MCP Agent 指南](./mcp-agent-guide.md)：调用顺序、任务状态、错误码和安全约束。
- [转写服务协议](./transcriber-service.md)：本地 faster-whisper 和 HTTP 转写后端。
- [配音指南](./tts.md)：Kokoro 可选依赖、`espeak-ng` 和模型准备。
- [Google Drive sidecar](../drive-service/README.md)：OAuth、任务级同步和 sidecar API。
- [机器引导配置模板](../config.example.json)：Redis 地址、数据路径、HTTP 与 MCP 监听默认值。

已交付能力的定位和适用场景见[中文版 README](./zh/README.md)的“能力概览”。

后续规划见 [macOS 桌面首版产品约定与交付计划](./macos-desktop-v1-product-brief.md)：记录已确认的零配置使用流程、验收标准、三天交付顺序与发行条件；其中目标不代表已交付能力。
