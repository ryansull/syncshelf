"""Config loading, secret references and validation (SPEC §2.3, §3.1)."""

import re
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from syncshelf.config import (
    CONFIG_ENV,
    DATA_DIR_ENV,
    DEFAULT_CONFIG_PATH,
    DEFAULT_DATA_DIR,
    Config,
    ConfigError,
    ProfileConfig,
    config_path,
    data_dir,
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


HC_URL = "https://api.hardcover.app/v1/graphql"
HC_TOKEN = "fake-hardcover-token"
HC_ENV = {"HC": HC_TOKEN}


def write(tmp_path: Path, text: str, *, hardcover: bool = True) -> Path:
    """Write ``text`` as the config, adding a one-profile ``profiles`` section (required) when
    the text has a server section but no profiles, so tests of other sections stay short.

    With ``hardcover``, the keys required from phase 3 are filled in where the text has a
    server section and leaves them out: ``services.hardcover_url``, and each profile's
    ``hardcover_token`` as ``${HC}`` (:data:`HC_ENV`).
    """
    text = dedent(text)
    if re.search(r"^server:", text, re.MULTILINE) and not re.search(
        r"^profiles:", text, re.MULTILINE
    ):
        text += "profiles:\n  alice: {}\n"
    if hardcover:
        text = _with_hardcover(text)
    path = tmp_path / "profiles.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _with_hardcover(text: str) -> str:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    if not isinstance(data, dict) or not isinstance(data.get("server"), dict):
        return text
    services = data.setdefault("services", {}) or {}
    if isinstance(services, dict):
        services.setdefault("hardcover_url", HC_URL)
        data["services"] = services
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        for body in profiles.values():
            if isinstance(body, dict) and not {"hardcover_token", "hardcover_token_file"} & set(
                body
            ):
                body["hardcover_token"] = "${HC}"
    return yaml.safe_dump(data, sort_keys=False)


def load(
    tmp_path: Path, text: str, environ: dict[str, str] | None = None, *, hardcover: bool = True
) -> Config:
    return load_config(write(tmp_path, text, hardcover=hardcover), environ=HC_ENV | (environ or {}))


def load_error(
    tmp_path: Path, text: str, environ: dict[str, str] | None = None, *, hardcover: bool = True
) -> str:
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path, text, environ, hardcover=hardcover)
    return str(excinfo.value)


# --- where the file comes from ----------------------------------------------------------


def test_config_path_defaults_to_config_profiles_yaml() -> None:
    assert config_path({}) == Path(DEFAULT_CONFIG_PATH) == Path("/config/profiles.yaml")


def test_config_path_comes_from_syncshelf_config() -> None:
    assert config_path({CONFIG_ENV: "/elsewhere/p.yaml"}) == Path("/elsewhere/p.yaml")


def test_load_config_without_a_path_reads_syncshelf_config(tmp_path: Path) -> None:
    path = write(tmp_path, MINIMAL)
    config = load_config(environ={CONFIG_ENV: str(path)} | HC_ENV)
    assert config.server.public_url == "http://localhost"


def test_data_dir_defaults_to_data() -> None:
    assert data_dir({}) == Path(DEFAULT_DATA_DIR) == Path("/data")


def test_data_dir_comes_from_syncshelf_data_dir() -> None:
    assert data_dir({DATA_DIR_ENV: "/srv/shelf"}) == Path("/srv/shelf")


def test_an_empty_syncshelf_data_dir_means_the_default() -> None:
    assert data_dir({DATA_DIR_ENV: ""}) == Path("/data")


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
    assert (config.server.listen_host, config.server.listen_port) == ("0.0.0.0", 8000)
    assert config.profiles == {"alice": ProfileConfig(name="alice", hardcover_token=HC_TOKEN)}
    assert config.services.hardcover_url == HC_URL
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


