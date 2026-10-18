"""Load ``profiles.yaml``, expand its secret references, and validate it (SPEC §2.3).

The file path comes from ``SYNCSHELF_CONFIG`` (default ``/config/profiles.yaml``). Secrets
are never written in the file: each secret key ``k`` is either ``k: ${VAR}``, expanded from
SyncShelf's own environment, or ``k_file: <path>``, read from that file.

Every failure raises :class:`ConfigError` with a message that names the key at fault. No
message ever contains a secret value, so startup errors are safe to paste anywhere.

Validation covers what phases 1 and 2 read (§2.3: "a key becomes required when the phase that
reads it ships"). Keys and sections for later phases may be present or absent, and are
ignored here apart from secret expansion, which covers every secret key the spec names so
that the log scrubber (§2.7) knows every configured secret from the first phase.
"""

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

import yaml

CONFIG_ENV = "SYNCSHELF_CONFIG"
"""The environment variable that holds the config file path."""

DEFAULT_CONFIG_PATH = "/config/profiles.yaml"
"""Where the config file is read from when ``SYNCSHELF_CONFIG`` is unset."""

DATA_DIR_ENV = "SYNCSHELF_DATA_DIR"
"""The environment variable that overrides the data directory (#51)."""

DEFAULT_DATA_DIR = "/data"
"""The data directory when ``SYNCSHELF_DATA_DIR`` is unset: the container's volume (§2.3)."""

DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8000
"""``server.listen``'s default, ``0.0.0.0:8000`` (#51). Every interface, so Traefik and a
published port reach it, and so does the container health check on ``127.0.0.1:8000``."""

PROFILE_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}")
"""A profile name, matched in full (§1.1)."""

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
"""The levels §2.7 defines for ``server.log_level``."""

AUTH_MODES = ("oauth", "dev")
"""The values of ``server.auth_mode``."""

PERMISSIONS = ("chaptarr",)
"""The values ``profiles.<name>.permissions`` may list (§2.8): ``chaptarr`` is the only one."""

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
"""The only ``server.public_url`` hosts dev mode accepts (§3.2.5), and, until OAuth mode is
complete (#69), the only ones any mode accepts (§3.1, Appendix D4). This guards the configured
name only: with ``server.listen``'s default ``0.0.0.0`` bind, anything that can reach the port
reaches it."""

SERVICE_ALIASES: Mapping[str, str] = {
    "hardcover_url": "hardcover",
    "abs_url": "abs",
    "chaptarr_url": "chaptarr",
    "browser_ws": "browser",
}
"""Each ``services`` URL key and the alias its host is logged as (§2.7)."""

# Every secret the spec names (§2.3), as key paths inside their section. A profile's paths
# are relative to ``profiles.<name>``.
_SERVER_SECRETS = (("dev", "token"), ("access", "aud"), ("consent_hmac_key",))
_SERVICE_SECRETS = (("chaptarr_api_key",),)
_PROFILE_SECRETS = (
    ("hardcover_token",),
    ("ingest_secret",),
    ("goodreads", "rss_key"),
    ("goodreads", "email"),
    ("goodreads", "password"),
    ("abs", "token"),
)

_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DURATION = re.compile(r"([0-9]+)([mh])")
_DURATION_UNITS = {"m": 60, "h": 3600}
# server.listen: a bracketed IPv6 address or a bare host, then a port with no leading zero.
_LISTEN = re.compile(r"(?:\[(?P<ipv6>[^\]]+)\]|(?P<host>[^:\[\]]+)):(?P<port>[1-9][0-9]{0,4})")
_HOSTNAME_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


class ConfigError(Exception):
    """The config can't be used. The message names the key, and never a secret value."""


@dataclass(frozen=True)
class DevConfig:
    """``server.dev``: the one static token dev mode accepts, and the profile it acts as."""

    token: str = field(repr=False)
    profile: str
    """A configured profile name."""


