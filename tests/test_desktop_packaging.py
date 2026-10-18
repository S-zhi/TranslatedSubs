"""Offline packaging contracts. Mock Apple tools, never sign or fetch real media."""
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import stat
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desktop_packaging", ROOT / "scripts/desktop/build.py")
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)


def distribution(tmp_path, kind):
    directory = tmp_path / kind
    directory.mkdir()
    archive = tmp_path / f"{kind}.zip"
    archive.write_bytes(b"verified upstream source archive fixture")
    files = {"licenses/LICENSE.txt": ("license", b"test license")}
    if kind == "ffmpeg":
        files.update({"bin/ffmpeg": ("ffmpeg", bytes.fromhex("cffaedfe")),
                      "bin/ffprobe": ("ffprobe", bytes.fromhex("cffaedfe"))})
    else:
        files["NotoSansCJKsc.otf"] = ("font", b"fixture font")
    inventory = {"schema": 1, "kind": kind, "source_url": "https://assets.translatedsubs.test/source.zip",
                 "source_sha256": packaging.sha256(archive), "license": "OFL-1.1" if kind == "fonts" else "LGPL-2.1-or-later",
                 "languages": ["en", "zh-CN"], "files": []}
    for name, (role, content) in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if role in {"ffmpeg", "ffprobe"}:
            path.chmod(0o755)
        inventory["files"].append({"path": name, "role": role, "sha256": packaging.sha256(path)})
    (directory / "inventory.json").write_text(json.dumps(inventory))
    return directory, archive, inventory


def arguments(tmp_path, mode="dev"):
    ffmpeg, ffmpeg_archive, _ = distribution(tmp_path, "ffmpeg")
    fonts, font_archive, _ = distribution(tmp_path, "fonts")
    native = tmp_path / "TranslateSub"
    native.write_bytes(bytes.fromhex("cffaedfe"))
    native.chmod(0o755)
    notes = tmp_path / "notes.md"
    notes.write_text("Desktop release")
    return packaging.parser().parse_args([
        "--mode", mode, "--ffmpeg-dir", str(ffmpeg), "--ffmpeg-archive", str(ffmpeg_archive),
        "--font-dir", str(fonts), "--font-archive", str(font_archive),
        "--native-executable", str(native), "--output", str(tmp_path / "output"),
        "--identity", "Developer ID Application: Fixture (TEAM)", "--notary-profile", "fixture",
        "--update-url", "https://downloads.translatedsubs.test/update.json",
        "--download-url", "https://downloads.translatedsubs.test/app.dmg", "--release-notes", str(notes)])


def test_metadata_single_source():
    data = packaging.project_metadata()
    assert data["version"] == "1.0.2"
    assert data["app-name"] == data["swift-product"] == "TranslateSub"
    assert data["minimum-macos"] == "14.0"
    assert "platforms: [.macOS(.v14)]" in (ROOT / "desktop/macos/Package.swift").read_text()


@pytest.mark.parametrize("url", ["http://host.test/a", "https://user:secret@host.test/a", "https://example.com/a", "file:///tmp/a", "https://host.test/a#hash"])
def test_reject_bad_urls(url):
    with pytest.raises(ValueError):
        packaging.https_url(url)


def test_inventory_checks_archive_and_every_file(tmp_path):
    directory, archive, _ = distribution(tmp_path, "ffmpeg")
    packaging.validate_assets(directory, archive, "ffmpeg")
    (directory / "bin/ffmpeg").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256"):
        packaging.validate_assets(directory, archive, "ffmpeg")


def test_reject_wrong_source_archive(tmp_path):
    directory, archive, _ = distribution(tmp_path, "fonts")
    archive.write_bytes(b"substituted source")
    with pytest.raises(ValueError, match="source archive"):
        packaging.validate_assets(directory, archive, "fonts")


@pytest.mark.parametrize("path", ["../outside", "/tmp/outside"])
def test_reject_inventory_path_escape(tmp_path, path):
    with pytest.raises(ValueError, match="relative"):
        packaging.checked_file(tmp_path, path, "a" * 64)


