"""The entrypoint (SPEC §2.1, §3.1): fail-fast config, one uvicorn worker with proxy headers,
and uvicorn's lines as JSON with no query string in the access log."""

import json
import logging
import socket
from pathlib import Path
from textwrap import dedent
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn

from syncshelf import __main__ as entry
from syncshelf.app import create_app
from syncshelf.config import load_config

pytestmark = pytest.mark.usefixtures("restore_logging")


def write_config(tmp_path: Path, public_url: str = "http://localhost:8000") -> Path:
    path = tmp_path / "profiles.yaml"
    path.write_text(
        dedent(f"""\
            server:
              public_url: {public_url}
              forwarded_allow_ips: 192.0.2.0/24,127.0.0.1
            """),
        encoding="utf-8",
    )
    return path


def json_lines(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


class FakeServer:
    """Stands in for ``uvicorn.Server`` so ``main`` can be run without serving."""

    configs: list[uvicorn.Config] = []
    started = True

    def __init__(self, config: uvicorn.Config) -> None:
        self.configs.append(config)

    def run(self) -> None:
        pass


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> type[FakeServer]:
    FakeServer.configs = []
    monkeypatch.setattr(entry.uvicorn, "Server", FakeServer)
    return FakeServer


def test_main_serves_one_worker_with_proxy_headers_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: type[FakeServer]
) -> None:
    monkeypatch.setenv("SYNCSHELF_CONFIG", str(write_config(tmp_path)))
    monkeypatch.setenv("WEB_CONCURRENCY", "4")  # uvicorn reads it when workers isn't given

    assert entry.main() == 0

    [config] = fake_server.configs
    assert config.workers == 1
    assert config.proxy_headers is True
    assert config.forwarded_allow_ips == "192.0.2.0/24,127.0.0.1"
    assert config.log_config is None  # syncshelf.logging owns logging, uvicorn's included


def test_main_refuses_a_public_url_that_is_not_localhost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_server: type[FakeServer],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path, "https://shelf.example.com")
    monkeypatch.setenv("SYNCSHELF_CONFIG", str(config))

    assert entry.main() != 0

    assert fake_server.configs == []
    [line] = json_lines(capsys.readouterr().out)
    assert line["level"] == "ERROR"
    assert "server.public_url" in line["msg"]


def test_configure_logging_aliases_the_public_and_access_hosts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path)
    with path.open("a", encoding="utf-8") as f:
        f.write("  access:\n    team_domain: https://example.cloudflareaccess.com\n")
    entry.configure_logging(load_config(path, environ={}))

    logging.getLogger("syncshelf.x").warning(
        "via http://localhost:8000 and example.cloudflareaccess.com", extra={"event": "e"}
    )

    [line] = json_lines(capsys.readouterr().out)
    assert line["msg"] == "via http://public:8000 and access"


async def serve_once(app_config: uvicorn.Config, path: str) -> int:
    """Serve on a loopback socket, make one request, and shut down."""
    server = uvicorn.Server(app_config)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve, [sock])
        with anyio.fail_after(10):
            while not server.started:
                await anyio.sleep(0)  # a yield to the server task, not a wait on the clock
        async with httpx2.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}{path}")
        server.should_exit = True
    sock.close()
    return response.status_code


@pytest.mark.anyio
async def test_the_real_server_logs_json_with_no_query_string(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load_config(write_config(tmp_path), environ={})
    data = tmp_path / "data"
    data.mkdir()
    entry.configure_logging(config)
    app_config = entry.server_config(create_app(config, data_dir=data), config)

    assert await serve_once(app_config, "/healthz?token=abc123") == 200

    lines = json_lines(capsys.readouterr().out)
    access = [line for line in lines if line["logger"] == "uvicorn.access"]
    assert [line["msg"].split(" - ", 1)[1] for line in access] == ['"GET /healthz HTTP/1.1" 200']
    assert all("abc123" not in json.dumps(line) for line in lines)
    assert any(line.get("event") == "startup" for line in lines)