@dataclass(frozen=True)
class ServerConfig:
    """The ``server`` section keys read so far."""

    public_url: str
    """The issuer URL, with no trailing slash."""
    forwarded_allow_ips: str
    """Comma-separated addresses or networks, passed to uvicorn's ``--forwarded-allow-ips``."""
    log_level: str = "INFO"
    auth_mode: str | None = None
    """``oauth`` or ``dev``, or ``None`` when absent, in which case no token is accepted
    (#73)."""
    dev: DevConfig | None = None
    """``server.dev``, read only in dev mode and ``None`` otherwise."""
    access_team_domain: str | None = None
    listen_host: str = DEFAULT_LISTEN_HOST
    """The address uvicorn binds, from ``server.listen``. An IPv6 address has no brackets."""
    listen_port: int = DEFAULT_LISTEN_PORT
    """The port uvicorn binds, from ``server.listen``."""

    @property
    def dev_mode(self) -> DevConfig | None:
        """``dev`` when ``auth_mode`` is ``dev``, else ``None``: the one test for dev mode."""
        return self.dev if self.auth_mode == "dev" else None

    @property
    def public_host(self) -> str:
        """The host of ``public_url``, lowercased, without the port."""
        return urlsplit(self.public_url).hostname or ""

    @property
    def mcp_url(self) -> str:
        """The MCP endpoint URL: ``public_url`` + ``/mcp`` (§2.3)."""
        return self.public_url + "/mcp"


@dataclass(frozen=True)
class ServicesConfig:
    """The service URLs from the ``services`` section, each ``None`` when absent."""

    hardcover_url: str | None = None
    abs_url: str | None = None
    chaptarr_url: str | None = None
    browser_ws: str | None = None


@dataclass(frozen=True)
class CadenceConfig:
    """The ``cadence`` durations, in seconds, with the §2.3 defaults.

    The other cadence keys (``csv_interval_days``, ``csv_window``,
    ``hardcover_daily_reserve``) are validated by the phases that read them.
    """

    rss_interval: int = 60 * 60
    abs_interval: int = 15 * 60
    chaptarr_pending_interval: int = 2 * 60
    mirror_ttl: int = 10 * 60


@dataclass(frozen=True)
class ProfileConfig:
    """One profile's identity keys that are validated so far."""

    name: str
    email: str | None = None
    """Lowercased. Unique across profiles."""
    permissions: frozenset[str] = frozenset()
    """The :data:`PERMISSIONS` this profile holds. Absent in the file means none, the least
    privilege, until #71 decides whether the key is required instead."""
    household_visible: bool = False
    """Whether this profile appears in the other profile's answers. Opt in (§2.8)."""


@dataclass(frozen=True)
class Config:
    """The loaded, validated config."""

    server: ServerConfig
    services: ServicesConfig
    cadence: CadenceConfig
    profiles: dict[str, ProfileConfig]
    secrets: Mapping[str, str] = field(repr=False)
    """Every configured secret, by its dotted key path without ``_file``
    (``profiles.alice.abs.token``). Kept out of ``repr`` so a logged config shows none."""

    def secret_values(self) -> frozenset[str]:
        """Every configured secret value, for the log scrubber to replace with ``***``."""
        return frozenset(self.secrets.values())

    def host_aliases(self) -> dict[str, str]:
        """Each configured host (lowercased, without the port) mapped to the alias it is
        logged as (§2.7).

        The ``server.public_url`` host is ``public`` and, when set, the
        ``server.access.team_domain`` host is ``access`` (#49, point 5). Then each configured
        service host is its ``SERVICE_ALIASES`` alias. A service alias wins over ``public``
        or ``access`` on a shared host, because it says more about the failing call. If two
        services share a host, the later one in ``SERVICE_ALIASES`` order wins.
        """
        aliases: dict[str, str] = {}
        if self.server.public_host:
            aliases[self.server.public_host] = "public"
        team_domain = self.server.access_team_domain
        if team_domain and (host := urlsplit(team_domain).hostname):
            aliases[host] = "access"
        for key, alias in SERVICE_ALIASES.items():
            url = getattr(self.services, key)
            if url and (host := urlsplit(url).hostname):
                aliases[host] = alias
        return aliases


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    """The config file path: ``SYNCSHELF_CONFIG``, or ``/config/profiles.yaml``."""
    env = os.environ if environ is None else environ
    return Path(env.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH)


