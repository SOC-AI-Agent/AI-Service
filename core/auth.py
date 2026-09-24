"""Bearer-token gate in front of every route except /health.

Pure ASGI rather than BaseHTTPMiddleware: it only has to look at one header and
either answer 401 or step aside, and it should not sit between /analyze and the
answer it is streaming back.
"""

import logging
import secrets

from starlette.responses import JSONResponse

logger = logging.getLogger("auth")

# Unauthenticated so a container healthcheck does not need the token.
PUBLIC_PATHS = frozenset({"/health"})


class BearerAuthMiddleware:
    def __init__(self, app, token: str) -> None:
        self.app = app
        self._token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            return await self.app(scope, receive, send)

        header = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                header = value
                break
        scheme, _, presented = header.partition(b" ")

        # Compared as bytes, and in constant time. Headers arrive as bytes, so a
        # non-ASCII token cannot turn this into a TypeError and a 500.
        if scheme.lower() != b"bearer" or not secrets.compare_digest(presented, self._token):
            logger.warning("rejected unauthenticated request to %s from %s",
                           scope["path"], scope.get("client"))
            response = JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await response(scope, receive, send)

        return await self.app(scope, receive, send)
