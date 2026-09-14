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
    """Raised when an authentication email cannot be delivered."""


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
    with sqlite3.connect(db_path, timeout=10) as connection:
        connection.execute("BEGIN IMMEDIATE")
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'access_grants'"
        ).fetchone()
        if table_exists:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(access_grants)")
            }
            if "code_hash" not in columns:
                connection.execute("ALTER TABLE access_grants ADD COLUMN code_hash BLOB")
            if "expires_at" not in columns:
                connection.execute("ALTER TABLE access_grants ADD COLUMN expires_at INTEGER")
                connection.execute(
                    "UPDATE access_grants SET expires_at = 9223372036854775807"
                )
        else:
            connection.execute(
                """
                CREATE TABLE access_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL,
                    code_hash BLOB,
                    terms_version TEXT NOT NULL,
                    requested_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    revoked_at INTEGER,
                    last_used_at INTEGER
                )
                """
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS access_grants_email_idx "
            "ON access_grants(email, requested_at)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS access_grants_code_hash_idx "
            "ON access_grants(code_hash)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grant_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                token_hash BLOB NOT NULL UNIQUE,
                token_hint TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                revoked_at INTEGER,
                last_used_at INTEGER
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS mcp_tokens_grant_idx "
            "ON mcp_tokens(grant_id, created_at)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS access_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        legacy_migrated = connection.execute(
            "SELECT 1 FROM access_metadata WHERE key = 'legacy_tokens_migrated'"
        ).fetchone()
        if not legacy_migrated:
            connection.execute(
                """
                INSERT OR IGNORE INTO mcp_tokens
                    (grant_id, name, token_hash, token_hint, created_at,
                     revoked_at, last_used_at)
                SELECT id, 'Legacy access token', code_hash, 'qx_legacy',
                       requested_at, revoked_at, last_used_at
                FROM access_grants
                WHERE code_hash IS NOT NULL
                """
            )
            connection.execute(
                "INSERT INTO access_metadata (key, value) VALUES (?, ?)",
                ("legacy_tokens_migrated", str(int(time.time()))),
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS magic_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grant_id INTEGER NOT NULL,
                token_hash BLOB NOT NULL UNIQUE,
                next_url TEXT NOT NULL,
                purpose TEXT NOT NULL DEFAULT 'login',
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used_at INTEGER
            )
            """
        )
        magic_link_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(magic_links)")
        }
        if "purpose" not in magic_link_columns:
            connection.execute(
                "ALTER TABLE magic_links ADD COLUMN purpose TEXT NOT NULL DEFAULT 'login'"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS magic_links_grant_idx "
            "ON magic_links(grant_id, created_at)"
        )


def _code_hash(secret: str, code: str) -> bytes:
    return hmac.new(secret.encode(), code.encode(), hashlib.sha256).digest()


def _read_only_connection(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def issue_magic_link(
    path: str,
    secret: str,
    email: str,
    next_url: str,
    *,
    terms_version: str | None = None,
    create_account: bool = False,
    ttl_seconds: int = 900,
    cooldown_seconds: int = 60,
    hourly_limit: int = 100,
) -> tuple[int, str] | None:
    """Issue a one-time login link for an existing or newly created account."""
    now = int(time.time())
    purpose = "access_request" if create_account else "login"

    with sqlite3.connect(path, timeout=5) as connection:
        connection.execute("BEGIN IMMEDIATE")
        issued_last_hour = connection.execute(
            "SELECT COUNT(*) FROM magic_links WHERE purpose = ? AND created_at > ?",
            (purpose, now - 3600),
        ).fetchone()[0]
        if issued_last_hour >= hourly_limit:
            raise AccessRequestLimitError("Hourly magic-link limit reached")
        account = connection.execute(
            """
            SELECT id FROM access_grants
            WHERE email = ? COLLATE NOCASE AND revoked_at IS NULL
            ORDER BY requested_at DESC, id DESC
            LIMIT 1
            """,
            (email,),
        ).fetchone()
        if not account and not create_account:
            return None
        if account:
            grant_id = int(account[0])
            if create_account and terms_version:
                connection.execute(
                    "UPDATE access_grants SET terms_version = ? WHERE id = ?",
                    (terms_version, grant_id),
                )
        else:
            cursor = connection.execute(
                """
                INSERT INTO access_grants
                    (email, code_hash, terms_version, requested_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    email,
                    _code_hash(secret, "account_" + secrets.token_urlsafe(32)),
                    terms_version or "",
                    now,
                    9223372036854775807,
                ),
            )
            grant_id = int(cursor.lastrowid)

        recent = connection.execute(
            """
            SELECT 1 FROM magic_links
            WHERE grant_id = ? AND created_at > ?
            LIMIT 1
            """,
            (grant_id, now - cooldown_seconds),
        ).fetchone()
        if recent:
            return None

        connection.execute(
            "UPDATE magic_links SET used_at = ? WHERE grant_id = ? AND used_at IS NULL",
            (now, grant_id),
        )

        for _ in range(3):
            token = "qml_" + secrets.token_urlsafe(32)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO magic_links
                        (grant_id, token_hash, next_url, purpose, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        grant_id,
                        _code_hash(secret, token),
                        next_url,
                        purpose,
                        now,
                        now + ttl_seconds,
                    ),
                )
                return int(cursor.lastrowid), token
            except sqlite3.IntegrityError:
                continue

    raise RuntimeError("Unable to generate a unique magic link")