def test_reject_symlink_asset(tmp_path):
    (tmp_path / "target").write_bytes(b"data")
    (tmp_path / "linked").symlink_to(tmp_path / "target")
    with pytest.raises(ValueError, match="regular"):
        packaging.checked_file(tmp_path, "linked", packaging.sha256(tmp_path / "target"))


def test_cjk_fonts_required(tmp_path):
    directory, archive, inventory = distribution(tmp_path, "fonts")
    inventory["languages"] = ["en"]
    (directory / "inventory.json").write_text(json.dumps(inventory))
    with pytest.raises(ValueError, match="Chinese"):
        packaging.validate_assets(directory, archive, "fonts")


@pytest.mark.parametrize("field", ["identity", "notary_profile", "update_url", "download_url", "ffmpeg_dir", "ffmpeg_archive", "release_notes"])
def test_release_fails_closed_missing_prerequisite(tmp_path, field):
    args = arguments(tmp_path, "release")
    setattr(args, field, None)
    with pytest.raises(ValueError, match="Release requires"):
        packaging.preflight(args)


def test_release_rejects_development_tools(tmp_path):
    args = arguments(tmp_path, "release")
    args.dev_ffmpeg = args.ffmpeg_dir / "bin/ffmpeg"
    with pytest.raises(ValueError, match="forbids"):
        packaging.preflight(args)


def test_explicit_dev_tools_allowed_without_distribution(tmp_path):
    args = arguments(tmp_path)
    args.dev_ffmpeg = args.ffmpeg_dir / "bin/ffmpeg"
    args.dev_ffprobe = args.ffmpeg_dir / "bin/ffprobe"
    args.ffmpeg_dir = args.ffmpeg_archive = None
    assert packaging.preflight(args)[0] is None


def test_sign_nested_before_app_with_hardened_runtime(tmp_path):
    app = tmp_path / "TranslateSub.app"
    nested = app / "Contents/Resources/backend/_internal/libtorch.dylib"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(bytes.fromhex("cffaedfe"))
    plan = packaging.signing_plan(app, "Developer ID Application: Fixture")
    assert plan[0][-1] == str(nested)
    assert plan[-1][-1] == str(app)
    assert all("--timestamp" in cmd and "runtime" in cmd for cmd in plan)
    assert all("--deep" not in cmd for cmd in plan)


def test_audit_rejects_host_dependency(tmp_path, monkeypatch):
    app = tmp_path / "App.app"
    app.mkdir()
    (app / "executable").write_bytes(bytes.fromhex("cffaedfe"))
    def command(*args, **kwargs):
        if args[0] == "lipo":
            return "arm64\n"
        if args[1] == "-l":
            return ""
        return "binary:\n /opt/homebrew/lib/libavcodec.dylib (compatibility version 1)\n"
    monkeypatch.setattr(packaging, "run", command)
    with pytest.raises(ValueError, match="Unresolved/external"):
        packaging.audit_macho(app)


def test_audit_accepts_contained_loader_path(tmp_path, monkeypatch):
    app = tmp_path / "App.app"
    app.mkdir()
    (app / "executable").write_bytes(bytes.fromhex("cffaedfe"))
    (app / "library.dylib").write_bytes(bytes.fromhex("cffaedfe"))
    def command(*args, **kwargs):
        if args[0] == "lipo":
            return "arm64\n"
        if args[1] == "-l":
            return ""
        return "binary:\n @loader_path/library.dylib (compatibility version 1)\n /usr/lib/libSystem.B.dylib (compatibility version 1)\n"
    monkeypatch.setattr(packaging, "run", command)
    assert len(packaging.audit_macho(app)) == 2


def test_notarization_rejection_is_not_success(monkeypatch, tmp_path):
    monkeypatch.setattr(packaging, "run", lambda *a, **k: '{"status":"Invalid"}')
    with pytest.raises(ValueError, match="not Accepted"):
        packaging.notarize(tmp_path / "app.zip", "fixture")


