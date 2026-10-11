from __future__ import annotations

import pytest

from src import cli


def test_parser_defaults_to_normal_local_server():
    args = cli.build_parser().parse_args([])
    assert args.debug is False
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.reload is False


def test_parser_accepts_debug_server_options():
    args = cli.build_parser().parse_args(
        ["--debug", "--host", "0.0.0.0", "--port", "9001", "--reload"]
    )
    assert args.debug is True
    assert args.host == "0.0.0.0"
    assert args.port == 9001
    assert args.reload is True


def test_main_debug_uses_debug_app_factory(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda target, **kwargs: captured.update(target=target, **kwargs),
    )

    cli.main(["--debug", "--host", "0.0.0.0", "--port", "9001", "--reload"])

    assert captured == {
        "target": "src.handler.app:create_debug_app",
        "factory": True,
        "host": "0.0.0.0",
        "port": 9001,
        "reload": True,
    }


def test_main_without_debug_keeps_existing_app_target(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda target, **kwargs: captured.update(target=target, **kwargs),
    )

    cli.main(["--port", "9002"])

    assert captured == {
        "target": "src.handler.app:app",
        "factory": False,
        "host": "127.0.0.1",
        "port": 9002,
        "reload": False,
    }


@pytest.mark.parametrize("argv", [["--debug=true"], ["--port", "0"], ["--port", "65536"]])
def test_parser_rejects_invalid_debug_or_port_arguments(argv):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


def test_help_mentions_debug(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--help"])
    assert "--debug" in capsys.readouterr().out