def consume_magic_link(
    path: str,
    secret: str,
    token: str,
) -> tuple[int, str] | None:
    """Consume a single-use login token and return its account and next URL."""
    if not token.startswith("qml_") or len(token) > 128:
        return None

    now = int(time.time())
    digest = _code_hash(secret, token)
    with sqlite3.connect(path, timeout=5) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT magic_links.id, magic_links.grant_id, magic_links.next_url
            FROM magic_links
            JOIN access_grants ON access_grants.id = magic_links.grant_id
            WHERE magic_links.token_hash = ?
              AND magic_links.expires_at > ?
              AND magic_links.used_at IS NULL
              AND access_grants.revoked_at IS NULL
            """,
            (digest, now),
        ).fetchone()
        if not row:
            return None
        link_id, grant_id, next_url = int(row[0]), int(row[1]), str(row[2])
        cursor = connection.execute(
            "UPDATE magic_links SET used_at = ? WHERE id = ? AND used_at IS NULL",
            (now, link_id),
        )
        if cursor.rowcount != 1:
            return None
        connection.execute(
            "UPDATE access_grants SET last_used_at = ? WHERE id = ?",
            (now, grant_id),
        )
        return grant_id, next_url


def delete_magic_link(path: str, link_id: int) -> None:
    """Delete an undelivered login link so the user can retry immediately."""
    with sqlite3.connect(path, timeout=5) as connection:
        connection.execute(
            "DELETE FROM magic_links WHERE id = ? AND used_at IS NULL", (link_id,)
        )


def is_access_grant_active(path: str, grant_id: int) -> bool:
    """Check whether a session's grant remains active."""
    with _read_only_connection(path) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM access_grants
            WHERE id = ? AND revoked_at IS NULL
            """,
            (grant_id,),
        ).fetchone()
    return row is not None


def get_access_grant(path: str, grant_id: int) -> str | None:
    """Return the email for an active grant."""
    with _read_only_connection(path) as connection:
        row = connection.execute(
            """
            SELECT email FROM access_grants
            WHERE id = ? AND revoked_at IS NULL
            """,
            (grant_id,),
        ).fetchone()
    return str(row[0]) if row else None


def create_mcp_token(
    path: str,
    secret: str,
    grant_id: int,
    name: str,
) -> tuple[int, str] | None:
    """Create a named MCP token and return its plaintext value once."""
    name = name.strip()
    if not name or len(name) > 80:
        raise ValueError("MCP token name must be between 1 and 80 characters")
    now = int(time.time())

    with sqlite3.connect(path, timeout=5) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT 1 FROM access_grants WHERE id = ? AND revoked_at IS NULL",
            (grant_id,),
        ).fetchone()
        if not row:
            return None
        for _ in range(3):
            token = "qxmcp_" + secrets.token_urlsafe(32)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO mcp_tokens
                        (grant_id, name, token_hash, token_hint, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        grant_id,
                        name,
                        _code_hash(secret, token),
                        f"qxmcp_...{token[-6:]}",
                        now,
                    ),
                )
                return int(cursor.lastrowid), token
            except sqlite3.IntegrityError:
                continue

    raise RuntimeError("Unable to generate a unique MCP token")


def list_mcp_tokens(path: str, grant_id: int) -> list[dict[str, int | str | None]]:
    """List active and revoked MCP tokens belonging to an account."""
    with _read_only_connection(path) as connection:
        rows = connection.execute(
            """
            SELECT token.id, token.name, token.token_hint, token.created_at,
                   token.revoked_at, token.last_used_at
            FROM mcp_tokens AS token
            JOIN access_grants AS token_account ON token_account.id = token.grant_id
            JOIN access_grants AS current_account ON current_account.id = ?
            WHERE token_account.email = current_account.email COLLATE NOCASE
              AND current_account.revoked_at IS NULL
            ORDER BY token.created_at DESC, token.id DESC
            """,
            (grant_id,),
        ).fetchall()
    return [
        {
            "id": int(row[0]),
            "name": str(row[1]),
            "hint": str(row[2]),
            "created_at": int(row[3]),
            "revoked_at": int(row[4]) if row[4] is not None else None,
            "last_used_at": int(row[5]) if row[5] is not None else None,
        }
        for row in rows
    ]


def revoke_mcp_token(path: str, grant_id: int, token_id: int) -> bool:
    """Revoke one MCP token belonging to the authenticated account."""
    with sqlite3.connect(path, timeout=5) as connection:
        cursor = connection.execute(
            """
            UPDATE mcp_tokens SET revoked_at = ?
            WHERE id = ? AND revoked_at IS NULL AND grant_id IN (
                SELECT sibling.id
                FROM access_grants AS sibling
                JOIN access_grants AS current_account ON current_account.id = ?
                WHERE sibling.email = current_account.email COLLATE NOCASE
                  AND current_account.revoked_at IS NULL
            )
            """,
            (int(time.time()), token_id, grant_id),
        )
    return cursor.rowcount == 1


def list_all_accounts(
    path: str, limit: int = 200, offset: int = 0
) -> list[dict[str, object]]:
    """List accounts with activity and token counts for operator review."""
    with _read_only_connection(path) as connection:
        rows = connection.execute(
            """
            SELECT account.id, account.email, account.requested_at,
                   account.revoked_at, account.last_used_at,
                   COUNT(token.id) AS token_count,
                   SUM(token.revoked_at IS NULL) AS active_token_count
            FROM access_grants AS account
            LEFT JOIN mcp_tokens AS token ON token.grant_id = account.id
            GROUP BY account.id
            ORDER BY account.requested_at DESC, account.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    accounts = []
    for row in rows:
        accounts.append({
            "id": int(row[0]),
            "email": str(row[1]),
            "created_at": int(row[2]),
            "revoked_at": int(row[3]) if row[3] is not None else None,
            "last_used_at": int(row[4]) if row[4] is not None else None,
            "token_count": int(row[5]),
            "active_token_count": int(row[6]),
        })
    return accounts


