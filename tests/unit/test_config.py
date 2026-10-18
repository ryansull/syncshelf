"""Config loading, secret references and validation (SPEC §2.3, §3.1)."""

from pathlib import Path
from textwrap import dedent

import pytest

from syncshelf.config import (
    CONFIG_ENV,
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    config_path,
    load_config,
    parse_duration,
)

ROOT = Path(__file__).resolve().parents[2]

MINIMAL = """
server:
  public_url: http://localhost
  forwarded_allow_ips: 192.0.2.0/24
"""

SECRET = "s3cr3t-value-never-shown"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profiles.yaml"
    path.write_text(dedent(text), encoding="utf-8")
    return path


def load(tmp_path: Path, text: str, environ: dict[str, str] | None = None) -> Config:
    return load_config(write(tmp_path, text), environ=environ or {})


def load_error(tmp_path: Path, text: str, environ: dict[str, str] | None = None) -> str:
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path, text, environ)
    return str(excinfo.value)


# --- where the file comes from ----------------------------------------------------------


def test_config_path_defaults_to_config_profiles_yaml() -> None:
    assert config_path({}) == Path(DEFAULT_CONFIG_PATH) == Path("/config/profiles.yaml")


def test_config_path_comes_from_syncshelf_config() -> None:
    assert config_path({CONFIG_ENV: "/elsewhere/p.yaml"}) == Path("/elsewhere/p.yaml")


def test_load_config_without_a_path_reads_syncshelf_config(tmp_path: Path) -> None:
    path = write(tmp_path, MINIMAL)
    config = load_config(environ={CONFIG_ENV: str(path)})
    assert config.server.public_url == "http://localhost"


def test_missing_file_names_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError, match="nope.yaml"):
        load_config(missing, environ={})


def test_invalid_yaml_reports_the_line_but_not_the_content(tmp_path: Path) -> None:
    message = load_error(tmp_path, f"server:\n  public_url: [unclosed {SECRET}\n")
    assert "line" in message
    assert SECRET not in message


def test_a_config_file_that_is_not_utf8_names_the_path_but_not_the_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_bytes(b"server:\n  public_url: \xff\xfe" + SECRET.encode() + b"\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, environ={})
    message = str(excinfo.value)
    assert "profiles.yaml" in message
    assert "UTF-8" in message
    assert SECRET not in message


def test_top_level_must_be_a_mapping(tmp_path: Path) -> None:
    assert "mapping" in load_error(tmp_path, "- just\n- a list\n")


# --- the minimal phase 1 file -----------------------------------------------------------


def test_minimal_file_loads_with_defaults(tmp_path: Path) -> None:
    config = load(tmp_path, MINIMAL)
    assert config.server.public_url == "http://localhost"
    assert config.server.mcp_url == "http://localhost/mcp"
    assert config.server.public_host == "localhost"
    assert config.server.forwarded_allow_ips == "192.0.2.0/24"
    assert config.server.log_level == "INFO"
    assert config.server.auth_mode is None
    assert config.profiles == {}
    assert config.cadence.rss_interval == 60 * 60
    assert config.cadence.abs_interval == 15 * 60
    assert config.cadence.chaptarr_pending_interval == 2 * 60
    assert config.cadence.mirror_ttl == 10 * 60


def test_sections_for_unbuilt_phases_are_tolerated(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        MINIMAL
        + """
  some_future_key: whatever
cadence:
  csv_window: "02:00-05:00"
  csv_interval_days: 5
profiles:
  alice:
    timezone: Etc/UTC
    permissions: [chaptarr]
    goodreads:
      user_id: "00000000"
      shelf_map: {read: 3}
""",
    )
    assert list(config.profiles) == ["alice"]


@pytest.mark.parametrize("key", ["public_url", "forwarded_allow_ips"])
def test_phase_1_server_keys_are_required(tmp_path: Path, key: str) -> None:
    text = MINIMAL.replace(f"  {key}:", "  unused:")
    assert f"server.{key}" in load_error(tmp_path, text)


def test_server_section_is_required(tmp_path: Path) -> None:
    assert "server" in load_error(tmp_path, "profiles: {}\n")


# --- the localhost-only guard (D4) ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://localhost", "http://localhost:8000", "http://127.0.0.1:8000", "https://localhost"],
)
def test_localhost_public_urls_are_accepted(tmp_path: Path, url: str) -> None:
    config = load(tmp_path, MINIMAL.replace("http://localhost", url))
    assert config.server.public_url == url


