"""TranslatedSubs MCP Server 入口。

默认使用 stdio，适合桌面 MCP Host；设置
``SUBTRANS_MCP_TRANSPORT=streamable-http`` 可启动独立 HTTP MCP 服务。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mcp.server import MCPServer

from .tools import register_tools

logging.basicConfig(level=os.getenv("SUBTRANS_MCP_LOG_LEVEL", "INFO"))

SERVER_INSTRUCTIONS = """你是 TranslatedSubs 的字幕处理助手。

请严格遵循下面的工作流：
1. 任何视频处理前先调用 check_subtitle_setup。
2. 如果返回 error_code=BUSINESS_UNAVAILABLE，说明业务 FastAPI 没有运行；请提示用户启动业务服务。
3. 如果返回 error_code=NOT_INITIALIZED 或 agent_action=ask_user_to_configure，使用返回的 config_file 和 missing
   告诉用户在固定的业务项目 .env 文件中完成配置，然后重启业务服务。不要要求用户把 API Key 作为工具参数传入，
   不要输出或生成 API Key。
4. setup 就绪后，可先调用 probe_video 验证 URL，再调用 start_subtitle_pipeline。
5. start_subtitle_pipeline 成功只代表任务已入队；保存 task_id，并使用 get_task_status 轮询。
6. 只有 get_task_status 返回 status=SUCCESS 时，才调用 get_task_artifacts。
7. status=FAILED 时先向用户说明错误；用户确认后才调用 retry_task，随后重新轮询。
8. 如果 setup 提示 agent_action=use_soft_burn_or_install_libass，优先询问用户是否接受外挂字幕，
   或提示安装带 libass 的 FFmpeg；不要擅自改变用户明确指定的 burn=hard。

默认参数是 source_lang=en、target_lang=zh-CN、mode=mono、burn=hard、model=local:tiny，默认翻译引擎为本地 CPU 英译中。
MCP 只负责调用业务 API，不直接访问业务数据库、文件或底层下载/翻译实现。"""

AGENT_GUIDE_PATH = Path(__file__).resolve().parents[2] / "docs" / "mcp-agent-guide.md"

mcp = MCPServer(
    name="TranslatedSubs MCP",
    title="TranslatedSubs 字幕处理服务",
    description="通过业务 API 执行视频下载、语音识别、字幕翻译和字幕烧录。",
    instructions=SERVER_INSTRUCTIONS,
    version="0.1.0",
)


@mcp.resource(
    "subtitles://agent-guide",
    name="subtitle-agent-guide",
    title="TranslatedSubs Agent 使用指南",
    description="Agent 使用字幕处理 MCP 时必须遵守的工作流、初始化和错误处理说明。",
    mime_type="text/markdown",
)
def read_agent_guide() -> str:
    """读取给 Agent 使用的完整工作流文档。"""
    return AGENT_GUIDE_PATH.read_text(encoding="utf-8")


registered_tools = register_tools(mcp)


def _port() -> int:
    try:
        return int(os.getenv("SUBTRANS_MCP_PORT", "3001"))
    except ValueError:
        return 3001


def main() -> None:
    transport = os.getenv("SUBTRANS_MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        mcp.run()
        return
    if transport in {"streamable-http", "streamable_http"}:
        mcp.run(
            transport="streamable-http",
            host=os.getenv("SUBTRANS_MCP_HOST", "127.0.0.1"),
            port=_port(),
            streamable_http_path=os.getenv("SUBTRANS_MCP_PATH", "/mcp"),
        )
        return
    raise ValueError(
        "SUBTRANS_MCP_TRANSPORT 必须是 stdio 或 streamable-http"
    )


if __name__ == "__main__":
    main()
