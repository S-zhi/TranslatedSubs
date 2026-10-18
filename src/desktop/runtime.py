"""Loopback backend lifecycle, deliberately free of business imports at import time."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import uuid


def desktop_version() -> str:
    """Use the same installed-package fallback as update discovery."""
    from importlib.metadata import PackageNotFoundError, version

    configured = os.environ.get("SUBTRANS_DESKTOP_VERSION")
    if configured is not None:
        return configured
    try:
        return version("translatedsubs-backend")
    except PackageNotFoundError:
        return "unknown"


async def stop_desktop_retention() -> None:
    """Stop the scheduler owned by this process without blocking the ASGI loop."""
    from src.service import retention_scheduler

    # The existing scheduler has no public stop API. Reuse its own stop event;
    # never touch another process or start a replacement scheduler during exit.
    retention_scheduler._stop_event.set()
    thread = retention_scheduler._scheduler_thread
    if thread is not None:
        await asyncio.to_thread(thread.join, timeout=2.0)


def configure_environment() -> Path:
    """Set bootstrap inputs before importing configuration or handler modules."""
    token = os.environ.get("SUBTRANS_DESKTOP_SESSION_TOKEN", "")
    if not token or not token.isascii() or any(
        char.isspace() or char in ';,"\\' for char in token
    ):
        raise ValueError("A valid SUBTRANS_DESKTOP_SESSION_TOKEN is required")
    home = Path(os.environ.get(
        "SUBTRANS_DESKTOP_HOME",
        str(Path.home() / "Library" / "Application Support" / "TranslateSub"),
    )).expanduser()
    if not home.is_absolute():
        raise ValueError("SUBTRANS_DESKTOP_HOME must be an absolute path")
    for name in ("SUBTRANS_DESKTOP_FFMPEG", "SUBTRANS_DESKTOP_FFPROBE"):
        value = os.environ.get(name)
        if value and not Path(value).is_absolute():
            raise ValueError(f"{name} must be an absolute path")
    os.environ["SUBTRANS_DESKTOP"] = "1"
    os.environ["SUBTRANS_DESKTOP_HOME"] = str(home)
    return home


def write_session_descriptor(path: Path, *, port: int, instance_id: str) -> None:
    """Publish only non-secret discovery metadata using an atomic 0600 file."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("The session file must be an absolute path")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = {"port": port, "pid": os.getpid(), "instance_id": instance_id}
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(descriptor, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def remove_session_descriptor(path: Path, instance_id: str) -> None:
    """Do not remove a descriptor belonging to a newer backend instance."""
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        if descriptor.get("instance_id") == instance_id and descriptor.get("pid") == os.getpid():
            path.unlink(missing_ok=True)
    except (OSError, ValueError, AttributeError):
        pass


async def serve_desktop(session_file: Path) -> None:
    configure_environment()
    session_file = Path(session_file)
    if not session_file.is_absolute():
        raise ValueError("The session file must be an absolute path")

    # These imports must remain below environment setup, including in frozen builds.
    import uvicorn
    from src.handler.app import app

    instance_id = uuid.uuid4().hex
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.setblocking(False)
        port = listener.getsockname()[1]
        app.state.desktop_port = port

        class DesktopServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                if self.started:
                    write_session_descriptor(session_file, port=port, instance_id=instance_id)

        server = DesktopServer(uvicorn.Config(
            app, host="127.0.0.1", port=port, proxy_headers=False,
            access_log=False, lifespan="on", timeout_graceful_shutdown=10,
        ))

        def request_shutdown() -> None:
            server.should_exit = True

        app.state.desktop_request_shutdown = request_shutdown
        try:
            await server.serve(sockets=[listener])
        finally:
            remove_session_descriptor(session_file, instance_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="TranslateSub desktop backend")
    parser.add_argument("--session-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        configure_environment()
        if not args.session_file.is_absolute():
            raise ValueError("The session file must be an absolute path")
    except ValueError as exc:
        parser.error(str(exc))
    asyncio.run(serve_desktop(args.session_file))
