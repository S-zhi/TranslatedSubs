"""arm64 onedir backend, including local conversion and dynamically loaded plugins."""
from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata,
)

root = Path(SPECPATH).resolve().parents[1]
datas = [(str(root / "web"), "web")]
binaries = []
hiddenimports = collect_submodules("src")
# PyInstaller's torch hook handles native assets. Explicit collection also keeps
# converter assets and Transformers' lazy imports available without a host install.
for package in ("torch", "transformers", "ctranslate2", "sentencepiece",
                "sacremoses", "faster_whisper", "tokenizers", "safetensors"):
    datas += collect_data_files(package, include_py_files=True)
    binaries += collect_dynamic_libs(package)
for package in ("ctranslate2.converters", "transformers.models.marian",
                "uvicorn", "yt_dlp"):
    hiddenimports += collect_submodules(package)
for distribution in ("torch", "transformers", "ctranslate2", "sentencepiece",
                     "sacremoses", "faster-whisper", "tokenizers", "safetensors",
                     "huggingface-hub", "yt-dlp"):
    datas += copy_metadata(distribution, recursive=True)
analysis = Analysis(
    [str(root / "desktop/packaging/backend_entry.py")], pathex=[str(root)],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    runtime_hooks=[str(root / "desktop/packaging/runtime_hook.py")],
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive, analysis.scripts, [], exclude_binaries=True,
    name="translatedsubs-backend", console=True, target_arch="arm64",
    codesign_identity=None, contents_directory="_internal",
)
bundle = COLLECT(executable, analysis.binaries, analysis.datas,
                 name="translatedsubs-backend")