def mocked_build(monkeypatch, tmp_path, mode, notary_status="Accepted"):
    args = arguments(tmp_path, mode)
    commands = []
    monkeypatch.setattr(packaging.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(packaging.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(packaging.importlib.metadata, "version", lambda _: "1")
    monkeypatch.setattr(packaging.importlib.metadata, "distributions", lambda: [])
    monkeypatch.setattr(packaging, "audit_macho", lambda _: [{"path": "mocked-audit"}])
    def command(*values, **kwargs):
        values = [str(v) for v in values]
        commands.append(values)
        if "PyInstaller" in values:
            directory = Path(values[values.index("--distpath") + 1]) / "translatedsubs-backend"
            (directory / "_internal/web").mkdir(parents=True)
            (directory / "_internal/web/index.html").write_text("fixture")
            (directory / "translatedsubs-backend").write_bytes(bytes.fromhex("cffaedfe"))
        if "-filters" in values:
            return "... subtitles libass\n"
        if "-buildconf" in values:
            return "configuration:\n  --enable-libass\n  --enable-libfontconfig\n"
        if values[:2] == ["security", "find-identity"]:
            return args.identity
        if values[:3] == ["xcrun", "notarytool", "submit"]:
            return json.dumps({"status": notary_status})
        if values[:2] == ["hdiutil", "create"]:
            Path(values[-1]).write_bytes(b"DMG before stapling")
        if values[:3] == ["xcrun", "stapler", "staple"] and values[-1].endswith(".dmg"):
            Path(values[-1]).write_bytes(b"DMG with ticket")
        return ""
    monkeypatch.setattr(packaging, "run", command)
    return args, commands


def test_dev_build_contract_and_no_notarization(tmp_path, monkeypatch):
    args, commands = mocked_build(monkeypatch, tmp_path, "dev")
    dmg = packaging.build(args)
    resources = args.output / "TranslateSub.app/Contents/Resources"
    assert dmg.is_file()
    assert (resources / "backend/translatedsubs-backend").is_file()
    assert (resources / "bin/ffmpeg").is_file()
    assert (resources / "fonts/NotoSansCJKsc.otf").is_file()
    assert (resources / "fonts/fonts.conf").is_file()
    assert not any("submit" in cmd for cmd in commands)
    assert not (args.output / "update-manifest.json").exists()
    info = plistlib.loads((resources.parent / "Info.plist").read_bytes())
    assert info["CFBundleShortVersionString"] == packaging.project_metadata()["version"]


def test_release_manifest_hashes_final_stapled_dmg(tmp_path, monkeypatch):
    args, commands = mocked_build(monkeypatch, tmp_path, "release")
    dmg = packaging.build(args)
    manifest = json.loads((args.output / "update-manifest.json").read_text())
    assert dmg.read_bytes() == b"DMG with ticket"
    assert manifest["platforms"]["macos-arm64"]["sha256"] == packaging.sha256(dmg)
    assert manifest["platforms"]["macos-arm64"]["url"] == args.download_url
    assert len([cmd for cmd in commands if cmd[:3] == ["xcrun", "notarytool", "submit"]]) == 2
    assert len([cmd for cmd in commands if cmd[:3] == ["xcrun", "stapler", "validate"]]) == 2


def test_rejected_notarization_produces_no_release_artifact(tmp_path, monkeypatch):
    args, _ = mocked_build(monkeypatch, tmp_path, "release", "Invalid")
    with pytest.raises(ValueError, match="not Accepted"):
        packaging.build(args)
    assert not list(args.output.glob("*.dmg"))
    assert not (args.output / "update-manifest.json").exists()


def test_shell_syntax_and_manual_ci_only():
    subprocess.run(["bash", "-n", str(ROOT / "scripts/desktop/ci-build.sh")], check=True)
    workflow = (ROOT / ".github/workflows/macos-desktop.yml").read_text()
    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow and "pull_request:" not in workflow
    assert "contents: write" not in workflow
    assert "upload-artifact" in workflow


def test_audit_rejects_absolute_bundle_paths_after_relocation(tmp_path, monkeypatch):
    app = tmp_path / "App.app"
    app.mkdir()
    binary = app / "binary"
    binary.write_bytes(bytes.fromhex("cffaedfe"))
    monkeypatch.setattr(packaging, "run", lambda *args, **kw: "arm64" if args[0] == "lipo" else
                        ("" if args[1] == "-l" else f"binary:\n {binary} (compatibility version 1)\n"))
    with pytest.raises(ValueError, match="non-relocatable"):
        packaging.audit_macho(app)


def test_audit_rejects_newer_minimum_os(tmp_path, monkeypatch):
    app = tmp_path / "App.app"
    app.mkdir()
    (app / "binary").write_bytes(bytes.fromhex("cffaedfe"))
    monkeypatch.setattr(packaging, "run", lambda *args, **kw: "arm64" if args[0] == "lipo" else
                        "cmd LC_BUILD_VERSION\n cmdsize 32\n platform 1\n minos 15.0\n")
    with pytest.raises(ValueError, match="minimum macOS"):
        packaging.audit_macho(app)


def test_frozen_smoke_is_offline_and_runs_after_signing(tmp_path, monkeypatch):
    args, commands = mocked_build(monkeypatch, tmp_path, "dev")
    packaging.build(args)
    smoke = next(i for i, cmd in enumerate(commands) if "--packaging-self-test" in cmd)
    verify = next(i for i, cmd in enumerate(commands) if cmd[:2] == ["codesign", "--verify"])
    assert smoke > verify


def test_release_missing_notes_file_fails_preflight(tmp_path):
    args = arguments(tmp_path, "release")
    args.release_notes.unlink()
    with pytest.raises(ValueError, match="readable"):
        packaging.preflight(args)


def test_existing_output_rejected_before_signing_or_upload(tmp_path, monkeypatch):
    args, commands = mocked_build(monkeypatch, tmp_path, "release")
    args.output.mkdir()
    (args.output / "older.dmg").write_bytes(b"old")
    with pytest.raises(ValueError, match="Output exists"):
        packaging.build(args)
    assert commands == []


@pytest.mark.parametrize("buildconf", [
    "configuration:\n  --enable-libass\n  --enable-libfontconfig\n",
    "--prefix='/path with spaces' '--enable-libfontconfig'",
])
def test_ffmpeg_accepts_real_fontconfig_token(buildconf):
    packaging.validate_ffmpeg_buildconf(buildconf)


@pytest.mark.parametrize("buildconf", [
    "--enable-fontconfig", "--disable-libfontconfig", "",
    "--enable-libfontconfig-extra", "--enable-libfontconfig=no",
    "--extra-cflags=--enable-libfontconfig",
    "--enable-libfontconfig --disable-libfontconfig",
])
def test_ffmpeg_rejects_absent_disabled_or_substring_fontconfig(buildconf):
    with pytest.raises(ValueError, match="libfontconfig"):
        packaging.validate_ffmpeg_buildconf(buildconf)


def test_native_override_with_scratch_rpath_is_rejected(tmp_path, monkeypatch):
    app = tmp_path / "App.app"
    binary = app / "Contents/MacOS/TranslateSub"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(bytes.fromhex("cffaedfe"))
    def command(*args, **kwargs):
        if args[0] == "lipo":
            return "arm64"
        if args[1] == "-l":
            return "cmd LC_RPATH\n cmdsize 128\n path /scratch/native-check (offset 12)\n"
        return "binary:\n /scratch/native-check/libDesktopCore.dylib (compatibility version 0)\n"
    monkeypatch.setattr(packaging, "run", command)
    with pytest.raises(ValueError, match="Non-relocatable LC_RPATH"):
        packaging.audit_macho(app)


@pytest.fixture
def asset_preparer(monkeypatch):
    # prepare_assets is also a direct script and imports its sibling as `build`.
    monkeypatch.setitem(sys.modules, "build", packaging)
    spec = importlib.util.spec_from_file_location("desktop_prepare_assets", ROOT / "scripts/desktop/prepare_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_spec(tmp_path, format):
    """Build real local archives; all network reads remain test doubles."""
    result, payloads = {}, {}
    for kind in ("ffmpeg", "fonts"):
        directory, archive, inventory = distribution(tmp_path, kind)
        if format == "zip":
            with zipfile.ZipFile(archive, "w") as bundle:
                for item in inventory["files"]:
                    bundle.write(directory / item["path"], item["path"])
        else:
            with tarfile.open(archive, "w") as bundle:
                for item in inventory["files"]:
                    bundle.add(directory / item["path"], arcname=item["path"])
        inventory["source_url"] = f"https://assets.translatedsubs.test/{kind}.{format}"
        inventory["source_sha256"] = packaging.sha256(archive)
        result[kind] = {"url": inventory["source_url"], "sha256": inventory["source_sha256"], "inventory": inventory}
        payloads[inventory["source_url"]] = archive.read_bytes()
    return result, payloads


def mock_asset_download(monkeypatch, preparer, payloads):
    calls = []
    def download(url, timeout):
        calls.append(url)
        response = io.BytesIO(payloads[url])
        response.url = url
        return response
    monkeypatch.setattr(preparer, "urlopen", download)
    return calls


@pytest.mark.parametrize("format", ["zip", "tar"])
def test_prepare_assets_verifies_and_extracts_real_archives(tmp_path, monkeypatch, asset_preparer, format):
    spec, payloads = archive_spec(tmp_path, format)
    calls = mock_asset_download(monkeypatch, asset_preparer, payloads)
    output = tmp_path / "prepared"
    asset_preparer.prepare(spec, output)
    assert len(calls) == 2
    for kind in ("ffmpeg", "fonts"):
        assert packaging.validate_assets(output / kind, output / f"{kind}.archive", kind) == spec[kind]["inventory"]
    assert (output / "ffmpeg/bin/ffmpeg").stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("field,value", [("source_url", "https://different.test/ffmpeg.zip"), ("source_sha256", "0" * 64)])
def test_prepare_assets_rejects_mismatched_source_before_download(tmp_path, monkeypatch, asset_preparer, field, value):
    spec, payloads = archive_spec(tmp_path, "zip")
    spec["ffmpeg"]["inventory"][field] = value
    calls = mock_asset_download(monkeypatch, asset_preparer, payloads)
    with pytest.raises(ValueError, match="provenance"):
        asset_preparer.prepare(spec, tmp_path / "prepared")
    assert calls == []


def test_prepare_assets_rejects_hash_before_extraction(tmp_path, monkeypatch, asset_preparer):
    spec, payloads = archive_spec(tmp_path, "zip")
    payloads[spec["ffmpeg"]["url"]] = b"corrupt archive"
    mock_asset_download(monkeypatch, asset_preparer, payloads)
    output = tmp_path / "prepared"
    with pytest.raises(ValueError, match="SHA-256"):
        asset_preparer.prepare(spec, output)
    assert not (output / "ffmpeg").exists()


@pytest.mark.parametrize("format,member", [
    ("zip", "../escaped"), ("zip", "/absolute"), ("zip", "symlink"),
    ("tar", "../escaped"), ("tar", "symlink"), ("tar", "hardlink"),
])
def test_prepare_assets_rejects_unsafe_members(tmp_path, monkeypatch, asset_preparer, format, member):
    spec, payloads = archive_spec(tmp_path, format)
    archive = tmp_path / "unsafe.archive"
    if format == "zip":
        with zipfile.ZipFile(archive, "w") as bundle:
            item = zipfile.ZipInfo(member)
            if member == "symlink":
                item.create_system = 3
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(item, "../escaped")
    else:
        with tarfile.open(archive, "w") as bundle:
            item = tarfile.TarInfo(member)
            if member in {"symlink", "hardlink"}:
                item.type = tarfile.SYMTYPE if member == "symlink" else tarfile.LNKTYPE
                item.linkname = "../escaped"
            bundle.addfile(item)
    entry = spec["ffmpeg"]
    entry["sha256"] = entry["inventory"]["source_sha256"] = packaging.sha256(archive)
    payloads[entry["url"]] = archive.read_bytes()
    mock_asset_download(monkeypatch, asset_preparer, payloads)
    with pytest.raises((ValueError, tarfile.FilterError)):
        asset_preparer.prepare(spec, tmp_path / "prepared")
    assert not (tmp_path / "prepared/escaped").exists()


def test_build_version_and_paths_match_native_contract(tmp_path, monkeypatch):
    args, _ = mocked_build(monkeypatch, tmp_path, "release")
    dmg = packaging.build(args)
    app = args.output / "TranslateSub.app"
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    inventory = json.loads((app / "Contents/Resources/source-inventory.json").read_text())
    manifest = json.loads((args.output / "update-manifest.json").read_text())
    version = packaging.project_metadata()["version"]
    assert inventory["version"] == manifest["version"] == info["CFBundleVersion"] == info["CFBundleShortVersionString"] == version
    assert dmg.name == f"TranslateSub-{version}-macos-arm64-release.dmg"
    assert (app / "Contents/MacOS" / info["CFBundleExecutable"]).is_file()
    assert (app / "Contents/Resources/backend/_internal/web/index.html").is_file()
    assert (app / "Contents/Resources/bin/ffprobe").is_file()
    assert manifest["platforms"]["macos-arm64"]["minimum_macos"] == info["LSMinimumSystemVersion"]
    assert info["SUBTRANS_DESKTOP_UPDATE_URL"] == args.update_url


def test_ci_checks_architecture_before_sync():
    workflow = (ROOT / ".github/workflows/macos-desktop.yml").read_text()
    assert "runs-on: macos-15\n" in workflow
    check = 'test "$(uname -m)" = arm64'
    assert workflow.index(check) < workflow.index("uv sync --frozen")


def test_native_mode_is_explicit_and_conflicting_override_rejected():
    common = ["--font-dir", "/fonts", "--font-archive", "/fonts.zip"]
    assert packaging.parser().parse_args(common).native_build_mode == "swiftpm"
    assert packaging.parser().parse_args([*common, "--native-build-mode", "swiftc"]).native_build_mode == "swiftc"
    with pytest.raises(SystemExit):
        packaging.parser().parse_args([*common, "--native-build-mode", "swiftc", "--native-executable", "/app"])


def test_direct_commands_link_one_core_object_without_dylib(tmp_path):
    native = sys.modules["native_build"]
    core, app = native.swiftc_commands(ROOT / "desktop/macos", tmp_path, "14.0", Path("/clt/swiftc"), Path("/clt/sdk"))
    for command in (core, app):
        assert command[0] == "/clt/swiftc"
        assert "-O" in command and "-parse-as-library" in command
        assert command[command.index("-swift-version") + 1] == "5"
        assert command[command.index("-target") + 1] == "arm64-apple-macosx14.0"
        assert command[command.index("-module-cache-path") + 1] == str(tmp_path / "module-cache")
        assert "-emit-library" not in command and "-lDesktopCore" not in command and "-L" not in command
    assert "-whole-module-optimization" in core and "-emit-module" in core and "-emit-object" in core
    assert core[core.index("-o") + 1] == str(tmp_path / "DesktopCore.o")
    assert app.count(str(tmp_path / "DesktopCore.o")) == 1
    assert len([arg for arg in core if arg.endswith(".swift")]) == len(list((ROOT / "desktop/macos/Sources/DesktopCore").glob("*.swift")))


def test_default_native_build_remains_swiftpm(tmp_path):
    native = sys.modules["native_build"]
    commands = []
    def runner(*args):
        args = list(map(str, args))
        commands.append(args)
        scratch = Path(args[args.index("--scratch-path") + 1])
        if "--show-bin-path" in args:
            return str(scratch / "release")
        binary = scratch / "release/TranslateSub"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"release executable")
        return ""
    output = native.build_native(ROOT / "desktop/macos", tmp_path / "TranslateSub", runner=runner)
    assert output.read_bytes() == b"release executable"
    assert all(cmd[:2] == ["swift", "build"] for cmd in commands)
    assert len(commands) == 2
    assert list(tmp_path.iterdir()) == [output]


def test_swiftpm_failure_never_falls_back_and_cleans_staging(tmp_path):
    native = sys.modules["native_build"]
    commands = []
    def runner(*args):
        commands.append(args)
        raise ValueError("manifest failure")
    with pytest.raises(ValueError, match="manifest failure"):
        native.build_native(ROOT / "desktop/macos", tmp_path / "TranslateSub", runner=runner)
    assert len(commands) == 1 and commands[0][:2] == ("swift", "build")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", [None, "compile", "external", "rpath"])
def test_direct_build_audits_and_publishes_only_complete_executable(tmp_path, monkeypatch, failure):
    native = sys.modules["native_build"]
    clt = tmp_path / "clt"
    (clt / "usr/bin").mkdir(parents=True)
    (clt / "usr/bin/swiftc").touch()
    (clt / "SDKs/MacOSX.sdk").mkdir(parents=True)
    monkeypatch.setattr(native, "CLT", clt)
    commands = []
    def runner(*args):
        args = list(map(str, args))
        commands.append(args)
        if args[0] == "otool":
            if args[1] == "-L":
                dependency = "/scratch/libDesktopCore.dylib" if failure == "external" else "/usr/lib/libSystem.B.dylib"
                return f"executable:\n {dependency} (compatibility version 1)\n"
            if failure == "rpath":
                return "cmd LC_RPATH\n cmdsize 128\n path /scratch (offset 12)\n"
            # swiftc always adds this system rpath; it must not fail the audit.
            return "cmd LC_RPATH\n cmdsize 32\n path /usr/lib/swift (offset 12)\n"
        if failure == "compile":
            raise ValueError("compiler failed")
        output = Path(args[args.index("-o") + 1])
        output.write_bytes(b"static executable")
        output.chmod(0o755)
        return ""
    output = tmp_path / "result/TranslateSub"
    if failure:
        with pytest.raises(ValueError):
            native.build_native(ROOT / "desktop/macos", output, mode="swiftc", runner=runner)
        assert not output.exists()
    else:
        native.build_native(ROOT / "desktop/macos", output, mode="swiftc", runner=runner)
        assert output.read_bytes() == b"static executable"
        assert output.stat().st_mode & stat.S_IXUSR
        assert len(commands) == 4
    assert not list(output.parent.glob("native-*"))
    assert all(cmd[0] != "swift" for cmd in commands)


def test_native_failure_reports_safe_stderr_not_command_or_source(monkeypatch):
    native = sys.modules["native_build"]
    monkeypatch.setenv("BUILD_TEST_TOKEN", "private-value")
    def failed(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["swiftc", "never-echo-command"], stderr=(
            "file.swift:2: error: cannot find type Widget\n"
            " 2 | let key = \"source-literal\"\n"
            "error: credentials https://user:pass@host.test\n"
            "note: private-value unavailable\n"))
    monkeypatch.setattr(native.subprocess, "run", failed)
    with pytest.raises(ValueError) as error:
        native.run_native("swiftc", "never-echo-command")
    message = str(error.value)
    assert "cannot find type Widget" in message and "exit 1" in message
    assert all(value not in message for value in ["never-echo-command", "source-literal", "user:pass", "private-value"])


@pytest.mark.parametrize("mode", ["swiftpm", "swiftc"])
def test_bundle_driver_passes_selected_native_mode_and_minimum(tmp_path, monkeypatch, mode):
    args, _ = mocked_build(monkeypatch, tmp_path, "dev")
    args.native_executable = None
    args.native_build_mode = mode
    calls = []
    def compile_native(package, output, **kwargs):
        calls.append((package, kwargs))
        output.write_bytes(bytes.fromhex("cffaedfe"))
        return output
    monkeypatch.setattr(packaging, "build_native", compile_native)
    packaging.build(args)
    assert calls == [(ROOT / "desktop/macos", {"mode": mode, "minimum_macos": "14.0", "product": "TranslateSub"})]
    info = plistlib.loads((args.output / "TranslateSub.app/Contents/Info.plist").read_bytes())
    assert info["LSMinimumSystemVersion"] == "14.0"


def test_audit_accepts_candidate_minimum_14(tmp_path, monkeypatch):
    app = tmp_path / "App.app"
    app.mkdir()
    (app / "binary").write_bytes(bytes.fromhex("cffaedfe"))
    def command(*args, **kwargs):
        if args[0] == "lipo":
            return "arm64"
        if args[1] == "-l":
            return "cmd LC_BUILD_VERSION\n cmdsize 32\n platform 1\n minos 14.0\n"
        return "binary:\n /usr/lib/libSystem.B.dylib (compatibility version 1)\n"
    monkeypatch.setattr(packaging, "run", command)
    assert len(packaging.audit_macho(app)) == 1
