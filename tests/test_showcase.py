"""Regression coverage for the public showcase and retained workbench routes."""

from fastapi.testclient import TestClient

from src.handler.app import create_app


def test_root_serves_showcase_and_workbench_routes_remain_available() -> None:
    client = TestClient(create_app())

    response = client.get("/")
    assert response.status_code == 200
    assert "Make every word" in response.text
    assert "TranslatedSubs" in response.text
    assert 'id="taskForm"' not in response.text

    for route in ("/tasks", "/mcp"):
        workbench = client.get(route)
        assert workbench.status_code == 200
        assert "TranslatedSubs" in workbench.text
        assert 'id="taskForm"' in workbench.text


def test_showcase_media_and_styles_are_served_from_local_assets() -> None:
    client = TestClient(create_app())

    css = client.get("/styles/showcase.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]

    for path, content_type in (
        ("/assets/showcase/landscape.webp", "image/webp"),
        ("/assets/showcase/promo-poster.jpg", "image/jpeg"),
        ("/assets/showcase/narration.vtt", "text/vtt"),
        ("/assets/showcase/translatedsubs-promo.mp4", "video/mp4"),
    ):
        asset = client.get(path)
        assert asset.status_code == 200
        assert content_type in asset.headers["content-type"]

    video = client.get(
        "/assets/showcase/translatedsubs-promo.mp4",
        headers={"Range": "bytes=0-1023"},
    )
    assert video.status_code == 206
    assert video.headers["content-range"].startswith("bytes 0-1023/")
    assert len(video.content) == 1024
