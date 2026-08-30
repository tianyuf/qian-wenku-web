"""Metadata API endpoints."""

from flask import Blueprint, jsonify, current_app

from .utils import open_db

meta_bp = Blueprint("meta", __name__)


@meta_bp.route("/persons")
def list_persons():
    """Get list of persons extracted from source titles with entry counts."""
    try:
        with open_db() as db:
            cursor = db.conn.execute("""
                SELECT s.id, s.title, s.volume_number, s.pdf_path, s.content_type,
                       COUNT(e.id) as entry_count
                FROM sources s
                LEFT JOIN entries e ON s.id = e.source_id
                GROUP BY s.id
                ORDER BY s.title
            """)

            persons = {}
            for row in cursor.fetchall():
                title = row["title"]
                person = extract_person_name(title)

                if person not in persons:
                    persons[person] = {
                        "name": person,
                        "sources": [],
                        "total_entries": 0,
                    }

                persons[person]["sources"].append({
                    "id": row["id"],
                    "title": title,
                    "volume": row["volume_number"],
                    "entry_count": row["entry_count"],
                    "pdf_available": bool(row["pdf_path"]),
                    "content_type": row["content_type"] or "nianpu",
                })
                persons[person]["total_entries"] += row["entry_count"]

        person_list = sorted(
            persons.values(),
            key=lambda x: x["total_entries"],
            reverse=True,
        )
        return jsonify({"persons": person_list, "count": len(person_list)})

    except Exception as e:
        current_app.logger.error(f"List persons error: {e}")
        return jsonify({"error": "List persons failed"}), 500


@meta_bp.route("/stats")
def get_stats():
    """Get overall database statistics."""
    try:
        with open_db() as db:
            stats = db.get_stats()

            cursor = db.conn.execute("""
                SELECT MIN(year), MAX(year), COUNT(DISTINCT year)
                FROM entries
                WHERE year IS NOT NULL
            """)
            min_year, max_year, distinct_years = cursor.fetchone()

            precision_cursor = db.conn.execute("""
                SELECT date_precision, COUNT(*) as count
                FROM entries
                GROUP BY date_precision
                ORDER BY count DESC
            """)
            precision_dist = [dict(r) for r in precision_cursor.fetchall()]

            sources_cursor = db.conn.execute("""
                SELECT s.id, s.title, s.volume_number,
                       COUNT(e.id) as entry_count,
                       MIN(e.year) as min_year,
                       MAX(e.year) as max_year
                FROM sources s
                LEFT JOIN entries e ON s.id = e.source_id
                GROUP BY s.id
                ORDER BY s.title
            """)
            sources = [dict(r) for r in sources_cursor.fetchall()]

        return jsonify({
            "overall": stats,
            "date_range": {
                "min": min_year,
                "max": max_year,
                "distinct_years": distinct_years,
            },
            "precision_distribution": precision_dist,
            "sources": sources,
        })

    except Exception as e:
        current_app.logger.error(f"Stats error: {e}")
        return jsonify({"error": "Stats failed"}), 500


@meta_bp.route("/source/<int:source_id>")
def source_detail(source_id):
    """Get detailed information about a specific source."""
    try:
        with open_db() as db:
            cursor = db.conn.execute("""
                SELECT s.*,
                       COUNT(e.id) as entry_count,
                       MIN(e.year) as min_year,
                       MAX(e.year) as max_year
                FROM sources s
                LEFT JOIN entries e ON s.id = e.source_id
                WHERE s.id = ?
                GROUP BY s.id
            """, (source_id,))
            row = cursor.fetchone()

            if not row:
                return jsonify({"error": "Source not found"}), 404

            year_cursor = db.conn.execute("""
                SELECT year, COUNT(*) as count
                FROM entries
                WHERE source_id = ? AND year IS NOT NULL
                GROUP BY year
                ORDER BY year
            """, (source_id,))
            year_dist = [{"year": r[0], "count": r[1]} for r in year_cursor.fetchall()]

        return jsonify({
            "source": {
                "id": row["id"],
                "title": row["title"],
                "volume": row["volume_number"],
                "pdf_available": bool(row["pdf_path"]),
                "total_pages": row["total_pages"],
                "content_type": row["content_type"] or "nianpu",
                "entry_count": row["entry_count"],
                "year_range": {
                    "min": row["min_year"],
                    "max": row["max_year"],
                },
                "year_distribution": year_dist,
            }
        })

    except Exception as e:
        current_app.logger.error(f"Source detail error: {e}")
        return jsonify({"error": "Source detail failed"}), 500


def extract_person_name(title):
    """Extract person name from source title."""
    known_persons = ["钱学森", "邓小平", "周恩来", "毛泽东"]

    for person in known_persons:
        if person in title:
            return person

    if "大事记" in title or "年鉴" in title:
        return title.split("（")[0]

    return title.split("（")[0].split("年")[0]
