#!/usr/bin/env python3
"""Explicit native builds; no toolchain selection, installation, or GUI launch."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


CLT = Path("/Library/Developer/CommandLineTools")


def safe_diagnostic(stderr: str | bytes | None) -> str:
    """Keep compiler diagnostics, but omit source excerpts and sensitive lines."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    lines = []
    for line in (stderr or "").splitlines():
        # Compiler excerpts can contain source literals; command echoes and URLs
        # can contain credentials. Only keep actual diagnostic summary lines.
        if not re.search(r"\b(error|warning|note|fatal error):", line, re.I):
            continue
        if re.search(r"token|secret|password|credential|authorization|api.?key|https?://", line, re.I):
            lines.append("[sensitive diagnostic omitted]")
            continue
        for key, value in os.environ.items():
            if value and re.search(r"token|secret|password|credential|api.?key", key, re.I):
                line = line.replace(value, "[redacted]")
        lines.append(line)
    return "\n".join(lines)[:6000] or "No safe diagnostic summary available."


def run_native(*args: object) -> str:
    try:
        return subprocess.run([str(arg) for arg in args], check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Native {Path(str(args[0])).name} failed (exit {exc.returncode}):\n"
                         f"{safe_diagnostic(exc.stderr)}") from None


def swiftc_commands(package: Path, staging: Path, minimum_macos: str,
                    swiftc: Path, sdk: Path) -> list[list[str]]:
    """WMO emits one object for the entire core, avoiding per-file duplicates."""
    if not re.fullmatch(r"\d+\.\d+", minimum_macos):
        raise ValueError("Minimum macOS must be major.minor")
    core = sorted((package / "Sources/DesktopCore").glob("*.swift"))
    app = sorted((package / "Sources/TranslateSub").glob("*.swift"))
    if not core or not app:
        raise ValueError("DesktopCore and TranslateSub Swift sources are required")
    common = [str(swiftc), "-O", "-swift-version", "5", "-target",
              f"arm64-apple-macosx{minimum_macos}", "-sdk", str(sdk),
              "-module-cache-path", str(staging / "module-cache"), "-parse-as-library"]
    return [
        [*common, "-whole-module-optimization", "-emit-object", "-emit-module",
         "-module-name", "DesktopCore", "-emit-module-path", str(staging / "DesktopCore.swiftmodule"),
         *map(str, core), "-o", str(staging / "DesktopCore.o")],
        [*common, "-module-name", "TranslateSub", "-I", str(staging),
         *map(str, app), str(staging / "DesktopCore.o"), "-o", str(staging / "TranslateSub")],
    ]


def verify_system_linkage(executable: Path, runner=run_native) -> None:
    dependencies = runner("otool", "-L", executable).splitlines()[1:]
    if not dependencies:
        raise ValueError("Native dependency audit returned no load commands")
    for line in dependencies:
        name = line.strip().split(" (", 1)[0]
        if not name.startswith(("/usr/lib/", "/System/Library/")):
            raise ValueError("Native executable has a non-system dynamic dependency")
    commands = runner("otool", "-l", executable)
    rpaths = re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset", commands)
    # The Swift toolchain adds /usr/lib/swift to every macOS binary; it is a
    # system path the binary is entitled to use. Only search paths that point
    # outside the system make the executable non-relocatable inside a bundle.
    if any(not r.startswith(("/usr/lib/", "/System/Library/")) for r in rpaths):
        raise ValueError("Native executable has a non-relocatable LC_RPATH")


def build_native(package: Path, output: Path, *, mode: str = "swiftpm",
                 minimum_macos: str = "14.0", product: str = "TranslateSub",
                 runner=run_native) -> Path:
    if mode not in {"swiftpm", "swiftc"}:
        raise ValueError("Unknown native build mode")
    output = output.resolve()
    if output.exists():
        raise ValueError("Native output exists; choose a fresh output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native-", dir=output.parent) as temporary:
        staging = Path(temporary)
        if mode == "swiftpm":
            command = ["swift", "build", "--package-path", package, "--scratch-path", staging / "swift-build",
                       "--configuration", "release", "--arch", "arm64"]
            runner(*command, "--product", product)
            executable = Path(runner(*command, "--show-bin-path").strip()) / product
        else:
            if product != "TranslateSub":
                raise ValueError("Direct native mode supports only TranslateSub")
            swiftc = CLT / "usr/bin/swiftc"
            sdk = CLT / "SDKs/MacOSX.sdk"
            if not swiftc.is_file() or not sdk.is_dir():
                raise ValueError("Explicit swiftc mode requires installed CLT swiftc and macOS SDK")
            for command in swiftc_commands(package, staging, minimum_macos, swiftc, sdk):
                runner(*command)
            executable = staging / product
            verify_system_linkage(executable, runner)
        shutil.copy2(executable, output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("swiftpm", "swiftc"), default="swiftpm")
    parser.add_argument("--output", type=Path, required=True, help="Executable output path")
    args = parser.parse_args()
    # Use the same metadata as the bundle driver; no second deployment target.
    from build import ROOT, project_metadata
    metadata = project_metadata()
    try:
        print(build_native(ROOT / "desktop/macos", args.output, mode=args.mode,
                           minimum_macos=metadata["minimum-macos"], product=metadata["swift-product"]))
    except (ValueError, OSError) as exc:
        print(f"Native build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
