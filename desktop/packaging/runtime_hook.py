"""Use only packaged fonts for libass/fontconfig in a frozen desktop process."""
import os
from pathlib import Path
import sys

if getattr(sys, "frozen", False):
    resources = Path(sys.executable).resolve().parents[1]
    os.environ["FONTCONFIG_FILE"] = str(resources / "fonts" / "fonts.conf")
    os.environ["FONTCONFIG_PATH"] = str(resources / "fonts")
