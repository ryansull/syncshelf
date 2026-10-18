"""``python -m syncshelf``: load config, set up logging, and serve the app with uvicorn.

uvicorn runs exactly **one worker** (§2.1): the DB writer, the locks and the sync loops are
all in-process. Proxy headers are trusted only from ``server.forwarded_allow_ips``, the
Traefik network. uvicorn's own logging config is turned off, so its lines go through
:mod:`syncshelf.logging` like everything else, as JSON with the access log's query strings
dropped.

Config is loaded here, before the app starts, because uvicorn itself needs
``forwarded_allow_ips`` and the log level. A bad config logs one line naming the key and
exits non-zero without serving.
"""

import logging
import sys

import uvicorn
from starlette.types import ASGIApp

from syncshelf import logging as slog
from syncshelf.app import create_app
from syncshelf.config import Config, ConfigError, load_config

log = logging.getLogger("syncshelf")


def configure_logging(config: Config) -> None:
    """Set up JSON logging at ``server.log_level``, scrubbing every configured secret and
    aliasing every configured service host."""
    slog.configure(
        level=config.server.log_level,
        secrets=config.secret_values(),
        host_aliases=config.host_aliases(),
    )


def server_config(app: ASGIApp, config: Config) -> uvicorn.Config:
    """The uvicorn settings SyncShelf serves with.

    ``workers=1`` is passed explicitly because uvicorn otherwise reads ``WEB_CONCURRENCY``.
    The listen address and port are left at uvicorn's defaults, ``127.0.0.1:8000``, until #51
    decides them.
    """
    return uvicorn.Config(
        app,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=config.server.forwarded_allow_ips,
        log_config=None,
    )


def main() -> int:
    """Run the service until it is stopped. Returns the process exit code."""
    try:
        config = load_config()
    except ConfigError as exc:
        # Logging isn't configured from the file yet, but ConfigError never carries a secret.
        slog.configure()
        log.error("invalid config: %s", exc, extra={"event": "config_invalid"})
        return 1
    configure_logging(config)
    server = uvicorn.Server(server_config(create_app(config), config))
    server.run()
    # uvicorn records a failed startup (such as an unwritable data directory) this way.
    return 0 if server.started else 1


if __name__ == "__main__":
    sys.exit(main())
