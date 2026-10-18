"""The container image's offline checks (SPEC §3.1, §2.4): the Dockerfile's fixed choices, its
health check against a real server, and the config and log checker CI's image job uses.

Docker itself runs only in CI (the ``image`` job in ``.github/workflows/ci.yml``).
"""

import json
import re
import socket
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import anyio
import pytest
import uvicorn
from starlette.types import Receive, Scope, Send

from syncshelf import __main__ as entry
from syncshelf.app import create_app
from syncshelf.config import load_config

from image.check_logs import LogCheckError, check_logs, main

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
CI_CONFIG = ROOT / "tests" / "image" / "config.yaml"
CI_ENV = {"HARDCOVER_TOKEN_ALICE": "ci-placeholder"}
"""The environment the CI image job passes (.github/workflows/ci.yml)."""
HEALTH_URL = "http://127.0.0.1:8000/healthz"


def instructions() -> list[tuple[str, str]]:
    """The Dockerfile's instructions as (keyword, arguments), continuations joined."""
    text = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    result = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keyword, _, args = line.partition(" ")
            result.append((keyword.upper(), " ".join(args.split())))
    return result


def runtime_stage() -> list[tuple[str, str]]:
    stages: list[list[tuple[str, str]]] = []
    for keyword, args in instructions():
        if keyword == "FROM":
            stages.append([])
        stages[-1].append((keyword, args))
    return stages[-1]


def exec_form(args: str) -> list[str]:
    """The JSON array of an exec-form ``CMD``, after any ``HEALTHCHECK`` options."""
    return json.loads(args[args.index("[") :])


def healthcheck_command() -> list[str]:
    [args] = [a for k, a in runtime_stage() if k == "HEALTHCHECK"]
    return exec_form(args.split(" CMD ", 1)[1])


class TestDockerfile:
    def test_every_stage_is_python_314_slim(self) -> None:
        bases = [a.split()[0] for k, a in instructions() if k == "FROM"]
        assert bases and all(base == "python:3.14-slim" for base in bases)

    def test_runs_as_the_fixed_non_root_user_with_the_data_volume(self) -> None:
        stage = runtime_stage()
        assert ("USER", "10001:10001") in stage
        assert ("VOLUME", "/data") in stage
        # /data must be owned by 10001 before VOLUME, or a fresh named volume isn't writable.
        assert any(k == "RUN" and "chown 10001:10001 /data" in a for k, a in stage)
        env = " ".join(a for k, a in stage if k == "ENV")
        assert "SYNCSHELF_CONFIG=/config/profiles.yaml" in env

    def test_runs_the_entrypoint_with_no_uvicorn_flags(self) -> None:
        [args] = [a for k, a in runtime_stage() if k == "CMD"]
        assert exec_form(args) == ["python", "-m", "syncshelf"]

    def test_health_check_probes_where_the_app_listens(self) -> None:
        command = healthcheck_command()
        assert command[:2] == ["python", "-c"]
        assert HEALTH_URL in command[2]
        # The health check runs inside the container, so loopback reaches the default bind
        # on every interface. If the default port moves, the health check must move with it.
        served = entry.server_config(_no_app, load_config(CI_CONFIG, environ=CI_ENV))
        assert (served.host, served.port) == ("0.0.0.0", 8000)
        assert f"http://127.0.0.1:{served.port}/healthz" == HEALTH_URL

    def test_dockerignore_keeps_secrets_and_local_config_out(self) -> None:
        lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        rules = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
        assert rules[0] == "*"
        included = [rule[1:] for rule in rules if rule.startswith("!")]
        assert not [rule for rule in included if re.search(r"\.env|local|profiles\.yaml", rule)]


async def _no_app(scope: Scope, receive: Receive, send: Send) -> None:
    raise AssertionError("never served")


def run_health_check(url: str) -> int:
    """Run the Dockerfile's health check command on this Python, pointed at ``url``."""
    [python, flag, code] = healthcheck_command()
    assert (python, flag) == ("python", "-c")
    return anyio.run(_run, [sys.executable, "-c", code.replace(HEALTH_URL, url)])


