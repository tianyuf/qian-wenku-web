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


def test_beta_login_flow(artifact_dir, monkeypatch):
    monkeypatch.setenv("BETA_PASSPHRASE", "fixture-passphrase")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    beta_client = app.test_client()

    response = beta_client.get("/browse?content_type=wenji")
    assert response.status_code == 302
    assert "/login?next=/browse?content_type%3Dwenji" in response.headers["Location"]
    assert beta_client.get("/api/meta/stats").status_code == 401

    wrong = beta_client.post(
        "/login", data={"passphrase": "wrong", "next": "/browse"}
    )
    assert wrong.status_code == 200
    assert "管理员口令不正确" in wrong.get_data(as_text=True)
    assert 'lang="zh-CN"' in wrong.get_data(as_text=True)
    assert "Request access" not in wrong.get_data(as_text=True)

    login = beta_client.post(
        "/login",
        data={"passphrase": "fixture-passphrase", "next": "/browse"},
    )
    assert login.status_code == 302
    assert login.headers["Location"].endswith("/browse")
    assert beta_client.get("/browse").status_code == 200
    assert "/account" not in beta_client.get("/browse").get_data(as_text=True)

    assert beta_client.post("/logout").status_code == 302
    assert beta_client.get("/browse").status_code == 302


def test_beta_login_rejects_external_redirects(artifact_dir, monkeypatch):
    monkeypatch.setenv("BETA_PASSPHRASE", "fixture-passphrase")
    monkeypatch.setenv("SECRET_KEY", "fixture-secret-key")
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    beta_client = app.test_client()
    response = beta_client.post(
        "/login",
        data={"passphrase": "fixture-passphrase", "next": "https://example.com"},
    )
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

    def fake_send(*args):
        sent_links.append(args[3])

    monkeypatch.setattr("qian_wenku_web.app.send_magic_link", fake_send)
    monkeypatch.setattr("qian_wenku_web.app.verify_turnstile", lambda *args: True)
    app = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    app.config.update(TESTING=True)
    access_client = app.test_client()

    assert access_client.get("/browse").status_code == 302
    request_page = access_client.get("/request-access")
    assert request_page.status_code == 200
    assert "申请并发送登录链接" in request_page.get_data(as_text=True)
    with access_client.session_transaction() as session:
        csrf_token = session["access_request_csrf"]

    malformed_csrf = access_client.post(
        "/request-access",
        data={
            "csrf_token": "非 ASCII",
            "email": "researcher@example.com",
            "accept_terms": "yes",
        },
    )
    assert malformed_csrf.status_code == 200
    assert "请刷新页面后重试" in malformed_csrf.get_data(as_text=True)

    response = access_client.post(
        "/request-access",
        data={
            "csrf_token": csrf_token,
            "email": "Researcher@Example.com",
            "accept_terms": "yes",
        },
    )
    assert response.status_code == 200
    assert "登录链接已发送至您的邮箱" in response.get_data(as_text=True)
    assert len(sent_links) == 1
    assert sent_links[0].startswith("qml_")
    assert sent_links[0].encode() not in access_db.read_bytes()

    duplicate = access_client.post(
        "/request-access",
        data={
            "csrf_token": csrf_token,
            "email": "Researcher@example.com",
            "accept_terms": "yes",
        },
    )
    assert duplicate.status_code == 200
    assert len(sent_links) == 1

    access_client.get("/login")
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
    assert "我们已发送一封登录邮件" in emailed_login.get_data(as_text=True)
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
