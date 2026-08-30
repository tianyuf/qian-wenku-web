"""Browse API endpoints."""

from flask import Blueprint, request, jsonify, current_app

from ..formatting import clamp_int, format_date_chinese, parse_page_numbers
from .utils import open_db

browse_bp = Blueprint("browse", __name__)


@browse_bp.route("/years")
def list_years():
    """List all years with entry counts."""
    person = request.args.get("person", "").strip()

    try:
        with open_db() as db:
            sql = """
            SELECT e.year, COUNT(*) as count
            FROM entries e
            JOIN sources s ON e.source_id = s.id
            WHERE e.year IS NOT NULL
            """
            params = []

            if person:
                sql += " AND s.title LIKE ?"
                params.append(f"%{person}%")

            sql += " GROUP BY e.year ORDER BY e.year"
            cursor = db.conn.execute(sql, params)
            years = [{"year": row[0], "count": row[1]} for row in cursor.fetchall()]

        return jsonify({"years": years})

    except Exception as e:
        current_app.logger.error(f"List years error: {e}")
        return jsonify({"error": "List years failed"}), 500


@browse_bp.route("/year/<int:year>")
def browse_year(year):
    """
    Browse entries by year.

    Query params:
    - person: filter by person
    - source_id: filter by source
    - month: filter by month (1-12)
    - limit: max entries (default 1000)
    """
    person = request.args.get("person", "").strip()
    source_ids = request.args.getlist("source_id", type=int)
    month = request.args.get("month", type=int)
    limit = clamp_int(request.args.get("limit"), 500, 1, 500)

    try:
        with open_db() as db:
            sql = """
            SELECT e.*, s.title as source_title, s.volume_number, s.pdf_path, s.content_type as source_content_type
            FROM entries e
            JOIN sources s ON e.source_id = s.id
            WHERE e.year = ?
            """
            params = [year]

            if person:
                sql += " AND s.title LIKE ?"
                params.append(f"%{person}%")

            if source_ids:
                placeholders = ",".join("?" * len(source_ids))
                sql += f" AND e.source_id IN ({placeholders})"
                params.extend(source_ids)

            if month:
                sql += " AND e.month = ?"
                params.append(month)

            sql += " ORDER BY e.date_iso, e.entry_order LIMIT ?"
            params.append(limit)

            cursor = db.conn.execute(sql, params)
            entries = []
            for row in cursor.fetchall():
                row = dict(row)
                content_type = row.get("content_type", "nianpu") or "nianpu"
                pdf_pages = parse_page_numbers(row.get("page_numbers"))
                pdf_start = pdf_pages[0] if pdf_pages else row["start_page"]
                pdf_end = pdf_pages[-1] if pdf_pages else row["end_page"]
                content = row["content_clean"] or ""
                entry = {
                    "id": row["id"],
                    "content_type": content_type,
                    "date_iso": row["date_iso"],
                    "date_display": format_date_chinese(row["date_iso"], row["date_precision"]),
                    "date_precision": row["date_precision"],
                    "date_original": row["date_original"],
                    "content_preview": content[:200] + "..."
                    if len(content) > 200
                    else content,
                    "year": row["year"],
                    "month": row["month"],
                    "day": row["day"],
                    "pages": {
                        "start": row["start_page"],
                        "end": row["end_page"],
                        "pdf_start": pdf_start,
                        "pdf_end": pdf_end,
                    },
                    "source": {
                        "id": row["source_id"],
                        "title": row["source_title"],
                        "volume": row["volume_number"],
                        "pdf_available": bool(row["pdf_path"]),
                        "content_type": content_type,
                    },
                }
                if content_type == "wenji":
                    entry["title"] = row.get("title")
                    entry["source_attribution"] = row.get("source_attribution")
                entries.append(entry)

            prev_year = db.conn.execute(
                "SELECT MAX(year) FROM entries WHERE year < ?", (year,)
            ).fetchone()[0]
            next_year = db.conn.execute(
                "SELECT MIN(year) FROM entries WHERE year > ?", (year,)
            ).fetchone()[0]

        return jsonify({
            "year": year,
            "entries": entries,
            "count": len(entries),
            "prev_year": prev_year,
            "next_year": next_year,
            "filters": {
                "person": person,
                "month": month,
            },
        })

    except Exception as e:
        current_app.logger.error(f"Browse error: {e}")
        return jsonify({"error": "Browse failed"}), 500


