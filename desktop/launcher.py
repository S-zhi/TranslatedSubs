"""Start the local TranslatedSubs service in an application window.

The packaged native window is a small Cocoa/WebKit helper that renders the
existing frontend. Keeping the helper lookup inside the window function lets
server-only and test invocations run without a GUI.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Sequence


APP_NAME = "TranslatedSubs"
LOGGER = logging.getLogger("translatedsubs.desktop")


class LauncherError(RuntimeError):
    """Raised when the desktop launcher cannot start the local service."""


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved paths used by the packaged and source launchers."""

    bundle_root: Path
    data_dir: Path
    ffmpeg: Path | None = None
    ffprobe: Path | None = None


def is_frozen() -> bool:
    """Return whether the process is running from a PyInstaller bundle."""

    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Return the read-only resource root for source and bundled runs."""

    if is_frozen():
        extracted = getattr(sys, "_MEIPASS", None)
        if extracted:
            return Path(extracted).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def default_data_dir(app_name: str = APP_NAME) -> Path:
    """Return the per-user writable data directory for the current OS."""

    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif system == "Darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / app_name


def _binary_filename(stem: str) -> str:
    return f"{stem}.exe" if platform.system() == "Windows" else stem


def _find_bundled_binary(root: Path, stem: str) -> Path | None:
    filename = _binary_filename(stem)
    candidates = (
        root / "bin" / filename,
        root / "resources" / "bin" / filename,
        root / "ffmpeg" / filename,
        root / filename,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _configured_binary(env_name: str, root: Path, stem: str) -> Path | None:
    """Resolve a binary setting while preserving PATH-style command names."""

    raw = os.environ.get(env_name)
    if raw:
        candidate = Path(raw).expanduser()
        # A bare value such as ``ffmpeg`` is a command name, not a path.  Keep
        # it in the inherited environment so shutil.which() can resolve it.
        if candidate.parent == Path(".") and not candidate.is_absolute():
            return None
        return candidate.resolve()
    return _find_bundled_binary(root, stem)


def resolve_runtime_paths(
    *,
    root: Path | None = None,
    data_dir: Path | None = None,
    ffmpeg: Path | None = None,
    ffprobe: Path | None = None,
) -> RuntimePaths:
    """Resolve bundled binaries and a writable data directory.

    Explicit arguments take precedence, followed by environment variables and
    finally the platform default.  The directory is created here so the
    backend can safely open its SQLite database and runtime settings file.
    """

    bundle = (root or bundle_root()).expanduser().resolve()
    configured_data = data_dir or (
        Path(os.environ["SUBTRANS_DATA_DIR"])
        if os.environ.get("SUBTRANS_DATA_DIR")
        else default_data_dir()
    )
    writable_data = configured_data.expanduser().resolve()
    writable_data.mkdir(parents=True, exist_ok=True)

    configured_ffmpeg = ffmpeg or _configured_binary("SUBTRANS_FFMPEG", bundle, "ffmpeg")
    configured_ffprobe = ffprobe or _configured_binary("SUBTRANS_FFPROBE", bundle, "ffprobe")
    return RuntimePaths(
        bundle_root=bundle,
        data_dir=writable_data,
        ffmpeg=configured_ffmpeg.expanduser().resolve() if configured_ffmpeg else None,
        ffprobe=configured_ffprobe.expanduser().resolve() if configured_ffprobe else None,
    )


def configure_environment(paths: RuntimePaths) -> dict[str, str]:
    """Build the child environment without modifying the parent process."""

    environment = os.environ.copy()
    environment["SUBTRANS_DATA_DIR"] = str(paths.data_dir)
    if paths.ffmpeg:
        environment["SUBTRANS_FFMPEG"] = str(paths.ffmpeg)
    if paths.ffprobe:
        environment["SUBTRANS_FFPROBE"] = str(paths.ffprobe)
    return environment


def choose_free_port(host: str = "127.0.0.1") -> int:
    """Reserve a currently free TCP port for the child server."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _server_command() -> list[str]:
    if is_frozen():
        return [sys.executable]
    # Module mode keeps the repository root on the import path when the
    # source launcher is started from either ``python -m`` or a file path.
    return [sys.executable, "-m", "desktop.launcher"]


