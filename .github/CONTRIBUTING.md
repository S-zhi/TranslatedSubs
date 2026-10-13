# TranslatedSubs 贡献指南 (Contributing Guide)

感谢你对 TranslatedSubs 的关注！本指南将帮助你了解如何参与本项目的开发与贡献。无论是报告 Bug、提出新功能建议，还是提交代码，我们都非常欢迎！

---

## 目录

1. [行为准则](#1-行为准则)
2. [如何反馈问题 (Issue 规范)](#2-如何反馈问题-issue-规范)
3. [开发流程 (Fork & PR 规范)](#3-开发流程-fork--pr-规范)
4. [代码及提交规范 (Commit Message & Style)](#4-代码及提交规范-commit-message--style)
5. [测试与本地验证](#5-测试与本地验证)
6. [签署与合规 (DCO / CLA)](#6-签署与合规-dco--cla)

---

## 1. 行为准则

请确保在参与本项目社区交流和代码贡献时，保持友善、专业和尊重。我们鼓励建设性的讨论，反对任何形式的骚扰、歧视或人身攻击。

---

## 2. 如何反馈问题 (Issue 规范)

在提交 Issue 之前，请先搜索已有的 Open/Closed Issues，确认您的问题是否已被讨论或解决。

### 2.1 Bug 反馈模板
当您遇到程序异常或非预期行为时，请按以下格式提交 Issue：

```markdown
### 1. 简要描述 (Description)
请一句话概括遇到了什么问题。

### 2. 复现步骤 (Steps to Reproduce)
1. 运行命令 / 访问页面：...
2. 输入参数 / 点击按钮：...
3. 观察到的实际现象：...

### 3. 预期行为 (Expected Behavior)
预期的正确运行结果或页面表现。

### 4. 运行环境 (Environment)
- 操作系统 (OS): [e.g., macOS Sonoma 14.2]
- Python 版本: [e.g., 3.11.5]
- FFmpeg 版本: [e.g., ffmpeg-full 6.1]
- 浏览器 (如有前端问题): [e.g., Chrome 120]

### 5. 相关日志 / 截图 (Logs / Screenshots)
- 请粘贴终端报错、FastAPI 错误日志或前端 Console 堆栈。
```

### 2.2 功能建议模板 (Feature Request)
如果您希望为项目添加新特性，请使用以下格式：

```markdown
### 1. 需求背景 (Problem / Context)
这个新功能主要是解决什么痛点？

### 2. 方案设想 (Proposed Solution)
您期望的用户故事、交互界面、命令行参数或 API 设计。

### 3. 备选方案 (Alternatives Considered)
是否有其他替代方案能达成相同的效果？
```

> **安全提示**：请**不要**在公开 Issue 中透露任何 API Key、Token 或敏感环境配置。如发现安全漏洞，请通过私信或项目维护者指定的安全渠道进行反馈。

---

## 3. 开发流程 (Fork & PR 规范)

本项目使用典型的 Fork-And-Pull 协作模式。

### 3.1 核心流程步骤
1. **Fork 仓库**：在 GitHub 上将本仓库 Fork 到您个人账号下。
2. **创建分支**：在本地克隆您 Fork 的仓库，并从默认分支 `main` 创建一个独立的功能/修复分支：
   ```bash
   git checkout -b feat/your-feature-name
   # 或者
   git checkout -b fix/bug-description
   ```
3. **本地开发**：编写代码，并进行自测。
4. **运行测试**：确保所有单测通过（详见下文）。
5. **推送到个人仓库**：
   ```bash
   git push origin feat/your-feature-name
   ```
6. **创建 Pull Request (PR)**：在原仓库发起 PR，PR 目标分支应为 `main`。

### 3.2 PR 标题与描述规范
- **PR 标题**：建议使用 动词前缀 + 简要描述。例如：`feat: 增加字幕预览控件` 或 `fix: 修复 FFmpeg 硬字幕烧录路径空格报错`。
- **关联 Issue**：如果 PR 解决了某个特定 Issue，请在描述中加上 `Closes #Issue号`（例如 `Closes #42`），以便合并时自动关闭该 Issue。
- **保持分支聚焦**：请避免在一个 PR 中塞入多个不相关功能的修改。

---

## 4. 代码及提交规范 (Commit Message & Style)

### 4.1 Commit Message 规范
本项目遵循 **Conventional Commits (约定式提交)** 规范。每个 Commit Message 应遵循以下结构：

```
<type>: <description>
```

#### 常见 `<type>` 类型：
- `feat`: 新增功能 (Feature)
- `fix`: 修复 Bug (Bug Fix)
- `docs`: 仅文档更新 (Documentation)
- `style`: 代码格式化调整（不影响逻辑的代码空白、格式等修改）
- `refactor`: 重构代码（既非修复 Bug 也非新增功能）
- `test`: 新增或修改测试
- `chore`: 构建过程、工具、依赖更新或辅助工具变动
- `perf`: 性能优化

#### 示例：
```text
feat: add subtitle preview controls
fix: handle ffmpeg subtitle filter errors
docs: update README
```

### 4.2 代码风格规范
- **Python 代码**：遵循 PEP 8 规范。建议在提交前使用 linter（如 Ruff 或 Black）进行格式化。
- **前端代码**：保持 Vanilla JS / ES Modules 风格，不引入不必要的重量级框架，确保简洁高效。

---

## 5. 测试与本地验证

为了保证代码库的健壮性，提交 PR 前必须确保所有测试均能通过，且新增功能应当附带相应的单测。

### 5.1 运行测试
本项目使用 `uv` 包装 `pytest`。在提交前请在根目录下执行：

```bash
uv run pytest -q  # Python unit tests (CI job: python-unit-test)
```

如果只测试了特定模块：

```bash
uv run pytest tests/test_translator.py -q
```

> **注意**：联网的 E2E/端到端测试（需要真实调用云服务和下载视频）默认是跳过的。如果你修改了底层核心流水线，请在配置好 `.env` 变量后，执行以下命令进行完整验证：
> ```bash
> SUBTRANS_LIVE_TEST=1 uv run pytest tests/test_live_pipeline.py -v -s
> ```

### 5.2 补充测试的要求
- **新增 Feature**：必须在 `tests/` 下编写对应的单测，测试需覆盖核心边界条件、正常链路与异常链路。
- **Bug 修复**：建议先编写一个能够复现 Bug 的失败测试（Test-Driven），修复代码后再确保测试通过。

---

## 6. 签署与合规 (DCO / CLA)

- **开发者原创声明 (DCO)**：参与贡献意味着您声明并保证所提交的所有代码和文档均为您原创，或者您拥有合法的授权，且同意在项目的开源许可证（如 MIT）下对外分发。
- 项目当前不要求复杂的 CLA 签署，但我们要求在 Commit 历史或 PR 描述中清晰表明作者身份，共同维护开源生态的健康发展。
