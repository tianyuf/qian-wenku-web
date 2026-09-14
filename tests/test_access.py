import hashlib
import hmac
import json
import sqlite3

import pytest

from qian_wenku_web.access import (
    AccessRequestLimitError,
    consume_magic_link,
    create_mcp_token,
    get_access_grant,
    initialize_access_database,
    issue_magic_link,
    list_mcp_tokens,
    normalize_email,
    revoke_mcp_token,
    send_magic_link,
    verify_bearer_token,
    verify_mcp_token,
    verify_turnstile,
)


def test_access_grant_lifecycle(tmp_path):
    db_path = tmp_path / "access" / "access.db"
    initialize_access_database(str(db_path))

    _, login_token = issue_magic_link(
        str(db_path),
        "secret",
        "reader@example.com",
        "/browse",
        terms_version="2026-09",
        create_account=True,
    )
    grant_id, next_url = consume_magic_link(str(db_path), "secret", login_token)
    assert next_url == "/browse"
    assert consume_magic_link(str(db_path), "secret", login_token) is None
    assert get_access_grant(str(db_path), grant_id) == "reader@example.com"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE access_grants SET revoked_at = 1 WHERE id = ?", (grant_id,)
        )
    assert get_access_grant(str(db_path), grant_id) is None


def test_magic_link_cooldown(tmp_path):
    db_path = tmp_path / "access.db"
    initialize_access_database(str(db_path))
    link = issue_magic_link(
        str(db_path),
        "secret",
        "reader@example.com",
        "/",
        terms_version="2026-09",
        create_account=True,
    )
    assert link is not None
    assert issue_magic_link(
        str(db_path), "secret", "reader@example.com", "/"
    ) is None


def test_magic_link_rejects_unknown_email_and_expired_token(tmp_path):
    db_path = tmp_path / "access.db"
    initialize_access_database(str(db_path))
    assert issue_magic_link(
        str(db_path), "secret", "unknown@example.com", "/"
    ) is None
    _, token = issue_magic_link(
        str(db_path),
        "secret",
        "reader@example.com",
        "/",
        terms_version="2026-09",
        create_account=True,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE magic_links SET expires_at = 0")
        connection.execute("UPDATE magic_links SET created_at = 0")
    assert consume_magic_link(str(db_path), "secret", token) is None
    assert issue_magic_link(
        str(db_path), "secret", "READER@example.com", "/"
    ) is not None


def test_mcp_tokens_are_independently_revocable(tmp_path):
    db_path = tmp_path / "access.db"
    initialize_access_database(str(db_path))
    _, login_token = issue_magic_link(
        str(db_path),
        "secret",
        "reader@example.com",
        "/",
        terms_version="2026-09",
        create_account=True,
    )
    grant_id, _ = consume_magic_link(str(db_path), "secret", login_token)
    first_id, first_token = create_mcp_token(
        str(db_path), "secret", grant_id, "Claude Desktop"
    )
    _, second_token = create_mcp_token(
        str(db_path), "secret", grant_id, "OpenCode"
    )

    assert first_token.startswith("qxmcp_")
    assert verify_mcp_token(str(db_path), "secret", first_token)
    assert verify_mcp_token(str(db_path), "secret", second_token)
    assert revoke_mcp_token(str(db_path), grant_id, first_id)
    assert not verify_mcp_token(str(db_path), "secret", first_token)
    assert verify_mcp_token(str(db_path), "secret", second_token)
    assert [token["name"] for token in list_mcp_tokens(str(db_path), grant_id)] == [
        "OpenCode",
        "Claude Desktop",
    ]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT last_used_at FROM mcp_tokens WHERE name = 'OpenCode'"
        ).fetchone()[0] is None


def test_tokens_from_duplicate_email_grants_remain_manageable(tmp_path):
    db_path = tmp_path / "access.db"
    initialize_access_database(str(db_path))
    _, login_token = issue_magic_link(
        str(db_path),
        "secret",
        "reader@example.com",
        "/",
        terms_version="2026-09",
        create_account=True,
    )
    first_grant_id, _ = consume_magic_link(str(db_path), "secret", login_token)
    first_token_id, first_token = create_mcp_token(
        str(db_path), "secret", first_grant_id, "Older grant"
    )
    with sqlite3.connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO access_grants
                (email, code_hash, terms_version, requested_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("READER@example.com", b"placeholder", "2026-09", 2, 9223372036854775807),
        )
        second_grant_id = cursor.lastrowid

    assert list_mcp_tokens(str(db_path), second_grant_id)[0]["name"] == "Older grant"
    assert revoke_mcp_token(str(db_path), second_grant_id, first_token_id)
    assert not verify_mcp_token(str(db_path), "secret", first_token)


