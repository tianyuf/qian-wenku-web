import hashlib
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
    assert "访问码不正确或已过期" in wrong.get_data(as_text=True)
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
    sent_codes = []

    def fake_send(*args):
        sent_codes.append(args[3])

    monkeypatch.setattr("qian_wenku_web.app.send_access_code", fake_send)
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
    assert "发送访问码" in request_page.get_data(as_text=True)
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
    assert "访问码已发送至您的邮箱" in response.get_data(as_text=True)
    assert len(sent_codes) == 1
    assert sent_codes[0].startswith("qx_")
    assert sent_codes[0].encode() not in access_db.read_bytes()

    duplicate = access_client.post(
        "/request-access",
        data={
            "csrf_token": csrf_token,
            "email": "Researcher@example.com",
            "accept_terms": "yes",
        },
    )
    assert duplicate.status_code == 200
    assert len(sent_codes) == 1

    login = access_client.post(
        "/login", data={"passphrase": sent_codes[0], "next": "/browse"}
    )
    assert login.status_code == 302
    browse = access_client.get("/browse")
    assert browse.status_code == 200
    assert 'href="/account"' in browse.get_data(as_text=True)

    account_page = access_client.get("/account")
    assert account_page.status_code == 200
    assert "Researcher@example.com" in account_page.get_data(as_text=True)
    assert "访问有效期至" in account_page.get_data(as_text=True)
    with access_client.session_transaction() as account_session:
        account_csrf = account_session["account_csrf"]

    invalid_email = access_client.post(
        "/account",
        data={"csrf_token": account_csrf, "email": "not-an-email"},
    )
    assert "请输入有效的邮箱地址" in invalid_email.get_data(as_text=True)

    invalid_csrf = access_client.post(
        "/account",
        data={"csrf_token": "wrong", "email": "other@example.com"},
    )
    assert "请刷新页面后重试" in invalid_csrf.get_data(as_text=True)

    updated = access_client.post(
        "/account",
        data={"csrf_token": account_csrf, "email": "Updated@Example.COM"},
    )
    assert updated.status_code == 200
    assert "邮箱已更新" in updated.get_data(as_text=True)
    assert "Updated@example.com" in updated.get_data(as_text=True)
    with sqlite3.connect(access_db) as connection:
        assert connection.execute(
            "SELECT email FROM access_grants WHERE id = 1"
        ).fetchone()[0] == "Updated@example.com"

    rotated = access_client.post(
        "/account",
        data={"csrf_token": account_csrf, "action": "rotate_code"},
    )
    rotated_text = rotated.get_data(as_text=True)
    assert rotated.status_code == 200
    assert "旧访问码已失效" in rotated_text
    assert "此页仅显示一次" in rotated_text
    assert sent_codes[0] not in rotated_text
    assert access_client.post(
        "/login", data={"passphrase": sent_codes[0]}
    ).status_code == 200

    with sqlite3.connect(access_db) as connection:
        connection.execute("UPDATE access_grants SET revoked_at = 1")
    revoked = access_client.get("/browse")
    assert revoked.status_code == 302
    assert "/login" in revoked.headers["Location"]
