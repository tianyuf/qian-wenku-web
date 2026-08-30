import sqlite3

import pytest

from qian_wenku_web.database import NianpuDatabase


def test_database_wrapper_is_read_only(artifact_dir):
    with NianpuDatabase(artifact_dir / "corpus.db") as database:
        assert database.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert database.get_stats()["entries"] == 5
        with pytest.raises(sqlite3.OperationalError):
            database.conn.execute("UPDATE entries SET content_clean = 'changed' WHERE id = 1")


def test_database_connection_lifecycle(artifact_dir):
    database = NianpuDatabase(artifact_dir / "corpus.db")
    assert database.conn is None
    database.connect()
    assert database.conn is not None
    database.close()
    assert database.conn is None