@pytest.mark.parametrize("text", ["", "profiles:\n"])
def test_profiles_section_is_required(text: str, tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_text(dedent(MINIMAL) + text, encoding="utf-8")
    with pytest.raises(ConfigError, match=r"^profiles: required$"):
        load_config(path, environ={})


def test_blank_permissions_and_household_visible_mean_absent(tmp_path: Path) -> None:
    text = MINIMAL + "profiles:\n  alice:\n    permissions:\n    household_visible:\n"
    alice = load(tmp_path, text).profiles["alice"]
    assert (alice.permissions, alice.household_visible) == (frozenset(), False)


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


DEV = MINIMAL + "  auth_mode: dev\n  dev:\n    token: ${DEV_TOKEN}\n    profile: alice\n"
DEV_ENV = {"DEV_TOKEN": SECRET}


OAUTH = MINIMAL + (
    "  auth_mode: oauth\n"
    "  access:\n"
    "    team_domain: https://example.cloudflareaccess.com\n"
    "    aud: ${AUD}\n"
    "  consent_hmac_key: ${HMAC}\n"
)
OAUTH_ENV = {"AUD": "aud-" + SECRET, "HMAC": "hmac-" + SECRET}


def test_oauth_mode_reads_the_access_app_and_consent_key(tmp_path: Path) -> None:
    config = load(tmp_path, OAUTH, OAUTH_ENV)
    assert config.server.auth_mode == "oauth"
    oauth = config.server.oauth
    assert oauth is not None
    assert oauth.access_team_domain == "https://example.cloudflareaccess.com"
    assert (oauth.access_aud, oauth.consent_hmac_key) == ("aud-" + SECRET, "hmac-" + SECRET)
    assert SECRET not in repr(config)


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("    team_domain:", "server.access.team_domain"),
        ("    aud:", "server.access.aud"),
        ("  consent_hmac_key:", "server.consent_hmac_key"),
    ],
)
def test_oauth_mode_requires_its_keys(tmp_path: Path, line: str, key: str) -> None:
    text = "\n".join(row for row in OAUTH.splitlines() if not row.startswith(line)) + "\n"
    message = load_error(tmp_path, text, OAUTH_ENV)
    assert key in message
    assert "required when server.auth_mode is oauth" in message
    assert SECRET not in message


@pytest.mark.parametrize(
    "domain",
    [
        "https://example.cloudflareaccess.com/",
        "https://example.cloudflareaccess.com/x",
        "https://example.cloudflareaccess.com?",
        "https://example.cloudflareaccess.com:443",
        "https://u@example.cloudflareaccess.com",
        "https://Example.cloudflareaccess.com",
    ],
)
def test_the_team_domain_is_only_a_scheme_and_host(tmp_path: Path, domain: str) -> None:
    text = OAUTH.replace("https://example.cloudflareaccess.com", domain)
    assert "server.access.team_domain" in load_error(tmp_path, text, OAUTH_ENV)


def test_other_modes_do_not_read_the_oauth_keys(tmp_path: Path) -> None:
    assert load(tmp_path, DEV, DEV_ENV).server.oauth is None


def test_auth_mode_may_be_absent_for_now(tmp_path: Path) -> None:
    # #73 asks whether phase 2 makes it required; absent accepts no token meanwhile.
    assert load(tmp_path, MINIMAL).server.auth_mode is None


def test_dev_mode_reads_the_dev_token_and_profile(tmp_path: Path) -> None:
    config = load(tmp_path, DEV, DEV_ENV)
    assert config.server.auth_mode == "dev"
    assert config.server.dev is not None
    assert (config.server.dev.token, config.server.dev.profile) == (SECRET, "alice")
    assert SECRET not in repr(config)


@pytest.mark.parametrize("key", ["token", "profile"])
def test_dev_mode_requires_its_keys(tmp_path: Path, key: str) -> None:
    text = "\n".join(line for line in DEV.splitlines() if f"    {key}:" not in line) + "\n"
    assert f"server.dev.{key}" in load_error(tmp_path, text, DEV_ENV)


def test_dev_mode_requires_the_dev_section(tmp_path: Path) -> None:
    assert "server.dev.token" in load_error(tmp_path, MINIMAL + "  auth_mode: dev\n")


@pytest.mark.parametrize("profile", ["bob", "[alice]", '""'])
def test_the_dev_profile_must_be_a_configured_profile(tmp_path: Path, profile: str) -> None:
    text = DEV.replace("profile: alice", f"profile: {profile}")
    assert "server.dev.profile" in load_error(tmp_path, text, DEV_ENV)


def test_oauth_mode_does_not_read_the_dev_section(tmp_path: Path) -> None:
    text = OAUTH + "  dev:\n    token: ${DEV_TOKEN}\n    profile: x\n"
    config = load(tmp_path, text, DEV_ENV | OAUTH_ENV)
    assert config.server.dev is None
    assert SECRET in config.secret_values(), "a configured dev token is still scrubbed"


@pytest.mark.parametrize("url", ["https://shelf.example.com", "http://192.0.2.10", "http://[::1]"])
def test_dev_mode_refuses_a_public_url_that_is_not_localhost(tmp_path: Path, url: str) -> None:
    message = load_error(tmp_path, DEV.replace("http://localhost", url), DEV_ENV)
    assert "server.public_url" in message
    assert "dev mode" in message


