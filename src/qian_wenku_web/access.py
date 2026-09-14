"""Persistent access grants and transactional email delivery."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
logger = logging.getLogger(__name__)


class EmailDeliveryError(RuntimeError):
    """Raised when an access-code email cannot be delivered."""


class AccessRequestLimitError(RuntimeError):
    """Raised when the global issuance limit has been reached."""


def normalize_email(value: str) -> str | None:
    """Return a normalized email address, or None when plainly invalid."""
    email = value.strip()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        return None
    local, domain = email.rsplit("@", 1)
    return f"{local}@{domain.casefold()}"


def initialize_access_database(path: str) -> None:
    """Create the independent, writable access-grant store."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS access_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                code_hash BLOB NOT NULL UNIQUE,
                terms_version TEXT NOT NULL,
                requested_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                revoked_at INTEGER,
                last_used_at INTEGER
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS access_grants_email_idx "
            "ON access_grants(email, requested_at)"
        )


def _code_hash(secret: str, code: str) -> bytes:
    return hmac.new(secret.encode(), code.encode(), hashlib.sha256).digest()


def _read_only_connection(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def issue_access_grant(
    path: str,
    secret: str,
    email: str,
    terms_version: str,
    ttl_days: int,
    cooldown_seconds: int = 900,
    hourly_limit: int = 100,
) -> tuple[int, str, int] | None:
    """Issue a grant, or return None when the email is in cooldown."""
    now = int(time.time())
    expires_at = now + ttl_days * 86400

    with sqlite3.connect(path, timeout=5) as connection:
        connection.execute("BEGIN IMMEDIATE")
        issued_last_hour = connection.execute(
            "SELECT COUNT(*) FROM access_grants WHERE requested_at > ?",
            (now - 3600,),
        ).fetchone()[0]
        if issued_last_hour >= hourly_limit:
            raise AccessRequestLimitError("Hourly access-code limit reached")
        recent = connection.execute(
            """
            SELECT 1 FROM access_grants
            WHERE email = ? AND requested_at > ? AND revoked_at IS NULL
            LIMIT 1
            """,
            (email, now - cooldown_seconds),
        ).fetchone()
        if recent:
            return None

        for _ in range(3):
            code = "qx_" + secrets.token_urlsafe(24)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO access_grants
                        (email, code_hash, terms_version, requested_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (email, _code_hash(secret, code), terms_version, now, expires_at),
                )
                return cursor.lastrowid, code, expires_at
            except sqlite3.IntegrityError:
                continue

    raise RuntimeError("Unable to generate a unique access code")


def verify_access_code(
    path: str,
    secret: str,
    code: str,
    *,
    mark_used: bool = False,
) -> int | None:
    """Return the active grant ID matching a supplied plaintext code."""
    if not code.startswith("qx_") or len(code) > 128:
        return None

    now = int(time.time())
    digest = _code_hash(secret, code)
    connection_factory = sqlite3.connect if mark_used else _read_only_connection
    with connection_factory(path) as connection:
        row = connection.execute(
            """
            SELECT id FROM access_grants
            WHERE code_hash = ? AND expires_at > ? AND revoked_at IS NULL
            """,
            (digest, now),
        ).fetchone()
        if not row:
            return None
        grant_id = int(row[0])
        if mark_used:
            connection.execute(
                "UPDATE access_grants SET last_used_at = ? WHERE id = ?",
                (now, grant_id),
            )
        return grant_id


def is_access_grant_active(path: str, grant_id: int) -> bool:
    """Check whether a session's grant remains active."""
    with _read_only_connection(path) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM access_grants
            WHERE id = ? AND expires_at > ? AND revoked_at IS NULL
            """,
            (grant_id, int(time.time())),
        ).fetchone()
    return row is not None


def get_access_grant(path: str, grant_id: int) -> tuple[str, int] | None:
    """Return the email and expiration for an active grant."""
    with _read_only_connection(path) as connection:
        row = connection.execute(
            """
            SELECT email, expires_at FROM access_grants
            WHERE id = ? AND expires_at > ? AND revoked_at IS NULL
            """,
            (grant_id, int(time.time())),
        ).fetchone()
    return (str(row[0]), int(row[1])) if row else None


def update_access_grant_email(path: str, grant_id: int, email: str) -> bool:
    """Update the email attached to an active grant."""
    with sqlite3.connect(path, timeout=5) as connection:
        cursor = connection.execute(
            """
            UPDATE access_grants SET email = ?
            WHERE id = ? AND expires_at > ? AND revoked_at IS NULL
            """,
            (email, grant_id, int(time.time())),
        )
    return cursor.rowcount == 1


def access_database_is_healthy(path: str) -> bool:
    """Return whether the configured grant store can be queried."""
    try:
        with _read_only_connection(path) as connection:
            connection.execute("SELECT 1 FROM access_grants LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def verify_bearer_token(
    authorization: str,
    operator_token: str,
    access_db_path: str,
    access_code_secret: str,
) -> bool:
    """Validate a hosted MCP Authorization header."""
    scheme, separator, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not value:
        return False
    if operator_token and hmac.compare_digest(value.encode(), operator_token.encode()):
        return True
    if access_db_path:
        return verify_access_code(access_db_path, access_code_secret, value) is not None
    return False


def verify_turnstile(
    secret: str,
    token: str,
    remote_ip: str | None,
    expected_action: str,
    expected_hostnames: set[str],
) -> bool:
    """Verify a Cloudflare Turnstile challenge response."""
    if not token or len(token) > 2048 or not expected_hostnames:
        return False
    values = {"secret": secret, "response": token}
    if remote_ip:
        values["remoteip"] = remote_ip
    request = Request(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        data=urlencode(values).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "qian-wenku-web/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            result = json.loads(response.read())
        valid = (
            isinstance(result, dict)
            and result.get("success") is True
            and result.get("action") == expected_action
            and result.get("hostname") in expected_hostnames
        )
        if not valid:
            logger.warning(
                "Turnstile rejected request: errors=%s action=%r hostname=%r",
                result.get("error-codes") if isinstance(result, dict) else None,
                result.get("action") if isinstance(result, dict) else None,
                result.get("hostname") if isinstance(result, dict) else None,
            )
        return valid
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        logger.warning("Turnstile verification request failed: %s", type(error).__name__)
        return False


def send_access_code(
    api_key: str,
    sender: str,
    recipient: str,
    code: str,
    expires_at: int,
    grant_id: int,
    base_url: str,
) -> None:
    """Send an issued code through Resend's HTTPS API."""
    expiration = time.strftime("%B %d, %Y", time.gmtime(expires_at))
    login_url = f"{base_url.rstrip('/')}/login"
    safe_code = html.escape(code)
    payload = {
        "from": sender,
        "to": [recipient],
        "subject": "Your Qian Xuesen Archive access code",
        "text": (
            "Your Qian Xuesen Archive access code is:\n\n"
            f"{code}\n\n"
            f"Log in at {login_url}\n\n"
            f"This code expires on {expiration}. It also works as your MCP "
            "bearer token. Do not share it."
        ),
        "html": (
            "<p>Your Qian Xuesen Archive access code is:</p>"
            f"<p><code>{safe_code}</code></p>"
            f'<p><a href="{html.escape(login_url)}">Log in to the archive</a></p>'
            f"<p>This code expires on {expiration}. It also works as your MCP "
            "bearer token. Do not share it.</p>"
        ),
    }
    request = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": f"qian-wenku-access-{grant_id}",
            "User-Agent": "qian-wenku-web/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            if response.status not in {200, 201}:
                raise EmailDeliveryError(f"Resend returned HTTP {response.status}")
    except (HTTPError, URLError, TimeoutError) as error:
        raise EmailDeliveryError("Resend could not deliver the access code") from error
