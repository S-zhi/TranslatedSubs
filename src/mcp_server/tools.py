"""MCP 工具定义。

工具只编排业务 API，不直接访问 SQLite 或调用底层下载 / 翻译函数。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import Field

from .business_client import BusinessApiClient, BusinessApiError
from src.store import DEFAULT_TRANSLATION_ENGINE_ID

logger = logging.getLogger(__name__)


def _valid_video_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_task(api: BusinessApiClient, data: dict[str, Any]) -> dict[str, Any]:
    outputs = data.get("outputs")
    if isinstance(outputs, dict):
        outputs = {
            key: api.absolute_url(value) if isinstance(value, str) else value
            for key, value in outputs.items()
        }
    return {
        "task_id": data.get("id"),
        "url": data.get("url"),
        "title": data.get("title"),
        "status": data.get("status"),
        "progress": data.get("progress", 0),
        "current_step": data.get("currentStep"),
        "error": data.get("error"),
        "error_code": data.get("errorCode"),
        "resource_status": data.get("resourceStatus"),
        "outputs": outputs,
        "created_at": data.get("createdAt"),
        "updated_at": data.get("updatedAt"),
        "source_lang": data.get("sourceLang"),
        "target_lang": data.get("targetLang"),
        "mode": data.get("mode"),
        "burn": data.get("burn"),
        "need_subtitle": data.get("needSubtitle"),
    }


class SubtitleMcpTools:
    """可注入业务客户端的工具集合，便于单元测试。"""

    def __init__(self, api: BusinessApiClient) -> None:
        self.api = api

    async def check_subtitle_setup(self) -> dict[str, Any]:
        try:
            result = await self.api.readiness()
            if not result.get("ok"):
                result.setdefault(
                    "error_code",
                    "NOT_INITIALIZED"
                    if not result.get("initialized")
                    else "HARD_BURN_UNAVAILABLE",
                )
            return result
        except BusinessApiError as exc:
            return exc.to_result()
        except Exception:
            logger.exception("MCP readiness 检查失败")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "message": "MCP Server 检查业务服务时发生内部错误",
            }

    async def probe_video(self, url: str) -> dict[str, Any]:
        if not _valid_video_url(url):
            return {
                "ok": False,
                "error_code": "INVALID_URL",
                "message": "请输入以 http:// 或 https:// 开头的视频页面地址",
            }
        try:
            result = await self.api.probe_video(url.strip())
            return {
                "ok": bool(result.get("ok")),
                "probe": result,
                **({} if result.get("ok") else {"error_code": "PROBE_FAILED"}),
            }
        except BusinessApiError as exc:
            return exc.to_result()
        except Exception:
            logger.exception("MCP 视频探测失败")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "message": "MCP Server 探测视频时发生内部错误",
            }

    async def start_subtitle_pipeline(
        self,
        url: str,
        *,
        source_lang: str = "en",
        target_lang: str = "zh-CN",
        mode: Literal["mono", "bilingual"] = "mono",
        burn: Literal["hard", "soft"] = "hard",
        model: str = "local:tiny",
        need_subtitle: bool = True,
    ) -> dict[str, Any]:
        if not _valid_video_url(url):
            return {
                "ok": False,
                "error_code": "INVALID_URL",
                "message": "请输入以 http:// 或 https:// 开头的视频页面地址",
            }
        if mode not in {"mono", "bilingual"}:
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "mode 必须是 mono 或 bilingual"}
        if burn not in {"hard", "soft"}:
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "burn 必须是 hard 或 soft"}
        if not source_lang.strip() or not target_lang.strip() or not model.strip():
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "语言和模型参数不能为空"}

        try:
            readiness = await self.api.readiness()
            capabilities = readiness.get("capabilities", {})
            required_capability = "full_pipeline" if need_subtitle else "download"
            if not capabilities.get(required_capability, False):
                readiness.setdefault("error_code", "NOT_INITIALIZED")
                return readiness
            if need_subtitle and burn == "hard" and not capabilities.get("hard_burn", False):
                return {
                    "ok": False,
                    "error_code": "HARD_BURN_UNAVAILABLE",
                    "message": "当前 FFmpeg 不支持硬字幕滤镜，请安装带 libass 的 ffmpeg-full，或将 burn 设置为 soft",
                    "setup": readiness,
                }

            data = await self.api.create_task(
                {
                    "url": url.strip(),
                    "sourceLang": source_lang.strip(),
                    "targetLang": target_lang.strip(),
                    "mode": mode,
                    "burn": burn,
                    "model": model.strip(),
                    "engine": DEFAULT_TRANSLATION_ENGINE_ID,
                    "needSubtitle": need_subtitle,
                }
            )
            task = _normalize_task(self.api, data)
            return {
                "ok": True,
                **task,
                "message": "任务已创建，请使用 get_task_status 查询进度",
                "next_tool": "get_task_status",
            }
        except BusinessApiError as exc:
            return exc.to_result()
        except Exception:
            logger.exception("MCP 创建字幕流水线任务失败")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "message": "MCP Server 创建任务时发生内部错误",
            }

    async def get_task_status(self, task_id: str) -> dict[str, Any]:
        if not task_id.strip():
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "task_id 不能为空"}
        try:
            return {"ok": True, **_normalize_task(self.api, await self.api.get_task(task_id.strip()))}
        except BusinessApiError as exc:
            return exc.to_result()
        except Exception:
            logger.exception("MCP 查询任务状态失败")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "message": "MCP Server 查询任务时发生内部错误",
            }

    async def get_task_artifacts(self, task_id: str) -> dict[str, Any]:
        status = await self.get_task_status(task_id)
        if not status.get("ok"):
            return status
        if status.get("resource_status") == "MISSING":
            return {
                "ok": False,
                "error_code": "RESOURCE_MISSING",
                "task_id": task_id,
                "message": "任务产物已丢失，请重新运行任务",
            }
        if status.get("status") != "SUCCESS" or not status.get("outputs"):
            return {
                "ok": False,
                "error_code": "TASK_NOT_READY",
                "task_id": task_id,
                "status": status.get("status"),
                "progress": status.get("progress", 0),
                "message": "任务尚未成功完成，暂时没有可用产物",
            }

        burn = status.get("burn")
        video_name = "output_hard.mp4" if burn == "hard" else "output_soft.mp4" if burn == "soft" else f"{task_id}.mp4"
        filenames = {"video": video_name, "subtitle": f"{task_id}.srt"}
        artifacts = [
            {
                "type": kind,
                "filename": filenames.get(kind, f"{task_id}-{kind}"),
                "download_url": url,
            }
            for kind, url in status["outputs"].items()
        ]
        return {"ok": True, "task_id": task_id, "artifacts": artifacts}

    async def list_tasks(
        self,
        limit: int = 20,
        offset: int = 0,
        before_id: str | None = None,
        after_id: str | None = None,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 200:
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "limit 必须在 1 到 200 之间"}
        if offset < 0:
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "offset 必须大于等于 0"}
        try:
            data = await self.api.list_tasks(
                limit=limit,
                offset=offset,
                before_id=before_id,
                after_id=after_id,
            )
            tasks = [_normalize_task(self.api, item) for item in data]
            return {"ok": True, "count": len(tasks), "tasks": tasks}
        except BusinessApiError as exc:
            return exc.to_result()
        except Exception:
            logger.exception("MCP 查询任务列表失败")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "message": "MCP Server 查询任务列表时发生内部错误",
            }

    async def retry_task(self, task_id: str) -> dict[str, Any]:
        if not task_id.strip():
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "task_id 不能为空"}
        try:
            task = _normalize_task(self.api, await self.api.retry_task(task_id.strip()))
            return {
                "ok": True,
                **task,
                "message": "失败任务已重新入队",
                "next_tool": "get_task_status",
            }
        except BusinessApiError as exc:
            return exc.to_result()
        except Exception:
            logger.exception("MCP 重试任务失败")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "message": "MCP Server 重试任务时发生内部错误",
            }


def register_tools(server: Any, api: BusinessApiClient | None = None) -> SubtitleMcpTools:
    """把工具注册到 MCP Server，同时返回可直接测试的工具集合。"""
    service = SubtitleMcpTools(api or BusinessApiClient())

    @server.tool(
        name="check_subtitle_setup",
        title="检查字幕处理环境",
        description=(
            "只读检查业务 API、必要密钥、FFmpeg、yt-dlp 和存储目录是否就绪。"
            "这是处理视频前的必做步骤；不会返回任何密钥内容。若 error_code 为 "
            "NOT_INITIALIZED，请根据 config_file 和 missing 提醒用户在业务项目的 .env "
            "中配置并重启业务服务；若 agent_action 为 continue，才继续后续工具。"
        ),
    )
    async def check_subtitle_setup() -> dict[str, Any]:
        """检查业务服务、密钥、FFmpeg 和存储目录是否已初始化。"""
        return await service.check_subtitle_setup()

    @server.tool(
        name="probe_video",
        title="预检查视频地址",
        description=(
            "无副作用预检查视频 URL 是否可解析和下载，不会下载媒体文件。"
            "数据库尝试探测记录会自动按小时窗口去重，重复调用不会产生沉余数据库行。"
            "调用前先确认 check_subtitle_setup 没有返回 BUSINESS_UNAVAILABLE 或 NOT_INITIALIZED，"
            "并且 capabilities.download=true；如果预检查失败，不要直接启动流水线，应把返回的错误信息告知用户并请求新的 URL。"
        ),
    )
    async def probe_video(
        url: Annotated[
            str,
            Field(
                description="视频页面地址，必须是可访问的 http:// 或 https:// URL。",
                min_length=1,
            ),
        ],
    ) -> dict[str, Any]:
        """在不下载媒体文件的情况下检查视频 URL 是否可解析和下载。"""
        return await service.probe_video(url)

    @server.tool(
        name="start_subtitle_pipeline",
        title="启动字幕处理流水线",
        description=(
            "异步创建视频字幕处理任务，可能触发下载、语音识别、翻译和字幕烧录等外部副作用。"
            "调用前必须先执行 check_subtitle_setup；需要时可先执行 probe_video。"
            "成功后只使用返回的 task_id 调用 get_task_status 轮询，状态为 SUCCESS 后才能调用 "
            "get_task_artifacts。初始化失败时不要把密钥作为参数传入，按返回的 config_file、missing "
            "和 agent_action 指引用户配置业务服务。"
        ),
    )
    async def start_subtitle_pipeline(
        url: Annotated[
            str,
            Field(
                description="视频页面地址，必须是可访问的 http:// 或 https:// URL。",
                min_length=1,
            ),
        ],
        source_lang: Annotated[
            str,
            Field(description="源语言代码；本地默认翻译引擎使用 en。"),
        ] = "en",
        target_lang: Annotated[
            str,
            Field(description="目标语言代码，默认 zh-CN。"),
        ] = "zh-CN",
        mode: Annotated[
            Literal["mono", "bilingual"],
            Field(description="字幕模式：mono 为单语字幕，bilingual 为双语字幕。"),
        ] = "mono",
        burn: Annotated[
            Literal["hard", "soft"],
            Field(
                description=(
                    "字幕输出方式：hard 会烧录到视频，需要 FFmpeg 的 libass 字幕滤镜；"
                    "soft 只生成外挂字幕，兼容性更高。"
                ),
            ),
        ] = "hard",
        model: Annotated[
            str,
            Field(description="语音识别模型名称，默认 small。"),
        ] = "small",
        need_subtitle: Annotated[
            bool,
            Field(description="是否执行语音识别和翻译；false 时只执行下载相关能力。"),
        ] = True,
    ) -> dict[str, Any]:
        """异步启动下载、语音识别、字幕翻译和字幕烧录流水线。"""
        return await service.start_subtitle_pipeline(
            url,
            source_lang=source_lang,
            target_lang=target_lang,
            mode=mode,
            burn=burn,
            model=model,
            need_subtitle=need_subtitle,
        )

    @server.tool(
        name="get_task_status",
        title="查询字幕任务状态",
        description=(
            "只读查询任务状态、进度、当前阶段和错误信息。"
            "对 start_subtitle_pipeline 返回的 task_id 进行轮询；只有 status=SUCCESS 时才调用 "
            "get_task_artifacts，status=FAILED 时可询问用户是否调用 retry_task。"
        ),
    )
    async def get_task_status(
        task_id: Annotated[
            str,
            Field(description="start_subtitle_pipeline 或 retry_task 返回的任务 ID。", min_length=1),
        ],
    ) -> dict[str, Any]:
        """查询任务状态、进度、当前阶段和错误信息。"""
        return await service.get_task_status(task_id)

    @server.tool(
        name="get_task_artifacts",
        title="获取字幕任务产物",
        description=(
            "获取成功任务的视频和字幕下载地址。调用前必须先用 get_task_status 确认 status=SUCCESS；"
            "如果任务尚未完成或产物丢失，会返回 TASK_NOT_READY 或 RESOURCE_MISSING，"
            "不要把未就绪的结果当作下载地址。"
        ),
    )
    async def get_task_artifacts(
        task_id: Annotated[
            str,
            Field(description="已成功完成的任务 ID。", min_length=1),
        ],
    ) -> dict[str, Any]:
        """获取成功任务的视频和译文字幕下载地址。"""
        return await service.get_task_artifacts(task_id)

    @server.tool(
        name="list_tasks",
        title="列出字幕处理任务",
        description="只读列出字幕处理任务，支持分页（limit/offset）与游标（before_id/after_id），适合查看历史任务或寻找 task_id。",
    )
    async def list_tasks(
        limit: Annotated[
            int,
            Field(description="最多返回的任务数量，范围 1 到 200，默认 20。", ge=1, le=200),
        ] = 20,
        offset: Annotated[
            int,
            Field(description="跳过前 N 条记录，默认 0。", ge=0),
        ] = 0,
        before_id: Annotated[
            str | None,
            Field(description="游标：仅返回 ID 早于（更早创建）该任务的记录。"),
        ] = None,
        after_id: Annotated[
            str | None,
            Field(description="游标：仅返回 ID 晚于（更晚创建）该任务的记录。"),
        ] = None,
    ) -> dict[str, Any]:
        """列出字幕处理任务。"""
        return await service.list_tasks(
            limit=limit,
            offset=offset,
            before_id=before_id,
            after_id=after_id,
        )

    @server.tool(
        name="retry_task",
        title="重试失败字幕任务",
        description=(
            "重新执行一个失败的字幕处理任务，会产生新的异步处理副作用。"
            "只对 status=FAILED 的任务使用；调用后继续用返回的 task_id 调用 get_task_status，"
            "不要对仍在运行或已成功的任务重复重试。"
        ),
    )
    async def retry_task(
        task_id: Annotated[
            str,
            Field(description="status=FAILED 的任务 ID。", min_length=1),
        ],
    ) -> dict[str, Any]:
        """重新执行一个失败的字幕处理任务。"""
        return await service.retry_task(task_id)

    return service
