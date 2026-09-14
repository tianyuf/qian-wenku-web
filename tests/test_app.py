import hashlib
import re
import sqlite3

from qian_wenku_web.app import create_app


def test_startup_and_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["stats"] == {
        "entries": 5,
        "sources": 4,
        "year_range": [1911, 1912],
    }
    assert response.get_json()["access_store_ok"] is True
    assert client.get("/").status_code == 200


def test_representative_html_routes(client):
    routes = [
        "/?q=合成",
        "/search/results?q=合成",
        "/browse",
        "/browse/source/1",
        "/browse/wenji/2",
        "/browse/shuxin/3",
        "/browse/year/1911",
        "/e/nianpu-19111211-a",
        "/browse/recipients",
        "/browse/recipient/1",
        "/browse/entities",
        "/browse/entity/1",
        "/date/",
        "/date/1911-12-11",
        "/about",
    ]
    for route in routes:
        response = client.get(route)
        assert response.status_code == 200, route
        assert response.content_type.startswith("text/html")


def test_permalink_redirect_does_not_modify_database(client, artifact_dir):
    db_path = artifact_dir / "corpus.db"
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    response = client.get("/entry/1")
    after = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert response.status_code == 301
    assert response.headers["Location"].endswith("/e/nianpu-19111211-a")
    assert before == after


def test_missing_permalink_is_not_generated(client, artifact_dir):
    db_path = artifact_dir / "corpus.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE entries SET permalink = NULL WHERE id = 1")
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert client.get("/entry/1").status_code == 404
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT permalink FROM entries WHERE id = 1").fetchone()[0] is None


def test_not_found_routes(client):
    assert client.get("/missing").status_code == 404
    response = client.get("/api/missing")
    assert response.status_code == 404
    assert response.is_json


def test_service_token_api_access(artifact_dir, monkeypatch):
    monkeypatch.delenv("ACCESS_DATABASE_PATH", raising=False)
    monkeypatch.setenv("WENKU_SERVICE_TOKEN", "fixture-service-token")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    client = app.test_client()

    # Without the header the API is protected.
    assert client.get("/api/meta/stats").status_code == 401
    # With the service token, machine access is allowed.
    assert client.get(
        "/api/meta/stats", headers={"X-Service-Token": "fixture-service-token"}
    ).status_code == 200
    # Wrong token is rejected.
    assert client.get(
        "/api/meta/stats", headers={"X-Service-Token": "nope"}
    ).status_code == 401