@pytest.mark.parametrize(
    "url",
    [
        "https://shelf.example.com",
        "http://192.0.2.10",
        "http://localhost.example.com",
        "http://[::1]",
    ],
)
def test_startup_refuses_a_public_url_that_is_not_localhost(tmp_path: Path, url: str) -> None:
    message = load_error(tmp_path, MINIMAL.replace("http://localhost", url))
    assert "server.public_url" in message
    assert "localhost" in message


@pytest.mark.parametrize(
    "url",
    ["localhost", "ftp://localhost", "http://localhost/", "http://localhost?x=1", "http://"],
)
def test_malformed_public_url_is_rejected(tmp_path: Path, url: str) -> None:
    assert "server.public_url" in load_error(tmp_path, MINIMAL.replace("http://localhost", url))


# --- other server keys ------------------------------------------------------------------


@pytest.mark.parametrize("value", ["192.0.2.0/24", "192.0.2.1", "192.0.2.0/24, 127.0.0.1"])
def test_forwarded_allow_ips_accepts_addresses_and_networks(tmp_path: Path, value: str) -> None:
    text = MINIMAL.replace("192.0.2.0/24", f'"{value}"')
    assert load(tmp_path, text).server.forwarded_allow_ips == value


@pytest.mark.parametrize("value", ["not-an-ip", "192.0.2.0/99", '""', '"*"'])
def test_forwarded_allow_ips_rejects_anything_else(tmp_path: Path, value: str) -> None:
    text = MINIMAL.replace("192.0.2.0/24", value)
    assert "server.forwarded_allow_ips" in load_error(tmp_path, text)


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_log_level_accepts_the_spec_levels(tmp_path: Path, level: str) -> None:
    assert load(tmp_path, MINIMAL + f"  log_level: {level}\n").server.log_level == level


def test_log_level_rejects_anything_else(tmp_path: Path) -> None:
    assert "server.log_level" in load_error(tmp_path, MINIMAL + "  log_level: LOUD\n")


@pytest.mark.parametrize("mode", ["oauth", "dev"])
def test_auth_mode_accepts_oauth_and_dev(tmp_path: Path, mode: str) -> None:
    assert load(tmp_path, MINIMAL + f"  auth_mode: {mode}\n").server.auth_mode == mode


def test_auth_mode_rejects_anything_else(tmp_path: Path) -> None:
    assert "server.auth_mode" in load_error(tmp_path, MINIMAL + "  auth_mode: none\n")


