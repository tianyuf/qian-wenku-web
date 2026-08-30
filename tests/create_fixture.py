#!/usr/bin/env python3
"""Create a deterministic, entirely synthetic corpus artifact for tests."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE schema_info (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL
);
INSERT INTO schema_info VALUES (1, 1);
CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    volume_number INTEGER,
    file_path TEXT NOT NULL,
    pdf_path TEXT,
    total_pages INTEGER,
    content_type TEXT NOT NULL
);
CREATE TABLE entries (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    entry_order INTEGER NOT NULL,
    permalink TEXT UNIQUE,
    content_type TEXT NOT NULL,
    title TEXT,
    date_iso TEXT,
    date_precision TEXT,
    date_original TEXT,
    date_lunar TEXT,
    year INTEGER,
    month INTEGER,
    day INTEGER,
    end_day INTEGER,
    content TEXT NOT NULL,
    content_clean TEXT,
    source_attribution TEXT,
    footnotes TEXT,
    avg_ocr_confidence REAL,
    start_page INTEGER NOT NULL,
    end_page INTEGER NOT NULL,
    page_numbers TEXT,
    is_continuation INTEGER DEFAULT 0,
    has_lunar_date INTEGER DEFAULT 0,
    FOREIGN KEY (source_id) REFERENCES sources(id)
);
CREATE VIRTUAL TABLE entries_fts USING fts5(
    content_clean, title, content='entries', content_rowid='id'
);
CREATE TABLE recipients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    letter_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE recipient_letters (
    entry_id INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    PRIMARY KEY (entry_id, recipient_id)
);
CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    nianpu_count INTEGER NOT NULL DEFAULT 0,
    wenji_count INTEGER NOT NULL DEFAULT 0,
    shuxin_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE entry_entities (
    entry_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    PRIMARY KEY (entry_id, entity_id)
);
CREATE TABLE entry_duplicates (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL,
    duplicate_entry_id INTEGER NOT NULL,
    similarity_score REAL
);
CREATE TABLE data_quality_flags (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER,
    severity TEXT NOT NULL,
    issue_type TEXT,
    note TEXT
);
PRAGMA user_version = 1;
"""

SOURCES = [
    (1, "测试人物年谱", 1, "synthetic/nianpu.json", "private/nianpu.pdf", 3, "nianpu"),
    (2, "测试人物文集", 1, "synthetic/wenji.json", "private/wenji.pdf", 2, "wenji"),
    (3, "测试人物书信", 1, "synthetic/shuxin.json", "private/shuxin.pdf", 2, "shuxin"),
    (4, "测试人物年谱补编", 2, "synthetic/supplement.json", None, 1, "nianpu"),
]

ENTRIES = [
    (1, 1, 1, "nianpu-19111211-a", "nianpu", None, "1911-12-11", "exact", "12月11日", None, 1911, 12, 11, None, "合成的年谱测试记录。", "合成的年谱测试记录。", None, None, 1.0, 1, 1, "1", 0, 0),
    (2, 1, 2, "nianpu-191201-b", "nianpu", None, "1912-01", "month", "1月", None, 1912, 1, None, None, "另一条合成记录，提及测试机构。", "另一条合成记录，提及测试机构。", None, None, 1.0, 2, 2, "2", 0, 0),
    (3, 2, 1, "wenji-19111211-c", "wenji", "合成文集文章", "1911-12-11", "exact", "1911年12月11日", None, 1911, 12, 11, None, "用于检索测试的合成文章。", "用于检索测试的合成文章。", "合成刊物", "合成注释", 1.0, 1, 1, "1", 0, 0),
    (4, 3, 1, "shuxin-19111211-d", "shuxin", "致测试收信人", "1911-12-11", "exact", "1911年12月11日", None, 1911, 12, 11, None, "用于收信人页面的合成书信。", "用于收信人页面的合成书信。", None, None, 1.0, 1, 1, "1", 0, 0),
    (5, 4, 1, "nianpu-19111211-e", "nianpu", None, "1911-12-11", "exact", "12月11日", None, 1911, 12, 11, None, "合成的年谱测试记录。", "合成的年谱测试记录。", None, None, 1.0, 1, 1, "1", 0, 0),
]


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def create_fixture(output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / "corpus.db"
    db_path.unlink(missing_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)", SOURCES)
        conn.executemany(
            "INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ENTRIES,
        )
        conn.execute("INSERT INTO entries_fts(entries_fts) VALUES ('rebuild')")
        conn.executemany(
            "INSERT INTO recipients VALUES (?, ?, ?)",
            [(1, "测试收信人", 1), (2, "另一收信人", 0)],
        )
        conn.execute("INSERT INTO recipient_letters VALUES (4, 1)")
        conn.executemany(
            "INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?)",
            [(1, "测试人物", "person", 2, 1, 1), (2, "测试机构", "organization", 1, 0, 0)],
        )
        conn.executemany(
            "INSERT INTO entry_entities VALUES (?, ?)",
            [(1, 1), (2, 2), (3, 1), (4, 1), (5, 1)],
        )
        conn.executemany(
            "INSERT INTO entry_duplicates VALUES (?, ?, ?, ?)",
            [(1, 1, 5, 1.0), (2, 5, 1, 1.0)],
        )
        conn.execute(
            "INSERT INTO data_quality_flags VALUES (1, 2, 'review', 'synthetic', 'Fixture-only flag')"
        )
        conn.commit()
        conn.execute("VACUUM")

    mapping = {
        str(source_id): {
            str(page): hashlib.sha256(
                f"fixture-{source_id}-{page}".encode()
            ).hexdigest()[:10]
            for page in range(1, total_pages + 1)
        }
        for source_id, _, _, _, pdf_path, total_pages, _ in SOURCES
        if pdf_path
    }
    mapping_path = output_dir / "page_images.json"
    mapping_path.write_text(
        json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00Z",
        "counts": {
            "sources": len(SOURCES),
            "entries": len(ENTRIES),
            "recipients": 2,
            "entities": 2,
            "page_images": sum(len(pages) for pages in mapping.values()),
        },
        "files": {
            "corpus.db": {
                "sha256": file_hash(db_path),
                "bytes": db_path.stat().st_size,
            },
            "page_images.json": {
                "sha256": file_hash(mapping_path),
                "bytes": mapping_path.stat().st_size,
            },
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return output_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    create_fixture(parser.parse_args().output)
