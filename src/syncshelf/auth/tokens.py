"""The bearer-token check behind ``/mcp`` (SPEC §3.2.4, §3.2.5).

The MCP SDK, as the resource server, hands every bearer token to :class:`TokenVerifier`. A
token it accepts becomes the request's identity: ``get_access_token().subject`` is the profile
name, and ``.claims`` carries ``iss``, ``email`` and ``grant_id`` (§2.2). A token it refuses,
or no token at all, gets the SDK's 401 naming the protected-resource metadata, which is what
starts Claude's sign-in (§3.2.1).

There is one verifier with one branch per ``server.auth_mode``. Only the dev branch exists so
far. The OAuth branch, which looks tokens up by hash in the token store, arrives with that
store (#62). With ``server.auth_mode`` absent, no branch runs and every token is refused
(#73).
"""

import hmac

from mcp.server.auth.provider import AccessToken

from syncshelf.config import Config, DevConfig

__all__ = ["DEV_CLIENT_ID", "SCOPE", "TokenVerifier"]

SCOPE = "syncshelf"
"""The scope every accepted token carries (§3.2.2). There is no read/write split."""

DEV_CLIENT_ID = "dev"
"""The ``client_id`` of the dev token, which belongs to no registered client."""


class TokenVerifier:
    """Implements the SDK's ``TokenVerifier`` protocol for ``config``'s auth mode."""

    def __init__(self, config: Config) -> None:
        self._config = config

    async def verify_token(self, token: str) -> AccessToken | None:
        """The identity ``token`` carries, or ``None`` to refuse it with a 401."""
        dev = self._config.server.dev_mode
        if dev is not None:
            return self._verify_dev(token, dev)
        return None

    def _verify_dev(self, token: str, dev: DevConfig) -> AccessToken | None:
        server = self._config.server
        if not hmac.compare_digest(token.encode(), dev.token.encode()):
            return None
        return AccessToken(
            token=token,
            client_id=DEV_CLIENT_ID,
            scopes=[SCOPE],
            expires_at=None,
            resource=server.mcp_url,
            subject=dev.profile,
            # A dev token has no grant behind it, so there is no grant id.
            claims={
                "iss": server.public_url,
                "email": self._config.profiles[dev.profile].email,
                "grant_id": None,
            },
        )