def _creation_options() -> dict[str, object]:
    if platform.system() == "Windows":
        # The packaged launcher is normally built without a console window.
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def _health_url(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{display_host}:{port}/api/health"


def wait_for_health(
    host: str,
    port: int,
    *,
    timeout: float = 60.0,
    interval: float = 0.25,
    process: subprocess.Popen[bytes] | None = None,
) -> None:
    """Wait until FastAPI accepts requests or fail with a useful error."""

    url = _health_url(host, port)
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise LauncherError(
                f"FastAPI 子进程提前退出（exit={process.returncode}），"
                "请查看用户数据目录中的 logs/server.log。"
            )
        try:
            with urllib.request.urlopen(url, timeout=min(interval * 4, 2.0)) as response:
                if 200 <= response.status < 300:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(interval)
    detail = f"：{last_error}" if last_error else ""
    raise LauncherError(f"FastAPI 服务在 {timeout:.0f} 秒内没有就绪{detail}")


def open_browser(url: str, browser_name: str | None = None) -> bool:
    """Open the local workbench in the default or named browser."""

    try:
        controller = webbrowser.get(browser_name) if browser_name else webbrowser
        return bool(controller.open_new_tab(url))
    except webbrowser.Error as exc:
        LOGGER.warning("无法自动打开浏览器，请手动访问 %s：%s", url, exc)
        return False


def _find_native_window(root: Path) -> Path | None:
    configured = os.environ.get("SUBTRANS_NATIVE_WINDOW")
    candidates = (
        Path(configured).expanduser() if configured else None,
        root / "bin" / "TranslatedSubsWindow",
        root / "resources" / "bin" / "TranslatedSubsWindow",
        root / "TranslatedSubsWindow",
    )
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    return None


def open_native_window(url: str) -> None:
    """Open the workbench in the bundled macOS WebKit window."""

    helper = _find_native_window(bundle_root())
    if helper is None:
        raise LauncherError(
            "缺少原生 macOS 窗口组件，请用 scripts/build-desktop.py 重新构建。"
        )
    try:
        completed = subprocess.run(
            [str(helper), "--url", url, "--title", APP_NAME],
            check=False,
        )
    except OSError as exc:
        raise LauncherError(f"无法启动原生 macOS 窗口：{exc}") from exc
    if completed.returncode:
        raise LauncherError(
            f"原生 macOS 窗口异常退出（exit={completed.returncode}）。"
        )


def _attach_file_logger(path: Path) -> logging.Handler:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    LOGGER.addHandler(handler)
    return handler


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            LOGGER.warning("FastAPI 子进程未能在退出时结束，pid=%s", process.pid)


def run_server(host: str, port: int, log_level: str) -> int:
    """Run the FastAPI app in server mode (used by the child process)."""

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - only reached outside uv
        raise LauncherError("缺少 uvicorn，请先安装项目依赖。") from exc

    uvicorn.run(
        "src.handler.app:app",
        host=host,
        port=port,
        log_level=log_level,
        access_log=False,
        reload=False,
        workers=1,
    )
    return 0


def run_launcher(args: argparse.Namespace) -> int:
    paths = resolve_runtime_paths(
        data_dir=args.data_dir,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
    )
    environment = configure_environment(paths)
    port = args.port or choose_free_port(args.host)
    display_host = (
        f"[{args.host}]"
        if ":" in args.host and not args.host.startswith("[")
        else args.host
    )
    base_url = f"http://{display_host}:{port}"
    command = [
        *_server_command(),
        "--server",
        "--host",
        args.host,
        "--port",
        str(port),
        "--log-level",
        args.log_level,
    ]

    log_dir = paths.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    server_log_path = log_dir / "server.log"
    server_log = server_log_path.open("a", encoding="utf-8")
    launcher_log_handler = _attach_file_logger(log_dir / "launcher.log")
    process: subprocess.Popen[bytes] | None = None
    stop_requested = Event()
    previous_handlers: dict[int, object] = {}

    def request_stop(_signum: int, _frame: object) -> None:
        stop_requested.set()

    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        except (OSError, RuntimeError, ValueError):
            pass

    try:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(paths.bundle_root) if not is_frozen() else None,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=server_log,
                stderr=server_log,
                **_creation_options(),
            )
        except OSError as exc:
            raise LauncherError(f"无法启动 FastAPI 子进程：{exc}") from exc
        wait_for_health(
            args.host,
            port,
            timeout=args.startup_timeout,
            process=process,
        )
        if not args.no_browser:
            if args.window_mode == "native":
                open_native_window(base_url)
                # The native helper blocks while its window is open. Returning
                # here lets the finally block stop the local FastAPI child as
                # soon as the user closes that window.
                return 0
            else:
                open_browser(base_url, args.browser)
        LOGGER.info("TranslatedSubs 已启动：http://%s:%s", args.host, port)
        LOGGER.info("服务日志：%s", server_log_path)

        while process.poll() is None:
            if stop_requested.wait(0.25):
                _terminate_process(process)
                break
        return process.wait()
    finally:
        if process is not None:
            _terminate_process(process)
        server_log.close()
        LOGGER.removeHandler(launcher_log_handler)
        launcher_log_handler.close()
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, RuntimeError, ValueError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start TranslatedSubs locally")
    default_window_mode = "native" if platform.system() == "Darwin" else "browser"
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1", help="本地监听地址")
    parser.add_argument("--port", type=int, default=0, help="端口，0 表示自动选择")
    parser.add_argument("--data-dir", type=Path, help="覆盖默认用户数据目录")
    parser.add_argument("--ffmpeg", type=Path, help="指定 ffmpeg 可执行文件")
    parser.add_argument("--ffprobe", type=Path, help="指定 ffprobe 可执行文件")
    parser.add_argument(
        "--window-mode",
        choices=("native", "browser"),
        default=default_window_mode,
        help="窗口模式：native 使用应用内窗口，browser 使用外部浏览器",
    )
    parser.add_argument("--browser", help="browser 模式下传给 webbrowser 的浏览器名称")
    parser.add_argument(
        "--no-window",
        "--no-browser",
        dest="no_browser",
        action="store_true",
        help="只启动服务，不打开窗口（保留 --no-browser 兼容写法）",
    )
    parser.add_argument("--startup-timeout", type=float, default=60.0, help="服务启动超时秒数")
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        default="info",
        help="FastAPI/Uvicorn 日志级别",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.server:
            paths = resolve_runtime_paths(
                data_dir=args.data_dir,
                ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe,
            )
            # The child receives the same environment as the parent in normal
            # operation.  Set these values as well for direct --server runs.
            os.environ.update(configure_environment(paths))
            return run_server(args.host, args.port, args.log_level)
        return run_launcher(args)
    except LauncherError as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised by the packaged app
    raise SystemExit(main())