def test_auth_mode_rejects_anything_else(tmp_path: Path) -> None:
    assert "server.auth_mode" in load_error(tmp_path, MINIMAL + "  auth_mode: none\n")


@pytest.mark.parametrize(
    ("value", "host", "port"),
    [
        ("0.0.0.0:8000", "0.0.0.0", 8000),
        ("127.0.0.1:9000", "127.0.0.1", 9000),
        ("localhost:1", "localhost", 1),
        ("syncshelf:65535", "syncshelf", 65535),
        ("[::]:8000", "::", 8000),
        ("[::1]:8080", "::1", 8080),
    ],
)
def test_listen_is_a_host_and_port(tmp_path: Path, value: str, host: str, port: int) -> None:
    server = load(tmp_path, MINIMAL + f'  listen: "{value}"\n').server
    assert (server.listen_host, server.listen_port) == (host, port)


@pytest.mark.parametrize(
    "value",
    [
        "8000",
        '"8000"',
        '":8000"',
        '"0.0.0.0"',
        '"0.0.0.0:"',
        '"0.0.0.0:0"',
        '"0.0.0.0:65536"',
        '"0.0.0.0:http"',
        '"0.0.0.0:+80"',
        '"0.0.0.0: 80"',
        '"0.0.0.0:08000"',
        '"::1:8000"',
        '"[localhost]:8000"',
        '"[::1]8000"',
        '"bad host:8000"',
        '"-bad:8000"',
        '"http://localhost:8000"',
        '"999.0.0.1:8000"',
        "null",
    ],
)
def test_listen_rejects_anything_else(tmp_path: Path, value: str) -> None:
    assert "server.listen" in load_error(tmp_path, MINIMAL + f"  listen: {value}\n")


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


def test_profile_permissions_and_household_visible_are_read(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        MINIMAL + "profiles:\n  alice:\n    permissions: [chaptarr]\n    household_visible: true\n",
    )
    assert config.profiles["alice"].permissions == frozenset({"chaptarr"})
    assert config.profiles["alice"].household_visible is True


def test_absent_permissions_grant_nothing_and_household_visible_is_opt_in(
    tmp_path: Path,
) -> None:
    alice = load(tmp_path, MINIMAL + "profiles:\n  alice: {}\n").profiles["alice"]
    assert alice.permissions == frozenset()
    assert alice.household_visible is False


def test_an_empty_permissions_list_grants_nothing(tmp_path: Path) -> None:
    config = load(tmp_path, MINIMAL + "profiles:\n  alice:\n    permissions: []\n")
    assert config.profiles["alice"].permissions == frozenset()


@pytest.mark.parametrize("value", ["chaptarr", "[admin]", "[chaptarr, 3]", "{chaptarr: true}"])
def test_permissions_outside_the_known_list_are_rejected(tmp_path: Path, value: str) -> None:
    message = load_error(tmp_path, MINIMAL + f"profiles:\n  alice:\n    permissions: {value}\n")
    assert "profiles.alice.permissions" in message
    assert "chaptarr" in message


@pytest.mark.parametrize("value", ["yes please", "1", '"true"', "[true]"])
def test_household_visible_must_be_a_boolean(tmp_path: Path, value: str) -> None:
    message = load_error(
        tmp_path, MINIMAL + f"profiles:\n  alice:\n    household_visible: {value}\n"
    )
    assert "profiles.alice.household_visible" in message


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
        "localhost": "public",
        "api.hardcover.app": "hardcover",
        "abs.home.arpa": "abs",
        "chaptarr.home.arpa": "chaptarr",
        "browser": "browser",
    }


def test_host_aliases_with_only_the_required_service(tmp_path: Path) -> None:
    assert load(tmp_path, MINIMAL).host_aliases() == {
        "localhost": "public",
        "api.hardcover.app": "hardcover",
    }


def test_host_aliases_include_the_public_and_access_hosts(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        MINIMAL.replace("http://localhost", "http://127.0.0.1:8000")
        + "  access:\n    team_domain: https://Example.cloudflareaccess.com\n",
    )
    assert config.host_aliases() == {
        "127.0.0.1": "public",
        "example.cloudflareaccess.com": "access",
        "api.hardcover.app": "hardcover",
    }


def test_a_service_alias_wins_over_the_public_alias_on_a_shared_host(tmp_path: Path) -> None:
    config = load(tmp_path, MINIMAL + "services:\n  browser_ws: ws://localhost:3000/\n")
    assert config.host_aliases() == {"localhost": "browser", "api.hardcover.app": "hardcover"}


