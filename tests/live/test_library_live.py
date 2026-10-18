"""``get_my_library`` end to end against the real Hardcover, reads only (SPEC §3.3.10, Live):
the app in dev mode, its mirror filled from each profile token's real library."""

from pathlib import Path
from textwrap import dedent

import pytest
from test_hardcover_live import profile_tokens

from syncshelf.app import create_app
from syncshelf.config import load_config

from serving import DEV_TOKEN, mcp_client, running

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_get_my_library_answers_from_the_real_library(
    live_env: dict[str, str], tmp_path: Path
) -> None:
    tokens = profile_tokens(live_env)
    assert tokens, "no HARDCOVER_TOKEN_<PROFILE> in the env file"
    for var, token in tokens.items():
        config_file = tmp_path / f"{var}.yaml"
        config_file.write_text(
            dedent("""\
                server:
                  public_url: http://localhost:8000
                  auth_mode: dev
                  forwarded_allow_ips: 127.0.0.1
                  dev: {token: "${DEV}", profile: alice}
                services:
                  hardcover_url: https://api.hardcover.app/v1/graphql
                profiles:
                  alice: {hardcover_token: "${HC}"}
                """),
            encoding="utf-8",
        )
        config = load_config(config_file, environ={"DEV": DEV_TOKEN, "HC": token})
        data = tmp_path / f"{var}-data"
        data.mkdir()
        async with running(create_app(config, data_dir=data)) as http, mcp_client(http) as mcp:
            result = await mcp.call_tool("get_my_library", {"limit": 5})
        assert not result.is_error, (var, result.content)
        page = result.structured_content
        assert page is not None
        assert page["stale"] is False, var
        assert len(page["books"]) == min(5, page["total"]), var
        # Real Hardcover credits authors as "Author"; a row without one means that was missed.
        assert all(b["author"] for b in page["books"]), var
