import pytest

from qian_wenku_web import create_app

from create_fixture import create_fixture


@pytest.fixture()
def artifact_dir(tmp_path):
    return create_fixture(tmp_path / "artifact")


@pytest.fixture()
def app(artifact_dir):
    application = create_app(
        db_path=artifact_dir / "corpus.db",
        mapping_path=artifact_dir / "page_images.json",
        manifest_path=artifact_dir / "manifest.json",
    )
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()
