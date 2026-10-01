"""Authentication for the panel.

Three modes (``WEB_AUTH``):

``off``       anyone who can reach the port gets in. Fine on a private network,
              and what the demo preview uses.
``token``     a shared secret (``WEB_TOKEN``, auto-generated on first run and
              written to ``data/.web_token``) exchanged for a signed session
              cookie. This is the recommended mode on a public host.
``telegram``  Telegram Mini App style: ``initData`` is verified with the bot
              token (HMAC-SHA256, ``WebAppData`` key) and only the ids in
              ``OWNER_USER_IDS`` (or ``WEB_ALLOWED_IDS``) are let in.

Sessions are HMAC-signed cookies - no server state, no database table, and they
expire. Every failure is rate limited per IP so the token cannot be brute forced.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from ..config import Settings
from ..logging_setup import get_logger

log = get_logger("web.auth")

__all__ = ["Authenticator", "verify_init_data"]

COOKIE_NAME = "gohan_session"
TOKEN_FILE = Path("data/.web_token")
SESSION_HOURS = 12
FAIL_LIMIT = 8
FAIL_WINDOW = 600.0


def verify_init_data(init_data: str, bot_token: str, *, max_age: int = 86400) -> dict[str, Any] | None:
    """Validate Telegram Mini App ``initData`` and return its fields.

    Implements the documented algorithm: build ``data_check_string`` from every
    field except ``hash``, key it with ``HMAC_SHA256("WebAppData", bot_token)``
    and compare in constant time. Also rejects stale payloads.
    """
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(
            chunk.split("=", 1) for chunk in init_data.split("&") if "=" in chunk
        )
        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return None
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            return None
        if max_age:
            auth_date = int(pairs.get("auth_date") or 0)
            if auth_date and time.time() - auth_date > max_age:
                return None
        user = pairs.get("user")
        if user:
            pairs["user"] = json.loads(user)
        return pairs
    except Exception as exc:  # malformed input is just a failed login
        log.debug("initData rejected: %s", exc)
        return None


class Authenticator:
    """Decides whether a request may see or change the bot."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mode = getattr(settings, "web_auth", "off") or "off"
        self._failures: dict[str, list[float]] = {}
        self._token: str | None = None
        self._secret: bytes = secrets.token_bytes(32)
        self._allowed_ids: set[int] = set()
        self._parse_allowed()

    # -- token handling ------------------------------------------------------

    def _parse_allowed(self) -> None:
        raw = str(getattr(self.settings, "web_allowed_ids", "") or "").replace(",", " ").split()
        ids: set[int] = set()
        for chunk in raw:
            try:
                ids.add(int(chunk))
            except ValueError:
                continue
        self._allowed_ids = ids or set(self.settings.owner_ids)

    @property
    def required(self) -> bool:
        return self.mode != "off"

    def token(self) -> str | None:
        """The shared secret, from the environment or ``data/.web_token``."""
        if self._token:
            return self._token
        configured = getattr(self.settings, "web_token", None)
        if configured is not None:
            value = configured.get_secret_value().strip()
            if value:
                self._token = value
                return value
        try:
            if TOKEN_FILE.exists():
                value = TOKEN_FILE.read_text(encoding="utf-8").strip()
                if value:
                    self._token = value
                    return value
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            value = secrets.token_urlsafe(24)
            TOKEN_FILE.write_text(value, encoding="utf-8")
            TOKEN_FILE.chmod(0o600)
            log.info("generated a web token at %s", TOKEN_FILE)
            self._token = value
            return value
        except OSError as exc:
            log.warning("cannot store the web token: %s", exc)
            return None

    # -- sessions ------------------------------------------------------------

    def _sign(self, payload: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        signature = hmac.new(self._secret, body, hashlib.sha256).hexdigest()[:32]
        return f"{body.decode()}.{signature}"

    def issue(self, subject: str) -> str:
        return self._sign({"sub": subject, "exp": time.time() + SESSION_HOURS * 3600})

    def read(self, cookie: str | None) -> dict[str, Any] | None:
        if not cookie or "." not in cookie:
            return None
        body, _, signature = cookie.rpartition(".")
        expected = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(expected, signature):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(body))
        except Exception:
            return None
        if float(payload.get("exp") or 0) < time.time():
            return None
        return payload

    # -- request helpers -----------------------------------------------------

    def _client_ip(self, request: web.Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For", "")
        return (forwarded.split(",")[0].strip() if forwarded else None) or request.remote or "?"

    def _throttled(self, ip: str) -> bool:
        now = time.time()
        hits = [t for t in self._failures.get(ip, []) if now - t < FAIL_WINDOW]
        self._failures[ip] = hits
        return len(hits) >= FAIL_LIMIT

    def _note_failure(self, ip: str) -> None:
        self._failures.setdefault(ip, []).append(time.time())
        if len(self._failures) > 5000:
            self._failures.clear()

    def authorised(self, request: web.Request) -> bool:
        """True when the request may proceed (and is not a login attempt)."""
        if not self.required:
            return True
        if self.read(request.cookies.get(COOKIE_NAME)):
            return True
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return self.check_token(header[7:].strip(), self._client_ip(request))
        init_data = request.headers.get("X-Telegram-Init-Data")
        if init_data and self.check_init_data(init_data):
            return True
        return False

    def check_token(self, candidate: str, ip: str = "?") -> bool:
        if self._throttled(ip):
            log.warning("web login throttled for %s", ip)
            return False
        expected = self.token()
        if expected and candidate and hmac.compare_digest(candidate, expected):
            return True
        self._note_failure(ip)
        return False

    def check_init_data(self, init_data: str) -> bool:
        token = self.settings.bot_token
        if token is None:
            return False
        fields = verify_init_data(init_data, token.get_secret_value())
        if not fields:
            return False
        user_id = int((fields.get("user") or {}).get("id") or 0)
        allowed = self._allowed_ids or set(self.settings.owner_ids)
        if not allowed:
            # nothing configured: any *verified* Telegram user is allowed
            return True
        return user_id in allowed

    # -- responses -----------------------------------------------------------

    @staticmethod
    def unauthorised(reason: str = "authentication required") -> web.Response:
        return web.json_response(
            {"ok": False, "error": reason, "auth_required": True}, status=401
        )


def generate_token_file(path: Path = TOKEN_FILE) -> str:
    """Write (or read) a token - handy from the CLI."""
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(24)
    path.write_text(value, encoding="utf-8")
    return value
