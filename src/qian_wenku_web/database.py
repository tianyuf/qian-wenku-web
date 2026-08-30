"""Minimal read-only SQLite access used by the web application."""

import sqlite3
from pathlib import Path


class NianpuDatabase:
    """Read-only connection and corpus queries needed by the web UI."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.conn = None

    def connect(self):
        uri = f"{self.db_path.expanduser().resolve().as_uri()}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        return self

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def get_stats(self):
        source_count = self.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        entry_count = self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        row = self.conn.execute(
            "SELECT MIN(year), MAX(year) FROM entries WHERE year IS NOT NULL"
        ).fetchone()
        return {
            "sources": source_count,
            "entries": entry_count,
            "year_range": (row[0], row[1]) if row else (None, None),
        }

    def get_recipients(self, query=None, sort="count", limit=50, offset=0):
        order = "letter_count DESC, name" if sort == "count" else "name"
        where = "WHERE name LIKE ?" if query else ""
        params = [f"%{query}%"] if query else []
        rows = self.conn.execute(
            f"SELECT * FROM recipients {where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        count = self.conn.execute(
            f"SELECT COUNT(*) FROM recipients {where}", params
        ).fetchone()[0]
        return [dict(row) for row in rows], count

    def get_recipient(self, recipient_id):
        row = self.conn.execute(
            "SELECT * FROM recipients WHERE id = ?", (recipient_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_recipient_entries(self, recipient_id):
        rows = self.conn.execute(
            """
            SELECT e.id, e.permalink, e.title, e.date_iso, e.date_precision,
                   e.start_page, e.end_page, e.page_numbers, e.source_id,
                   e.content_clean, e.content_type, s.title AS source_title,
                   s.pdf_path, s.total_pages, s.volume_number
            FROM entries e
            JOIN recipient_letters rl ON e.id = rl.entry_id
            JOIN sources s ON e.source_id = s.id
            WHERE rl.recipient_id = ?
            ORDER BY e.date_iso, e.entry_order
            """,
            (recipient_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_recipient_neighbors(self, recipient_id, sort="count"):
        row = self.conn.execute(
            "SELECT letter_count, name FROM recipients WHERE id = ?", (recipient_id,)
        ).fetchone()
        if not row:
            return None, None
        count, name = row
        if sort == "count":
            previous = self.conn.execute(
                """SELECT id, name FROM recipients
                   WHERE letter_count < ? OR (letter_count = ? AND name < ?)
                   ORDER BY letter_count DESC, name DESC LIMIT 1""",
                (count, count, name),
            ).fetchone()
            next_row = self.conn.execute(
                """SELECT id, name FROM recipients
                   WHERE letter_count > ? OR (letter_count = ? AND name > ?)
                   ORDER BY letter_count ASC, name ASC LIMIT 1""",
                (count, count, name),
            ).fetchone()
        else:
            previous = self.conn.execute(
                "SELECT id, name FROM recipients WHERE name < ? ORDER BY name DESC LIMIT 1",
                (name,),
            ).fetchone()
            next_row = self.conn.execute(
                "SELECT id, name FROM recipients WHERE name > ? ORDER BY name LIMIT 1",
                (name,),
            ).fetchone()
        return self._neighbor(previous), self._neighbor(next_row)

    def get_entities(self, entity_type=None, query=None, sort="nianpu", limit=50, offset=0):
        clauses = []
        params = []
        if entity_type:
            clauses.append("type = ?")
            params.append(entity_type)
        if query:
            clauses.append("name LIKE ?")
            params.append(f"%{query}%")
        where = " AND ".join(clauses) if clauses else "1=1"
        orders = {
            "nianpu": "nianpu_count DESC, name",
            "wenji": "wenji_count DESC, name",
            "shuxin": "shuxin_count DESC, name",
            "total": "(COALESCE(nianpu_count, 0) + COALESCE(wenji_count, 0) + COALESCE(shuxin_count, 0)) DESC, name",
        }
        order = orders.get(sort, orders["total"])
        rows = self.conn.execute(
            f"SELECT * FROM entities WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        count = self.conn.execute(
            f"SELECT COUNT(*) FROM entities WHERE {where}", params
        ).fetchone()[0]
        return [dict(row) for row in rows], count

    def get_entity(self, entity_id):
        row = self.conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_entity_entries(self, entity_id):
        rows = self.conn.execute(
            """
            SELECT e.id, e.permalink, e.title, e.date_iso, e.date_precision,
                   e.start_page, e.end_page, e.page_numbers, e.source_id,
                   e.content_clean, e.content_type, s.title AS source_title,
                   s.pdf_path, s.total_pages, s.volume_number
            FROM entries e
            JOIN entry_entities ee ON e.id = ee.entry_id
            JOIN sources s ON e.source_id = s.id
            WHERE ee.entity_id = ?
            ORDER BY e.date_iso, e.entry_order
            """,
            (entity_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_entity_neighbors(self, entity_id, sort="nianpu"):
        columns = {
            "nianpu": "nianpu_count",
            "wenji": "wenji_count",
            "shuxin": "shuxin_count",
            "total": "(COALESCE(nianpu_count, 0) + COALESCE(wenji_count, 0) + COALESCE(shuxin_count, 0))",
        }
        count_column = columns.get(sort, columns["total"])
        row = self.conn.execute(
            f"SELECT {count_column}, name FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            return None, None
        count, name = row
        previous = self.conn.execute(
            f"""SELECT id, name FROM entities
                WHERE {count_column} < ? OR ({count_column} = ? AND name < ?)
                ORDER BY {count_column} DESC, name DESC LIMIT 1""",
            (count, count, name),
        ).fetchone()
        next_row = self.conn.execute(
            f"""SELECT id, name FROM entities
                WHERE {count_column} > ? OR ({count_column} = ? AND name > ?)
                ORDER BY {count_column} ASC, name ASC LIMIT 1""",
            (count, count, name),
        ).fetchone()
        return self._neighbor(previous), self._neighbor(next_row)

    @staticmethod
    def _neighbor(row):
        return {"id": row[0], "name": row[1]} if row else None
