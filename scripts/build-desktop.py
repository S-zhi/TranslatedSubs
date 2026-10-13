#!/usr/bin/env python3
"""Build the native-window TranslatedSubs desktop bundle."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "desktop" / "translatedsubs.spec"
APP_NAME = "TranslatedSubs"
NATIVE_WINDOW_SOURCE = ROOT / "desktop" / "native_window.m"
NATIVE_WINDOW_OUTPUT = ROOT / "build" / "native-window" / "TranslatedSubsWindow"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ffmpeg-dir",
        type=Path,
        help="包含 ffmpeg/ffprobe 的目录；建议发布包必须提供",
    )
    parser.add_argument("--clean", action="store_true", help="删除旧的 build/dist 后再构建")
    parser.add_argument(
        "--dmg",
        action="store_true",
        help="在 macOS 上把 .app 放进可拖拽安装的 DMG",
    )
    parser.add_argument(
        "--dmg-only",
        action="store_true",
        help="使用已有的 dist/TranslatedSubs.app 只生成 DMG，不重新打包",
    )
    return parser.parse_args()


def build_native_window() -> Path | None:
    """Compile the macOS WebKit window helper used by the launcher."""

    if platform.system() != "Darwin":
        return None
    if not NATIVE_WINDOW_SOURCE.is_file():
        raise SystemExit(f"Native window source not found: {NATIVE_WINDOW_SOURCE}")
    clang = shutil.which("clang")
    xcrun = shutil.which("xcrun")
    if not clang or not xcrun:
        raise SystemExit("找不到 clang/xcrun，请先安装 Xcode Command Line Tools。")
    try:
        sdk = subprocess.check_output(
            [xcrun, "--sdk", "macosx", "--show-sdk-path"],
            cwd=ROOT,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"无法定位 macOS SDK：{exc}") from exc

    NATIVE_WINDOW_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    command = [
        clang,
        "-fobjc-arc",
        "-mmacosx-version-min=12.0",
        "-isysroot",
        sdk,
        f"-fmodules-cache-path={ROOT / 'build' / 'objc-module-cache'}",
        "-framework",
        "Cocoa",
        "-framework",
        "WebKit",
        "-o",
        str(NATIVE_WINDOW_OUTPUT),
        str(NATIVE_WINDOW_SOURCE),
    ]
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode:
        raise SystemExit(
            f"原生窗口组件编译失败（exit={completed.returncode}）：{' '.join(command)}"
        )
    return NATIVE_WINDOW_OUTPUT


def create_dmg(app_path: Path) -> Path:
    """Create a compressed DMG containing the app and Applications shortcut."""

    if platform.system() != "Darwin":
        raise SystemExit("--dmg 只能在 macOS 构建机上使用。")
    hdiutil = shutil.which("hdiutil")
    if not hdiutil:
        raise SystemExit("找不到 hdiutil，无法生成 macOS DMG。")
    if not app_path.is_dir():
        raise SystemExit(f"macOS .app 不存在：{app_path}")

    staging = ROOT / "build" / "dmg-staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    shutil.copytree(app_path, staging / app_path.name, symlinks=True)
    (staging / "Applications").symlink_to("/Applications")

    architecture = platform.machine()
    output = ROOT / "dist" / f"{APP_NAME}-mac-{architecture}.dmg"
    output.unlink(missing_ok=True)
    raw_image = ROOT / "build" / f"{APP_NAME}-{architecture}.hfs.dmg"
    raw_image.unlink(missing_ok=True)

    # hdiutil create normally attaches a temporary device.  That is blocked in
    # some CI/sandbox environments, while makehybrid + convert produces the
    # same mountable UDZO image without requiring a device attachment.
    hybrid_command = [
        hdiutil,
        "makehybrid",
        "-o",
        str(raw_image),
        "-hfs",
        "-hfs-volume-name",
        APP_NAME,
        str(staging),
    ]
    hybrid = subprocess.run(hybrid_command, cwd=ROOT)
    if hybrid.returncode:
        raise SystemExit(
            f"DMG 文件系统生成失败（exit={hybrid.returncode}）：{' '.join(hybrid_command)}"
        )

    convert_command = [
        hdiutil,
        "convert",
        str(raw_image),
        "-format",
        "UDZO",
        "-imagekey",
        "zlib-level=9",
        "-o",
        str(output),
    ]
    converted = subprocess.run(convert_command, cwd=ROOT)
    if converted.returncode:
        raise SystemExit(
            f"DMG 压缩失败（exit={converted.returncode}）：{' '.join(convert_command)}"
        )
    raw_image.unlink(missing_ok=True)
    return output


def main() -> int:
    args = parse_args()
    if not SPEC.is_file():
        raise SystemExit(f"PyInstaller spec not found: {SPEC}")

    system = platform.system()
    if args.dmg_only:
        if system != "Darwin":
            raise SystemExit("--dmg-only 只能在 macOS 构建机上使用。")
        if args.clean:
            raise SystemExit("--dmg-only 不能和 --clean 同时使用。")
        app_path = ROOT / "dist" / f"{APP_NAME}.app"
        print(f"macOS DMG: {create_dmg(app_path)}")
        return 0

    environment = os.environ.copy()
    if args.dmg and system != "Darwin":
        raise SystemExit("--dmg 只能在 macOS 构建机上使用。")
    if args.dmg and not (args.ffmpeg_dir or environment.get("SUBTRANS_FFMPEG_DIR")):
        raise SystemExit("生成 DMG 前必须通过 --ffmpeg-dir 或 SUBTRANS_FFMPEG_DIR 提供 FFmpeg。")

    if args.clean:
        for name in ("build", "dist"):
            shutil.rmtree(ROOT / name, ignore_errors=True)

    native_window = build_native_window()
    if native_window:
        environment["SUBTRANS_NATIVE_WINDOW"] = str(native_window)

    if args.ffmpeg_dir:
        environment["SUBTRANS_FFMPEG_DIR"] = str(args.ffmpeg_dir.expanduser().resolve())
    elif not environment.get("SUBTRANS_FFMPEG_DIR"):
        print(
            "warning: 未提供 FFmpeg，发布包将依赖用户机器上的 PATH；"
            "硬字幕功能可能不可用。",
            file=sys.stderr,
        )

    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", str(SPEC)]
    completed = subprocess.run(command, cwd=ROOT, env=environment)
    if completed.returncode:
        return completed.returncode

    if system == "Darwin":
        output = ROOT / "dist" / f"{APP_NAME}.app"
        if not output.is_dir():
            raise SystemExit(f"macOS .app 未生成：{output}")
        print(f"macOS app bundle: {output}")
        if args.dmg:
            print(f"macOS DMG: {create_dmg(output)}")
    elif system == "Windows":
        print(f"Windows bundle directory: {ROOT / 'dist' / APP_NAME}")
    else:
        print(f"Bundle directory: {ROOT / 'dist' / APP_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

