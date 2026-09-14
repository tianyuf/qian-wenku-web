import json
import sqlite3

import pytest

from qian_wenku_web.access import (
    AccessRequestLimitError,
    get_access_grant,
    initialize_access_database,
    issue_access_grant,
    normalize_email,
    send_access_code,
    update_access_grant_email,
    verify_access_code,
    verify_bearer_token,
    verify_turnstile,
)


def test_access_grant_lifecycle(tmp_path):
    db_path = tmp_path / "access" / "access.db"
    initialize_access_database(str(db_path))

    grant_id, code, _ = issue_access_grant(
        str(db_path), "secret", "reader@example.com", "2026-09", 90
    )
    assert code.startswith("qx_")
    assert verify_access_code(str(db_path), "secret", code) == grant_id
    assert get_access_grant(str(db_path), grant_id)[0] == "reader@example.com"
    assert update_access_grant_email(
        str(db_path), grant_id, "updated@example.com"
    )
    assert get_access_grant(str(db_path), grant_id)[0] == "updated@example.com"
    assert verify_access_code(str(db_path), "wrong-secret", code) is None
    assert verify_access_code(str(db_path), "secret", "wrong") is None

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE access_grants SET expires_at = 0 WHERE id = ?", (grant_id,)
        )
    assert verify_access_code(str(db_path), "secret", code) is None
    assert get_access_grant(str(db_path), grant_id) is None
    assert not update_access_grant_email(
        str(db_path), grant_id, "late@example.com"
    )


def test_access_grant_cooldown(tmp_path):
    db_path = tmp_path / "access.db"
    initialize_access_database(str(db_path))
    grant = issue_access_grant(
        str(db_path), "secret", "reader@example.com", "2026-09", 90
    )
    assert grant is not None
    assert issue_access_grant(
        str(db_path), "secret", "reader@example.com", "2026-09", 90
    ) is None


def test_access_grant_global_hourly_limit(tmp_path):
    db_path = tmp_path / "access.db"
    initialize_access_database(str(db_path))
    assert issue_access_grant(
        str(db_path), "secret", "one@example.com", "2026-09", 90,
        hourly_limit=1,
    ) is not None
    with pytest.raises(AccessRequestLimitError):
        issue_access_grant(
            str(db_path), "secret", "two@example.com", "2026-09", 90,
            hourly_limit=1,
        )

def test_bearer_token_validation(tmp_path):
    db_path = tmp_path / "access.db"
    initialize_access_database(str(db_path))
    _, code, _ = issue_access_grant(
        str(db_path), "secret", "reader@example.com", "2026-09", 90
    )

    assert verify_bearer_token(
        f"Bearer {code}", "operator", str(db_path), "secret"
    )
    assert verify_bearer_token(
        "Bearer operator", "operator", str(db_path), "secret"
    )
    assert not verify_bearer_token(
        f"Basic {code}", "operator", str(db_path), "secret"
    )
    assert not verify_bearer_token("Bearer wrong", "", str(db_path), "secret")


def test_normalize_email():
    assert normalize_email(" Reader@Example.COM ") == "Reader@example.com"
    assert normalize_email("not-an-email") is None


def test_send_access_code_uses_resend_api(monkeypatch):
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
    send_access_code(
        "secret-api-key",
        "Archive <access@example.com>",
        "reader@example.com",
        "qx_example",
        1798761600,
        42,
        "https://archive.example.com/",
    )

    request = captured["request"]
    payload = json.loads(request.data)
    assert request.full_url == "https://api.resend.com/emails"
    assert request.headers["Authorization"] == "Bearer secret-api-key"
    assert request.headers["Idempotency-key"] == "qian-wenku-access-42"
    assert request.headers["User-agent"] == "qian-wenku-web/0.1"
    assert payload["to"] == ["reader@example.com"]
    assert "qx_example" in payload["text"]
    assert "https://archive.example.com/login" in payload["html"]
    assert captured["timeout"] == 5


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
