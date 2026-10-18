from __future__ import annotations

from src.core.downloader import ProbeResult
from src.service.probe_batch import ProbeBatchManager, STARTUP_PROBE_SITES
from src.store import ProbeStore


def test_startup_probe_registry_matches_the_fixed_ten_sites():
    assert [site.id for site in STARTUP_PROBE_SITES] == [
        "youtube",
        "vimeo",
        "dailymotion",
        "twitch",
        "tiktok",
        "twitter",
        "instagram",
        "acfun",
        "niconico",
        "pornhub",
    ]


def test_startup_batch_runs_sites_with_bounded_background_workers(tmp_path):
    calls = []

    def fake_probe(url):
        calls.append(url)
        return ProbeResult(ok=url.endswith("aqz-KE-bpKQ"), title="T" if url.endswith("aqz-KE-bpKQ") else None)

    store = ProbeStore(tmp_path / "probes.db")
    manager = ProbeBatchManager()
    manager.start(store, probe_provider=fake_probe)
    assert manager._thread is not None
    manager._thread.join(timeout=5)

    status = manager.status()
    assert status["state"] == "completed"
    assert status["total"] == 10
    assert status["completed"] == 10
    assert status["successful"] == 1
    assert status["failed"] == 9
    assert len(calls) == 10
    assert len(store.list(limit=0)) == 10


def test_startup_batch_skips_all_probes_in_debug_mode(tmp_path):
    calls = []

    def fake_probe(url):
        calls.append(url)
        return ProbeResult(ok=True)

    store = ProbeStore(tmp_path / "probes.db")
    manager = ProbeBatchManager()
    manager.start(store, probe_provider=fake_probe, debug=True)

    status = manager.status()
    assert status["state"] == "disabled"
    assert status["completed"] == 0
    assert manager._thread is None
    assert calls == []
    assert store.list(limit=10) == []


def test_probe_credential_resolver_accepts_cookie_path(monkeypatch, tmp_path):
    from src.config.config_repository import get_config_provider
    from src.handler import tasks
    from src.plugin_system import ProbeResult as PluginProbeResult
    from src.plugins import get_plugin_manager

    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text("cookie")
    get_config_provider().mutate(lambda document: document["business"].update({
        "cookies_file": str(cookie_path),
    }))
    real_manager = get_plugin_manager()

    class Manager:
        catalog = real_manager.catalog
        validate_config = real_manager.validate_config

        def run_probe(self, plugin_id, version, request, config, context):
            assert context.resolve_credential("settings:cookies_file") == str(cookie_path)
            assert config["cookies_ref"] == "settings:cookies_file"
            return PluginProbeResult(ok=True, title="Video")

    monkeypatch.setattr(tasks, "get_plugin_manager", lambda: Manager())
    result = tasks.probe_video("https://example.test/video")
    assert result.ok is True
    assert result.title == "Video"
