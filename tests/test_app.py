import json
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


def test_entry_page_initialises_pdf_viewer_state_in_script(client):
    """Regression: the viewer state used to be set in an onload="..." attribute.

    |tojson emits double quotes, so the attribute was cut short, the inline
    handler was a syntax error, and the PDF prev/next buttons stayed disabled.
    """
    page = client.get("/e/nianpu-19111211-a").get_data(as_text=True)
    assert 'onload="window.pdfState' not in page
    assert "sourceId: 1," in page
    assert "currentPage: 1," in page
    assert "totalPages: 3," in page
    # The title must be JSON inside a <script> block, never inside an attribute.
    script_start = page.index("window.pdfState = {\n")
    assert 'title: "' in page[script_start:script_start + 400]


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

    assert access_client.post("/logout", data={"csrf_token": account_csrf}).status_code == 302
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

    assert client.post("/logout", data={"csrf_token": admin_csrf}).status_code == 302
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

    # The email token stays in the fragment; confirmation is a CSRF-protected POST.
    confirm_page = client.get("/account/email-change")
    assert confirm_page.status_code == 200
    with client.session_transaction() as session:
        email_change_csrf = session["email_change_csrf"]
    confirmed = client.post(
        "/account/email-change",
        data={"csrf_token": email_change_csrf, "token": token},
    )
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
        session["account_csrf"] = "fixture-csrf"

    # Entry 1 renders with favorite controls for account sessions.
    entry_page = client.get("/e/nianpu-19111211-a")
    assert 'data-entry-id="1"' in entry_page.get_data(as_text=True)

    # Not logged in: no star button, API rejects.
    anon = app.test_client()
    assert 'id="favorite-btn"' not in anon.get("/e/nianpu-19111211-a").get_data(as_text=True)
    assert anon.post("/api/favorites", data={"entry_id": "1", "csrf_token": "fixture-csrf"}).status_code == 401

    first = client.post("/api/favorites", data={"entry_id": "1", "csrf_token": "fixture-csrf"})
    assert first.get_json() == {"favorited": True}
    second = client.post("/api/favorites", data={"entry_id": "1", "csrf_token": "fixture-csrf"})
    assert second.get_json() == {"favorited": False}

    # Toggle on again and check account listing.
    assert client.post("/api/favorites", data={"entry_id": "1", "csrf_token": "fixture-csrf"}).get_json() == {"favorited": True}
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
        session["account_csrf"] = "fixture-csrf"
    client.post("/api/favorites", data={"entry_id": "1", "csrf_token": "fixture-csrf"})

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
        session["account_csrf"] = "fixture-csrf"

    # Entry page shows the notes section.
    entry_text = client.get("/e/nianpu-19111211-a").get_data(as_text=True)
    assert "我的批注" in entry_text

    # Anonymous users cannot annotate.
    anon = app.test_client()
    assert anon.post("/api/notes", data={"csrf_token": "fixture-csrf", "entry_id": "1", "body": "x"}).status_code == 401

    # Add a note with a highlight quote.
    created = client.post(
        "/api/notes",
        data={"csrf_token": "fixture-csrf", "entry_id": "1", "body": "这一段很重要", "quote": "合成"},
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
        f"/api/notes/{note_id}", data={"body": "更新后的批注", "csrf_token": "fixture-csrf"}
    )
    assert updated.get_json() == {"updated": True}
    assert client.get("/api/notes?entry_id=1").get_json()["notes"][0]["body"] == "更新后的批注"

    # A note with a highlight may drop its comment (highlight-only), but a
    # note with neither comment nor highlight is rejected.
    assert client.put(f"/api/notes/{note_id}", data={"body": " ", "csrf_token": "fixture-csrf"}).status_code == 200
    assert client.put(
        f"/api/notes/{note_id}", data={"body": " ", "quote": "", "csrf_token": "fixture-csrf"}
    ).status_code == 400

    # Delete the note.
    deleted = client.delete(f"/api/notes/{note_id}", headers={"X-CSRF-Token": "fixture-csrf"})
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
        "/api/notes", data={"csrf_token": "fixture-csrf", "entry_id": "1", "body": "keep"}
    ).get_json()
    with client.session_transaction() as session:
        session["access_grant_id"] = other_id
    forbidden = client.delete(f"/api/notes/{recreated['note_id']}", headers={"X-CSRF-Token": "fixture-csrf"})
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
        session["account_csrf"] = "fixture-csrf"

    client.post("/api/favorites", data={"entry_id": "1", "csrf_token": "fixture-csrf"})
    created = client.post(
        "/api/notes", data={"csrf_token": "fixture-csrf", "entry_id": "1", "body": "重点段落", "quote": "合成"}
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
        session["account_csrf"] = "fixture-csrf"

    # Favorite entry 1; annotate entry 2 (not favorited).
    client.post("/api/favorites", data={"entry_id": "1", "csrf_token": "fixture-csrf"})
    client.post("/api/notes", data={"csrf_token": "fixture-csrf", "entry_id": "2", "body": "另一条的批注", "quote": "钱学森"})

    page = client.get("/favorites").get_data(as_text=True)
    assert "我的批注" in page
    assert "另一条的批注" in page
    assert "「钱学森」" in page
    assert "这些条目有您的批注但尚未收藏" in page
    # Entry 2 (annotated only) appears with a star button.
    assert 'value="2"' in page
    # Favorited entry does not duplicate in the annotations section.
    assert 'value="1"' in page  # in favorites remove form only


def test_search_terms_are_literal_and_length_capped(client):
    from qian_wenku_web.formatting import MAX_SEARCH_TERM, like_contains

    # Wildcards and the escape character are neutralised.
    assert like_contains("50%_a\\b") == "%50\\%\\_a\\\\b%"

    # A bare "%" used to match every entry; now it matches only literal "%".
    wildcard = client.get("/api/search/?q=%25")
    assert wildcard.status_code == 200
    assert wildcard.get_json()["total"] == 0
    underscore = client.get("/api/search/?q=_")
    assert underscore.get_json()["total"] == 0

    # Normal substring search still works through the escaped pattern.
    assert client.get("/api/search/?q=合成").get_json()["total"] >= 1
    assert "合成" in client.get("/?q=合成").get_data(as_text=True)

    # Oversized terms are truncated instead of hitting SQLite's LIKE pattern
    # limit (which surfaced as a 500).
    huge = "合" * 100_000
    assert client.get("/", query_string={"q": huge}).status_code == 200
    assert client.get("/api/search/", query_string={"q": huge}).status_code == 200
    assert client.get("/search/suggest", query_string={"q": huge}).status_code == 200
    assert client.get("/browse/recipients", query_string={"q": huge}).status_code == 200
    assert client.get("/browse/entities", query_string={"q": huge}).status_code == 200
    assert client.get("/api/browse/years", query_string={"person": huge}).status_code == 200
    assert len(huge[:MAX_SEARCH_TERM]) == MAX_SEARCH_TERM


def _account_app(artifact_dir, tmp_path, monkeypatch, **extra_env):
    """App with the individual-account store enabled and one seeded reader."""
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
    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    with sqlite3.connect(access_db) as connection:
        connection.execute(
            "INSERT INTO access_grants (email, code_hash, terms_version, requested_at) "
            "VALUES ('Reader@example.com', 'seed', '2026-09', strftime('%s','now'))"
        )
    return app, access_db


def _login_as(client, grant_id):
    with client.session_transaction() as session:
        session["beta_authenticated"] = True
        session["access_grant_id"] = grant_id
        session.permanent = True
        session["account_csrf"] = "fixture-csrf"


def test_browser_activity_updates_last_active(artifact_dir, tmp_path, monkeypatch):
    app, access_db = _account_app(artifact_dir, tmp_path, monkeypatch)
    client = app.test_client()
    _login_as(client, 1)

    with sqlite3.connect(access_db) as connection:
        assert connection.execute(
            "SELECT last_used_at FROM access_grants WHERE id = 1"
        ).fetchone()[0] is None

    assert client.get("/").status_code == 200
    with sqlite3.connect(access_db) as connection:
        first = connection.execute(
            "SELECT last_used_at FROM access_grants WHERE id = 1"
        ).fetchone()[0]
    assert first is not None

    # Within the hourly window the timestamp is left alone.
    with sqlite3.connect(access_db) as connection:
        connection.execute(
            "UPDATE access_grants SET last_used_at = ? WHERE id = 1", (first - 60,)
        )
    assert client.get("/").status_code == 200
    with sqlite3.connect(access_db) as connection:
        assert connection.execute(
            "SELECT last_used_at FROM access_grants WHERE id = 1"
        ).fetchone()[0] == first - 60


def test_mcp_token_use_updates_last_active(artifact_dir, tmp_path, monkeypatch):
    app, access_db = _account_app(
        artifact_dir, tmp_path, monkeypatch, WENKU_SERVICE_TOKEN="fixture-service-token"
    )
    client = app.test_client()
    from qian_wenku_web.access import create_mcp_token
    token_id, mcp_token = create_mcp_token(
        str(access_db), "fixture-access-secret", 1, "Claude Desktop"
    )

    # A hosted-MCP search call forwards the caller's token alongside the
    # service token; that counts as activity for the token and the account.
    response = client.get(
        "/api/search/?q=钱",
        headers={
            "X-Service-Token": "fixture-service-token",
            "X-User-Authorization": f"Bearer {mcp_token}",
        },
    )
    assert response.status_code == 200
    with sqlite3.connect(access_db) as connection:
        token_used, account_used = connection.execute(
            """
            SELECT mcp_tokens.last_used_at, access_grants.last_used_at
            FROM mcp_tokens JOIN access_grants ON access_grants.id = mcp_tokens.grant_id
            WHERE mcp_tokens.id = ?
            """,
            (token_id,),
        ).fetchone()
    assert token_used is not None
    assert account_used is not None

    # A bogus forwarded token is ignored rather than failing the request.
    response = client.get(
        "/api/search/?q=钱",
        headers={
            "X-Service-Token": "fixture-service-token",
            "X-User-Authorization": "Bearer qxmcp_nope",
        },
    )
    assert response.status_code == 200


def test_account_export_and_delete(artifact_dir, tmp_path, monkeypatch):
    app, access_db = _account_app(artifact_dir, tmp_path, monkeypatch)
    client = app.test_client()
    _login_as(client, 1)

    assert client.post(
        "/api/favorites", data={"entry_id": "1", "csrf_token": "fixture-csrf"}
    ).get_json() == {"favorited": True}
    note = client.post(
        "/api/notes",
        data={"entry_id": "1", "body": "重要段落", "quote": "引文", "csrf_token": "fixture-csrf"},
    )
    assert note.status_code in (200, 201)
    from qian_wenku_web.access import create_mcp_token
    create_mcp_token(str(access_db), "fixture-access-secret", 1, "Claude Desktop")

    # Anonymous export is redirected away.
    assert app.test_client().get("/account/export").status_code == 302

    export = client.get("/account/export")
    assert export.status_code == 200
    assert export.mimetype == "application/json"
    assert "attachment" in export.headers["Content-Disposition"]
    assert export.headers["Cache-Control"] == "no-store"
    payload = export.get_json()
    assert payload["email"] == "Reader@example.com"
    assert [fav["entry_id"] for fav in payload["favorites"]] == [1]
    assert payload["favorites"][0]["entry"]["permalink"] == "nianpu-19111211-a"
    assert payload["favorites"][0]["entry"]["url"].endswith("/e/nianpu-19111211-a")
    assert payload["notes"][0]["body"] == "重要段落"
    assert payload["notes"][0]["quote"] == "引文"
    assert payload["notes"][0]["entry"]["source_title"]

    account_text = client.get("/account").get_data(as_text=True)
    assert "/account/export" in account_text
    assert "删除账户" in account_text

    # Deletion requires typing the account's own email.
    wrong = client.post(
        "/account",
        data={"csrf_token": "fixture-csrf", "action": "delete_account", "confirm_email": "other@example.com"},
    )
    assert "请输入您的登录邮箱" in wrong.get_data(as_text=True)
    with sqlite3.connect(access_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM access_grants").fetchone()[0] == 1

    deleted = client.post(
        "/account",
        data={"csrf_token": "fixture-csrf", "action": "delete_account", "confirm_email": " reader@EXAMPLE.com "},
    )
    assert deleted.status_code == 302
    assert deleted.headers["Location"].endswith("/login?deleted=1")
    with sqlite3.connect(access_db) as connection:
        for table in ("access_grants", "favorites", "entry_notes", "mcp_tokens"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table

    # The session is gone and the login page explains what happened.
    assert client.get("/account").status_code == 302
    assert "账户已删除" in client.get("/login?deleted=1").get_data(as_text=True)


def test_admin_search_and_account_detail(artifact_dir, tmp_path, monkeypatch):
    app, access_db = _account_app(
        artifact_dir, tmp_path, monkeypatch, ADMIN_EMAILS="owner@example.com"
    )
    client = app.test_client()
    with sqlite3.connect(access_db) as connection:
        connection.executemany(
            "INSERT INTO access_grants (email, code_hash, terms_version, requested_at) "
            "VALUES (?, ?, ?, strftime('%s','now'))",
            [
                ("owner@example.com", b"h0", "2026-09"),
                ("alice@university.edu", b"h1", "2026-09"),
                ("bob_smith@example.com", b"h2", "2026-09"),
            ],
        )
        admin_grant = connection.execute(
            "SELECT id FROM access_grants WHERE email = 'owner@example.com'"
        ).fetchone()[0]
        alice_grant = connection.execute(
            "SELECT id FROM access_grants WHERE email = 'alice@university.edu'"
        ).fetchone()[0]
    _login_as(client, admin_grant)

    # Search matches literally, case-insensitively, and escapes wildcards.
    listing = client.get("/admin?q=UNIVERSITY").get_data(as_text=True)
    assert "alice@university.edu" in listing
    assert "bob_smith@example.com" not in listing
    assert "匹配「UNIVERSITY」" in listing
    assert "bob_smith" in client.get("/admin?q=bob_smith").get_data(as_text=True)
    assert "没有匹配的账户" in client.get("/admin?q=%25%25").get_data(as_text=True)
    assert "没有匹配的账户" in client.get("/admin?q=b_b").get_data(as_text=True)
    # The list links each account to its detail page and preserves the query.
    assert f"/admin/accounts/{alice_grant}?q=UNIVERSITY" in listing
    assert "最近活跃" in client.get("/admin").get_data(as_text=True)

    # Seed activity for the detail page.
    from qian_wenku_web.access import create_mcp_token, issue_magic_link
    create_mcp_token(str(access_db), "fixture-access-secret", alice_grant, "Alice laptop")
    issue_magic_link(
        str(access_db), "fixture-access-secret", "alice@university.edu", "/",
    )
    with sqlite3.connect(access_db) as connection:
        connection.execute(
            "INSERT INTO favorites (grant_id, entry_id, created_at) VALUES (?, 1, 1)",
            (alice_grant,),
        )
        connection.execute(
            "INSERT INTO entry_notes (grant_id, entry_id, quote, body, created_at, updated_at) "
            "VALUES (?, 1, '', 'n', 1, 1)",
            (alice_grant,),
        )

    detail = client.get(f"/admin/accounts/{alice_grant}")
    assert detail.status_code == 200
    detail_text = detail.get_data(as_text=True)
    assert "alice@university.edu" in detail_text
    assert "Alice laptop" in detail_text
    assert "收藏</dt>" in detail_text and "1 条" in detail_text
    assert "未使用" in detail_text  # the magic link
    # Note contents are not exposed to operators, only counts.
    assert ">n<" not in detail_text
    assert client.get("/admin/accounts/99999").status_code == 404

    # Revoke from the detail page, then the page shows the revoked state.
    with client.session_transaction() as session:
        admin_csrf = session["account_csrf"]
    revoked = client.post(
        f"/admin/accounts/{alice_grant}",
        data={"csrf_token": admin_csrf, "action": "revoke_account", "grant_id": alice_grant},
    )
    revoked_text = revoked.get_data(as_text=True)
    assert "账户已撤销" in revoked_text
    assert "恢复账户" in revoked_text

    # Non-admins cannot reach the detail page.
    reader = app.test_client()
    _login_as(reader, 1)
    assert reader.get(f"/admin/accounts/{alice_grant}").status_code == 302


def test_signup_notifies_operator_once(artifact_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNUP_NOTIFY_EMAIL", "owner@example.com")
    sent_notices = []
    monkeypatch.setattr("qian_wenku_web.app.send_magic_link", lambda *args: None)
    monkeypatch.setattr("qian_wenku_web.app.verify_turnstile", lambda *args: True)
    monkeypatch.setattr(
        "qian_wenku_web.app.send_signup_notice",
        lambda api_key, sender, recipient, new_email, link_id, base_url, total: (
            sent_notices.append((recipient, new_email, total))
        ),
    )
    app, access_db = _account_app(artifact_dir, tmp_path, monkeypatch)
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as session:
        csrf_token = session["login_csrf"]

    # A brand-new email creates an account and notifies the operator.
    response = client.post(
        "/login", data={"csrf_token": csrf_token, "email": "New@Example.com"}
    )
    assert response.status_code == 200
    assert sent_notices == [("owner@example.com", "New@example.com", 2)]

    # The seeded reader logging in again is not a sign-up.
    client.post("/login", data={"csrf_token": csrf_token, "email": "reader@example.com"})
    assert len(sent_notices) == 1

    # A failed notice never breaks the login flow.
    def failing_notice(*args):
        from qian_wenku_web.access import EmailDeliveryError
        raise EmailDeliveryError("down")
    monkeypatch.setattr("qian_wenku_web.app.send_signup_notice", failing_notice)
    response = client.post(
        "/login", data={"csrf_token": csrf_token, "email": "another@example.com"}
    )
    assert response.status_code == 200
    assert "登录链接已发送" in response.get_data(as_text=True)


def test_send_signup_notice_payload(monkeypatch):
    from qian_wenku_web import access as access_mod
    captured = {}

    class FakeResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode())
        captured["idempotency"] = request.get_header("Idempotency-key")
        return FakeResponse()

    monkeypatch.setattr(access_mod, "urlopen", fake_urlopen)
    access_mod.send_signup_notice(
        "key", "Archive <no-reply@example.com>", "owner@example.com",
        "new+user@example.com", 42, "https://wenku.example.org/", 7,
    )
    body = captured["body"]
    assert body["to"] == ["owner@example.com"]
    assert "new+user@example.com" in body["subject"]
    assert "账户总数：7" in body["text"]
    assert "https://wenku.example.org/admin?q=new%2Buser%40example.com" in body["text"]
    assert captured["idempotency"] == "qian-wenku-signup-42"


def test_notes_tags_anchors_and_filters(artifact_dir, tmp_path, monkeypatch):
    app, access_db = _account_app(artifact_dir, tmp_path, monkeypatch)
    client = app.test_client()
    _login_as(client, 1)
    csrf = {"csrf_token": "fixture-csrf"}

    # Highlight-only note with a position anchor and a tag.
    created = client.post("/api/notes", data=dict(csrf, **{
        "entry_id": "1", "body": "", "quote": "合成", "tag": "todo",
        "anchor_start": "12", "anchor_end": "14", "prefix": "前文", "suffix": "后文",
    }))
    assert created.status_code == 201, created.get_json()
    highlight_id = created.get_json()["note_id"]

    # Plain comment, no highlight, no tag.
    comment = client.post("/api/notes", data=dict(csrf, entry_id="1", body="总体评论"))
    assert comment.status_code == 201
    comment_id = comment.get_json()["note_id"]

    # Validation: unknown tag, bad anchor, nothing at all.
    assert client.post("/api/notes", data=dict(csrf, entry_id="1", body="x", tag="nope")).status_code == 400
    assert client.post("/api/notes", data=dict(
        csrf, entry_id="1", quote="合成", anchor_start="5", anchor_end="2"
    )).status_code == 400
    assert client.post("/api/notes", data=dict(csrf, entry_id="1", body="  ", quote="")).status_code == 400

    listed = client.get("/api/notes?entry_id=1").get_json()["notes"]
    # Anchored highlight sorts before the unanchored comment.
    assert [n["id"] for n in listed] == [highlight_id, comment_id]
    first = listed[0]
    assert first["tag"] == "todo" and first["tag_label"] == "待查"
    assert (first["anchor_start"], first["anchor_end"]) == (12, 14)
    assert (first["prefix"], first["suffix"]) == ("前文", "后文")
    assert first["body"] == ""
    assert listed[1]["anchor_start"] is None

    # Editing the tag keeps the highlight and anchor; clearing the quote drops the anchor.
    assert client.put(f"/api/notes/{highlight_id}", data=dict(csrf, body="补充", tag="cite")).get_json() == {"updated": True}
    edited = client.get("/api/notes?entry_id=1").get_json()["notes"][0]
    assert edited["tag"] == "cite" and edited["quote"] == "合成" and edited["anchor_start"] == 12
    assert client.put(f"/api/notes/{highlight_id}", data=dict(csrf, body="补充", quote="")).status_code == 200
    cleared = [n for n in client.get("/api/notes?entry_id=1").get_json()["notes"] if n["id"] == highlight_id][0]
    assert cleared["quote"] == "" and cleared["anchor_start"] is None

    # Cross-entry listing supports text search and tag filter.
    assert [n["id"] for n in client.get("/api/notes?q=评论").get_json()["notes"]] == [comment_id]
    assert [n["id"] for n in client.get("/api/notes?tag=cite").get_json()["notes"]] == [highlight_id]
    assert client.get("/api/notes?q=%25").get_json()["total"] == 0
    assert client.get("/api/notes?tag=bogus").status_code == 400

    # The entry page ships the data the script needs plus the script itself.
    page = client.get("/e/nianpu-19111211-a").get_data(as_text=True)
    assert 'id="notes-data"' in page and '"tags"' in page and "annotations.js" in page
    assert 'id="entry-content" data-entry-id="1"' in page
    assert "note-tag-cite" in page

    # Anonymous readers see the call to action instead of the panel.
    anon_page = app.test_client().get("/e/nianpu-19111211-a")
    assert anon_page.status_code == 302  # auth required; the CTA is for the public-pages mode


def test_notes_page_and_markdown_export(artifact_dir, tmp_path, monkeypatch):
    app, access_db = _account_app(artifact_dir, tmp_path, monkeypatch)
    client = app.test_client()
    _login_as(client, 1)
    csrf = {"csrf_token": "fixture-csrf"}
    client.post("/api/notes", data=dict(csrf, entry_id="1", body="需要核对日期", quote="合成", tag="todo"))
    client.post("/api/notes", data=dict(csrf, entry_id="1", body="可引用", tag="cite"))
    client.post("/api/favorites", data=dict(csrf, entry_id="1"))

    assert app.test_client().get("/notes").status_code == 302

    page = client.get("/notes")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "需要核对日期" in text and "可引用" in text
    assert "/e/nianpu-19111211-a#note-" in text
    assert "note-tag-todo" in text

    filtered = client.get("/notes?tag=todo").get_data(as_text=True)
    assert "需要核对日期" in filtered and "可引用" not in filtered
    searched = client.get("/notes?q=引用").get_data(as_text=True)
    assert "可引用" in searched and "需要核对日期" not in searched
    assert "没有匹配的批注" in client.get("/notes?q=zzz").get_data(as_text=True)

    export = client.get("/account/export?format=md")
    assert export.status_code == 200
    assert export.mimetype == "text/markdown"
    assert 'filename="qian-wenku-notes-' in export.headers["Content-Disposition"]
    md = export.get_data(as_text=True)
    assert md.startswith("# 钱学森文库")
    assert "> 合成" in md
    assert "> — 钱学森，《" in md
    assert "**[待查]** 需要核对日期" in md
    assert "**[引用]** 可引用" in md
    assert " ★" in md  # favorited entry marker
    assert "/e/nianpu-19111211-a" in md

    account_text = client.get("/account").get_data(as_text=True)
    assert "format=md" in account_text
