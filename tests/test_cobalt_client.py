"""Offline protocol, isolation, and publication checks for the Cobalt client."""

from __future__ import annotations

import json
import logging
from dataclasses import FrozenInstanceError
from pathlib import Path

import httpx
import pytest

from src.plugin_system.errors import PluginCancelled, PluginError
from src.plugins.builtin import cobalt_client as cobalt

MEDIA = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256
OPTIONS = {
    "video_quality": "1080", "download_mode": "auto",
    "filename_style": "basic", "audio_format": "best",
    "check_cancelled": lambda: None,
}


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


@pytest.fixture
def client_factory(monkeypatch):
    real_client = httpx.Client
    created = []
    options = []

    def make(handler, **kwargs):
        def new_client(**client_options):
            options.append(client_options.copy())
            client = real_client(transport=httpx.MockTransport(handler), **client_options)
            created.append(client)
            return client
        monkeypatch.setattr(cobalt.httpx, "Client", new_client)
        return cobalt.CobaltClient(endpoint="https://api.test/", **kwargs)

    make.created = created
    make.options = options
    yield make
    for client in created:
        client.close()


def resolve(client):
    return client.resolve("https://video.test/watch?v=1", **OPTIONS)


def download(client, root, *, media=None, **kwargs):
    return client.download(
        media or cobalt.CobaltMedia("https://media.test/file?token=signed", "movie.mp4"),
        target_dir=root, source_stem="source", check_cancelled=kwargs.pop("check_cancelled", lambda: None),
        on_progress=kwargs.pop("on_progress", lambda percent: None), **kwargs,
    )


def test_contract_and_private_repr():
    media = cobalt.CobaltMedia("https://media.test?secret=x", "movie.mp4")
    assert "secret" not in repr(media)
    with pytest.raises(FrozenInstanceError):
        media.url = "changed"
    error = cobalt.CobaltError("invalid_result", "Safe message")
    assert isinstance(error, PluginError)
    assert error.to_dict() == {
        "code": "invalid_result", "stage": "download",
        "plugin_id": "builtin.cobalt", "message": "Safe message",
    }
    assert not error.fallback_allowed


def test_resolve_payload_and_client_isolation(client_factory, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={
                "status": "tunnel", "url": "https://api.test/tunnel?token=signed",
                "filename": "Human title.MP4",
            }, headers={"Set-Cookie": "session=private; Path=/"})
        return httpx.Response(200, content=MEDIA, headers={"Content-Type": "video/mp4"})

    client = client_factory(handler, api_key="private-api-key")
    result = download(client, tmp_path, media=resolve(client))
    assert result == tmp_path / "source.mp4"
    assert result.read_bytes() == MEDIA
    assert requests[0].headers["authorization"] == "Api-Key private-api-key"
    assert json.loads(requests[0].content)["localProcessing"] == "disabled"
    assert json.loads(requests[0].content)["videoQuality"] == "1080"
    assert "authorization" not in requests[1].headers
    assert "cookie" not in requests[1].headers
    assert len(client_factory.created) == 2
    assert all(options["trust_env"] is False for options in client_factory.options)
    assert all(options["follow_redirects"] is False for options in client_factory.options)
    assert all(options["timeout"].read == 30 for options in client_factory.options)


@pytest.mark.parametrize("status,code,fallback", [
    (401, "access_denied", False), (403, "access_denied", False),
    (404, "execution_error", False), (429, "rate_limited", True),
    (500, "service_unavailable", True), (503, "service_unavailable", True),
    (302, "invalid_result", False),
])
def test_http_statuses_and_no_api_redirect(client_factory, status, code, fallback):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://attacker.test/"}, content=b"SECRET")

    client = client_factory(handler, api_key="secret-key")
    with pytest.raises(cobalt.CobaltError) as raised:
        resolve(client)
    assert raised.value.code == code
    assert raised.value.fallback_allowed is fallback
    assert "SECRET" not in str(raised.value)
    assert len(calls) == 1


