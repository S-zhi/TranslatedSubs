from __future__ import annotations

from src.core.downloader import ProbeResult
from src.service import probe_batch
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


def test_startup_batch_runs_sites_with_bounded_background_workers(monkeypatch, tmp_path):
    calls = []

    def fake_probe(url):
        calls.append(url)
        return ProbeResult(ok=url.endswith("aqz-KE-bpKQ"), title="T" if url.endswith("aqz-KE-bpKQ") else None)

    monkeypatch.setattr(probe_batch, "probe_video", fake_probe)
    store = ProbeStore(tmp_path / "probes.db")
    manager = ProbeBatchManager()
    manager.start(store)
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