def data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The data directory: ``SYNCSHELF_DATA_DIR``, or ``/data``.

    An env var rather than a config key (#51), so a developer can run outside a container
    without a ``/data`` on the current drive.
    """
    env = os.environ if environ is None else environ
    return Path(env.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR)


def load_config(
    path: str | os.PathLike[str] | None = None, environ: Mapping[str, str] | None = None
) -> Config:
    """Read, expand and validate the config file.

    ``path`` defaults to :func:`config_path`. ``${VAR}`` references expand from
    ``environ``, which defaults to the process environment. Raises :class:`ConfigError`.
    """
    env = os.environ if environ is None else environ
    file = Path(path) if path is not None else config_path(env)
    raw = _read_yaml(file)

    server_raw = _section(raw, "server", required=True)
    services_raw = _section(raw, "services")
    # Required from phase 2: the profiles table follows this section, so a file that lost it
    # by accident would otherwise revoke every connection at the next start.
    profiles_raw = _section(raw, "profiles", required=True)

    secrets: dict[str, str] = {}
    _expand_secrets(server_raw, "server", _SERVER_SECRETS, env, secrets)
    _expand_secrets(services_raw, "services", _SERVICE_SECRETS, env, secrets)
    profiles = _profiles(profiles_raw, env, secrets)
    server = _server(server_raw)
    if server.auth_mode == "dev":
        server = replace(server, dev=_dev(secrets, server_raw.get("dev"), profiles))

    return Config(
        server=server,
        services=_services(services_raw),
        cadence=_cadence(_section(raw, "cadence")),
        profiles=profiles,
        secrets=secrets,
    )


def parse_duration(text: str) -> int:
    """Parse a ``<n>m`` or ``<n>h`` duration into seconds. Raises ``ValueError``."""
    match = _DURATION.fullmatch(text)
    if match is None:
        raise ValueError("a duration is written <n>m or <n>h")
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


# --- reading ----------------------------------------------------------------------------


def _read_yaml(file: Path) -> dict[str, Any]:
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read the config file {file} ({type(exc).__name__})") from None
    except UnicodeDecodeError:
        # The decoder's message quotes the offending bytes, which may be part of a secret.
        raise ConfigError(f"the config file {file} is not UTF-8 text") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # The parser's own message quotes the file, which may hold an email or a pasted
        # secret, so only its position is reported.
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        raise ConfigError(f"the config file {file} is not valid YAML{where}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("the config file must be a mapping of sections")
    return data


def _section(raw: Mapping[str, Any], key: str, *, required: bool = False) -> dict[str, Any]:
    value = raw.get(key)
    if value is None:
        if required:
            raise ConfigError(f"{key}: required")
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: must be a mapping")
    return value


# --- secrets ----------------------------------------------------------------------------


def _expand_secrets(
    section: Mapping[str, Any],
    prefix: str,
    paths: tuple[tuple[str, ...], ...],
    env: Mapping[str, str],
    out: dict[str, str],
) -> None:
    for path in paths:
        parent: Any = section
        where = prefix
        for part in path[:-1]:
            parent = parent.get(part)
            where = f"{where}.{part}"
            if parent is None:
                break
            if not isinstance(parent, dict):
                raise ConfigError(f"{where}: must be a mapping")
        if parent is None:
            continue
        key = f"{where}.{path[-1]}"
        value = _secret(parent, path[-1], key, env)
        if value is not None:
            out[key] = value


def _secret(parent: Mapping[str, Any], name: str, key: str, env: Mapping[str, str]) -> str | None:
    inline, from_file = name in parent, f"{name}_file" in parent
    if inline and from_file:
        raise ConfigError(f"{key}: set either {name} or {name}_file, not both")
    if inline:
        return _from_env(parent[name], key, env)
    if from_file:
        return _from_file(parent[f"{name}_file"], f"{key}_file")
    return None


def _from_env(value: Any, key: str, env: Mapping[str, str]) -> str:
    match = _REFERENCE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        # Never echo the value: it is most likely the secret itself.
        raise ConfigError(
            f"{key}: a secret must be a ${{VAR}} reference or set through {key}_file, "
            "never written in the file"
        )
    var = match.group(1)
    expanded = env.get(var)
    if not expanded:
        raise ConfigError(f"{key}: environment variable {var} is not set or is empty")
    return expanded


def _from_file(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key}: must be a file path")
    try:
        text = Path(value).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{key}: cannot read {value} ({type(exc).__name__})") from None
    except UnicodeDecodeError:
        # The decoder's message quotes the offending bytes, which are part of the secret.
        raise ConfigError(f"{key}: the file {value} is not UTF-8 text") from None
    # Secret files conventionally end in a newline that isn't part of the secret.
    secret = text.rstrip("\r\n")
    if not secret:
        raise ConfigError(f"{key}: the file is empty")
    return secret


# --- sections ---------------------------------------------------------------------------


def _url(value: Any, key: str, schemes: tuple[str, ...]) -> SplitResult:
    if not isinstance(value, str):
        raise ConfigError(f"{key}: must be a URL")
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises on a malformed port
    except ValueError:
        raise ConfigError(f"{key}: must be a URL") from None
    if parts.scheme not in schemes or not parts.hostname:
        raise ConfigError(f"{key}: must be a {' or '.join(schemes)} URL with a host")
    return parts


def _server(raw: Mapping[str, Any]) -> ServerConfig:
    auth_mode = raw.get("auth_mode")
    if auth_mode is not None and auth_mode not in AUTH_MODES:
        raise ConfigError(f"server.auth_mode: must be one of {', '.join(AUTH_MODES)}")

    if "public_url" not in raw:
        raise ConfigError("server.public_url: required")
    public_url = raw["public_url"]
    parts = _url(public_url, "server.public_url", ("http", "https"))
    if parts.query or parts.fragment or public_url.endswith("/"):
        raise ConfigError("server.public_url: must have no trailing slash, query or fragment")
    if parts.hostname not in LOCAL_HOSTS:
        if auth_mode == "dev":
            # Dev mode's one static token would let anyone who reached a public name in.
            raise ConfigError("server.public_url: dev mode serves only localhost or 127.0.0.1")
        # Lifted for oauth mode once it is complete (#69).
        raise ConfigError(
            "server.public_url: OAuth sign-in isn't complete yet, so the host must be "
            "localhost or 127.0.0.1"
        )

    if "forwarded_allow_ips" not in raw:
        raise ConfigError("server.forwarded_allow_ips: required")
    forwarded = raw["forwarded_allow_ips"]
    if not _is_ip_list(forwarded):
        raise ConfigError(
            "server.forwarded_allow_ips: must be comma-separated IP addresses or networks"
        )

    log_level = raw.get("log_level", "INFO")
    if log_level not in LOG_LEVELS:
        raise ConfigError(f"server.log_level: must be one of {', '.join(LOG_LEVELS)}")

    team_domain = None
    access = raw.get("access")
    if isinstance(access, dict) and access.get("team_domain") is not None:
        team_domain = access["team_domain"]
        _url(team_domain, "server.access.team_domain", ("https",))

    listen_host, listen_port = DEFAULT_LISTEN_HOST, DEFAULT_LISTEN_PORT
    if "listen" in raw:
        listen_host, listen_port = _listen(raw["listen"])

    return ServerConfig(
        public_url=public_url,
        forwarded_allow_ips=forwarded,
        log_level=log_level,
        auth_mode=auth_mode,
        access_team_domain=team_domain,
        listen_host=listen_host,
        listen_port=listen_port,
    )


def _dev(secrets: Mapping[str, str], raw: Any, profiles: Mapping[str, ProfileConfig]) -> DevConfig:
    """``server.dev``, which dev mode requires. ``raw`` is a mapping or ``None``:
    :func:`_expand_secrets` has already refused anything else."""
    token = secrets.get("server.dev.token")
    if token is None:
        raise ConfigError("server.dev.token: required when server.auth_mode is dev")
    profile = (raw or {}).get("profile")
    if profile is None:
        raise ConfigError("server.dev.profile: required when server.auth_mode is dev")
    if not isinstance(profile, str) or profile not in profiles:
        # Not echoed: like every profile name, it may be someone's real name.
        raise ConfigError("server.dev.profile: must name a profile in the profiles section")
    return DevConfig(token=token, profile=profile)


def _listen(value: Any) -> tuple[str, int]:
    """Split ``server.listen`` into the host and port uvicorn binds."""
    error = ConfigError(
        "server.listen: must be host:port, with an IP address or hostname "
        "(an IPv6 address in brackets) and a port from 1 to 65535"
    )
    match = _LISTEN.fullmatch(value) if isinstance(value, str) else None
    if match is None or int(match["port"]) > 65535:
        raise error
    if match["ipv6"] is not None:
        try:
            ipaddress.IPv6Address(match["ipv6"])
        except ValueError:
            raise error from None
        return match["ipv6"], int(match["port"])
    host = match["host"]
    if not _is_ipv4(host) and not _is_hostname(host):
        raise error
    return host, int(match["port"])


def _is_ipv4(host: str) -> bool:
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return True


def _is_hostname(host: str) -> bool:
    # All-digit labels are left to _is_ipv4, so 999.0.0.1 isn't taken for a hostname.
    labels = host.split(".")
    return (
        len(host) <= 253
        and not labels[-1].isdigit()
        and all(_HOSTNAME_LABEL.fullmatch(label) for label in labels)
    )


def _is_ip_list(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    entries = [entry.strip() for entry in value.split(",")]
    try:
        for entry in entries:
            ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return False
    return True


def _services(raw: Mapping[str, Any]) -> ServicesConfig:
    urls: dict[str, str] = {}
    for key in SERVICE_ALIASES:
        value = raw.get(key)
        if value is None:
            continue
        schemes = ("ws", "wss") if key == "browser_ws" else ("http", "https")
        _url(value, f"services.{key}", schemes)
        urls[key] = value
    return ServicesConfig(**urls)


def _cadence(raw: Mapping[str, Any]) -> CadenceConfig:
    durations: dict[str, int] = {}
    for key in ("rss_interval", "abs_interval", "chaptarr_pending_interval", "mirror_ttl"):
        value = raw.get(key)
        if value is None:
            continue
        try:
            if not isinstance(value, str):
                raise ValueError
            durations[key] = parse_duration(value)
        except ValueError:
            raise ConfigError(f"cadence.{key}: must be a duration written <n>m or <n>h") from None
    return CadenceConfig(**durations)


def _profiles(
    raw: Mapping[str, Any], env: Mapping[str, str], secrets: dict[str, str]
) -> dict[str, ProfileConfig]:
    profiles: dict[str, ProfileConfig] = {}
    owners: dict[str, str] = {}
    for name, body in raw.items():
        if not isinstance(name, str) or not PROFILE_NAME_PATTERN.fullmatch(name):
            # The name itself is only a profile key, but it may be someone's real name.
            raise ConfigError("profiles: a profile name must match ^[a-z][a-z0-9_]{0,31}$")
        prefix = f"profiles.{name}"
        if not isinstance(body, dict):
            raise ConfigError(f"{prefix}: must be a mapping")
        _expand_secrets(body, prefix, _PROFILE_SECRETS, env, secrets)

        email = body.get("email")
        if email is not None:
            if not isinstance(email, str) or not email.strip():
                raise ConfigError(f"{prefix}.email: must be an email address")
            email = email.strip().lower()
            if email in owners:
                raise ConfigError(
                    f"{prefix}.email: the same email is already used by profiles.{owners[email]}"
                )
            owners[email] = name
        profiles[name] = ProfileConfig(
            name=name,
            email=email,
            permissions=_permissions(body.get("permissions"), f"{prefix}.permissions"),
            household_visible=_household_visible(
                body.get("household_visible"), f"{prefix}.household_visible"
            ),
        )
    return profiles


def _permissions(value: Any, key: str) -> frozenset[str]:
    # A blank value counts as absent, as it does for email.
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(p in PERMISSIONS for p in value):
        raise ConfigError(f"{key}: must be a list of permissions from: {', '.join(PERMISSIONS)}")
    return frozenset(value)


def _household_visible(value: Any, key: str) -> bool:
    if value is None:
        return False
    # YAML reads true/false as a bool; a quoted "true" or a 1 is a typo worth stopping on.
    if not isinstance(value, bool):
        raise ConfigError(f"{key}: must be true or false")
    return value
