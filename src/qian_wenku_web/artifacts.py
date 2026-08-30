"""Validation for the prepared public corpus artifact."""

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
ARTIFACT_FILES = ("corpus.db", "page_images.json", "manifest.json")
REQUIRED_COLUMNS = {
    "schema_info": {"id", "schema_version"},
    "sources": {"id", "title", "volume_number", "pdf_path", "total_pages", "content_type"},
    "entries": {
        "id", "source_id", "entry_order", "permalink", "content_type", "title",
        "date_iso", "date_precision", "date_original", "year", "month", "day",
        "content_clean", "source_attribution", "footnotes", "start_page", "end_page",
        "page_numbers",
    },
    "entries_fts": set(),
    "recipients": {"id", "name", "letter_count"},
    "recipient_letters": {"entry_id", "recipient_id"},
    "entities": {"id", "name", "type", "nianpu_count", "wenji_count", "shuxin_count"},
    "entry_entities": {"entry_id", "entity_id"},
    "entry_duplicates": {"entry_id", "duplicate_entry_id", "similarity_score"},
}
COUNT_TABLES = ("sources", "entries", "recipients", "entities")
PAGE_IMAGE_ID_PATTERN = re.compile(r"^[0-9a-f]{10}$")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_artifact(artifact_dir):
    """Return named checks for an artifact directory without modifying it."""
    artifact_dir = Path(artifact_dir)
    paths = {name: artifact_dir / name for name in ARTIFACT_FILES}
    checks = []
    for name, path in paths.items():
        checks.append((f"{name}_exists", path.is_file(), str(path)))
    if not all(path.is_file() for path in paths.values()):
        return checks

    try:
        manifest = json.loads(paths["manifest.json"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(("manifest_json", False, str(exc)))
        return checks
    checks.append(("manifest_json", isinstance(manifest, dict), "ok" if isinstance(manifest, dict) else "object required"))
    if not isinstance(manifest, dict):
        return checks

    version = manifest.get("schema_version")
    checks.append(("schema_version", version == SCHEMA_VERSION, str(version)))
    try:
        datetime.fromisoformat(str(manifest.get("generated_at", "")).replace("Z", "+00:00"))
        generated_at_valid = True
    except ValueError:
        generated_at_valid = False
    checks.append(("generated_at", generated_at_valid, str(manifest.get("generated_at"))))

    files = manifest.get("files", {})
    for name in ("corpus.db", "page_images.json"):
        record = files.get(name, {}) if isinstance(files, dict) else {}
        expected = record.get("sha256") if isinstance(record, dict) else None
        actual = sha256_file(paths[name])
        checks.append((f"checksum_{name}", expected == actual, actual))
        expected_bytes = record.get("bytes") if isinstance(record, dict) else None
        actual_bytes = paths[name].stat().st_size
        checks.append((f"size_{name}", expected_bytes == actual_bytes, str(actual_bytes)))

    try:
        mapping = json.loads(paths["page_images.json"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(("mapping_json", False, str(exc)))
        return checks
    mapping_shape_ok = isinstance(mapping, dict) and bool(mapping)
    if mapping_shape_ok:
        mapping_shape_ok = all(
            str(source_id).isdigit()
            and isinstance(pages, dict)
            and bool(pages)
            and all(
                str(page).isdigit()
                and int(page) > 0
                and isinstance(value, str)
                and PAGE_IMAGE_ID_PATTERN.fullmatch(value)
                for page, value in pages.items()
            )
            for source_id, pages in mapping.items()
        )
    checks.append(("mapping_valid", mapping_shape_ok, f"{len(mapping) if isinstance(mapping, dict) else 0} sources"))

    db_uri = f"{paths['corpus.db'].resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(db_uri, uri=True) as conn:
            conn.execute("PRAGMA query_only = ON")
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
            }
            missing_tables = sorted(REQUIRED_COLUMNS.keys() - tables)
            checks.append(("required_tables", not missing_tables, ",".join(missing_tables) or "ok"))
            if missing_tables:
                return checks

            missing_columns = []
            for table, required in REQUIRED_COLUMNS.items():
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                missing_columns.extend(f"{table}.{name}" for name in sorted(required - columns))
            checks.append(("required_columns", not missing_columns, ",".join(missing_columns) or "ok"))

            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            schema_row = conn.execute(
                "SELECT schema_version FROM schema_info WHERE id = 1"
            ).fetchone()
            database_version = schema_row[0] if schema_row else None
            version_ok = (
                user_version == SCHEMA_VERSION
                and database_version == SCHEMA_VERSION
            )
            checks.append(
                (
                    "database_schema_version",
                    version_ok,
                    f"user_version={user_version}, schema_info={database_version}",
                )
            )

            mapped_source_ids = {
                str(row[0])
                for row in conn.execute(
                    "SELECT id FROM sources WHERE pdf_path IS NOT NULL AND trim(pdf_path) != ''"
                )
            }
            mapping_sources_ok = isinstance(mapping, dict) and set(mapping) == mapped_source_ids
            checks.append(("mapping_sources", mapping_sources_ok, "matches PDF-backed sources"))

            counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in COUNT_TABLES}
            checks.append(("database_content", counts["sources"] > 0 and counts["entries"] > 0, str(counts)))
            null_permalinks = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE permalink IS NULL OR trim(permalink) = ''"
            ).fetchone()[0]
            checks.append(("permalinks", null_permalinks == 0, f"{null_permalinks} missing"))
    except sqlite3.Error as exc:
        checks.append(("database_readable", False, str(exc)))
        return checks

    manifest_counts = manifest.get("counts", {})
    counts_ok = isinstance(manifest_counts, dict) and all(
        manifest_counts.get(name) == value for name, value in counts.items()
    )
    page_count = sum(len(pages) for pages in mapping.values()) if isinstance(mapping, dict) else 0
    counts_ok = counts_ok and manifest_counts.get("page_images") == page_count
    checks.append(("manifest_counts", counts_ok, str(manifest_counts)))
    return checks


def artifact_is_valid(checks):
    return bool(checks) and all(ok for _, ok, _ in checks)