@pytest.mark.parametrize("body,code,fallback", [
    ({"status": "picker", "picker": []}, "unsupported_response", True),
    ({"status": "local-processing"}, "unsupported_response", True),
    ({"status": "future"}, "invalid_result", False),
    ({"status": "redirect"}, "invalid_result", False),
    ([], "invalid_result", False),
    ({"status": "error", "error": {}}, "invalid_result", False),
    ({"status": "error", "error": {"code": "api.service.unsupported"}}, "unsupported_source", True),
    ({"status": "error", "error": {"code": "api.link.unsupported"}}, "unsupported_source", True),
    ({"status": "error", "error": {"code": "api.auth.key.invalid"}}, "access_denied", False),
    ({"status": "error", "error": {"code": "api.content.video.private"}}, "access_denied", False),
    ({"status": "error", "error": {"code": "api.content.video.age"}}, "access_denied", False),
    ({"status": "error", "error": {"code": "api.content.video.region"}}, "access_denied", False),
    ({"status": "error", "error": {"code": "api.content.paid"}}, "access_denied", False),
    ({"status": "error", "error": {"code": "api.youtube.drm"}}, "access_denied", False),
    ({"status": "error", "error": {"code": "raw-secret"}}, "execution_error", False),
])
def test_response_classification(client_factory, body, code, fallback):
    client = client_factory(lambda request: httpx.Response(200, json=body))
    with pytest.raises(cobalt.CobaltError) as raised:
        resolve(client)
    assert raised.value.code == code
    assert raised.value.fallback_allowed is fallback
    assert "raw-secret" not in str(raised.value)


@pytest.mark.parametrize("body", [b"not-json-secret", b"[]", b"\xff", b"x" * (1024 * 1024 + 1)])
def test_malformed_and_bounded_json(client_factory, body):
    client = client_factory(lambda request: httpx.Response(200, content=body))
    with pytest.raises(cobalt.CobaltError) as raised:
        resolve(client)
    assert raised.value.code == "invalid_result"
    assert not raised.value.fallback_allowed


@pytest.mark.parametrize("upstream,fallback", [
    ("api.service.unsupported", True), ("api.link.unsupported", True),
    ("api.content.paid", False), ("api.content.video.private", False),
    ("unknown-upstream-secret", False),
])
def test_http_400_classifies_cobalt_error(client_factory, upstream, fallback):
    client = client_factory(lambda request: httpx.Response(400, json={
        "status": "error", "error": {"code": upstream, "context": {"secret": "private"}},
    }))
    with pytest.raises(cobalt.CobaltError) as raised:
        resolve(client)
    assert raised.value.fallback_allowed is fallback
    assert upstream not in str(raised.value)


@pytest.mark.parametrize("url", [
    "file:///tmp/video.mp4", "https://user:password@media.test/file", "https://media.test/file#fragment",
    "https://media.test:bad/file", "https://media.test/a\nb", "https://media.test\\@other.test/file",
])
def test_unsafe_media_urls(client_factory, url):
    client = client_factory(lambda request: httpx.Response(200, json={
        "status": "redirect", "url": url, "filename": "movie.mp4",
    }))
    with pytest.raises(cobalt.CobaltError) as raised:
        resolve(client)
    assert raised.value.code == "invalid_result"
    assert not raised.value.fallback_allowed


@pytest.mark.parametrize("filename", [
    "../movie.mp4", "..\\movie.mp4", "/tmp/movie.mp4", "movie.exe", "movie.mp4\x00",
    "%2e%2e%2fmovie.mp4", "movie.mp4?secret=x", "", ".mp4",
])
def test_unsafe_media_filenames(client_factory, filename):
    client = client_factory(lambda request: httpx.Response(200, json={
        "status": "redirect", "url": "https://media.test/file", "filename": filename,
    }))
    with pytest.raises(cobalt.CobaltError) as raised:
        resolve(client)
    assert raised.value.code == "invalid_result"