@browse_bp.route("/entry/<int:entry_id>")
def entry_detail(entry_id):
    """Get full entry details with navigation."""
    try:
        with open_db() as db:
            cursor = db.conn.execute("""
                SELECT e.*, s.title as source_title, s.volume_number, s.pdf_path, s.total_pages, s.content_type as source_content_type
                FROM entries e
                JOIN sources s ON e.source_id = s.id
                WHERE e.id = ?
            """, (entry_id,))
            row = cursor.fetchone()

            if not row:
                return jsonify({"error": "Entry not found"}), 404

            entry = dict(row)
            pdf_pages = parse_page_numbers(entry.get("page_numbers"))
            pdf_start = pdf_pages[0] if pdf_pages else entry["start_page"]
            pdf_end = pdf_pages[-1] if pdf_pages else entry["end_page"]

            prev_cursor = db.conn.execute("""
                SELECT id, date_iso, date_display, content_clean
                FROM (
                    SELECT e.id,
                           e.date_iso,
                           CASE
                               WHEN e.date_precision = 'year' THEN e.date_iso || '年'
                               WHEN e.date_precision = 'month' THEN e.date_iso || '月'
                               ELSE e.date_iso
                           END as date_display,
                           substr(e.content_clean, 1, 100) || '...' as content_clean
                    FROM entries e
                    WHERE (e.date_iso < ? OR (e.date_iso = ? AND e.entry_order < ?))
                      AND e.source_id = ?
                    ORDER BY e.date_iso DESC, e.entry_order DESC
                    LIMIT 1
                )
            """, (entry["date_iso"], entry["date_iso"], entry["entry_order"], entry["source_id"]))
            prev_row = prev_cursor.fetchone()

            next_cursor = db.conn.execute("""
                SELECT id, date_iso, date_display, content_clean
                FROM (
                    SELECT e.id,
                           e.date_iso,
                           CASE
                               WHEN e.date_precision = 'year' THEN e.date_iso || '年'
                               WHEN e.date_precision = 'month' THEN e.date_iso || '月'
                               ELSE e.date_iso
                           END as date_display,
                           substr(e.content_clean, 1, 100) || '...' as content_clean
                    FROM entries e
                    WHERE (e.date_iso > ? OR (e.date_iso = ? AND e.entry_order > ?))
                      AND e.source_id = ?
                    ORDER BY e.date_iso ASC, e.entry_order ASC
                    LIMIT 1
                )
            """, (entry["date_iso"], entry["date_iso"], entry["entry_order"], entry["source_id"]))
            next_row = next_cursor.fetchone()

            same_day = []
            if entry["date_iso"] and len(entry["date_iso"]) >= 10:
                same_cursor = db.conn.execute("""
                    SELECT e.id, e.date_original, substr(e.content_clean, 1, 100) || '...' as content_clean,
                           s.title as source_title
                    FROM entries e
                    JOIN sources s ON e.source_id = s.id
                    WHERE e.date_iso = ?
                      AND e.id != ?
                    ORDER BY e.source_id, e.entry_order
                """, (entry["date_iso"][:10], entry_id))
                same_day = [dict(r) for r in same_cursor.fetchall()]

        return jsonify({
            "entry": {
                "id": entry["id"],
                "entry_order": entry["entry_order"],
                "content_type": entry.get("content_type", "nianpu") or "nianpu",
                "title": entry.get("title"),
                "date_iso": entry["date_iso"],
                "date_display": format_date_chinese(entry["date_iso"], entry["date_precision"]),
                "date_precision": entry["date_precision"],
                "date_original": entry["date_original"],
                "content": entry["content_clean"],
                "year": entry["year"],
                "month": entry["month"],
                "day": entry["day"],
                "source_attribution": entry.get("source_attribution"),
                "footnotes": entry.get("footnotes"),
                "pages": {
                    "start": entry["start_page"],
                    "end": entry["end_page"],
                    "pdf_start": pdf_start,
                    "pdf_end": pdf_end,
                },
                "source": {
                    "id": entry["source_id"],
                    "title": entry["source_title"],
                    "volume": entry["volume_number"],
                    "pdf_available": bool(entry["pdf_path"]),
                    "total_pages": entry["total_pages"],
                    "content_type": entry.get("source_content_type", "nianpu") or "nianpu",
                },
            },
            "navigation": {
                "prev": dict(prev_row) if prev_row else None,
                "next": dict(next_row) if next_row else None,
                "same_day": same_day,
            },
        })

    except Exception as e:
        current_app.logger.error(f"Entry detail error: {e}")
        return jsonify({"error": "Entry detail failed"}), 500