# --- the Hardcover keys (phase 3) -------------------------------------------------------


def test_hardcover_url_is_required(tmp_path: Path) -> None:
    text = MINIMAL + "profiles:\n  alice:\n    hardcover_token: ${HC}\n"
    assert load_error(tmp_path, text, hardcover=False) == "services.hardcover_url: required"


def test_each_profile_needs_a_hardcover_token(tmp_path: Path) -> None:
    text = MINIMAL + f"services:\n  hardcover_url: {HC_URL}\nprofiles:\n  alice: {{}}\n"
    message = load_error(tmp_path, text, hardcover=False)
    assert message == "profiles.alice.hardcover_token: required"


def test_the_hardcover_token_is_read_from_a_file(tmp_path: Path) -> None:
    token_file = tmp_path / "hc"
    token_file.write_text(SECRET + "\n", encoding="utf-8")
    text = (
        MINIMAL
        + f"services:\n  hardcover_url: {HC_URL}\n"
        + f"profiles:\n  alice:\n    hardcover_token_file: {token_file.as_posix()}\n"
    )
    alice = load(tmp_path, text, hardcover=False).profiles["alice"]
    assert alice.hardcover_token == SECRET
    assert SECRET not in repr(alice)


def test_hardcover_url_must_be_https(tmp_path: Path) -> None:
    text = MINIMAL + "services:\n  hardcover_url: http://api.hardcover.app/v1/graphql\n"
    assert "services.hardcover_url: must be a https URL" in load_error(tmp_path, text)


@pytest.mark.parametrize("prefix", ["Bearer ", "bearer "])
def test_a_hardcover_token_with_the_bearer_prefix_is_refused(tmp_path: Path, prefix: str) -> None:
    message = load_error(tmp_path, MINIMAL, {"HC": prefix + SECRET})
    assert message == "profiles.alice.hardcover_token: give the token without the leading 'Bearer '"
    assert SECRET not in message


@pytest.mark.parametrize("key", ["hardcover_url", "abs_url", "chaptarr_url", "browser_ws"])
def test_service_url_without_a_host_is_rejected(tmp_path: Path, key: str) -> None:
    message = load_error(tmp_path, MINIMAL + f"services:\n  {key}: not-a-url\n")
    assert f"services.{key}" in message


# --- the committed example --------------------------------------------------------------


EXAMPLE_ENV = {
    "SYNCSHELF_DEV_TOKEN": SECRET,
    "HARDCOVER_TOKEN_ALICE": "a",
    "HARDCOVER_TOKEN_BOB": "b",
}


def test_committed_example_loads(tmp_path: Path) -> None:
    config = load_config(ROOT / "profiles.example.yaml", environ=EXAMPLE_ENV)
    assert config.server.public_host in {"localhost", "127.0.0.1"}
    assert set(config.profiles) == {"alice", "bob"}


def test_committed_example_sets_every_key_phase_1_validates() -> None:
    config = load_config(ROOT / "profiles.example.yaml", environ=EXAMPLE_ENV)
    assert config.server.auth_mode is not None
    assert config.server.access_team_domain is not None
    assert all(vars(config.services).values()), "every services URL key is shown"
    raw = (ROOT / "profiles.example.yaml").read_text(encoding="utf-8")
    assert "\n  listen: " in raw, "server.listen is shown"
    for key in ("rss_interval", "abs_interval", "chaptarr_pending_interval", "mirror_ttl"):
        assert f"\n  {key}: " in raw, f"cadence.{key} is shown"


def test_committed_example_sets_the_profile_keys_phase_2_validates() -> None:
    config = load_config(ROOT / "profiles.example.yaml", environ=EXAMPLE_ENV)
    for profile in config.profiles.values():
        assert profile.permissions == frozenset({"chaptarr"})
    assert config.server.dev is not None and config.server.dev.profile in config.profiles
    raw = (ROOT / "profiles.example.yaml").read_text(encoding="utf-8")
    assert raw.count("\n    household_visible: ") == len(config.profiles)


def test_committed_example_sets_the_keys_phase_3_requires() -> None:
    config = load_config(ROOT / "profiles.example.yaml", environ=EXAMPLE_ENV)
    assert config.services.hardcover_url == HC_URL
    assert {p.hardcover_token for p in config.profiles.values()} == {"a", "b"}


def test_real_profiles_yaml_is_git_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "profiles.yaml" in ignored