def test_media_redirects_validate_each_hop_and_drop_cookies(client_factory, tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "/second", "Set-Cookie": "media=secret; Path=/"})
        return httpx.Response(200, content=MEDIA)

    client = client_factory(handler)
    download(client, tmp_path)
    assert seen[1].url.path == "/second"
    assert "cookie" not in seen[1].headers
    assert "authorization" not in seen[1].headers


@pytest.mark.parametrize("location", ["file:///tmp/file", "https://user:pass@host.test/file", "#frag"])
def test_unsafe_redirect_never_requested(client_factory, tmp_path, location):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": location})

    client = client_factory(handler)
    with pytest.raises(cobalt.CobaltError):
        download(client, tmp_path)
    assert len(calls) == 1
    assert not list(tmp_path.iterdir())


def test_slow_stream_cancellation_does_not_wait_for_full_buffer(client_factory, tmp_path):
    cancelled = False

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal cancelled
            yield MEDIA[:16]
            cancelled = True
            yield MEDIA[16:32]
            pytest.fail("must cancel before reading another transport chunk")

    def check():
        if cancelled:
            raise PluginCancelled("cancelled")

    client = client_factory(lambda request: httpx.Response(200, stream=SlowStream()))
    with pytest.raises(PluginCancelled):
        download(client, tmp_path, check_cancelled=check)
    assert not list(tmp_path.iterdir())


def test_output_chunks_are_bounded():
    response = httpx.Response(200, stream=Chunks([b"x" * (cobalt._CHUNK_SIZE * 2 + 1)]))
    chunks = list(cobalt._checked_chunks(response, lambda: None))
    assert [len(chunk) for chunk in chunks] == [256 * 1024, 256 * 1024, 1]


def test_redirect_loop_is_bounded(client_factory, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "/loop"})

    client = client_factory(handler)
    with pytest.raises(cobalt.CobaltError) as raised:
        download(client, tmp_path)
    assert len(calls) == 6
    assert not raised.value.fallback_allowed


@pytest.mark.parametrize("body,headers", [
    (b"", {}), (b"<html>private error</html>", {}), (b'{"secret":"error"}', {}),
    (MEDIA, {"Content-Type": "application/json"}),
    (MEDIA, {"Content-Length": "100000"}), (MEDIA, {"Content-Length": "1"}),
    (MEDIA, {"Content-Length": "bad"}), (MEDIA, {"Content-Length": "0"}),
])
def test_invalid_media_does_not_publish(client_factory, tmp_path, body, headers):
    stream = Chunks([body])
    client = client_factory(lambda request: httpx.Response(200, stream=stream, headers=headers))
    with pytest.raises(cobalt.CobaltError) as raised:
        download(client, tmp_path)
    assert raised.value.code == "invalid_result"
    assert not raised.value.fallback_allowed
    assert not list(tmp_path.iterdir())
    assert stream.closed


@pytest.mark.parametrize("estimate", ["1", "999999999", "invalid", None])
def test_estimated_length_is_progress_only(client_factory, tmp_path, estimate):
    headers = {"Estimated-Content-Length": estimate} if estimate else {}
    client = client_factory(lambda request: httpx.Response(200, stream=Chunks([MEDIA]), headers=headers))
    progress = []
    result = download(client, tmp_path, on_progress=progress.append)
    assert result.read_bytes() == MEDIA
    assert progress[-1] == 100
    assert all(value is None or 0 <= value <= 100 for value in progress)