def count_all_accounts(path: str) -> int:
    """Return the total number of accounts for pagination."""
    with _read_only_connection(path) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM access_grants"
        ).fetchone()[0])


def revoke_account(path: str, grant_id: int) -> bool:
    """Revoke one account; its browser sessions and MCP tokens stop working."""
    now = int(time.time())
    with sqlite3.connect(path, timeout=5) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE access_grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now, grant_id),
        )
        if cursor.rowcount == 1:
            connection.execute(
                "UPDATE mcp_tokens SET revoked_at = ? "
                "WHERE grant_id = ? AND revoked_at IS NULL",
                (now, grant_id),
            )
            return True
    return False


def reinstate_account(path: str, grant_id: int) -> bool:
    """Reinstate one account. MCP tokens stay revoked until recreated."""
    with sqlite3.connect(path, timeout=5) as connection:
        cursor = connection.execute(
            "UPDATE access_grants SET revoked_at = NULL WHERE id = ? AND revoked_at IS NOT NULL",
            (grant_id,),
        )
    return cursor.rowcount == 1


def verify_mcp_token(path: str, secret: str, token: str) -> bool:
    """Validate an MCP token without requiring write access to the database."""
    if not token.startswith(("qx_", "qxmcp_")) or len(token) > 128:
        return False
    with _read_only_connection(path) as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM mcp_tokens
            JOIN access_grants ON access_grants.id = mcp_tokens.grant_id
            WHERE mcp_tokens.token_hash = ?
              AND mcp_tokens.revoked_at IS NULL
              AND access_grants.revoked_at IS NULL
            """,
            (_code_hash(secret, token),),
        ).fetchone()
    return row is not None


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
        return verify_mcp_token(access_db_path, access_code_secret, value)
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


def send_magic_link(
    api_key: str,
    sender: str,
    recipient: str,
    token: str,
    link_id: int,
    base_url: str,
) -> None:
    """Send a single-use login link through Resend's HTTPS API."""
    login_url = f"{base_url.rstrip('/')}/login#token={token}"
    safe_login_url = html.escape(login_url, quote=True)
    _send_email(
        api_key,
        sender,
        recipient,
        subject="登录 qianxuesen.org",
        text=(
            "请使用以下一次性链接登录 qianxuesen.org：\n\n"
            f"{login_url}\n\n"
            "链接将在 15 分钟后失效，且仅可使用一次。"
            "如非本人操作，可忽略本邮件。"
        ),
        html=(
            "<p>请使用以下一次性链接登录 qianxuesen.org：</p>"
            f'<p><a href="{safe_login_url}">点击登录</a></p>'
            "<p>链接将在 15 分钟后失效，且仅可使用一次。"
            "如非本人操作，可忽略本邮件。</p>"
        ),
        idempotency_key=f"qian-wenku-magic-link-{link_id}",
    )


