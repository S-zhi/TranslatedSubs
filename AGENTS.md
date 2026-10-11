# TranslatedSubs Agent 工作指南

本仓库是视频与音频信息理解工作台，已实现字幕下载、转写、翻译、封装、Web 工作台和 MCP 接入。先阅读相关模块及其测试，再按现有架构做局部修改；不要把尚未交付的媒体分类、情绪或语调能力当作现有功能。

## 项目位置

- `src/handler/`：FastAPI 路由、请求模型和 Web 服务入口；入口是 `src.handler.app:app`。
- `src/core/`：下载、音频提取、转写、翻译和字幕处理。
- `src/service/`、`src/store/`：任务执行、调度与持久化；`src/mcp_server/`：MCP 接入。
- `web/`：原生 JavaScript ES Modules 前端；`drive-service/`：可选的 Google Drive sidecar。
- `tests/`、`web/__tests__/`：后端与前端测试；`docs/`：部署和接口文档。

## 开发与验证

- 使用 Python 3.10–3.12、`uv` 和 FFmpeg。硬字幕需要 FFmpeg 的 `subtitles`（libass）滤镜；环境说明见 `docs/local-quick-start.md`。
- `uv sync` 安装 Python 依赖；`uv run uvicorn src.handler.app:app --port 8000` 启动业务 API。
- `uv run pytest -q` 运行 Python 测试；前端测试在 `web/` 目录运行 `npm test`。修改某一模块时先跑相关测试，改动跨模块行为时再跑完整测试。
- `tests/test_live_pipeline.py` 涉及真实云服务和视频下载，只有配置好环境后才通过 `SUBTRANS_LIVE_TEST=1 uv run pytest tests/test_live_pipeline.py -v -s` 显式运行。
- 本地配置从 `.env.example` 复制到 `.env`；不要提交 API Key、OAuth 凭据、Cookie 或测试生成的媒体文件。

保持 Python 代码符合现有 PEP 8 风格；前端沿用原生 JS/ES Modules。行为变化应更新相关测试和文档。提交规范与 PR 要求见 `.github/CONTRIBUTING.md`。

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **TranslatedSubs** (4495 symbols, 12442 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/TranslatedSubs/context` | Codebase overview, check index freshness |
| `gitnexus://repo/TranslatedSubs/clusters` | All functional areas |
| `gitnexus://repo/TranslatedSubs/processes` | All execution flows |
| `gitnexus://repo/TranslatedSubs/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
