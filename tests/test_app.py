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
    assert "Incorrect passphrase" in wrong.get_data(as_text=True)
    assert 'lang="en"' in wrong.get_data(as_text=True)
    assert "mailto:mail@qianxuesen.org" in wrong.get_data(as_text=True)

    login = beta_client.post(
        "/login",
        data={"passphrase": "fixture-passphrase", "next": "/browse"},
    )
    assert login.status_code == 302
    assert login.headers["Location"].endswith("/browse")
    assert beta_client.get("/browse").status_code == 200

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