def send_token_revoked_notice(
    api_key: str,
    sender: str,
    recipient: str,
    token_name: str,
    token_hint: str,
    revoked_by_operator: bool = False,
) -> None:
    """Notify the account owner that an MCP token was revoked."""
    if revoked_by_operator:
        reason = "管理员已撤销您的一个 MCP 访问令牌。"
    else:
        reason = "您（或使用该令牌的人）撤销了一个 MCP 访问令牌。"
    _send_email(
        api_key,
        sender,
        recipient,
        subject="MCP 访问令牌已撤销 · qianxuesen.org",
        text=(
            f"{reason}\n\n"
            f"令牌名称：{token_name}\n"
            f"令牌标识：{token_hint}\n\n"
            "该令牌已立即失效。如非本人操作，请尽快登录并检查您的账户。"
        ),
        html=(
            f"<p>{reason}</p>"
            f"<p>令牌名称：<strong>{html.escape(token_name)}</strong><br>"
            f"令牌标识：<code>{html.escape(token_hint)}</code></p>"
            "<p>该令牌已立即失效。如非本人操作，请尽快登录并检查您的账户。</p>"
        ),
        idempotency_key=f"qian-wenku-token-revoked-{token_hint}",
    )


def _send_email(
    api_key: str,
    sender: str,
    recipient: str,
    *,
    subject: str,
    text: str,
    html: str,
    idempotency_key: str,
) -> None:
    """POST one message through Resend's HTTPS API."""
    payload = {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "text": text,
        "html": html,
    }
    request = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "User-Agent": "qian-wenku-web/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            if response.status not in {200, 201}:
                raise EmailDeliveryError(f"Resend returned HTTP {response.status}")
    except (HTTPError, URLError, TimeoutError) as error:
        raise EmailDeliveryError("Resend could not deliver the email") from error
