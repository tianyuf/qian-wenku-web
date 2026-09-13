"""Search API endpoints."""

from flask import Blueprint, request, jsonify, current_app

from ..formatting import clamp_int, format_date_chinese, parse_page_numbers
from .utils import open_db

search_bp = Blueprint('search', __name__)


def format_entry(row):
    """Format database row as API response."""
    row = dict(row)
    content_type = row.get("content_type", "nianpu") or "nianpu"
    pdf_pages = parse_page_numbers(row.get("page_numbers"))
    pdf_start = pdf_pages[0] if pdf_pages else row["start_page"]
    pdf_end = pdf_pages[-1] if pdf_pages else row["end_page"]
    content = row["content_clean"] or ""
    result = {
        "id": row["id"],
        "entry_order": row["entry_order"],
        "content_type": content_type,
        "permalink": row.get("permalink"),
        "date_iso": row["date_iso"],
        "date_display": format_date_chinese(row["date_iso"], row["date_precision"]),
        "date_precision": row["date_precision"],
        "date_original": row["date_original"],
        "content": content[:300] + "..." if len(content) > 300 else content,
        "content_full": content,
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
            "title": row.get("source_title", "Unknown"),
            "volume": row.get("volume_number"),
            "pdf_available": bool(row.get("pdf_path")),
            "content_type": content_type
        }
    }
    # Add wenji-specific fields
    if content_type == "wenji":
        result["title"] = row.get("title")
        result["source_attribution"] = row.get("source_attribution")
        result["footnotes"] = row.get("footnotes")
    return result


@search_bp.route('/')
def search():
    """
    Search entries.

    Query params:
    - q: search query (Chinese substring search)
    - person: filter by person name
    - year_start: minimum year
    - year_end: maximum year
    - source_id: filter by source (can be multiple)
    - content_type: filter by 'nianpu' or 'wenji'
    - limit: max results (default 100)
    - offset: pagination offset
    """
    query = request.args.get('q', '').strip()
    person = request.args.get('person', '').strip()
    year_start = request.args.get('year_start', type=int)
    year_end = request.args.get('year_end', type=int)
    source_ids = request.args.getlist('source_id', type=int)
    content_type = request.args.get('content_type', '').strip()
    limit = clamp_int(request.args.get('limit'), 100, 1, 500)
    offset = clamp_int(request.args.get('offset'), 0, 0, 1_000_000)

    try:
        # Build WHERE clauses
        where_clauses = []
        params = []

        # Substring search is intentional for Chinese text: SQLite's default
        # FTS tokenizer has poor recall for unsegmented Chinese terms.
        if query:
            where_clauses.append("e.content_clean LIKE ?")
            params.append(f"%{query}%")

        # Person filter (match against source title)
        if person:
            where_clauses.append("s.title LIKE ?")
            params.append(f"%{person}%")

        # Year range
        if year_start:
            where_clauses.append("e.year >= ?")
            params.append(year_start)
        if year_end:
            where_clauses.append("e.year <= ?")
            params.append(year_end)

        # Source filter
        if source_ids:
            placeholders = ','.join('?' * len(source_ids))
            where_clauses.append(f"e.source_id IN ({placeholders})")
            params.extend(source_ids)

        # Content type filter
        if content_type:
            where_clauses.append("e.content_type = ?")
            params.append(content_type)

        # Build full query
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        sql = f"""
            SELECT e.*, s.title as source_title, s.volume_number, s.pdf_path, s.content_type as source_content_type
            FROM entries e
            JOIN sources s ON e.source_id = s.id
            WHERE {where_sql}
            ORDER BY e.date_iso, e.entry_order
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        with open_db() as db:
            cursor = db.conn.execute(sql, params)
            rows = cursor.fetchall()

            # Format results
            results = [format_entry(dict(row)) for row in rows]

            # Get total count (for pagination)
            count_sql = f"""
                SELECT COUNT(*) FROM entries e
                JOIN sources s ON e.source_id = s.id
                WHERE {where_sql}
            """
            # Count query doesn't use limit/offset
            count_params = params[:-2] if len(params) >= 2 else params
            count_cursor = db.conn.execute(count_sql, count_params)
            total = count_cursor.fetchone()[0]

        return jsonify({
            "results": results,
            "total": total,
            "limit": limit,
            "offset": offset,
            "query": query,
            "filters": {
                "person": person,
                "year_start": year_start,
                "year_end": year_end,
                "source_ids": source_ids,
                "content_type": content_type
            }
        })

    except Exception as e:
        current_app.logger.error(f"Search error: {e}")
        return jsonify({"error": "Search failed"}), 500


@search_bp.route('/suggest')
def suggest():
    """Search suggestions for autocomplete."""
    query = request.args.get('q', '').strip()
    if not query or len(query) < 2:
        return jsonify({"suggestions": []})

    try:
        with open_db() as db:
            # Get common terms starting with query
            sql = """
            SELECT DISTINCT substr(content_clean, instr(content_clean, ?), 20) as term
            FROM entries
            WHERE content_clean LIKE ?
            LIMIT 10
            """
            cursor = db.conn.execute(sql, [query, f"%{query}%"])
            suggestions = [row[0] for row in cursor.fetchall()]
        return jsonify({"suggestions": suggestions})

    except Exception as e:
        current_app.logger.error(f"Search suggest error: {e}")
        return jsonify({"suggestions": []})
