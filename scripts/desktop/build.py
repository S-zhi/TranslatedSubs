#!/usr/bin/env python3
"""Build a local arm64 app/DMG. No publication or implicit dependency installs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

try:
    import tomllib
except ImportError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts/desktop") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts/desktop"))
from native_build import build_native

MACHO = {bytes.fromhex(x) for x in ("feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca")}


def run(*args: object, **kwargs) -> str:
    return subprocess.run([str(a) for a in args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          **kwargs).stdout


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment):
        raise ValueError("A real HTTPS URL without credentials or fragment is required")
    if parsed.hostname in {"example.com", "example.org", "localhost"} or parsed.hostname.endswith(".invalid"):
        raise ValueError("Placeholder download/update URLs are forbidden")
    return value


def project_metadata() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    result = dict(project["tool"]["translatedsubs"]["desktop"])
    result["version"] = project["project"]["version"]
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", result["version"]):
        raise ValueError("Desktop release version must be stable major.minor.patch")
    return result


def checked_file(root: Path, relative: str, digest: str) -> Path:
    candidate = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Asset paths must be relative and contained")
    if candidate.is_symlink() or not candidate.is_file() or not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("Asset must be a regular file inside its distribution")
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or sha256(candidate) != digest:
        raise ValueError(f"SHA-256 mismatch: {relative}")
    return candidate


def validate_assets(directory: Path, archive: Path, kind: str) -> dict:
    """Inventory is supplied by the distributor; never guess a license/source hash."""
    inventory = json.loads((directory / "inventory.json").read_text())
    if inventory.get("schema") != 1 or inventory.get("kind") != kind:
        raise ValueError("Invalid asset inventory schema/kind")
    https_url(inventory["source_url"])
    digest = inventory["source_sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or sha256(archive) != digest:
        raise ValueError("Distribution source archive SHA-256 mismatch")
    if not inventory.get("license"):
        raise ValueError("Distribution license identifier is required")
    roles = set()
    names = set()
    for item in inventory["files"]:
        checked_file(directory, item["path"], item["sha256"])
        relative = Path(item["path"])
        if kind == "ffmpeg" and (relative.parts[0] not in {"bin", "lib", "licenses"}
                                 or item["role"] not in {"ffmpeg", "ffprobe", "library", "license"}):
            raise ValueError("FFmpeg inventory permits only bin, lib and licenses assets")
        if kind == "fonts" and (relative.suffix.lower() not in {".otf", ".ttf", ".ttc"}
                                if item["role"] == "font" else item["role"] != "license"):
            raise ValueError("Font inventory permits only font files and license notices")
        if item["path"] in names:
            raise ValueError("Duplicate inventory path")
        names.add(item["path"])
        roles.add(item["role"])
    required = {"ffmpeg", "ffprobe", "license"} if kind == "ffmpeg" else {"font", "license"}
    if not required <= roles:
        raise ValueError(f"Inventory missing roles: {sorted(required - roles)}")
    if kind == "fonts" and not {"en", "zh-CN"} <= set(inventory.get("languages", [])):
        raise ValueError("Font distribution must declare English and simplified Chinese coverage")
    if kind == "ffmpeg":
        for executable in ("ffmpeg", "ffprobe"):
            matches = [x for x in inventory["files"] if x["role"] == executable]
            if len(matches) != 1 or matches[0]["path"] != f"bin/{executable}":
                raise ValueError("FFmpeg binaries must be bin/ffmpeg and bin/ffprobe")
        if any(item["path"].startswith("bin/") and item["role"] not in {"ffmpeg", "ffprobe"}
               for item in inventory["files"]):
            raise ValueError("Unexpected executable in FFmpeg distribution")
    return inventory


def copy_assets(directory: Path, inventory: dict, destination: Path) -> None:
    for item in inventory["files"]:
        source = checked_file(directory, item["path"], item["sha256"])
        target = destination / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        return stream.read(4) in MACHO


def audit_macho(app: Path) -> list[dict]:
    """Reject outside dylibs and unresolved rpaths, including Homebrew leakage."""
    root = app.resolve()
    minimum = tuple(int(n) for n in project_metadata()["minimum-macos"].split("."))
    results = []
    for path in sorted(app.rglob("*")):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise ValueError(f"Bundle symlink escapes app: {path}")
        if not is_macho(path):
            continue
        arches = run("lipo", "-archs", path).split()
        if "arm64" not in arches:
            raise ValueError(f"Non-arm64 binary: {path}")
        # Executable-relative paths in Python extensions belong to the backend.
        executable_dir = (app / "Contents/Resources/backend" if
                          path.is_relative_to(app / "Contents/Resources/backend") else path.parent)
        def expand(value):
            return Path(value.replace("@loader_path", str(path.parent))
                        .replace("@executable_path", str(executable_dir)))
        commands = run("otool", "-l", path)
        targets = re.findall(r"\bminos (\d+\.\d+(?:\.\d+)?)", commands)
        targets += re.findall(r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+\.\d+(?:\.\d+)?)", commands)
        if any(tuple((list(map(int, target.split("."))) + [0, 0])[:2]) > minimum[:2]
               for target in targets):
            raise ValueError(f"Binary minimum macOS exceeds configured target: {path.name}: {targets}")
        rpaths = re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset", commands)
        # Swift binaries carry a default /usr/lib/swift rpath; that is a system
        # path the bundle may rely on. Only paths outside the system, or that are
        # neither bundle-relative, make the binary non-relocatable.
        if any(not r.startswith(("@loader_path", "@executable_path", "/usr/lib/", "/System/Library/"))
               for r in rpaths):
            raise ValueError(f"Non-relocatable LC_RPATH in {path.name}")
        dependencies = []
        for line in run("otool", "-L", path).splitlines()[1:]:
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith(("/usr/lib/", "/System/Library/")):
                continue
            if not dependency.startswith(("@rpath/", "@loader_path/", "@executable_path/")):
                raise ValueError(f"Unresolved/external non-relocatable dependency in {path.name}: {dependency}")
            if dependency.startswith("@rpath/"):
                candidates = [expand(r) / dependency[len("@rpath/"):] for r in rpaths]
                # dyld inherits LC_RPATH from the loading executable, not arbitrary host dirs.
                if executable_dir == app / "Contents/Resources/backend":
                    exe = executable_dir / "translatedsubs-backend"
                    inherited = re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset", run("otool", "-l", exe))
                    candidates += [Path(r.replace("@loader_path", str(executable_dir)).replace("@executable_path", str(executable_dir))) / dependency[7:] for r in inherited]
            else:
                candidates = [expand(dependency)]
            if not any(p.exists() and p.resolve().is_relative_to(root) for p in candidates):
                raise ValueError(f"Unresolved/external dependency in {path.name}: {dependency}")
            dependencies.append(dependency)
        results.append({"path": str(path.relative_to(app)), "architectures": arches,
                        "dependencies": dependencies, "minimum_macos": targets})
    if not results:
        raise ValueError("Bundle contains no Mach-O executables")
    return results


def signing_plan(app: Path, identity: str) -> list[list[str]]:
    entitlements = ROOT / "desktop/packaging/entitlements.plist"
    paths = [p for p in app.rglob("*") if is_macho(p) or
             (p.is_dir() and not p.is_symlink() and p.suffix in {".framework", ".app", ".xpc"})]
    paths.sort(key=lambda p: len(p.parts), reverse=True)
    return [["codesign", "--force", "--sign", identity, "--options", "runtime",
             *( ["--timestamp"] if identity != "-" else []),
             "--entitlements", str(entitlements), str(path)] for path in [*paths, app]]


def notarize(path: Path, profile: str) -> None:
    result = json.loads(run("xcrun", "notarytool", "submit", path,
                            "--keychain-profile", profile, "--wait", "--output-format", "json"))
    if result.get("status") != "Accepted":
        raise ValueError("Apple notarization was not Accepted")


def update_manifest(metadata: dict, dmg: Path, url: str, notes: str) -> dict:
    return {"version": metadata["version"], "release_notes": notes,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "platforms": {"macos-arm64": {"url": https_url(url), "sha256": sha256(dmg),
                                           "minimum_macos": metadata["minimum-macos"]}}}


def preflight(args: argparse.Namespace) -> tuple[dict | None, dict]:
    if args.mode == "release":
        for field in ("identity", "notary_profile", "update_url", "download_url", "ffmpeg_dir", "ffmpeg_archive", "release_notes"):
            if not getattr(args, field):
                raise ValueError(f"Release requires --{field.replace('_', '-')}")
        if not args.identity.startswith("Developer ID Application:"):
            raise ValueError("Release requires a Developer ID Application identity")
        if not args.release_notes.is_file() or not args.release_notes.read_text().strip():
            raise ValueError("Release requires a readable, non-empty release notes file")
        if args.dev_ffmpeg or args.dev_ffprobe:
            raise ValueError("Release forbids development binaries")
    for value in (args.update_url, args.download_url):
        if value:
            https_url(value)
    if bool(args.ffmpeg_dir) != bool(args.ffmpeg_archive):
        raise ValueError("--ffmpeg-dir and --ffmpeg-archive must be supplied together")
    if args.ffmpeg_dir:
        if args.dev_ffmpeg or args.dev_ffprobe:
            raise ValueError("Choose verified distribution OR explicit development tools")
        ffmpeg = validate_assets(args.ffmpeg_dir, args.ffmpeg_archive, "ffmpeg")
    else:
        ffmpeg = None
        if not args.dev_ffmpeg or not args.dev_ffprobe:
            raise ValueError("Development build needs verified distribution or both explicit dev binaries")
        for binary in (args.dev_ffmpeg, args.dev_ffprobe):
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise ValueError("Development binary must exist and be executable")
    fonts = validate_assets(args.font_dir, args.font_archive, "fonts")
    return ffmpeg, fonts


def validate_ffmpeg_buildconf(buildconf: str) -> None:
    """Check configure arguments, not substrings in other argument values."""
    arguments = shlex.split(buildconf)
    if "--enable-libfontconfig" not in arguments or "--disable-libfontconfig" in arguments:
        raise ValueError("FFmpeg must enable libfontconfig to use the bundled CJK fonts")


def build(args: argparse.Namespace) -> Path:
    metadata = project_metadata()
    ffmpeg, fonts = preflight(args)
    if ((args.output / f'{metadata["app-name"]}.app').exists() or
            list(args.output.glob("*.dmg")) or (args.output / "update-manifest.json").exists()):
        raise ValueError("Output exists; choose a fresh output directory")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("Build requires a native Apple Silicon macOS host")
    if sys.version_info < (3, 11):
        raise ValueError("Packaging driver requires Python 3.11 or 3.12")
    # Report missing build dependencies before producing any app.
    for dependency in ("pyinstaller", "torch", "transformers", "sentencepiece", "sacremoses", "ctranslate2"):
        importlib.metadata.version(dependency)
    if args.mode == "release":
        identities = run("security", "find-identity", "-v", "-p", "codesigning")
        if args.identity not in identities:
            raise ValueError("Requested signing identity is not available in the keychain")
        run("xcrun", "notarytool", "history", "--keychain-profile", args.notary_profile,
            "--output-format", "json")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="desktop-", dir=output) as temporary:
        staging = Path(temporary)
        run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", staging / "backend-dist", "--workpath", staging / "pyinstaller",
            ROOT / "desktop/packaging/backend.spec", cwd=ROOT)
        native = args.native_executable
        if native is None:
            native = build_native(ROOT / "desktop/macos", staging / metadata["swift-product"],
                                  mode=args.native_build_mode, minimum_macos=metadata["minimum-macos"],
                                  product=metadata["swift-product"])
        volume = staging / "volume"
        app = volume / f'{metadata["app-name"]}.app'
        resources = app / "Contents/Resources"
        (app / "Contents/MacOS").mkdir(parents=True)
        resources.mkdir(parents=True)
        shutil.copy2(native, app / "Contents/MacOS" / metadata["swift-product"])
        shutil.copytree(staging / "backend-dist/translatedsubs-backend", resources / "backend", symlinks=True)
        if not (resources / "backend/_internal/web/index.html").is_file():
            raise ValueError("Frozen backend is missing Web resources")
        if ffmpeg:
            copy_assets(args.ffmpeg_dir, ffmpeg, resources)
        else:
            (resources / "bin").mkdir()
            shutil.copy2(args.dev_ffmpeg, resources / "bin/ffmpeg")
            shutil.copy2(args.dev_ffprobe, resources / "bin/ffprobe")
        copy_assets(args.font_dir, fonts, resources / "fonts")
        (resources / "fonts/fonts.conf").write_text('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd"><fontconfig><dir prefix="relative">.</dir><cachedir prefix="xdg">fontconfig</cachedir></fontconfig>\n')
        filters = run(resources / "bin/ffmpeg", "-hide_banner", "-filters")
        if not re.search(r"\bsubtitles\b", filters):
            raise ValueError("FFmpeg lacks the libass subtitles filter")
        validate_ffmpeg_buildconf(run(resources / "bin/ffmpeg", "-buildconf"))
        run(resources / "bin/ffprobe", "-version")
        info = {"CFBundleName": metadata["app-name"], "CFBundleDisplayName": metadata["app-name"],
                "CFBundleExecutable": metadata["swift-product"], "CFBundleIdentifier": metadata["bundle-id"],
                "CFBundlePackageType": "APPL", "CFBundleShortVersionString": metadata["version"],
                "CFBundleVersion": metadata["version"], "LSMinimumSystemVersion": metadata["minimum-macos"],
                "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication",
                "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True}}
        if args.update_url:
            info["SUBTRANS_DESKTOP_UPDATE_URL"] = args.update_url
        with (app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump(info, stream)
        inventory = {"mode": args.mode, "version": metadata["version"], "ffmpeg": ffmpeg or {"development_only": True},
                     "fonts": fonts, "python_distributions": sorted(
                         [{"name": d.metadata["Name"], "version": d.version,
                           "license": d.metadata.get("License-Expression") or d.metadata.get("License", "UNKNOWN")}
                          for d in importlib.metadata.distributions()], key=lambda d: d["name"])}
        (resources / "source-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
        shutil.copy2(ROOT / "uv.lock", resources / "python-dependencies.lock")
        shutil.copy2(ROOT / "LICENSE", resources / "TranslateSub-LICENSE.txt")
        audit = audit_macho(app)
        (resources / "dependency-audit.json").write_text(json.dumps(audit, indent=2) + "\n")
        for command in signing_plan(app, args.identity if args.mode == "release" else "-"):
            run(*command)
        run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app)
        smoke_environment = {key: value for key, value in os.environ.items()
                             if not key.startswith(("PYTHON", "SUBTRANS_", "DYLD_"))}
        smoke_environment.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        run(resources / "backend/translatedsubs-backend", "--packaging-self-test",
            env=smoke_environment, cwd=staging, timeout=180)
        if args.mode == "release":
            archive = staging / "notarize.zip"
            run("ditto", "-c", "-k", "--keepParent", app, archive)
            notarize(archive, args.notary_profile)
            run("xcrun", "stapler", "staple", app)
            run("xcrun", "stapler", "validate", app)
            run("spctl", "--assess", "--type", "execute", "--verbose=2", app)
        (volume / "Applications").symlink_to("/Applications")
        basename = f'{metadata["app-name"]}-{metadata["version"]}-macos-arm64-{args.mode}'
        dmg = staging / f"{basename}.dmg"
        run("hdiutil", "create", "-volname", metadata["app-name"], "-srcfolder", volume,
            "-format", "UDZO", "-ov", dmg)
        if args.mode == "release":
            run("codesign", "--force", "--sign", args.identity, "--timestamp", dmg)
            notarize(dmg, args.notary_profile)
            run("xcrun", "stapler", "staple", dmg)
            run("xcrun", "stapler", "validate", dmg)
            run("spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=2", dmg)
        run("hdiutil", "verify", dmg)
        final = output / dmg.name
        if final.exists() or (output / app.name).exists():
            raise ValueError("Output exists; choose a fresh output directory")
        shutil.copy2(dmg, final)
        shutil.copytree(app, output / app.name, symlinks=True)
        (output / f"{basename}.sha256").write_text(f"{sha256(final)}  {final.name}\n")
        if args.mode == "release":
            notes = args.release_notes.read_text()
            (output / "update-manifest.json").write_text(json.dumps(
                update_manifest(metadata, final, args.download_url, notes), indent=2) + "\n")
        return final


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--mode", choices=("dev", "release"), default="dev")
    for name in ("ffmpeg-dir", "ffmpeg-archive", "dev-ffmpeg", "dev-ffprobe", "release-notes"):
        result.add_argument(f"--{name}", type=Path)
    native = result.add_mutually_exclusive_group()
    native.add_argument("--native-executable", type=Path)
    native.add_argument("--native-build-mode", choices=("swiftpm", "swiftc"), default="swiftpm",
                        help="Default: SwiftPM; swiftc explicitly uses CLT and statically links DesktopCore")
    result.add_argument("--font-dir", type=Path, required=True)
    result.add_argument("--font-archive", type=Path, required=True)
    result.add_argument("--output", type=Path, default=ROOT / "dist/desktop")
    for name in ("identity", "notary-profile", "update-url", "download-url"):
        result.add_argument(f"--{name}")
    return result


if __name__ == "__main__":
    try:
        print(build(parser().parse_args()))
    except (ValueError, OSError, KeyError, importlib.metadata.PackageNotFoundError,
            subprocess.CalledProcessError) as exc:
        # Do not dump subprocess arguments/output: signing tools can include account details.
        print(f"Desktop build failed: {type(exc).__name__}: " +
              (str(exc) if not isinstance(exc, subprocess.CalledProcessError) else "external command failed"), file=sys.stderr)
        sys.exit(1)
