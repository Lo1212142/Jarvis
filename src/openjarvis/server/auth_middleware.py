"""API key authentication middleware for the OpenJarvis server."""

from __future__ import annotations

import base64
import logging
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_WS_AUTH_PROTOCOL = "openjarvis.auth.v1"
_WS_KEY_PROTOCOL_PREFIX = "openjarvis.key.b64url."


def _api_keys_match(presented: str, expected: str) -> bool:
    """Compare API keys as bytes so non-ASCII values do not raise ``TypeError``."""
    try:
        presented_bytes = presented.encode("utf-8")
        expected_bytes = expected.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return secrets.compare_digest(presented_bytes, expected_bytes)


class AuthMiddleware(BaseHTTPMiddleware):
    """Validates ``Authorization: Bearer <key>`` on ``/v1/*`` and ``/api/*`` routes.

    Webhook routes and health checks are exempt — they use
    per-channel signature verification instead.
    """

    def __init__(self, app, api_key: str = "") -> None:  # noqa: ANN001
        super().__init__(app)
        self._api_key = api_key or os.environ.get("OPENJARVIS_API_KEY", "")

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        # Browser CORS preflights never carry Authorization, and rejecting
        # them here means they never reach CORSMiddleware to get
        # Access-Control-Allow-* headers, breaking cross-origin access
        # outright (#758). Do not exempt arbitrary OPTIONS requests: a real
        # preflight is identified by both headers required by the CORS protocol.
        if self._is_cors_preflight(request):
            return await call_next(request)
        if self._api_key and self._requires_auth(request.url.path):
            # The one-time pairing exchange is authenticated by the short-lived
            # QR token in its body; it must not require the server key on a new
            # phone. Every other pairing route remains global-key protected.
            if request.url.path == "/api/pairing/consume":
                return await call_next(request)
            auth = request.headers.get("Authorization", "")
            if not auth:
                return JSONResponse(
                    {"detail": "Missing Authorization header"},
                    status_code=401,
                )
            scheme, _, token = auth.partition(" ")
            if scheme.lower() != "bearer":
                return JSONResponse({"detail": "Invalid authorization scheme"}, status_code=401)
            # Constant-time comparison to avoid leaking the global key. A
            # scoped device token is accepted only for its permitted surface.
            if _api_keys_match(token, self._api_key):
                return await call_next(request)
            registry = getattr(request.app.state, "pairing_service", None)
            is_self_revoke = request.url.path == "/api/pairing/self-revoke" and request.method == "POST"
            required_scope = _device_scope_for_path(request.url.path, request.method)
            device = (
                registry.authenticate(token, required_scope=None if is_self_revoke else required_scope)
                if registry is not None and (required_scope or is_self_revoke)
                else None
            )
            if device is None:
                return JSONResponse({"detail": "Invalid API key or device token"}, status_code=401)
            request.state.device_id = device.device_id
            request.state.device_scopes = sorted(device.scopes)
        return await call_next(request)

    @staticmethod
    def _is_cors_preflight(request: Request) -> bool:
        return (
            request.method == "OPTIONS"
            and bool(request.headers.get("Origin"))
            and bool(request.headers.get("Access-Control-Request-Method"))
        )

    @staticmethod
    def _requires_auth(path: str) -> bool:
        """Protect API routes and operational metrics; leave the UI/health open.

        ``/metrics`` exposes request/token counters that should not be readable
        by unauthenticated clients, so it is gated alongside ``/v1`` and
        ``/api``. ``/health`` stays open for liveness probes.
        """
        return (
            path.startswith("/v1/")
            or path.startswith("/api/")
            or path == "/metrics"
            or path.startswith("/metrics/")
        )


def _device_scope_for_path(path: str, method: str) -> str | None:
    """Map a client route to a narrowly scoped device capability."""
    if path.startswith("/v1/"):
        if path.startswith("/v1/approvals"):
            return "approvals"
        if path.startswith("/v1/speech"):
            return "chat"
        return "chat" if path.startswith("/v1/chat") else None
    if path.startswith("/api/events") or path.startswith("/api/ws"):
        return "events"
    if path.startswith("/api/browser"):
        return "browser"
    if path.startswith("/api/audio"):
        return "audio"
    if path.startswith("/api/jobs"):
        return "jobs"
    if path.startswith("/api/reminders"):
        return "reminders"
    if path.startswith("/api/vision"):
        return "camera"
    if path.startswith("/api/logs"):
        # Device tokens may read already-registered, redacted log entries only.
        # Enforce the read-only contract here as well as in the route so a
        # future mutating logs endpoint cannot accidentally inherit the scope.
        return "logs" if method == "GET" else None
    if path.startswith(("/api/files", "/api/video", "/api/3d", "/api/image", "/api/catalog")):
        return "media"
    if path.startswith(("/api/weather", "/api/news", "/api/resources")):
        return "read"
    # Device tokens never reach Settings, pairing administration, metrics, or
    # unknown mutating surfaces. Global server key remains required there.
    return None


