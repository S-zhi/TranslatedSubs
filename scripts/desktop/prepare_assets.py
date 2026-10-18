#!/usr/bin/env python3
"""Fetch ONLY explicitly supplied, hash-pinned build assets for manual CI builds."""
import argparse
import json
from pathlib import Path
import shutil
import stat
import tarfile
from urllib.request import urlopen
import zipfile

from build import https_url, sha256, validate_assets


def prepare(spec: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    for kind in ("ffmpeg", "fonts"):
        entry = spec[kind]
        inventory = entry["inventory"]
        url = https_url(entry["url"])
        if inventory["source_url"] != url or inventory["source_sha256"] != entry["sha256"]:
            raise ValueError("Source provenance differs from requested archive")
        archive = output / f"{kind}.archive"
        with urlopen(url, timeout=60) as response:
            https_url(response.url)
            with archive.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        if sha256(archive) != entry["sha256"]:
            raise ValueError("Downloaded asset SHA-256 mismatch")
        destination = output / kind
        destination.mkdir()
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                for item in bundle.infolist():
                    mode = item.external_attr >> 16
                    path = Path(item.filename)
                    if path.is_absolute() or ".." in path.parts or stat.S_ISLNK(mode):
                        raise ValueError("Unsafe asset archive member")
                    bundle.extract(item, destination)
                    if mode and not item.is_dir():
                        (destination / item.filename).chmod(mode & 0o777)
        else:
            with tarfile.open(archive) as bundle:
                if any(not (item.isfile() or item.isdir()) for item in bundle.getmembers()):
                    raise ValueError("Asset archive may contain only regular files/directories")
                bundle.extractall(destination, filter="data")
        (destination / "inventory.json").write_text(json.dumps(inventory, indent=2))
        validate_assets(destination, archive, kind)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    prepare(json.loads(args.spec.read_text()), args.output)