# --- durations --------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "seconds"), [("1m", 60), ("90m", 5400), ("2h", 7200)])
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["60", "60s", "1d", "m", "1.5h", " 5m", "-5m", "5M"])
def test_parse_duration_rejects_other_forms(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


def test_cadence_durations_are_read(tmp_path: Path) -> None:
    config = load(tmp_path, MINIMAL + "cadence:\n  rss_interval: 2h\n  mirror_ttl: 5m\n")
    assert config.cadence.rss_interval == 7200
    assert config.cadence.mirror_ttl == 300


@pytest.mark.parametrize("value", ["60", "1d", "true"])
def test_bad_cadence_duration_names_the_key(tmp_path: Path, value: str) -> None:
    message = load_error(tmp_path, MINIMAL + f"cadence:\n  abs_interval: {value}\n")
    assert "cadence.abs_interval" in message


# --- profiles ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["alice", "b", "bob_2", "a" * 32])
def test_profile_names_matching_the_pattern_are_accepted(tmp_path: Path, name: str) -> None:
    assert name in load(tmp_path, MINIMAL + f"profiles:\n  {name}: {{}}\n").profiles


@pytest.mark.parametrize("name", ["Alice", "2bob", "_x", "a-b", "a" * 33, '"al ice"'])
def test_profile_names_outside_the_pattern_are_rejected(tmp_path: Path, name: str) -> None:
    assert "profile name" in load_error(tmp_path, MINIMAL + f"profiles:\n  {name}: {{}}\n")


def test_profile_emails_are_lowercased(tmp_path: Path) -> None:
    config = load(tmp_path, MINIMAL + "profiles:\n  alice:\n    email: Alice@Example.COM\n")
    assert config.profiles["alice"].email == "alice@example.com"


def test_profile_emails_must_be_unique_after_lowercasing(tmp_path: Path) -> None:
    message = load_error(
        tmp_path,
        MINIMAL
        + "profiles:\n  alice:\n    email: alice@example.com\n"
        + "  bob:\n    email: ALICE@example.com\n",
    )
    assert "profiles.bob.email" in message
    assert "alice@example.com" not in message.lower()


def test_profile_email_must_be_a_string(tmp_path: Path) -> None:
    message = load_error(tmp_path, MINIMAL + "profiles:\n  alice:\n    email: [x]\n")
    assert "profiles.alice.email" in message


def test_profile_must_be_a_mapping(tmp_path: Path) -> None:
    assert "profiles.alice" in load_error(tmp_path, MINIMAL + "profiles:\n  alice: 3\n")


# --- secret references ------------------------------------------------------------------


def test_env_reference_expands_from_the_given_environment(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        MINIMAL + "  consent_hmac_key: ${CONSENT_HMAC_KEY}\n",
        {"CONSENT_HMAC_KEY": SECRET},
    )
    assert config.secrets["server.consent_hmac_key"] == SECRET


def test_file_reference_reads_the_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "aud"
    secret_file.write_text(SECRET + "\n", encoding="utf-8")
    config = load(
        tmp_path,
        MINIMAL + f"  access:\n    aud_file: {secret_file.as_posix()}\n",
    )
    # The file's trailing newline is not part of the secret.
    assert config.secrets["server.access.aud"] == SECRET


def test_every_secret_key_is_expanded(tmp_path: Path) -> None:
    keys = {
        "server.dev.token": "DEV",
        "server.access.aud": "AUD",
        "server.consent_hmac_key": "HMAC",
        "services.chaptarr_api_key": "CHAPTARR",
        "profiles.alice.hardcover_token": "HC",
        "profiles.alice.ingest_secret": "INGEST",
        "profiles.alice.goodreads.rss_key": "RSS",
        "profiles.alice.goodreads.email": "GR_EMAIL",
        "profiles.alice.goodreads.password": "GR_PASSWORD",
        "profiles.alice.abs.token": "ABS",
    }
    text = MINIMAL + dedent(
        """\
          dev:
            token: ${DEV}
            profile: alice
          access:
            team_domain: https://example.cloudflareaccess.com
            aud: ${AUD}
          consent_hmac_key: ${HMAC}
        services:
          chaptarr_api_key: ${CHAPTARR}
        profiles:
          alice:
            hardcover_token: ${HC}
            ingest_secret: ${INGEST}
            goodreads:
              rss_key: ${RSS}
              email: ${GR_EMAIL}
              password: ${GR_PASSWORD}
            abs:
              token: ${ABS}
        """
    )
    environ = {var: f"value-of-{var}" for var in keys.values()}
    config = load(tmp_path, text, environ)
    assert config.secrets == {key: f"value-of-{var}" for key, var in keys.items()}
    assert config.secret_values() == frozenset(environ.values())


def test_missing_variable_names_the_key_and_variable(tmp_path: Path) -> None:
    message = load_error(tmp_path, MINIMAL + "  consent_hmac_key: ${CONSENT_HMAC_KEY}\n")
    assert "server.consent_hmac_key" in message
    assert "CONSENT_HMAC_KEY" in message


def test_empty_variable_counts_as_missing(tmp_path: Path) -> None:
    message = load_error(
        tmp_path, MINIMAL + "  consent_hmac_key: ${CONSENT_HMAC_KEY}\n", {"CONSENT_HMAC_KEY": ""}
    )
    assert "server.consent_hmac_key" in message


def test_missing_file_names_the_key(tmp_path: Path) -> None:
    missing = (tmp_path / "absent").as_posix()
    message = load_error(
        tmp_path, MINIMAL + f"profiles:\n  alice:\n    abs:\n      token_file: {missing}\n"
    )
    assert "profiles.alice.abs.token_file" in message


def test_a_secret_file_that_is_not_utf8_names_the_key_but_not_the_content(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "key"
    secret_file.write_bytes(b"\xff\xfe" + SECRET.encode())
    message = load_error(tmp_path, MINIMAL + f"  consent_hmac_key_file: {secret_file.as_posix()}\n")
    assert "server.consent_hmac_key_file" in message
    assert "UTF-8" in message
    assert SECRET not in message


def test_empty_file_counts_as_missing(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")
    message = load_error(tmp_path, MINIMAL + f"  consent_hmac_key_file: {empty.as_posix()}\n")
    assert "server.consent_hmac_key_file" in message


def test_literal_secret_is_refused_without_showing_it(tmp_path: Path) -> None:
    message = load_error(tmp_path, MINIMAL + f"  consent_hmac_key: {SECRET}\n")
    assert "server.consent_hmac_key" in message
    assert SECRET not in message


def test_both_forms_of_one_secret_are_refused(tmp_path: Path) -> None:
    secret_file = tmp_path / "key"
    secret_file.write_text(SECRET, encoding="utf-8")
    message = load_error(
        tmp_path,
        MINIMAL
        + "  consent_hmac_key: ${CONSENT_HMAC_KEY}\n"
        + f"  consent_hmac_key_file: {secret_file.as_posix()}\n",
        {"CONSENT_HMAC_KEY": SECRET},
    )
    assert "server.consent_hmac_key" in message
    assert SECRET not in message


def test_no_error_ever_contains_a_secret_value(tmp_path: Path) -> None:
    # A bad profile name, after a secret has already been expanded.
    message = load_error(
        tmp_path,
        MINIMAL + "  consent_hmac_key: ${K}\nprofiles:\n  Bad: {}\n",
        {"K": SECRET},
    )
    assert SECRET not in message


def test_repr_never_shows_secret_values(tmp_path: Path) -> None:
    config = load(tmp_path, MINIMAL + "  consent_hmac_key: ${K}\n", {"K": SECRET})
    assert SECRET not in repr(config)
    assert SECRET not in str(config)


# --- service hosts and aliases ----------------------------------------------------------


def test_host_aliases_map_configured_service_hosts(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        MINIMAL
        + dedent(
            """\
            services:
              hardcover_url: https://api.hardcover.app/v1/graphql
              abs_url: https://ABS.home.arpa:13378
              chaptarr_url: http://chaptarr.home.arpa
              browser_ws: ws://browser:3000/
            """
        ),
    )
    assert config.host_aliases() == {
        "api.hardcover.app": "hardcover",
        "abs.home.arpa": "abs",
        "chaptarr.home.arpa": "chaptarr",
        "browser": "browser",
    }


def test_host_aliases_are_empty_without_services(tmp_path: Path) -> None:
    assert load(tmp_path, MINIMAL).host_aliases() == {}


def test_other_configured_hosts(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        MINIMAL + "  access:\n    team_domain: https://example.cloudflareaccess.com\n",
    )
    assert config.other_hosts() == frozenset({"localhost", "example.cloudflareaccess.com"})


@pytest.mark.parametrize("key", ["hardcover_url", "abs_url", "chaptarr_url", "browser_ws"])
def test_service_url_without_a_host_is_rejected(tmp_path: Path, key: str) -> None:
    message = load_error(tmp_path, MINIMAL + f"services:\n  {key}: not-a-url\n")
    assert f"services.{key}" in message


# --- the committed example --------------------------------------------------------------


def test_committed_example_loads(tmp_path: Path) -> None:
    config = load_config(ROOT / "profiles.example.yaml", environ={})
    assert config.server.public_host in {"localhost", "127.0.0.1"}
    assert set(config.profiles) == {"alice", "bob"}


def test_committed_example_sets_every_key_phase_1_validates() -> None:
    config = load_config(ROOT / "profiles.example.yaml", environ={})
    assert config.server.auth_mode is not None
    assert config.server.access_team_domain is not None
    assert all(vars(config.services).values()), "every services URL key is shown"
    raw = (ROOT / "profiles.example.yaml").read_text(encoding="utf-8")
    for key in ("rss_interval", "abs_interval", "chaptarr_pending_interval", "mirror_ttl"):
        assert f"\n  {key}: " in raw, f"cadence.{key} is shown"


def test_real_profiles_yaml_is_git_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "profiles.yaml" in ignored
