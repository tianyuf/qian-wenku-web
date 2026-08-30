import json
import sqlite3

from qian_wenku_web.artifacts import artifact_is_valid, sha256_file, validate_artifact
from qian_wenku_web.preflight import run_preflight


def check_map(checks):
    return {name: ok for name, ok, _ in checks}


def test_valid_fixture_passes_preflight(artifact_dir):
    checks = run_preflight(artifact_dir)
    assert artifact_is_valid(checks), checks


def test_preflight_rejects_bad_schema_version(artifact_dir):
    manifest_path = artifact_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 2
    manifest_path.write_text(json.dumps(manifest))
    assert check_map(validate_artifact(artifact_dir))["schema_version"] is False


def test_preflight_rejects_mapping_checksum_and_shape(artifact_dir):
    mapping_path = artifact_dir / "page_images.json"
    mapping_path.write_text('{"999":{"0":"not-a-hash"}}')
    checks = check_map(validate_artifact(artifact_dir))
    assert checks["checksum_page_images.json"] is False
    assert checks["mapping_valid"] is False
    assert checks["mapping_sources"] is False


def test_preflight_requires_populated_permalinks(artifact_dir):
    db_path = artifact_dir / "corpus.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE entries SET permalink = NULL WHERE id = 1")
    manifest_path = artifact_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["corpus.db"]["sha256"] = sha256_file(db_path)
    manifest["files"]["corpus.db"]["bytes"] = db_path.stat().st_size
    manifest_path.write_text(json.dumps(manifest))
    checks = check_map(validate_artifact(artifact_dir))
    assert checks["permalinks"] is False


def test_preflight_requires_matching_database_schema_version(artifact_dir):
    db_path = artifact_dir / "corpus.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 2")
    manifest_path = artifact_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["corpus.db"]["sha256"] = sha256_file(db_path)
    manifest["files"]["corpus.db"]["bytes"] = db_path.stat().st_size
    manifest_path.write_text(json.dumps(manifest))
    checks = check_map(validate_artifact(artifact_dir))
    assert checks["database_schema_version"] is False