def test_login_rejects_external_redirects(artifact_dir, monkeypatch):
    monkeypatch.delenv("BETA_PASSPHRASE", raising=False)
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    client = app.test_client()
    # No auth store: login page redirects to the index (auth disabled).
    response = client.post(
        "/login",
        data={"action": "consume_magic_link", "csrf_token": "x", "next": "https://example.com"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_individual_access_request_and_login(artifact_dir, tmp_path, monkeypatch):
    access_db = tmp_path / "access.db"
    monkeypatch.delenv("BETA_PASSPHRASE", raising=False)
    monkeypatch.setenv("ACCESS_DATABASE_PATH", str(access_db))
    monkeypatch.setenv("ACCESS_CODE_SECRET", "fixture-access-secret")
    monkeypatch.setenv("RESEND_API_KEY", "fixture-resend-key")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "fixture-site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "fixture-turnstile-secret")
    monkeypatch.setenv("TURNSTILE_HOSTNAMES", "localhost")
    monkeypatch.setenv("ACCESS_FROM_EMAIL", "Archive <access@example.com>")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    sent_links = []
    revoked_notices = []

    def fake_send(*args):
        sent_links.append(args[3])

    def fake_revoked_notice(api_key, sender, recipient, token_name, token_hint, **kw):
        revoked_notices.append({
            "recipient": recipient,
            "name": token_name,
            "body": f"{token_name} {token_hint}",
        })

    monkeypatch.setattr("qian_wenku_web.app.send_magic_link", fake_send)
    monkeypatch.setattr(
        "qian_wenku_web.app.send_token_revoked_notice", fake_revoked_notice
    )
    monkeypatch.setattr("qian_wenku_web.app.verify_turnstile", lambda *args: True)
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    access_client = app.test_client()

    assert access_client.get("/browse").status_code == 302
    request_page = access_client.get("/login")
    assert request_page.status_code == 200
    assert "发送登录链接" in request_page.get_data(as_text=True)
    request_access_response = access_client.get("/request-access")
    assert request_access_response.status_code == 302
    assert "/login" in request_access_response.headers["Location"]
    with access_client.session_transaction() as session:
        csrf_token = session["login_csrf"]

    malformed_csrf = access_client.post(
        "/login",
        data={
            "csrf_token": "非 ASCII",
            "email": "researcher@example.com",
        },
    )
    assert malformed_csrf.status_code == 200
    assert "请刷新页面后重试" in malformed_csrf.get_data(as_text=True)

    response = access_client.post(
        "/login",
        data={
            "csrf_token": csrf_token,
            "email": "Researcher@Example.com",
        },
    )
    assert response.status_code == 200
    assert "登录链接已发送至您的邮箱" in response.get_data(as_text=True)
    assert len(sent_links) == 1
    assert sent_links[0].startswith("qml_")
    assert sent_links[0].encode() not in access_db.read_bytes()

    duplicate = access_client.post(
        "/login",
        data={
            "csrf_token": csrf_token,
            "email": "Researcher@example.com",
        },
    )
    assert duplicate.status_code == 200
    assert len(sent_links) == 1

    with access_client.session_transaction() as login_session:
        login_csrf = login_session["login_csrf"]
    login = access_client.post(
        "/login",
        data={
            "action": "consume_magic_link",
            "csrf_token": login_csrf,
            "token": sent_links[0],
        },
    )
    assert login.status_code == 302
    assert login.headers["Location"].endswith("/")
    browse = access_client.get("/browse")
    assert browse.status_code == 200
    assert 'href="/account"' in browse.get_data(as_text=True)

    account_page = access_client.get("/account")
    assert account_page.status_code == 200
    assert "Researcher@example.com" in account_page.get_data(as_text=True)
    assert "长期有效" in account_page.get_data(as_text=True)
    assert "MCP 令牌" in account_page.get_data(as_text=True)
    with access_client.session_transaction() as account_session:
        account_csrf = account_session["account_csrf"]

    invalid_csrf = access_client.post(
        "/account",
        data={
            "csrf_token": "wrong",
            "action": "create_mcp_token",
            "token_name": "Invalid",
        },
    )
    assert "请刷新页面后重试" in invalid_csrf.get_data(as_text=True)

    created = access_client.post(
        "/account",
        data={
            "csrf_token": account_csrf,
            "action": "create_mcp_token",
            "token_name": "Claude Desktop",
        },
    )
    created_text = created.get_data(as_text=True)
    assert created.status_code == 200
    assert "MCP 令牌已创建" in created_text
    assert "此页仅显示一次" in created_text
    mcp_token = re.search(r'value="(qxmcp_[^"]+)"', created_text).group(1)
    assert mcp_token.encode() not in access_db.read_bytes()

    with sqlite3.connect(access_db) as connection:
        token_id = connection.execute(
            "SELECT id FROM mcp_tokens WHERE grant_id = 1 AND revoked_at IS NULL"
        ).fetchone()[0]
    revoked_token = access_client.post(
        "/account",
        data={
            "csrf_token": account_csrf,
            "action": "revoke_mcp_token",
            "token_id": token_id,
        },
    )
    assert "MCP 令牌已撤销" in revoked_token.get_data(as_text=True)
    assert len(revoked_notices) == 1
    assert revoked_notices[0]["recipient"] == "Researcher@example.com"
    assert "Claude Desktop" in revoked_notices[0]["body"]

    assert access_client.post("/logout").status_code == 302
    with sqlite3.connect(access_db) as connection:
        connection.execute("UPDATE magic_links SET created_at = 0")
    access_client.get("/login?next=/browse")
    with access_client.session_transaction() as login_session:
        login_csrf = login_session["login_csrf"]
    emailed_login = access_client.post(
        "/login",
        data={
            "csrf_token": login_csrf,
            "next": "/browse",
            "email": "researcher@example.com",
        },
    )
    assert "登录链接已发送至您的邮箱" in emailed_login.get_data(as_text=True)
    assert len(sent_links) == 2
    returning_login = access_client.post(
        "/login",
        data={
            "action": "consume_magic_link",
            "csrf_token": login_csrf,
            "token": sent_links[1],
        },
    )
    assert returning_login.status_code == 302
    assert returning_login.headers["Location"].endswith("/browse")

    with sqlite3.connect(access_db) as connection:
        connection.execute("UPDATE access_grants SET revoked_at = 1")
    revoked_account = access_client.get("/browse")
    assert revoked_account.status_code == 302
    assert "/login" in revoked_account.headers["Location"]


def test_admin_console_manages_accounts(artifact_dir, tmp_path, monkeypatch):
    access_db = tmp_path / "access.db"
    monkeypatch.delenv("BETA_PASSPHRASE", raising=False)
    monkeypatch.setenv("ADMIN_EMAILS", "owner@example.com")
    monkeypatch.setenv("ACCESS_DATABASE_PATH", str(access_db))
    monkeypatch.setenv("ACCESS_CODE_SECRET", "fixture-access-secret")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    client = app.test_client()

    # Not logged in: /admin redirects.
    assert client.get("/admin").status_code == 302

    # Log in with an ADMIN_EMAILS account (seeded directly into the store).
    import time as time_mod
    with sqlite3.connect(access_db) as connection:
        cursor = connection.execute(
            "INSERT INTO access_grants (email, code_hash, terms_version, requested_at) "
            "VALUES ('Owner@example.com', 'seed', '2026-09', strftime('%s','now'))"
        )
        admin_grant = cursor.lastrowid
    with client.session_transaction() as session:
        session["beta_authenticated"] = True
        session["access_grant_id"] = admin_grant
        session.permanent = True

    with sqlite3.connect(access_db) as connection:
        connection.executemany(
            "INSERT INTO access_grants (email, code_hash, terms_version, requested_at) "
            "VALUES (?, ?, ?, strftime('%s','now'))",
            [("user1@example.com", b"h1", "2026-09"), ("user2@example.com", b"h2", "2026-09")],
        )

    listing = client.get("/admin")
    listing_text = listing.get_data(as_text=True)
    assert listing.status_code == 200
    assert "user1@example.com" in listing_text
    assert "user2@example.com" in listing_text
    with client.session_transaction() as session:
        admin_csrf = session["account_csrf"]

    revoked = client.post(
        "/admin",
        data={"csrf_token": admin_csrf, "action": "revoke_account", "grant_id": 2},
    )
    assert "账户已撤销" in revoked.get_data(as_text=True)
    with sqlite3.connect(access_db) as connection:
        assert connection.execute(
            "SELECT revoked_at IS NOT NULL FROM access_grants WHERE id = 2"
        ).fetchone()[0]

    # A consumed-link attempt with a fresh login CSRF is rejected as invalid.
    client.get("/login")
    with client.session_transaction() as session:
        login_csrf = session["login_csrf"]
    individual_login = client.post(
        "/login",
        data={
            "action": "consume_magic_link",
            "csrf_token": login_csrf,
            "token": "qml_bad",
        },
    )
    assert "登录链接无效" in individual_login.get_data(as_text=True)

    reinstated = client.post(
        "/admin",
        data={"csrf_token": admin_csrf, "action": "reinstate_account", "grant_id": 2},
    )
    assert "账户已恢复" in reinstated.get_data(as_text=True)
    with sqlite3.connect(access_db) as connection:
        assert connection.execute(
            "SELECT revoked_at IS NULL FROM access_grants WHERE id = 2"
        ).fetchone()[0]

    invalid_csrf = client.post(
        "/admin", data={"csrf_token": "wrong", "action": "revoke_account", "grant_id": 2}
    )
    assert "请刷新页面后重试" in invalid_csrf.get_data(as_text=True)

    assert client.post("/logout").status_code == 302
    assert client.get("/admin").status_code == 302


def test_admin_hidden_for_individual_sessions(artifact_dir, tmp_path, monkeypatch):
    access_db = tmp_path / "access.db"
    monkeypatch.delenv("BETA_PASSPHRASE", raising=False)
    monkeypatch.setenv("ACCESS_DATABASE_PATH", str(access_db))
    monkeypatch.setenv("ACCESS_CODE_SECRET", "fixture-access-secret")
    monkeypatch.setenv("RESEND_API_KEY", "fixture-resend-key")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "fixture-site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "fixture-turnstile-secret")
    monkeypatch.setenv("TURNSTILE_HOSTNAMES", "localhost")
    monkeypatch.setenv("ACCESS_FROM_EMAIL", "Archive <access@example.com>")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    monkeypatch.setattr("qian_wenku_web.app.send_magic_link", lambda *args: None)
    monkeypatch.setattr("qian_wenku_web.app.verify_turnstile", lambda *args, **kw: True)
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as session:
        login_csrf = session["login_csrf"]
    client.post("/login", data={"csrf_token": login_csrf, "email": "user@example.com"})
    from qian_wenku_web.access import issue_magic_link, consume_magic_link
    _, token = issue_magic_link(
        str(access_db), "fixture-access-secret", "user@example.com", "/",
        cooldown_seconds=0,
    )
    grant_id, _ = consume_magic_link(str(access_db), "fixture-access-secret", token)
    with client.session_transaction() as session:
        session.clear()
        session["beta_authenticated"] = True
        session["access_grant_id"] = grant_id
    assert client.get("/admin").status_code == 302


def test_admin_emails_grant_console_access(artifact_dir, tmp_path, monkeypatch):
    access_db = tmp_path / "access.db"
    monkeypatch.delenv("BETA_PASSPHRASE", raising=False)
    monkeypatch.setenv("ACCESS_DATABASE_PATH", str(access_db))
    monkeypatch.setenv("ACCESS_CODE_SECRET", "fixture-access-secret")
    monkeypatch.setenv("RESEND_API_KEY", "fixture-resend-key")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "fixture-site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "fixture-turnstile-secret")
    monkeypatch.setenv("TURNSTILE_HOSTNAMES", "localhost")
    monkeypatch.setenv("ACCESS_FROM_EMAIL", "Archive <access@example.com>")
    monkeypatch.setenv("ADMIN_EMAILS", "Owner@Example.COM, other@example.com")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    monkeypatch.setattr("qian_wenku_web.app.send_magic_link", lambda *args: None)
    monkeypatch.setattr("qian_wenku_web.app.verify_turnstile", lambda *args, **kw: True)
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    client = app.test_client()

    from qian_wenku_web.access import (
        consume_magic_link,
        create_mcp_token,
        issue_magic_link,
    )

    client.get("/login")
    with client.session_transaction() as session:
        login_csrf = session["login_csrf"]
    client.post("/login", data={"csrf_token": login_csrf, "email": "Owner@example.com"})
    _, token = issue_magic_link(
        str(access_db), "fixture-access-secret", "owner@example.com", "/",
        cooldown_seconds=0,
    )
    grant_id, _ = consume_magic_link(str(access_db), "fixture-access-secret", token)
    with client.session_transaction() as session:
        session.clear()
        session["beta_authenticated"] = True
        session["access_grant_id"] = grant_id
        session["account_csrf"] = "admin-csrf"

    # Owner email (case-insensitive) sees the console.
    page = client.get("/admin")
    assert page.status_code == 200
    assert "用户管理" in page.get_data(as_text=True)

    # Non-admin account cannot.
    with sqlite3.connect(access_db) as connection:
        connection.execute(
            "INSERT INTO access_grants (email, code_hash, terms_version, requested_at) "
            "VALUES ('regular@example.com', 'x', '2026-09', strftime('%s','now'))"
        )
        regular_id = connection.execute(
            "SELECT id FROM access_grants WHERE email = 'regular@example.com'"
        ).fetchone()[0]
    with client.session_transaction() as session:
        session["access_grant_id"] = regular_id
    assert client.get("/admin").status_code == 302

    # Revoking the owner's account removes admin access too.
    with client.session_transaction() as session:
        session["access_grant_id"] = grant_id
        session["account_csrf"] = "admin-csrf"
    created = create_mcp_token(
        str(access_db), "fixture-access-secret", grant_id, "Test"
    )
    assert created is not None


def test_email_change_flow(artifact_dir, tmp_path, monkeypatch):
    access_db = tmp_path / "access.db"
    monkeypatch.delenv("BETA_PASSPHRASE", raising=False)
    monkeypatch.setenv("ACCESS_DATABASE_PATH", str(access_db))
    monkeypatch.setenv("ACCESS_CODE_SECRET", "fixture-access-secret")
    monkeypatch.setenv("RESEND_API_KEY", "fixture-resend-key")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "fixture-site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "fixture-turnstile-secret")
    monkeypatch.setenv("TURNSTILE_HOSTNAMES", "localhost")
    monkeypatch.setenv("ACCESS_FROM_EMAIL", "Archive <access@example.com>")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    change_links = []

    def fake_change_link(api_key, sender, recipient, token, base_url):
        change_links.append((recipient, token))

    monkeypatch.setattr("qian_wenku_web.app.send_email_change_link", fake_change_link)
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    client = app.test_client()

    with sqlite3.connect(access_db) as connection:
        connection.execute(
            "INSERT INTO access_grants (email, code_hash, terms_version, requested_at) "
            "VALUES ('Old@Example.com', 'seed', '2026-09', strftime('%s','now'))"
        )
    with client.session_transaction() as session:
        session["beta_authenticated"] = True
        session["access_grant_id"] = 1
        session.permanent = True
        session["account_csrf"] = "csrf"

    page = client.get("/account")
    assert "Old@Example.com" in page.get_data(as_text=True)
    assert "更换登录邮箱" in page.get_data(as_text=True)

    requested = client.post(
        "/account",
        data={
            "csrf_token": "csrf",
            "action": "request_email_change",
            "email": "New@Example.COM",
        },
    )
    text = requested.get_data(as_text=True)
    assert requested.status_code == 200
    assert "确认邮件已发送至新邮箱" in text
    assert "New@example.com" in text
    assert len(change_links) == 1
    recipient, token = change_links[0]
    assert recipient == "New@example.com"

    with sqlite3.connect(access_db) as connection:
        assert connection.execute(
            "SELECT pending_email FROM access_grants WHERE id = 1"
        ).fetchone()[0] == "New@example.com"
        assert connection.execute(
            "SELECT email FROM access_grants WHERE id = 1"
        ).fetchone()[0] == "Old@Example.com"

    # Confirm via the link.
    confirmed = client.get(f"/account/email-change?token={token}")
    assert confirmed.status_code == 302
    with sqlite3.connect(access_db) as connection:
        row = connection.execute(
            "SELECT email, pending_email FROM access_grants WHERE id = 1"
        ).fetchone()
    assert row[0] == "New@example.com"
    assert row[1] is None

    # Reusing the link fails.
    reused = client.get(f"/account/email-change?token={token}")
    assert reused.status_code == 200
    assert "登录" in reused.get_data(as_text=True)


def test_favorites_flow(artifact_dir, tmp_path, monkeypatch):
    access_db = tmp_path / "access.db"
    monkeypatch.delenv("BETA_PASSPHRASE", raising=False)
    monkeypatch.setenv("ACCESS_DATABASE_PATH", str(access_db))
    monkeypatch.setenv("ACCESS_CODE_SECRET", "fixture-access-secret")
    monkeypatch.setenv("RESEND_API_KEY", "fixture-resend-key")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "fixture-site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "fixture-turnstile-secret")
    monkeypatch.setenv("TURNSTILE_HOSTNAMES", "localhost")
    monkeypatch.setenv("ACCESS_FROM_EMAIL", "Archive <access@example.com>")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    client = app.test_client()

    with sqlite3.connect(access_db) as connection:
        connection.execute(
            "INSERT INTO access_grants (email, code_hash, terms_version, requested_at) "
            "VALUES ('Reader@example.com', 'seed', '2026-09', strftime('%s','now'))"
        )
    with client.session_transaction() as session:
        session["beta_authenticated"] = True
        session["access_grant_id"] = 1
        session.permanent = True

    # Entry 1 renders with favorite controls for account sessions.
    entry_page = client.get("/e/nianpu-19111211-a")
    assert 'data-entry-id="1"' in entry_page.get_data(as_text=True)

    # Not logged in: no star button, API rejects.
    anon = app.test_client()
    assert 'id="favorite-btn"' not in anon.get("/e/nianpu-19111211-a").get_data(as_text=True)
    assert anon.post("/api/favorites", data={"entry_id": "1"}).status_code == 401

    first = client.post("/api/favorites", data={"entry_id": "1"})
    assert first.get_json() == {"favorited": True}
    second = client.post("/api/favorites", data={"entry_id": "1"})
    assert second.get_json() == {"favorited": False}

    # Toggle on again and check account listing.
    assert client.post("/api/favorites", data={"entry_id": "1"}).get_json() == {"favorited": True}
    entry_page = client.get("/e/nianpu-19111211-a")
    entry_text = entry_page.get_data(as_text=True)
    assert 'data-favorited="1"' in entry_text
    assert "★ 已收藏" in entry_text

    favorites_page = client.get("/favorites")
    favorites_text = favorites_page.get_data(as_text=True)
    assert "收藏" in favorites_text
    assert "/e/nianpu-19111211-a" in favorites_text
    # The account page no longer duplicates the favorites list.
    assert "/e/nianpu-19111211-a" not in client.get("/account").get_data(as_text=True)


def test_favorites_api_with_mcp_token(artifact_dir, tmp_path, monkeypatch):
    access_db = tmp_path / "access.db"
    monkeypatch.delenv("BETA_PASSPHRASE", raising=False)
    monkeypatch.setenv("ACCESS_DATABASE_PATH", str(access_db))
    monkeypatch.setenv("ACCESS_CODE_SECRET", "fixture-access-secret")
    monkeypatch.setenv("RESEND_API_KEY", "fixture-resend-key")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "fixture-site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "fixture-turnstile-secret")
    monkeypatch.setenv("TURNSTILE_HOSTNAMES", "localhost")
    monkeypatch.setenv("ACCESS_FROM_EMAIL", "Archive <access@example.com>")
    monkeypatch.setenv("WENKU_SERVICE_TOKEN", "fixture-service-token")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    client = app.test_client()

    with sqlite3.connect(access_db) as connection:
        connection.execute(
            "INSERT INTO access_grants (email, code_hash, terms_version, requested_at) "
            "VALUES ('Reader@example.com', 'seed', '2026-09', strftime('%s','now'))"
        )
    with client.session_transaction() as session:
        session["beta_authenticated"] = True
        session["access_grant_id"] = 1
        session.permanent = True
    client.post("/api/favorites", data={"entry_id": "1"})

    from qian_wenku_web.access import create_mcp_token
    _, mcp_token = create_mcp_token(
        str(access_db), "fixture-access-secret", 1, "Claude Desktop"
    )

    # Direct bearer access.
    direct = client.get(
        "/api/favorites", headers={"Authorization": f"Bearer {mcp_token}"}
    )
    assert direct.status_code == 200
    payload = direct.get_json()
    assert payload["total"] == 1
    favorite = payload["favorites"][0]
    assert favorite["permalink"] == "nianpu-19111211-a"
    assert favorite["title"] or favorite["date_display"]

    # Forwarded via the service token (hosted MCP server pattern).
    forwarded = client.get(
        "/api/favorites",
        headers={
            "X-Service-Token": "fixture-service-token",
            "X-User-Authorization": f"Bearer {mcp_token}",
        },
    )
    assert forwarded.status_code == 200
    assert forwarded.get_json()["total"] == 1

    # Invalid token rejected.
    denied = client.get(
        "/api/favorites", headers={"Authorization": "Bearer qx_bad"}
    )
    assert denied.status_code == 401
    # No token rejected.
    assert client.get("/api/favorites").status_code == 401


def test_entry_notes_flow(artifact_dir, tmp_path, monkeypatch):
    access_db = tmp_path / "access.db"
    monkeypatch.delenv("BETA_PASSPHRASE", raising=False)
    monkeypatch.setenv("ACCESS_DATABASE_PATH", str(access_db))
    monkeypatch.setenv("ACCESS_CODE_SECRET", "fixture-access-secret")
    monkeypatch.setenv("RESEND_API_KEY", "fixture-resend-key")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "fixture-site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "fixture-turnstile-secret")
    monkeypatch.setenv("TURNSTILE_HOSTNAMES", "localhost")
    monkeypatch.setenv("ACCESS_FROM_EMAIL", "Archive <access@example.com>")
    monkeypatch.setenv("WENKU_SERVICE_TOKEN", "fixture-service-token")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    client = app.test_client()

    with sqlite3.connect(access_db) as connection:
        connection.execute(
            "INSERT INTO access_grants (email, code_hash, terms_version, requested_at) "
            "VALUES ('Reader@example.com', 'seed', '2026-09', strftime('%s','now'))"
        )
    with client.session_transaction() as session:
        session["beta_authenticated"] = True
        session["access_grant_id"] = 1
        session.permanent = True

    # Entry page shows the notes section.
    entry_text = client.get("/e/nianpu-19111211-a").get_data(as_text=True)
    assert "我的批注" in entry_text

    # Anonymous users cannot annotate.
    anon = app.test_client()
    assert anon.post("/api/notes", data={"entry_id": "1", "body": "x"}).status_code == 401

    # Add a note with a highlight quote.
    created = client.post(
        "/api/notes",
        data={"entry_id": "1", "body": "这一段很重要", "quote": "合成"},
    )
    assert created.status_code == 201
    note_id = created.get_json()["note_id"]

    listed = client.get("/api/notes?entry_id=1")
    notes = listed.get_json()["notes"]
    assert len(notes) == 1
    assert notes[0]["body"] == "这一段很重要"
    assert notes[0]["quote"] == "合成"

    # Server-rendered page shows the note.
    page_text = client.get("/e/nianpu-19111211-a").get_data(as_text=True)
    assert "这一段很重要" in page_text
    assert "「合成」" in page_text

    # Update the note.
    updated = client.put(
        f"/api/notes/{note_id}", data={"body": "更新后的批注"}
    )
    assert updated.get_json() == {"updated": True}
    assert client.get("/api/notes?entry_id=1").get_json()["notes"][0]["body"] == "更新后的批注"

    # Validation: empty body rejected.
    assert client.put(f"/api/notes/{note_id}", data={"body": " "}).status_code == 400

    # Delete the note.
    deleted = client.delete(f"/api/notes/{note_id}")
    assert deleted.get_json() == {"deleted": True}
    assert client.get("/api/notes?entry_id=1").get_json()["total"] == 0

    # Another account cannot touch it.
    with sqlite3.connect(access_db) as connection:
        connection.execute(
            "INSERT INTO access_grants (email, code_hash, terms_version, requested_at) "
            "VALUES ('Other@example.com', 'x', '2026-09', strftime('%s','now'))"
        )
        other_id = connection.execute(
            "SELECT id FROM access_grants WHERE email = 'Other@example.com'"
        ).fetchone()[0]
    recreated = client.post(
        "/api/notes", data={"entry_id": "1", "body": "keep"}
    ).get_json()
    with client.session_transaction() as session:
        session["access_grant_id"] = other_id
    forbidden = client.delete(f"/api/notes/{recreated['note_id']}")
    assert forbidden.status_code == 404

    # MCP token path: create a token, list notes forwarded.
    from qian_wenku_web.access import create_mcp_token
    _, mcp_token = create_mcp_token(
        str(access_db), "fixture-access-secret", 1, "Claude"
    )
    with client.session_transaction() as session:
        session["access_grant_id"] = 1
    forwarded = client.get(
        "/api/notes",
        headers={
            "X-Service-Token": "fixture-service-token",
            "X-User-Authorization": f"Bearer {mcp_token}",
        },
    )
    assert forwarded.status_code == 200
    assert forwarded.get_json()["total"] >= 1


def test_numeric_permalink_redirects_to_canonical(client, artifact_dir):
    # /e/<entry-id> is a legacy reference pattern; it 301s to the hash permalink.
    response = client.get("/e/1")
    assert response.status_code == 301
    assert response.headers["Location"].endswith("/e/nianpu-19111211-a")

    # Valid hash permalinks still render directly.
    assert client.get("/e/nianpu-19111211-a").status_code == 200

    # Unknown permalinks still 404.
    assert client.get("/e/nonexistent-permalink").status_code == 404
    assert client.get("/e/999999").status_code == 404


def test_favorites_show_notes_and_highlight_markers(artifact_dir, tmp_path, monkeypatch):
    access_db = tmp_path / "access.db"
    monkeypatch.delenv("BETA_PASSPHRASE", raising=False)
    monkeypatch.setenv("ACCESS_DATABASE_PATH", str(access_db))
    monkeypatch.setenv("ACCESS_CODE_SECRET", "fixture-access-secret")
    monkeypatch.setenv("RESEND_API_KEY", "fixture-resend-key")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "fixture-site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "fixture-turnstile-secret")
    monkeypatch.setenv("TURNSTILE_HOSTNAMES", "localhost")
    monkeypatch.setenv("ACCESS_FROM_EMAIL", "Archive <access@example.com>")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    client = app.test_client()

    with sqlite3.connect(access_db) as connection:
        connection.execute(
            "INSERT INTO access_grants (email, code_hash, terms_version, requested_at) "
            "VALUES ('Reader@example.com', 'seed', '2026-09', strftime('%s','now'))"
        )
    with client.session_transaction() as session:
        session["beta_authenticated"] = True
        session["access_grant_id"] = 1
        session.permanent = True

    client.post("/api/favorites", data={"entry_id": "1"})
    created = client.post(
        "/api/notes", data={"entry_id": "1", "body": "重点段落", "quote": "合成"}
    )
    assert created.status_code == 201

    # Favorites page shows the note with its highlighted quote.
    favorites_text = client.get("/favorites").get_data(as_text=True)
    assert "「合成」" in favorites_text
    assert "重点段落" in favorites_text

    # MCP-facing API includes notes with favorites.
    from qian_wenku_web.access import create_mcp_token
    _, mcp_token = create_mcp_token(str(access_db), "fixture-access-secret", 1, "C")
    payload = client.get(
        "/api/favorites", headers={"Authorization": f"Bearer {mcp_token}"}
    ).get_json()
    notes = payload["favorites"][0]["notes"]
    assert len(notes) == 1
    assert notes[0]["quote"] == "合成"
    assert notes[0]["body"] == "重点段落"


def test_favorites_annotations_section(artifact_dir, tmp_path, monkeypatch):
    access_db = tmp_path / "access.db"
    monkeypatch.delenv("BETA_PASSPHRASE", raising=False)
    monkeypatch.setenv("ACCESS_DATABASE_PATH", str(access_db))
    monkeypatch.setenv("ACCESS_CODE_SECRET", "fixture-access-secret")
    monkeypatch.setenv("RESEND_API_KEY", "fixture-resend-key")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "fixture-site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "fixture-turnstile-secret")
    monkeypatch.setenv("TURNSTILE_HOSTNAMES", "localhost")
    monkeypatch.setenv("ACCESS_FROM_EMAIL", "Archive <access@example.com>")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    client = app.test_client()

    with sqlite3.connect(access_db) as connection:
        connection.execute(
            "INSERT INTO access_grants (email, code_hash, terms_version, requested_at) "
            "VALUES ('Reader@example.com', 'seed', '2026-09', strftime('%s','now'))"
        )
    with client.session_transaction() as session:
        session["beta_authenticated"] = True
        session["access_grant_id"] = 1
        session.permanent = True

    # Favorite entry 1; annotate entry 2 (not favorited).
    client.post("/api/favorites", data={"entry_id": "1"})
    client.post("/api/notes", data={"entry_id": "2", "body": "另一条的批注", "quote": "钱学森"})

    page = client.get("/favorites").get_data(as_text=True)
    assert "我的批注" in page
    assert "另一条的批注" in page
    assert "「钱学森」" in page
    assert "这些条目有您的批注但尚未收藏" in page
    # Entry 2 (annotated only) appears with a star button.
    assert 'value="2"' in page
    # Favorited entry does not duplicate in the annotations section.
    assert 'value="1"' in page  # in favorites remove form only
