"""后台固定站点可用性批次。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from src.core.downloader import ProbeResult, probe_video
from src.store import ProbeStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartupProbeSite:
    id: str
    name: str
    domain: str
    hostnames: tuple[str, ...]
    url: str


STARTUP_PROBE_SITES = (
    StartupProbeSite(
        "youtube",
        "YouTube",
        "youtube.com",
        ("youtube.com", "youtu.be"),
        "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
    ),
    StartupProbeSite(
        "vimeo",
        "Vimeo",
        "vimeo.com",
        ("vimeo.com",),
        "https://vimeo.com/76979871",
    ),
    StartupProbeSite(
        "dailymotion",
        "Dailymotion",
        "dailymotion.com",
        ("dailymotion.com",),
        "https://www.dailymotion.com/video/x8ocv9e",
    ),
    StartupProbeSite(
        "twitch",
        "Twitch",
        "twitch.tv",
        ("twitch.tv",),
        "https://www.twitch.tv/getquakedon/clip/ShyInspiringCoffeeTwitchRPG-6J9krE7mDuZi42H2",
    ),
    StartupProbeSite(
        "tiktok",
        "TikTok",
        "tiktok.com",
        ("tiktok.com",),
        "https://www.tiktok.com/@scout2015/video/6718335390845095173",
    ),
    StartupProbeSite(
        "twitter",
        "X / Twitter",
        "x.com",
        ("x.com", "twitter.com"),
        "https://x.com/plumdred/status/2105614379609669701/video/1",
    ),
    StartupProbeSite(
        "instagram",
        "Instagram",
        "instagram.com",
        ("instagram.com",),
        "https://www.instagram.com/p/CxKJZJ1P8wB/",
    ),
    StartupProbeSite(
        "acfun",
        "AcFun",
        "acfun.cn",
        ("acfun.cn",),
        "https://www.acfun.cn/v/ac48876221",
    ),
    StartupProbeSite(
        "niconico",
        "ニコニコ動画",
        "nicovideo.jp",
        ("nicovideo.jp", "niconico.jp"),
        "https://www.nicovideo.jp/watch/sm9",
    ),
    StartupProbeSite(
        "pornhub",
        "Pornhub",
        "pornhub.com",
        ("pornhub.com",),
        "https://cn.pornhub.com/view_video.php?viewkey=6aba9401ec1d3",
    ),
)


@dataclass
class _SiteState:
    status: str = "pending"
    result: Optional[ProbeResult] = None
    source: Optional[str] = None
    updated_at: Optional[int] = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hostname(url: str) -> str:
    try:
        return (urlparse(str(url).strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _site_for_url(url: str) -> Optional[StartupProbeSite]:
    hostname = _hostname(url)
    if not hostname:
        return None
    for site in STARTUP_PROBE_SITES:
        if any(hostname == alias or hostname.endswith(f".{alias}") for alias in site.hostnames):
            return site
    return None


def _result_status(result: ProbeResult) -> str:
    return "ok" if result.ok else "fail"


def _result_payload(result: Optional[ProbeResult], created_at: Optional[int]) -> Optional[dict]:
    if result is None:
        return None
    return {
        "ok": bool(result.ok),
        "title": result.title,
        "extractor": result.extractor,
        "duration": result.duration,
        "formatsCount": result.formats_count,
        "webpageUrl": result.webpage_url,
        "reason": result.reason,
        "detail": result.detail,
        "cached": bool(result.cached),
        "language": result.language,
        "availableQualities": result.available_qualities,
        "formats": [],
        "thumbnail": None,
        "uploader": result.uploader,
        "createdAt": created_at,
    }


class ProbeBatchManager:
    """管理一次启动批次，并接受开发者页的单站点结果覆盖。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._run_id: Optional[str] = None
        self._state = "idle"
        self._run_started_at: Optional[int] = None
        self._updated_at: Optional[int] = None
        self._completed = 0
        self._sites = {site.id: _SiteState() for site in STARTUP_PROBE_SITES}

    def start(self, probes: ProbeStore) -> None:
        with self._lock:
            if self._state == "running":
                return
            self._stop_event.clear()
            self._run_id = f"startup_{uuid.uuid4().hex[:12]}"
            self._state = "running"
            self._run_started_at = _now_ms()
            self._updated_at = self._run_started_at
            self._completed = 0
            self._sites = {site.id: _SiteState() for site in STARTUP_PROBE_SITES}
            run_id = self._run_id
            started_at = self._run_started_at
            self._thread = threading.Thread(
                target=self._run,
                args=(run_id, started_at, probes),
                name="startup-probe-batch",
                daemon=True,
            )
            self._thread.start()

    def disable(self) -> None:
        with self._lock:
            if self._state == "idle":
                self._state = "disabled"
                self._updated_at = _now_ms()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self, run_id: str, started_at: int, probes: ProbeStore) -> None:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="startup-probe") as executor:
            futures = {
                executor.submit(self._probe_one, site, run_id, started_at, probes): site
                for site in STARTUP_PROBE_SITES
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    logger.exception("启动站点探测线程异常: site=%s", futures[future].id)
                with self._lock:
                    if self._run_id == run_id:
                        self._completed += 1
                        self._updated_at = _now_ms()

        with self._lock:
            if self._run_id == run_id:
                self._state = "completed"
                self._updated_at = _now_ms()

    def _probe_one(
        self,
        site: StartupProbeSite,
        run_id: str,
        started_at: int,
        probes: ProbeStore,
    ) -> None:
        with self._lock:
            current = self._sites[site.id]
            if self._run_id != run_id or self._stop_event.is_set():
                return
            if current.source != "manual" or (current.updated_at or 0) <= started_at:
                current.status = "testing"
                current.result = None
                current.source = "startup"
                current.updated_at = _now_ms()

        try:
            result = probe_video(site.url)
        except Exception:
            logger.exception("启动站点探测失败: site=%s", site.id)
            result = ProbeResult(ok=False, reason="启动探测失败")

        try:
            record = probes.record(
                url=site.url,
                ok=result.ok,
                title=result.title,
                extractor=result.extractor,
                duration=result.duration,
                formats_count=result.formats_count,
                webpage_url=result.webpage_url,
                reason=result.reason,
                detail=result.detail,
                language=result.language,
                available_qualities=result.available_qualities,
                formats=result.formats,
                thumbnail=result.thumbnail,
                uploader=result.uploader,
            )
            created_at = record.created_at
        except Exception:
            logger.exception("启动站点探测结果写入失败: site=%s", site.id)
            created_at = _now_ms()

        with self._lock:
            if self._run_id != run_id:
                return
            current = self._sites[site.id]
            if current.source == "manual" and (current.updated_at or 0) > started_at:
                return
            current.status = _result_status(result)
            current.result = result
            current.source = "startup"
            current.updated_at = created_at

    def record_manual_probe(
        self,
        url: str,
        result: ProbeResult,
        created_at: Optional[int] = None,
    ) -> None:
        site = _site_for_url(url)
        if site is None:
            return
        with self._lock:
            current = self._sites[site.id]
            current.status = _result_status(result)
            current.result = result
            current.source = "manual"
            current.updated_at = created_at or _now_ms()
            self._updated_at = _now_ms()

    def status(self) -> dict:
        with self._lock:
            successful = sum(item.status == "ok" for item in self._sites.values())
            failed = sum(item.status == "fail" for item in self._sites.values())
            return {
                "runId": self._run_id,
                "state": self._state,
                "total": len(STARTUP_PROBE_SITES),
                "completed": self._completed,
                "successful": successful,
                "failed": failed,
                "runStartedAt": self._run_started_at,
                "updatedAt": self._updated_at,
                "sites": [
                    {
                        "id": site.id,
                        "name": site.name,
                        "domain": site.domain,
                        "url": site.url,
                        "status": self._sites[site.id].status,
                        "source": self._sites[site.id].source,
                        "updatedAt": self._sites[site.id].updated_at,
                        "result": _result_payload(
                            self._sites[site.id].result,
                            self._sites[site.id].updated_at,
                        ),
                    }
                    for site in STARTUP_PROBE_SITES
                ],
            }


probe_batch_manager = ProbeBatchManager()


def start_startup_probe(probes: ProbeStore, enabled: bool = True) -> None:
    if enabled:
        probe_batch_manager.start(probes)
    else:
        probe_batch_manager.disable()


def stop_startup_probe() -> None:
    probe_batch_manager.stop()