async def _run(command: list[str]) -> int:
    with anyio.fail_after(30):
        result = await anyio.run_process(command, check=False)
    return result.returncode


@pytest.fixture
def stub_server() -> Iterator[HTTPServer]:
    """A loopback HTTP server whose status is set per test."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(self.server.status)  # type: ignore[attr-defined]
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


class TestHealthCheck:
    @pytest.mark.parametrize(("status", "exit_code"), [(200, 0), (503, 1)])
    def test_exit_code_follows_the_status(
        self, stub_server: HTTPServer, status: int, exit_code: int
    ) -> None:
        stub_server.status = status  # type: ignore[attr-defined]
        port = stub_server.server_address[1]
        assert run_health_check(f"http://127.0.0.1:{port}/healthz") == exit_code

    def test_unhealthy_when_nothing_listens(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        assert run_health_check(f"http://127.0.0.1:{port}/healthz") == 1


class TestCiConfigAndLogs:
    @pytest.mark.usefixtures("restore_logging")
    @pytest.mark.anyio
    async def test_the_ci_config_starts_a_server_whose_logs_pass_the_check(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = load_config(CI_CONFIG, environ=CI_ENV)
        data = tmp_path / "data"
        data.mkdir()  # empty, as in CI
        entry.configure_logging(config)
        app_config = entry.server_config(create_app(config, data_dir=data), config)

        await serve_and_probe(app_config)

        startup = check_logs(capsys.readouterr().out)
        assert startup["version"]
        assert startup["sqlite_version"]

    def test_a_non_json_line_fails(self) -> None:
        with pytest.raises(LogCheckError, match="line 2 is not JSON"):
            check_logs(startup_line() + "\nTraceback (most recent call last):\n")

    def test_a_line_without_the_stable_fields_fails(self) -> None:
        with pytest.raises(LogCheckError, match="line 1 lacks ts"):
            check_logs(json.dumps({"level": "INFO", "logger": "x", "event": "x", "msg": ""}))

    @pytest.mark.parametrize("key", ["version", "sqlite_version"])
    def test_a_startup_line_without_a_version_fails(self, key: str) -> None:
        line = json.loads(startup_line())
        del line[key]
        with pytest.raises(LogCheckError, match=f"no {key}"):
            check_logs(json.dumps(line))

    def test_no_startup_line_fails(self) -> None:
        with pytest.raises(LogCheckError, match="no startup line"):
            check_logs("")

    def test_main_reports_the_versions(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        logs = tmp_path / "logs.txt"
        logs.write_text(startup_line() + "\n", encoding="utf-8")
        assert main(["check_logs.py", str(logs)]) == 0
        assert capsys.readouterr().out == "ok: version 0.0.0, SQLite 3.46.1\n"
        logs.write_text("not json\n", encoding="utf-8")
        assert main(["check_logs.py", str(logs)]) == 1


def startup_line() -> str:
    return json.dumps(
        {
            "ts": "2000-01-01T00:00:00.000+00:00",
            "level": "INFO",
            "logger": "syncshelf.app",
            "event": "startup",
            "msg": "syncshelf 0.0.0 starting on SQLite 3.46.1",
            "version": "0.0.0",
            "sqlite_version": "3.46.1",
        }
    )


async def serve_and_probe(app_config: uvicorn.Config) -> None:
    """Serve on a loopback socket, run the Dockerfile's health check against it, shut down."""
    server = uvicorn.Server(app_config)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    [_, _, code] = healthcheck_command()
    probe = [sys.executable, "-c", code.replace(HEALTH_URL, f"http://127.0.0.1:{port}/healthz")]
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve, [sock])
        with anyio.fail_after(10):
            while not server.started:
                await anyio.sleep(0)  # a yield to the server task, not a wait on the clock
        exit_code = await _run(probe)
        server.should_exit = True
    sock.close()
    assert exit_code == 0
