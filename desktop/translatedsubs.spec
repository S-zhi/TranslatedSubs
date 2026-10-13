# PyInstaller build specification for the native-window desktop package.
# Build from the repository root with:
#   SUBTRANS_FFMPEG_DIR=/path/to/ffmpeg/bin python scripts/build-desktop.py

from pathlib import Path
import os
import sys

from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT
from PyInstaller.utils.hooks import collect_all, collect_submodules

if sys.platform == "darwin":
    from PyInstaller.building.osx import BUNDLE


ROOT = Path(SPEC).resolve().parents[1]


datas = []
binaries = []
hiddenimports = ["src.handler.app"]

web_dir = ROOT / "web"
if web_dir.is_dir():
    datas.append((str(web_dir), "web"))
resource_dir = ROOT / "resource"
if resource_dir.is_dir():
    datas.append((str(resource_dir), "resource"))

# Uvicorn imports its protocol/loop implementation dynamically.  The source
# package also imports optional handlers dynamically during app assembly.
hiddenimports.extend(collect_submodules("src"))
hiddenimports.extend(
    [
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ]
)

# The base package uses the Replicate/cloud path by default.
# These packages belong to optional local model features.  Keeping them out of
# the base bundle also prevents their bundled FFmpeg libraries from colliding
# with the separately shipped FFmpeg binary.
optional_local_model_excludes = [
    "faster_whisper",
    "av",
    "ctranslate2",
    "onnxruntime",
    "tokenizers",
    "huggingface_hub",
    "hf_xet",
    "torch",
    "torchvision",
    "transformers",
    "tensorflow",
    "tensorboard",
    "matplotlib",
    "pandas",
    "pyarrow",
    "scipy",
    "sympy",
    "sklearn",
]

for package in ("yt_dlp", "replicate"):
    try:
        package_datas, package_binaries, package_hiddenimports = collect_all(package)
    except ModuleNotFoundError:
        continue
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)

ffmpeg_dir = (
    Path(os.environ["SUBTRANS_FFMPEG_DIR"]).expanduser()
    if os.environ.get("SUBTRANS_FFMPEG_DIR")
    else None
)
if ffmpeg_dir:
    for stem in ("ffmpeg", "ffprobe"):
        filename = f"{stem}.exe" if sys.platform == "win32" else stem
        candidate = ffmpeg_dir / filename
        if not candidate.is_file():
            raise SystemExit(f"Missing bundled binary: {candidate}")
        binaries.append((str(candidate), "bin"))

native_window = os.environ.get("SUBTRANS_NATIVE_WINDOW")
if native_window:
    native_window_path = Path(native_window).expanduser()
    if not native_window_path.is_file():
        raise SystemExit(f"Missing native window helper: {native_window_path}")
    binaries.append((str(native_window_path), "bin"))


a = Analysis(
    [str(ROOT / "desktop" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", *optional_local_model_excludes],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TranslatedSubs",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="TranslatedSubs",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="TranslatedSubs.app",
        bundle_identifier="com.translatedsubs.desktop",
        version="0.1.0",
        info_plist={
            "CFBundleVersion": "0.1.0",
            "LSApplicationCategoryType": "public.app-category.video",
            "LSMinimumSystemVersion": "12.0",
        },
    )