def test_magic_link_global_hourly_limit(tmp_path):
    db_path = tmp_path / "access.db"
    initialize_access_database(str(db_path))
    assert issue_magic_link(
        str(db_path),
        "secret",
        "one@example.com",
        "/",
        terms_version="2026-09",
        create_account=True,
        hourly_limit=1,
    ) is not None
    with pytest.raises(AccessRequestLimitError):
        issue_magic_link(
            str(db_path),
            "secret",
            "two@example.com",
            "/",
            terms_version="2026-09",
            create_account=True,
            hourly_limit=1,
        )


def test_bearer_token_validation(tmp_path):
    db_path = tmp_path / "access.db"
    initialize_access_database(str(db_path))
    _, login_token = issue_magic_link(
        str(db_path),
        "secret",
        "reader@example.com",
        "/",
        terms_version="2026-09",
        create_account=True,
    )
    grant_id, _ = consume_magic_link(str(db_path), "secret", login_token)
    _, mcp_token = create_mcp_token(str(db_path), "secret", grant_id, "Test")

    assert verify_bearer_token(
        f"Bearer {mcp_token}", "operator", str(db_path), "secret"
    )
    assert verify_bearer_token(
        "Bearer operator", "operator", str(db_path), "secret"
    )
    assert not verify_bearer_token(
        f"Basic {mcp_token}", "operator", str(db_path), "secret"
    )
    assert not verify_bearer_token("Bearer wrong", "", str(db_path), "secret")


def test_normalize_email():
    assert normalize_email(" Reader@Example.COM ") == "Reader@example.com"
    assert normalize_email("not-an-email") is None


def test_send_magic_link_uses_resend_api(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("qian_wenku_web.access.urlopen", fake_urlopen)
    send_magic_link(
        "secret-api-key",
        "Archive <access@example.com>",
        "reader@example.com",
        "qml_example",
        42,
        "https://archive.example.com/",
    )

    request = captured["request"]
    payload = json.loads(request.data)
    assert request.full_url == "https://api.resend.com/emails"
    assert request.headers["Authorization"] == "Bearer secret-api-key"
    assert request.headers["Idempotency-key"] == "qian-wenku-magic-link-42"
    assert request.headers["User-agent"] == "qian-wenku-web/0.1"
    assert payload["to"] == ["reader@example.com"]
    assert "qml_example" in payload["text"]
    assert "15 分钟后失效" in payload["text"]
    assert "https://archive.example.com/login#token=" in payload["html"]
    assert captured["timeout"] == 5


def test_current_schema_is_expanded_and_access_code_becomes_mcp_token(tmp_path):
    db_path = tmp_path / "access.db"
    code = "qx_existing"
    digest = hmac.new(b"secret", code.encode(), hashlib.sha256).digest()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE access_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                code_hash BLOB NOT NULL UNIQUE,
                terms_version TEXT NOT NULL,
                requested_at INTEGER NOT NULL,
                revoked_at INTEGER,
                last_used_at INTEGER
            )
            """
        )
        connection.execute(
            """
            INSERT INTO access_grants
                (email, code_hash, terms_version, requested_at)
            VALUES (?, ?, ?, ?)
            """,
            ("reader@example.com", digest, "2026-09", 1),
        )

    initialize_access_database(str(db_path))

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(access_grants)")
        }
    assert "expires_at" in columns
    assert "code_hash" in columns
    assert verify_mcp_token(str(db_path), "secret", code)
    assert list_mcp_tokens(str(db_path), 1)[0]["name"] == "Legacy access token"


def test_verify_turnstile_checks_action_and_hostname(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(
                {
                    "success": True,
                    "action": "request_access",
                    "hostname": "wenku.qianxuesen.org",
                }
            ).encode()

    monkeypatch.setattr(
        "qian_wenku_web.access.urlopen", lambda request, timeout: FakeResponse()
    )

    assert verify_turnstile(
        "secret",
        "token",
        "192.0.2.1",
        "request_access",
        {"wenku.qianxuesen.org"},
    )
    assert not verify_turnstile(
        "secret",
        "token",
        "192.0.2.1",
        "login",
        {"wenku.qianxuesen.org"},
    )
    assert not verify_turnstile(
        "secret",
        "token",
        "192.0.2.1",
        "request_access",
        {"example.com"},
    )