def generate_api_key() -> str:
    """Generate a new API key with ``oj_sk_`` prefix."""
    return f"oj_sk_{secrets.token_urlsafe(32)}"


def check_bind_safety(host: str, *, api_key: str) -> None:
    """Refuse to bind non-loopback without an API key.

    Raises ``SystemExit`` if *host* is not a loopback address and
    *api_key* is empty.
    """
    import ipaddress
    import sys

    try:
        is_loop = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loop = host in ("localhost", "")

    if not is_loop and not api_key:
        logger.error(
            "Binding to %s requires OPENJARVIS_API_KEY to be set. "
            "Run: jarvis auth generate-key",
            host,
        )
        sys.exit(1)


def _websocket_key_protocol(api_key: str) -> str:
    """Encode an API key as a browser-safe WebSocket protocol token."""
    try:
        encoded = base64.urlsafe_b64encode(api_key.encode("utf-8")).decode("ascii")
    except UnicodeEncodeError:
        return ""
    return f"{_WS_KEY_PROTOCOL_PREFIX}{encoded.rstrip('=')}"


def _offered_websocket_auth(websocket) -> tuple[str, str | None]:  # noqa: ANN001
    """Return the credential protocol and protocol to negotiate, if well formed.

    ASGI exposes the complete, flattened protocol offer in ``scope``. Reading
    it avoids ambiguity from repeated ``Sec-WebSocket-Protocol`` header fields.
    Exactly one stable protocol marker and one encoded credential are required.
    """
    offered = getattr(websocket, "scope", {}).get("subprotocols", [])
    if not isinstance(offered, (list, tuple)) or len(offered) != 2:
        return "", None
    if offered.count(_WS_AUTH_PROTOCOL) != 1:
        return "", None
    credentials = [
        protocol
        for protocol in offered
        if isinstance(protocol, str) and protocol != _WS_AUTH_PROTOCOL
    ]
    if len(credentials) != 1 or not credentials[0].startswith(_WS_KEY_PROTOCOL_PREFIX):
        return "", None
    if credentials[0] == _WS_KEY_PROTOCOL_PREFIX:
        return "", None
    return credentials[0], _WS_AUTH_PROTOCOL


def authenticate_websocket(
    websocket,  # noqa: ANN001
    expected_key: str,
) -> tuple[bool, str | None]:
    """Authenticate a WebSocket and return its negotiated auth subprotocol.

    Programmatic clients can send ``Authorization: Bearer <key>``. Browser
    clients, which cannot set that header, offer ``openjarvis.auth.v1`` plus a
    marked, unpadded base64url encoding of the UTF-8 key. The encoding only
    makes the credential valid subprotocol syntax; it does not make it secret.
    """
    credential_protocol, selected_protocol = _offered_websocket_auth(websocket)

    # Match AuthMiddleware's local, keyless behavior. If a stale client still
    # offers a well-formed auth protocol, negotiate it so the browser does not
    # fail an otherwise allowed handshake.
    if not expected_key:
        return True, selected_protocol

    auth = websocket.headers.get("authorization", "")
    scheme, _, header_token = auth.partition(" ")
    header_valid = scheme.lower() == "bearer" and _api_keys_match(
        header_token, expected_key
    )

    expected_protocol = _websocket_key_protocol(expected_key)
    protocol_valid = bool(credential_protocol and expected_protocol) and (
        _api_keys_match(credential_protocol, expected_protocol)
    )
    return header_valid or protocol_valid, selected_protocol


def websocket_authorized(websocket, expected_key: str) -> bool:  # noqa: ANN001
    """Return ``True`` if a WebSocket connection presents the expected key.

    ``AuthMiddleware`` is a ``BaseHTTPMiddleware`` and never sees WebSocket
    upgrade requests, so streaming endpoints must check the token themselves
    in the handshake before calling ``websocket.accept()``.

    When *expected_key* is empty, authentication is disabled (the loopback /
    local-only default, matching :class:`AuthMiddleware`) and all connections
    are allowed. See :func:`authenticate_websocket` for the supported
    credential transports. URL query parameters are deliberately not accepted
    because request targets commonly appear in access logs and browser history.
    """
    return authenticate_websocket(websocket, expected_key)[0]
