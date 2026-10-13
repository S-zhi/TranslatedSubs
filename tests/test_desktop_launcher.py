from __future__ import annotations

import os

from desktop import launcher


def test_default_data_dir_uses_platform_user_location(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("APPDATA", raising=False)

    assert launcher.default_data_dir() == tmp_path / "local" / "TranslatedSubs"


def test_resolve_runtime_paths_prefers_explicit_values(tmp_path):
    ffmpeg = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    ffmpeg.write_bytes(b"")
    ffprobe.write_bytes(b"")

    paths = launcher.resolve_runtime_paths(
        root=tmp_path / "bundle",
        data_dir=tmp_path / "data",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )

    assert paths.data_dir == (tmp_path / "data").resolve()
    assert paths.ffmpeg == ffmpeg.resolve()
    assert paths.ffprobe == ffprobe.resolve()
    assert paths.data_dir.is_dir()


def test_resolve_runtime_paths_finds_bundled_ffmpeg(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    binary_dir = bundle / "bin"
    binary_dir.mkdir(parents=True)
    (binary_dir / "ffmpeg").write_bytes(b"")
    (binary_dir / "ffprobe").write_bytes(b"")
    monkeypatch.delenv("SUBTRANS_FFMPEG", raising=False)
    monkeypatch.delenv("SUBTRANS_FFPROBE", raising=False)

    paths = launcher.resolve_runtime_paths(root=bundle, data_dir=tmp_path / "data")

    assert paths.ffmpeg == (binary_dir / "ffmpeg").resolve()
    assert paths.ffprobe == (binary_dir / "ffprobe").resolve()


def test_configure_environment_isolated_from_parent(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBTRANS_DATA_DIR", "parent-value")
    paths = launcher.RuntimePaths(
        bundle_root=tmp_path,
        data_dir=tmp_path / "data",
        ffmpeg=tmp_path / "bin" / "ffmpeg",
        ffprobe=tmp_path / "bin" / "ffprobe",
    )

    environment = launcher.configure_environment(paths)

    assert os.environ["SUBTRANS_DATA_DIR"] == "parent-value"
    assert environment["SUBTRANS_DATA_DIR"] == str(paths.data_dir)
    assert environment["SUBTRANS_FFMPEG"] == str(paths.ffmpeg)
    assert environment["SUBTRANS_FFPROBE"] == str(paths.ffprobe)


def test_choose_free_port_asks_os_for_ephemeral_port(monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def bind(self, address):
            assert address == ("127.0.0.1", 0)

        def getsockname(self):
            return ("127.0.0.1", 43123)

    monkeypatch.setattr(launcher.socket, "socket", lambda *_args: FakeSocket())

    assert launcher.choose_free_port() == 43123


def test_parser_defaults_to_native_window_mode():
    args = launcher.build_parser().parse_args([])

    assert args.window_mode == "native"
    assert args.no_browser is False