@pytest.mark.parametrize("when", ["during", "published"])
def test_cancel_cleans_only_own_file(client_factory, tmp_path, when):
    keep = tmp_path / ".cobalt-other.part"
    keep.write_bytes(b"keep")
    cancelled = False

    def check():
        if cancelled:
            raise PluginCancelled("cancelled")

    def progress(percent):
        nonlocal cancelled
        if when == "published" and percent == 100:
            assert (tmp_path / "source.mp4").exists()
            cancelled = True
        elif when == "during" and percent and percent > 0:
            cancelled = True

    client = client_factory(lambda request: httpx.Response(200, content=MEDIA))
    with pytest.raises(PluginCancelled):
        download(client, tmp_path, check_cancelled=check, on_progress=progress)
    assert list(tmp_path.iterdir()) == [keep]
    assert keep.read_bytes() == b"keep"


def test_symlink_destination_is_rejected(client_factory, tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    (tmp_path / "source.mp4").symlink_to(victim)
    client = client_factory(lambda request: pytest.fail("must not request unsafe target"))
    with pytest.raises(cobalt.CobaltError) as raised:
        download(client, tmp_path)
    assert not raised.value.fallback_allowed
    assert victim.read_bytes() == b"keep"


@pytest.mark.parametrize("operation", ["resolve", "download"])
def test_timeout_is_safe_and_fallback_eligible(client_factory, tmp_path, operation):
    def handler(request):
        raise httpx.ReadTimeout("secret-url?token=SECRET", request=request)

    client = client_factory(handler)
    with pytest.raises(cobalt.CobaltError) as raised:
        resolve(client) if operation == "resolve" else download(client, tmp_path)
    assert raised.value.fallback_allowed
    assert "SECRET" not in str(raised.value)
    assert raised.value.cause is None
    assert raised.value.__suppress_context__
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", [429, 500])
def test_restriction_body_on_retryable_http_status_blocks_fallback(client_factory, status):
    client = client_factory(lambda request: httpx.Response(status, json={
        "status": "error", "error": {"code": "api.content.video.private"},
    }))
    with pytest.raises(cobalt.CobaltError) as raised:
        resolve(client)
    assert raised.value.code == "access_denied"
    assert not raised.value.fallback_allowed


def test_programming_bug_is_not_converted_to_fallback(client_factory, tmp_path):
    def handler(request):
        raise RuntimeError("programming error")

    client = client_factory(handler)
    with pytest.raises(RuntimeError, match="programming error"):
        download(client, tmp_path)
    assert not list(tmp_path.iterdir())


def test_cancellation_before_network(client_factory, tmp_path):
    client = client_factory(lambda request: pytest.fail("must not request after cancellation"))

    def cancelled():
        raise PluginCancelled("cancelled")

    with pytest.raises(PluginCancelled):
        client.resolve("https://video.test", **{**OPTIONS, "check_cancelled": cancelled})
    with pytest.raises(PluginCancelled):
        download(client, tmp_path, check_cancelled=cancelled)
    assert not list(tmp_path.iterdir())


def test_chunked_copy_and_failed_download_preserve_existing_file(client_factory, tmp_path, monkeypatch):
    seen_chunks = []
    original = httpx.Response.iter_bytes

    def observed(response, chunk_size=None):
        seen_chunks.append(chunk_size)
        yield from original(response, chunk_size=chunk_size)

    monkeypatch.setattr(httpx.Response, "iter_bytes", observed)
    stream = Chunks([MEDIA + b"\x00" * (cobalt._CHUNK_SIZE * 3)])
    client = client_factory(lambda request: httpx.Response(200, stream=stream))
    download(client, tmp_path)
    assert seen_chunks == [None]  # cancellation checks each incoming transport chunk
    assert stream.closed
    existing = (tmp_path / "source.mp4").read_bytes()
    client.close()
    bad = client_factory(lambda request: httpx.Response(200, content=b"<html>failure</html>"))
    with pytest.raises(cobalt.CobaltError):
        download(bad, tmp_path)
    assert (tmp_path / "source.mp4").read_bytes() == existing


@pytest.mark.parametrize("proxy", ["socks5://localhost:1080", "socks4://localhost:1080", "http://user:pass@proxy.test:80"])
def test_unsupported_proxy_fails_without_optional_install(proxy):
    with pytest.raises(cobalt.CobaltError) as raised:
        cobalt.CobaltClient(endpoint="https://api.test", proxy=proxy)
    assert raised.value.code == "invalid_input"
    assert "HTTP" in str(raised.value)
    assert not raised.value.fallback_allowed


def test_close_is_idempotent_and_rejects_use(client_factory):
    client = client_factory(lambda request: httpx.Response(200))
    client.close()
    client.close()
    assert all(item.is_closed for item in client_factory.created)
    with pytest.raises(cobalt.CobaltError, match="closed"):
        resolve(client)


@pytest.mark.parametrize("logger_name", ["httpx", "httpx2"])
def test_sensitive_http_logs_are_scoped(client_factory, tmp_path, caplog, monkeypatch, logger_name):
    client = client_factory(lambda request: httpx.Response(200, content=MEDIA))
    logger = logging.getLogger(logger_name)
    monkeypatch.setattr(httpx._client, "logger", logger)
    before_filters = logger.filters.copy()
    with caplog.at_level(logging.DEBUG):
        download(client, tmp_path)
        logger.info("unrelated HTTP request remains visible")
    assert "signed" not in caplog.text
    assert "media.test" not in caplog.text
    assert "unrelated HTTP request remains visible" in caplog.text
    assert logger.filters == before_filters


@pytest.mark.parametrize("endpoint", [
    "https://api.test?key=secret", "https://user:pass@api.test/", "file:///tmp/api",
    "https://api.test:bad/", "https://api.test/#fragment", "https://api.test/ bad",
])
def test_invalid_endpoint_has_no_network_or_fallback(endpoint):
    with pytest.raises(cobalt.CobaltError) as raised:
        cobalt.CobaltClient(endpoint=endpoint)
    assert raised.value.code == "invalid_input"
    assert not raised.value.fallback_allowed
    assert "secret" not in str(raised.value)


def test_selfhost_private_tunnel_supported(client_factory, tmp_path):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.method == "POST":
            return httpx.Response(200, json={
                "status": "tunnel", "url": "http://127.0.0.1:9000/tunnel?id=signed",
                "filename": "video.mp4",
            })
        return httpx.Response(200, content=MEDIA)

    client = client_factory(handler)
    download(client, tmp_path, media=resolve(client))
    assert seen[1].startswith("http://127.0.0.1:9000/")


def test_compressed_api_is_rejected_before_body_iteration(client_factory):
    class UnreadCompressedStream(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("compressed response must be rejected before decoding")
            yield b"unreachable"

    client = client_factory(lambda request: httpx.Response(
        200, stream=UnreadCompressedStream(), headers={"Content-Encoding": "gzip"},
    ))
    with pytest.raises(cobalt.CobaltError) as raised:
        resolve(client)
    assert raised.value.code == "invalid_result"
    assert not raised.value.fallback_allowed


def test_source_page_fragment_is_removed_before_resolution(client_factory):
    posted = []

    def handler(request):
        posted.append(json.loads(request.content)["url"])
        return httpx.Response(200, json={
            "status": "tunnel", "url": "https://media.test/file", "filename": "video.mp4",
        })

    client = client_factory(handler)
    result = client.resolve("https://video.test/watch?v=1#t=20", **OPTIONS)
    assert posted == ["https://video.test/watch?v=1"]
    assert result.filename == "video.mp4"


@pytest.mark.parametrize("status", [400, 429])
@pytest.mark.parametrize("upstream,fallback", [
    ("error.api.rate_exceeded", True), ("error.api.service.disabled", True),
    ("error.api.service.audio_not_supported", True), ("error.api.service.unsupported", True),
    ("error.api.content.video.private", False), ("error.api.content.video.age", False),
    ("error.api.content.video.region", False), ("error.api.content.paid", False),
    ("error.api.youtube.drm", False), ("error.api.auth.key.invalid", False),
    ("error.error.api.service.unsupported", False),
])
def test_actual_wire_error_codes(client_factory, status, upstream, fallback):
    client = client_factory(lambda request: httpx.Response(status, json={
        "status": "error", "error": {"code": upstream},
    }))
    with pytest.raises(cobalt.CobaltError) as raised:
        resolve(client)
    assert raised.value.fallback_allowed is fallback
    assert upstream not in str(raised.value)


@pytest.mark.parametrize("filename", [
    "Episode #1 (720p, h264).mp4", ".NET tutorial (720p, h264).mp4",
])
def test_regular_title_punctuation_is_safe(client_factory, tmp_path, filename):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={
                "status": "tunnel", "url": "https://media.test/file", "filename": filename,
            })
        return httpx.Response(200, content=MEDIA)

    client = client_factory(handler)
    media = resolve(client)
    assert media.filename == filename
    path = download(client, tmp_path, media=media)
    assert path == tmp_path / "source.mp4"
    assert path.read_bytes() == MEDIA


def test_target_stem_escape_is_rejected(client_factory, tmp_path):
    client = client_factory(lambda request: pytest.fail("must not request unsafe target"))
    with pytest.raises(cobalt.CobaltError) as raised:
        client.download(
            cobalt.CobaltMedia("https://media.test/file", "video.mp4"),
            target_dir=tmp_path, source_stem="../outside", check_cancelled=lambda: None,
            on_progress=lambda percent: None,
        )
    assert raised.value.code == "invalid_input"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("extension,body", [
    ("mp4", b"\x00\x00\x00\x3cftypmp42" + b"\x00" * 48),
    ("mov", b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8),
    ("mov", b"\x00\x00\x00\x10moov" + b"\x00" * 8),
    ("mov", b"\x00\x00\x00\x08wide\x00\x00\x00\x10mdat" + b"\x00" * 8),
    ("m4v", MEDIA), ("m4a", MEDIA),
    ("webm", b"\x1a\x45\xdf\xa3\x80" + b"\x00" * 27),
    ("mkv", b"\x1a\x45\xdf\xa3\x80" + b"\x00" * 27),
    ("mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 22),
    ("mp3", b"\xff\xfb\x90\x64" + b"\x00" * 28),
    ("ogg", b"OggS\x00" + b"\x00" * 27),
    ("opus", b"OggS\x00" + b"\x00" * 27),
    ("wav", b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20),
])
def test_valid_container_signatures(client_factory, tmp_path, extension, body):
    # Split the magic across transport chunks to avoid chunk-boundary assumptions.
    client = client_factory(lambda request: httpx.Response(
        200, stream=Chunks([body[:2], body[2:7], body[7:]]),
        headers={"Content-Type": "application/octet-stream"},
    ))
    media = cobalt.CobaltMedia("https://media.test/file", f"movie.{extension}")
    target = download(client, tmp_path, media=media)
    assert target == tmp_path / f"source.{extension}"
    assert target.read_bytes() == body


@pytest.mark.parametrize("extension", sorted(cobalt._EXTENSIONS))
@pytest.mark.parametrize("body", [
    b"temporary upstream error", b"<html>upstream error</html>", b'{"error":"private"}',
])
def test_text_disguised_as_media_is_not_published(client_factory, tmp_path, extension, body):
    client = client_factory(lambda request: httpx.Response(
        200, content=body, headers={"Content-Type": "application/octet-stream"},
    ))
    with pytest.raises(cobalt.CobaltError) as raised:
        download(client, tmp_path, media=cobalt.CobaltMedia(
            "https://media.test/file", f"movie.{extension}",
        ))
    assert raised.value.code == "invalid_result"
    assert not raised.value.fallback_allowed
    assert not list(tmp_path.iterdir())
