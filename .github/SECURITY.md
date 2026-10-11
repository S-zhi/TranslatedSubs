# Security Policy / 安全策略

[English](#english) | [简体中文](#简体中文)

---

## English

### Supported Versions

We actively support and fix security vulnerabilities for the following versions of TranslatedSubs:

| Version | Supported |
| ------- | --------- |
| `< 0.1.0` | No        |
| `0.1.0` | Yes       |

### Reporting a Vulnerability

If you discover a security vulnerability in this project, **please do NOT open a public GitHub issue**. Instead, report it using one of the following private channels:

1. **GitHub Security Advisories (Private Vulnerability Reporting):**
   If enabled on the repository, please use the "Report a vulnerability" button under the **Security** tab of the repository.
2. **Email Disclosure:**
   You can send an email to the project maintainers at **security@example.com** (replace with the repository maintainer's actual email or use our GitHub profile contact if applicable). Please include:
   - A detailed description of the vulnerability.
   - Steps to reproduce (or a Proof of Concept).
   - Potential impact of the exploit.

### Response SLA & Patch Cycle

- **Acknowledgement:** We aim to acknowledge your report within **48 hours** of receipt.
- **Triage & Fix:** We will evaluate the report and aim to provide a fix or mitigation plan within **7 to 14 days**, depending on the complexity of the issue.
- **Disclosure:** A coordinated public disclosure or release advisory will be published after the fix is successfully released.

### Bug Bounty

Currently, TranslatedSubs is an open-source project maintained on a voluntary basis. We **do not** offer any monetary rewards or bug bounty programs for vulnerability reports. We sincerely appreciate your contribution to making this tool safer for everyone.

### Security Research Scope & Rules

#### Allowed (White Hat Scope):
- Local deployment security audits.
- Analysis of local SQLite storage, task output path traversal, or potential command injection in FFmpeg/yt-dlp integration.
- Analysis of API endpoints under a safe sandbox environment.

#### Prohibited (Forbidden Testing):
- Do NOT perform Denial of Service (DoS/DDoS) attacks against third-party APIs used by this project.
- Do NOT leak or abuse API Keys, credentials, or user-uploaded media from production environments.

---

## 简体中文

### 支持的版本

我们为以下版本的 TranslatedSubs 提供积极的安全维护和漏洞修复：

| 版本 | 是否支持 |
| ---- | -------- |
| `< 0.1.0` | 否        |
| `0.1.0` | 是        |

### 报告安全漏洞

如果您在本项目中发现安全漏洞，**请不要通过公开的 GitHub Issue 进行反馈**。请选择以下私密渠道进行披露：

1. **GitHub 安全通告 (Private Vulnerability Reporting):**
   如果仓库已启用此功能，请前往仓库的 **Security** 选项卡，点击 "Report a vulnerability" 进行私密提交。
2. **邮件披露:**
   您可以将漏洞详情发送至项目维护者邮箱：**security@example.com**（或通过 GitHub Profile 上的联系方式与我们取得联系）。邮件中请尽量包含以下信息：
   - 漏洞的详细描述；
   - 重现步骤（或漏洞证明 Proof of Concept）；
   - 该漏洞可能带来的潜在安全影响。

### 响应时间与修复周期 (SLA)

- **确认接收:** 我们会在收到报告后的 **48 小时内** 给出初步确认。
- **评估与修复:** 我们将对漏洞进行评估，并争取在 **7 到 14 天内** 提供修复补丁或缓解方案（视漏洞复杂程度而定）。
- **公开披露:** 漏洞修复完成并发布新版本后，我们将协调进行漏洞细节的公开披露。

### 漏洞赏金 (Bug Bounty)

目前 TranslatedSubs 是一个主要由志愿者维护的开源项目，我们**不提供**任何形式的资金或实物漏洞赏金。我们对所有协助提升项目安全性的研究人员表示由衷的感谢！

### 安全研究范围与白名单

#### 允许的测试范围（白名单）:
- 本地部署环境下的安全审计；
- 对本地 SQLite 存储、任务输出路径穿越（Path Traversal）、FFmpeg/yt-dlp 命令行拼接注入等潜在风险面的深度分析；
- 在隔离的沙箱或测试环境内对后端 API 进行合规的安全测试。

#### 禁止的行为（黑名单）:
- 严禁对本项目集成的第三方云服务 API（如第三方翻译服务等）进行任何形式的拒绝服务（DoS/DDoS）测试或滥用；
- 严禁泄露、盗用或恶意消耗他人的 API Key 及凭据。
