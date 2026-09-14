"""Frontend view routes (HTML pages)."""

import calendar
import json
import re
from collections import defaultdict
from contextlib import contextmanager
from datetime import date
from flask import Blueprint, render_template, request, current_app, url_for, redirect
from markupsafe import escape, Markup

from .database import NianpuDatabase
from .formatting import add_pdf_page_fields, clamp_int, format_date_chinese

views_bp = Blueprint('views', __name__, template_folder='templates')


def _entry_is_favorited(entry_id):
    """Return whether the current session's account favorited an entry."""
    from flask import session
    from .access import is_favorite
    access_db_path = current_app.config.get('ACCESS_DATABASE_PATH', '')
    grant_id = session.get("access_grant_id")
    if grant_id is None or not access_db_path:
        return False
    try:
        return is_favorite(access_db_path, grant_id, entry_id)
    except Exception:
        current_app.logger.exception("Favorite state check failed")
        return False


def _entry_notes(entry_id):
    """Return the current session's notes on an entry."""
    from flask import session
    from datetime import datetime, timezone
    from .access import list_entry_notes
    access_db_path = current_app.config.get('ACCESS_DATABASE_PATH', '')
    grant_id = session.get("access_grant_id")
    if grant_id is None or not access_db_path:
        return []
    try:
        notes = list_entry_notes(access_db_path, grant_id, entry_id)
    except Exception:
        current_app.logger.exception("Entry notes lookup failed")
        return []
    for note in notes:
        note["created_display"] = datetime.fromtimestamp(
            int(note["created_at"]), timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
        if note["updated_at"] != note["created_at"]:
            note["edited_display"] = datetime.fromtimestamp(
                int(note["updated_at"]), timezone.utc
            ).strftime("%Y-%m-%d %H:%M")
    return notes


@contextmanager
def get_db():
    """Context manager for database connections."""
    db = NianpuDatabase(current_app.config['DATABASE_PATH'])
    db.connect()
    try:
        yield db
    finally:
        db.close()


def extract_person_name(title):
    """Extract person name from source title using pattern matching."""
    # Remove parenthetical date ranges: 邓小平年谱（1975-1997）→ 邓小平年谱
    base = title.split('（')[0].split('(')[0]

    # Known name patterns: 2-4 Chinese chars followed by 年谱/传记/回忆录
    m = re.match(r'([一-鿿]{2,4})(?:年谱|传|回忆录|思想)', base)
    if m:
        return m.group(1)

    # Institutional chronicles: keep full base title
    if '大事记' in base or '年鉴' in base:
        return base

    return base


def highlight_search(text, query):
    """Highlight search terms in text with <mark> tags."""
    if not query:
        return escape(text)
    # Escape HTML first, then wrap matches in mark tags.
    # Must work with plain str so Jinja's |safe renders the <mark> tags.
    escaped_text = str(escape(text))
    escaped_query = str(escape(query))
    pattern = re.compile(r'(' + re.escape(escaped_query) + r')')
    result = pattern.sub(r'<mark class="highlight">\1</mark>', escaped_text)
    # Mark as safe so Jinja renders the mark tags as HTML
    from markupsafe import Markup
    return Markup(result)


def snippet_around_query(text, query, max_len=300):
    """Extract a snippet of text centered around the first occurrence of query.

    If query is found, returns up to max_len characters centered on the match
    with ellipsis on either side as needed. If no query, returns a truncated
    version from the start.
    """
    if not query:
        if len(text) > max_len:
            return text[:max_len] + "..."
        return text

    idx = text.find(query)
    if idx < 0:
        # Query not in this text (can happen with OR-style filtering)
        if len(text) > max_len:
            return text[:max_len] + "..."
        return text

    # Center snippet around the match
    context_before = max_len // 3
    start = max(0, idx - context_before)
    end = min(len(text), start + max_len)

    # Adjust start to avoid cutting mid-character for Chinese
    snippet = text[start:end]

    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""

    return prefix + snippet + suffix


def build_search_query(query=None, person=None, year_start=None, year_end=None, source_ids=None, content_type=None, extra_query=None):
    """Build WHERE clause and params for search queries."""
    where_clauses = []
    params = []

    if query:
        # Search across content_clean, title, and footnotes
        where_clauses.append("(e.content_clean LIKE ? OR e.title LIKE ? OR e.footnotes LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
    if extra_query:
        # Additional narrowing query (AND with main query)
        where_clauses.append("(e.content_clean LIKE ? OR e.title LIKE ? OR e.footnotes LIKE ?)")
        params.extend([f"%{extra_query}%", f"%{extra_query}%", f"%{extra_query}%"])
    if person:
        where_clauses.append("s.title LIKE ?")
        params.append(f"%{person}%")
    if year_start:
        where_clauses.append("e.year >= ?")
        params.append(year_start)
    if year_end:
        where_clauses.append("e.year <= ?")
        params.append(year_end)
    if source_ids:
        placeholders = ','.join('?' * len(source_ids))
        where_clauses.append(f"e.source_id IN ({placeholders})")
        params.extend(source_ids)
    if content_type:
        where_clauses.append("e.content_type = ?")
        params.append(content_type)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    return where_sql, params


def do_search(query=None, person=None, year_start=None, year_end=None,
              source_ids=None, content_type=None, limit=20, offset=0, extra_query=None):
    """Execute a search query and return (results, total, limit, offset)."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    results = []
    total = 0

    if query or person or year_start or year_end or source_ids or content_type or extra_query:
        where_sql, params = build_search_query(query, person, year_start, year_end, source_ids, content_type, extra_query)

        with get_db() as db:
            sql = f"""
                SELECT e.*, s.title as source_title, s.volume_number, s.pdf_path, s.total_pages, s.content_type as source_content_type
                FROM entries e
                JOIN sources s ON e.source_id = s.id
                WHERE {where_sql}
                ORDER BY e.date_iso, e.entry_order
                LIMIT ? OFFSET ?
            """
            params.extend([limit, offset])
            rows = db.conn.execute(sql, params).fetchall()

            for row in rows:
                results.append(format_entry(dict(row), query))

            count_params = params[:-2]
            total = db.conn.execute(
                f"SELECT COUNT(*) FROM entries e JOIN sources s ON e.source_id = s.id WHERE {where_sql}",
                count_params
            ).fetchone()[0]

    return results, total, limit, offset


def format_entry(row, query=None):
    """Format a database row as a template-friendly dict."""
    row = add_pdf_page_fields(row)
    source_title = row["source_title"] if "source_title" in row.keys() else row.get("title", "")
    content_type = row.get("content_type", "nianpu") or "nianpu"
    content_raw = row["content_clean"]
    content_display = snippet_around_query(content_raw, query, max_len=300)

    entry_title = row.get("title") if content_type in ("wenji", "shuxin") else None
    date_display = format_date_chinese(row.get("date_iso"), row.get("date_precision"))

    # Build citation differently for wenji/shuxin vs nianpu
    if content_type in ("wenji", "shuxin") and entry_title:
        citation = f"{source_title}，{entry_title}"
        if date_display and date_display != "日期不详":
            citation += f"，{date_display}"
        citation += f"，第{row['start_page']}页。"
    else:
        citation = f"{source_title}，{date_display}，第{row['start_page']}页。"

    return {
        "id": row["id"],
        "permalink": row.get("permalink"),
        "content_type": content_type,
        "title": entry_title,
        "date_display": date_display,
        "source_title": source_title,
        "preview": content_display,
        "content": highlight_search(content_display, query),
        "pdf_start_page": row["pdf_start_page"],
        "pdf_end_page": row["pdf_end_page"],
        "source_id": row["source_id"],
        "total_pages": row.get("total_pages"),
        "pdf_available": bool(row.get("pdf_path")),
        "pages": {
            "start": row["start_page"],
            "end": row["end_page"],
            "pdf_start": row["pdf_start_page"],
            "pdf_end": row["pdf_end_page"],
        },
        "source": {
            "id": row["source_id"],
            "title": source_title,
            "volume": row.get("volume_number"),
            "total_pages": row.get("total_pages"),
            "pdf_available": bool(row.get("pdf_path")),
            "content_type": content_type,
        },
        "citation": citation
    }


def get_filter_data():
    """Get data for filter dropdowns."""
    with get_db() as db:
        cursor = db.conn.execute("""
            SELECT s.id, s.title, s.volume_number, s.content_type, COUNT(e.id) as entry_count
            FROM sources s
            LEFT JOIN entries e ON s.id = e.source_id
            GROUP BY s.id
            ORDER BY s.title
        """)

        persons = {}
        sources = []
        for row in cursor.fetchall():
            title = row["title"]
            person = extract_person_name(title)

            if person not in persons:
                persons[person] = {"name": person, "total_entries": 0}
            persons[person]["total_entries"] += row["entry_count"]

            sources.append({
                "id": row["id"],
                "title": title,
                "volume": row["volume_number"],
                "entry_count": row["entry_count"],
                "content_type": row["content_type"] or "nianpu"
            })

    person_list = sorted(persons.values(), key=lambda x: x["total_entries"], reverse=True)
    return person_list, sources


@views_bp.route('/')
def index():
    """Main search page."""
    query = request.args.get('q', '').strip()
    person = request.args.get('person', '').strip()
    year_start = request.args.get('year_start', type=int)
    year_end = request.args.get('year_end', type=int)
    source_ids = request.args.getlist('source_id', type=int)
    content_type = request.args.get('content_type', '').strip()
    extra_query = request.args.get('q2', '').strip()
    limit = clamp_int(request.args.get('limit'), 20, 1, 200)
    offset = clamp_int(request.args.get('offset'), 0, 0, 1_000_000)

    persons, sources = get_filter_data()
    results, total, limit, offset = do_search(
        query, person, year_start, year_end, source_ids, content_type or None, limit, offset, extra_query or None)

    on_this_day = []
    today_label = None
    if not (query or person or year_start or year_end or source_ids or content_type or extra_query):
        today = date.today()
        today_label = f"{today.month}月{today.day}日"
        with get_db() as db:
            on_this_day = [dict(r) for r in db.conn.execute("""
                SELECT e.id, e.date_iso, e.content_type, e.permalink,
                       substr(e.content_clean, 1, 400) AS preview
                FROM entries e
                WHERE length(e.date_iso) = 10 AND substr(e.date_iso, 6) = ?
                ORDER BY e.date_iso
            """, (today.strftime('%m-%d'),)).fetchall()]

    return render_template('search.html',
                           query=query,
                           results=results,
                           total=total,
                           limit=limit,
                           offset=offset,
                           persons=persons,
                           sources=sources,
                           selected_person=person,
                           selected_sources=source_ids,
                           year_start=year_start,
                           year_end=year_end,
                           content_type=content_type,
                           on_this_day=on_this_day,
                           today_label=today_label)


@views_bp.route('/search/results')
def search_results():
    """HTMX partial for search results; renders the full page for direct visits."""
    query = request.args.get('q', '').strip()
    person = request.args.get('person', '').strip()
    year_start = request.args.get('year_start', type=int)
    year_end = request.args.get('year_end', type=int)
    source_ids = request.args.getlist('source_id', type=int)
    content_type = request.args.get('content_type', '').strip()
    extra_query = request.args.get('q2', '').strip()
    limit = clamp_int(request.args.get('limit'), 20, 1, 200)
    offset = clamp_int(request.args.get('offset'), 0, 0, 1_000_000)

    if not request.headers.get('HX-Request'):
        # Direct visit (bookmark/refresh of an HTMX-pushed URL): render full page
        return index()

    results, total, limit, offset = do_search(
        query, person, year_start, year_end, source_ids, content_type or None, limit, offset, extra_query or None)

    # Get source titles for filter chips
    source_titles = {}
    if source_ids:
        with get_db() as db:
            for sid in source_ids:
                row = db.conn.execute("SELECT title FROM sources WHERE id = ?", (sid,)).fetchone()
                if row:
                    source_titles[sid] = row[0]

    return render_template('search_results_partial.html',
                           results=results,
                           total=total,
                           limit=limit,
                           offset=offset,
                           query=query,
                           extra_query=extra_query,
                           selected_person=person,
                           selected_sources=source_ids,
                           source_titles=source_titles,
                           year_start=year_start,
                           year_end=year_end,
                           content_type=content_type)


@views_bp.route('/search/suggest')
def search_suggest():
    """Search autocomplete suggestions."""
    query = request.args.get('q', '').strip()
    if not query:
        return ''

    with get_db() as db:
        # Find entries containing the query and extract meaningful phrases
        cursor = db.conn.execute("""
            SELECT content_clean FROM entries
            WHERE content_clean LIKE ?
            LIMIT 20
        """, [f"%{query}%"])

        # Extract unique short phrases containing the query
        seen = set()
        suggestions = []
        for (text,) in cursor.fetchall():
            idx = text.find(query)
            if idx >= 0:
                # Take ~15 chars of context around the match
                start = max(0, idx - 5)
                end = min(len(text), idx + len(query) + 10)
                phrase = text[start:end].strip()
                if phrase and phrase not in seen and len(phrase) >= len(query):
                    seen.add(phrase)
                    suggestions.append(phrase)
                if len(suggestions) >= 8:
                    break

    if not suggestions:
        return ''

    html = '<div class="list-group list-group-flush" role="listbox">'
    for i, s in enumerate(suggestions):
        escaped = escape(s)
        json_val = json.dumps(s, ensure_ascii=False)
        html += (
            f'<a href="#" id="search-suggestion-{i}" class="list-group-item list-group-item-action py-1 small" '
            f'role="option" aria-selected="false" '
            f'onclick="document.querySelector(\'input[name=q]\').value={json_val};'
            f'document.getElementById(\'search-form\').dispatchEvent(new Event(\'submit\'));'
            f'this.closest(\'.list-group\').remove();return false;">{escaped}</a>'
        )
    html += '</div>'
    return html


@views_bp.route('/browse')
def browse():
    """Browse by source."""
    with get_db() as db:
        cursor = db.conn.execute("""
            SELECT s.id, s.title, s.volume_number, s.content_type, COUNT(e.id) as entry_count,
                   MIN(e.year) as min_year, MAX(e.year) as max_year
            FROM sources s
            LEFT JOIN entries e ON s.id = e.source_id
            GROUP BY s.id
            ORDER BY s.content_type, s.volume_number, s.title
        """)
        sources = [dict(r) for r in cursor.fetchall()]

    # Group by content type
    nianpu_sources = [s for s in sources if (s.get("content_type") or "nianpu") == "nianpu"]
    wenji_sources = [s for s in sources if s.get("content_type") == "wenji"]
    shuxin_sources = [s for s in sources if s.get("content_type") == "shuxin"]

    return render_template('browse.html',
                           sources=sources,
                           nianpu_sources=nianpu_sources,
                           wenji_sources=wenji_sources,
                           shuxin_sources=shuxin_sources)


@views_bp.route('/browse/source/<int:source_id>')
def browse_source(source_id):
    """Browse entries for a specific source, paginated by year."""
    with get_db() as db:
        # Get source info
        row = db.conn.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()

        if not row:
            return render_template('404.html'), 404

        source = dict(row)

        type_rows = db.conn.execute("""
            SELECT content_type, COUNT(*) as count
            FROM entries
            WHERE source_id = ?
            GROUP BY content_type
        """, (source_id,)).fetchall()
        type_counts = {r["content_type"] or "nianpu": r["count"] for r in type_rows}

        # Homogeneous wenji/shuxin sources use their compact source-specific views.
        if set(type_counts) == {"wenji"}:
            return redirect(url_for('views.browse_wenji', source_id=source_id))

        if set(type_counts) == {"shuxin"}:
            return redirect(url_for('views.browse_shuxin', source_id=source_id))

        # Mixed sources need a table-of-contents view so no content type is hidden.
        if len(type_counts) > 1:
            rows = db.conn.execute("""
                SELECT id, title, date_iso, date_precision, content_type,
                       start_page, end_page, page_numbers, entry_order, content_clean
                FROM entries
                WHERE source_id = ?
                ORDER BY entry_order, id
            """, (source_id,)).fetchall()
            entries = []
            for r in rows:
                e = add_pdf_page_fields(dict(r))
                e["date_display"] = format_date_chinese(e.get("date_iso"), e.get("date_precision"))
                e["source_title"] = source["title"]
                text = e.get("content_clean") or ""
                e["preview"] = text[:100] + "..." if len(text) > 100 else text
                entries.append(e)
            return render_template('browse_source_mixed.html',
                                   source=source,
                                   entries=entries,
                                   type_counts=type_counts,
                                   total=sum(type_counts.values()))

        # Get year distribution for this source
        year_cursor = db.conn.execute("""
            SELECT year, COUNT(*) as count
            FROM entries
            WHERE source_id = ? AND year IS NOT NULL
            GROUP BY year
            ORDER BY year
        """, (source_id,))
        years = [dict(r) for r in year_cursor.fetchall()]

        # Get total entries
        total = db.conn.execute(
            "SELECT COUNT(*) FROM entries WHERE source_id = ?", (source_id,)
        ).fetchone()[0]

    return render_template('browse_source.html',
                           source=source,
                           years=years,
                           total=total)


@views_bp.route('/browse/wenji/<int:source_id>')
def browse_wenji(source_id):
    """Browse wenji articles for a specific volume."""
    with get_db() as db:
        # Get source info
        row = db.conn.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()

        if not row:
            return render_template('404.html'), 404

        source = dict(row)

        # Get articles ordered by entry_order
        articles = db.conn.execute("""
            SELECT id, title, date_iso, date_precision, start_page, end_page, page_numbers,
                   entry_order, content_clean
            FROM entries
            WHERE source_id = ? AND content_type = 'wenji'
            ORDER BY entry_order
        """, (source_id,)).fetchall()

        articles_list = []
        for a in articles:
            a = add_pdf_page_fields(dict(a))
            date_display = format_date_chinese(a["date_iso"], a["date_precision"])
            preview = (a["content_clean"] or "")[:80] + "..." if a["content_clean"] and len(a["content_clean"]) > 80 else (a["content_clean"] or "")
            articles_list.append({
                "id": a["id"],
                "title": a["title"] or "无标题",
                "date_display": date_display,
                "content_type": "wenji",
                "source_title": source["title"],
                "start_page": a["start_page"],
                "end_page": a["end_page"],
                "pdf_start_page": a["pdf_start_page"],
                "pdf_end_page": a["pdf_end_page"],
                "entry_order": a["entry_order"],
                "preview": preview,
            })

    return render_template('browse_wenji.html',
                           source=source,
                           articles=articles_list)


@views_bp.route('/browse/shuxin/<int:source_id>')
def browse_shuxin(source_id):
    """Browse shuxin letters for a specific volume."""
    with get_db() as db:
        row = db.conn.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()

        if not row:
            return render_template('404.html'), 404

        source = dict(row)

        letters = db.conn.execute("""
            SELECT id, title, date_iso, date_precision, start_page, end_page, page_numbers,
                   entry_order, content_clean
            FROM entries
            WHERE source_id = ? AND content_type = 'shuxin'
            ORDER BY entry_order
        """, (source_id,)).fetchall()

        letters_list = []
        for l in letters:
            l = add_pdf_page_fields(dict(l))
            date_display = format_date_chinese(l["date_iso"], l["date_precision"])
            preview = (l["content_clean"] or "")[:80] + "..." if l["content_clean"] and len(l["content_clean"]) > 80 else (l["content_clean"] or "")
            letters_list.append({
                "id": l["id"],
                "title": l["title"] or "致（无收件人）",
                "date_display": date_display,
                "content_type": "shuxin",
                "source_title": source["title"],
                "start_page": l["start_page"],
                "end_page": l["end_page"],
                "pdf_start_page": l["pdf_start_page"],
                "pdf_end_page": l["pdf_end_page"],
                "entry_order": l["entry_order"],
                "preview": preview,
            })

    return render_template('browse_shuxin.html',
                           source=source,
                           letters=letters_list)


@views_bp.route('/browse/year/<int:year>')
def browse_year(year):
    """Browse all entries for a specific year, grouped by content type."""
    month = request.args.get('month', type=int)

    with get_db() as db:
        sql = """
            SELECT e.*, s.title as source_title, s.volume_number, s.pdf_path, s.total_pages, s.content_type as source_content_type
            FROM entries e
            JOIN sources s ON e.source_id = s.id
            WHERE e.year = ?
        """
        params = [year]

        if month:
            sql += " AND e.month = ?"
            params.append(month)

        sql += " ORDER BY e.content_type, e.date_iso, e.entry_order LIMIT 500"
        cursor = db.conn.execute(sql, params)
        rows = cursor.fetchall()

        # Format and group by content type
        grouped = {"nianpu": [], "wenji": [], "shuxin": []}
        entries_list = []
        for row in rows:
            e = add_pdf_page_fields(dict(row))
            e["date_display"] = format_date_chinese(e.get("date_iso"), e.get("date_precision"))
            e["preview"] = e.get("content_clean", "") or ""
            if len(e["preview"]) > 120:
                e["preview"] = e["preview"][:120] + "..."
            ct = e.get("content_type", "nianpu") or "nianpu"
            if ct in grouped:
                grouped[ct].append(e)
            entries_list.append(e)

        count = len(entries_list)

        # Prev/next years
        prev_year = db.conn.execute(
            "SELECT MAX(year) FROM entries WHERE year < ?", (year,)
        ).fetchone()[0]
        next_year = db.conn.execute(
            "SELECT MIN(year) FROM entries WHERE year > ?", (year,)
        ).fetchone()[0]

    if month:
        display_date = f"{year}年{month}月"
    else:
        display_date = f"{year}年"

    return render_template('browse_year.html',
                           year=year,
                           month=month,
                           display_date=display_date,
                           entries=entries_list,
                           grouped=grouped,
                           count=count,
                           prev_year=prev_year,
                           next_year=next_year)


@views_bp.route('/entry/<int:entry_id>')
def entry_detail(entry_id):
    """Legacy ID URL: 301 redirect to the canonical content-hash permalink."""
    with get_db() as db:
        row = db.conn.execute("""
            SELECT e.permalink, e.date_iso, e.content_clean, s.title AS source_title
            FROM entries e JOIN sources s ON e.source_id = s.id WHERE e.id = ?
        """, (entry_id,)).fetchone()
        if not row:
            return render_template('404.html'), 404
        permalink = row["permalink"]
        if not permalink:
            return render_template('404.html'), 404
    return redirect(url_for('views.entry_permalink', permalink=permalink), code=301)


@views_bp.route('/e/<permalink>')
def entry_permalink(permalink):
    """Canonical entry URL."""
    with get_db() as db:
        row = db.conn.execute(
            "SELECT id FROM entries WHERE permalink = ?", (permalink,)).fetchone()
    if not row:
        return render_template('404.html'), 404
    return _render_entry_detail(row["id"])


def _render_entry_detail(entry_id):
    """Full entry detail page."""
    with get_db() as db:
        cursor = db.conn.execute("""
            SELECT e.*, s.title as source_title, s.volume_number, s.pdf_path, s.total_pages, s.content_type as source_content_type
            FROM entries e
            JOIN sources s ON e.source_id = s.id
            WHERE e.id = ?
        """, (entry_id,))
        row = cursor.fetchone()

        if not row:
            return render_template('404.html'), 404

        entry = dict(row)
        entry = add_pdf_page_fields(entry)
        entry["date_display"] = format_date_chinese(entry.get("date_iso"), entry.get("date_precision"))
        content_type = entry.get("content_type", "nianpu") or "nianpu"

        # Build citation based on content type
        if content_type == "wenji" and entry.get("title"):
            entry["citation"] = f"{entry['source_title']}，{entry['title']}"
            if entry["date_display"] != "日期不详":
                entry["citation"] += f"，{entry['date_display']}"
            entry["citation"] += f"，第{entry['start_page']}页。"
        else:
            entry["citation"] = f"{entry['source_title']}，{entry['date_display']}，第{entry['start_page']}页。"

        # Prev/next within same source
        prev_entry = db.conn.execute("""
            SELECT e.id, e.permalink, e.title, e.date_iso, e.date_precision, e.content_type,
                   substr(e.content_clean, 1, 80) || '...' as preview
            FROM entries e
            WHERE (e.entry_order < ? OR (e.entry_order = ? AND e.id < ?))
              AND e.source_id = ?
            ORDER BY e.entry_order DESC, e.id DESC
            LIMIT 1
        """, (entry["entry_order"], entry["entry_order"], entry["id"], entry["source_id"])).fetchone()

        next_entry = db.conn.execute("""
            SELECT e.id, e.permalink, e.title, e.date_iso, e.date_precision, e.content_type,
                   substr(e.content_clean, 1, 80) || '...' as preview
            FROM entries e
            WHERE (e.entry_order > ? OR (e.entry_order = ? AND e.id > ?))
              AND e.source_id = ?
            ORDER BY e.entry_order ASC, e.id ASC
            LIMIT 1
        """, (entry["entry_order"], entry["entry_order"], entry["id"], entry["source_id"])).fetchone()

        # Format prev/next display
        prev_data = None
        if prev_entry:
            prev_ct = prev_entry["content_type"] or "nianpu"
            prev_label = prev_entry["title"] if prev_ct in ("wenji", "shuxin") and prev_entry["title"] else format_date_chinese(prev_entry["date_iso"], prev_entry["date_precision"])
            prev_data = {
                "id": prev_entry["id"],
                "permalink": prev_entry["permalink"],
                "date_display": prev_label,
                "preview": prev_entry["preview"],
                "content_type": prev_ct
            }

        next_data = None
        if next_entry:
            next_ct = next_entry["content_type"] or "nianpu"
            next_label = next_entry["title"] if next_ct in ("wenji", "shuxin") and next_entry["title"] else format_date_chinese(next_entry["date_iso"], next_entry["date_precision"])
            next_data = {
                "id": next_entry["id"],
                "permalink": next_entry["permalink"],
                "date_display": next_label,
                "preview": next_entry["preview"],
                "content_type": next_ct
            }

        # Recipients for this entry (shuxin only)
        recipients = []
        if content_type == "shuxin":
            rec_cursor = db.conn.execute("""
                SELECT r.id, r.name FROM recipients r
                JOIN recipient_letters er ON r.id = er.recipient_id
                WHERE er.entry_id = ?
                ORDER BY r.name
            """, (entry_id,))
            recipients = [dict(r) for r in rec_cursor.fetchall()]

        # Cross-referencing: find related entries across content types
        same_day = []
        date_iso = entry.get("date_iso")
        date_precision = entry.get("date_precision")

        if date_iso:
            if date_precision == "exact" and len(date_iso) >= 10:
                # Exact date: find all entries on same day (capped)
                same_cursor = db.conn.execute("""
                    SELECT e.id, e.permalink, e.title, e.date_iso, e.date_precision, e.content_type,
                           s.title as source_title,
                           substr(e.content_clean, 1, 60) as preview
                    FROM entries e
                    JOIN sources s ON e.source_id = s.id
                    WHERE e.date_iso = ? AND e.id != ?
                    ORDER BY e.content_type, e.date_iso, e.entry_order
                    LIMIT 50
                """, (date_iso[:10], entry_id))
            elif date_precision == "month" and len(date_iso) >= 7:
                # Month precision: find entries in same month
                month_prefix = date_iso[:7]  # YYYY-MM
                same_cursor = db.conn.execute("""
                    SELECT e.id, e.permalink, e.title, e.date_iso, e.date_precision, e.content_type,
                           s.title as source_title,
                           substr(e.content_clean, 1, 60) as preview
                    FROM entries e
                    JOIN sources s ON e.source_id = s.id
                    WHERE e.date_iso LIKE ? AND e.id != ?
                    ORDER BY e.content_type, e.date_iso, e.entry_order
                """, (month_prefix + "%", entry_id))
            elif date_precision == "year" and len(date_iso) >= 4:
                # Year precision: find entries in same year (limit to avoid huge results)
                year_val = entry.get("year")
                same_cursor = db.conn.execute("""
                    SELECT e.id, e.permalink, e.title, e.date_iso, e.date_precision, e.content_type,
                           s.title as source_title,
                           substr(e.content_clean, 1, 60) as preview
                    FROM entries e
                    JOIN sources s ON e.source_id = s.id
                    WHERE e.year = ? AND e.id != ?
                    ORDER BY e.content_type, e.date_iso, e.entry_order
                    LIMIT 50
                """, (year_val, entry_id))
            else:
                same_cursor = None

            if same_cursor:
                same_day = [dict(r) for r in same_cursor.fetchall()]

        # Entity-based cross-references: find entries sharing entities
        entity_related = []
        same_day_ids = {r["id"] for r in same_day} if same_day else set()
        entity_cursor = db.conn.execute("""
            SELECT e2.id, e2.permalink, e2.title, e2.date_iso, e2.date_precision, e2.content_type,
                   s.title as source_title,
                   substr(e2.content_clean, 1, 60) as preview,
                   ent.name as entity_name, ent.type as entity_type
            FROM entry_entities ee1
            JOIN entry_entities ee2 ON ee1.entity_id = ee2.entity_id AND ee2.entry_id != ?
            JOIN entities ent ON ent.id = ee1.entity_id
            JOIN entries e2 ON e2.id = ee2.entry_id
            JOIN sources s ON e2.source_id = s.id
            WHERE ee1.entry_id = ?
            ORDER BY ent.name, e2.content_type, e2.date_iso
            LIMIT 30
        """, (entry_id, entry_id))
        entity_rows = entity_cursor.fetchall()

        # Group by related entry, collecting shared entity names
        entry_entities_map = {}
        for r in entity_rows:
            rd = dict(r)
            eid = rd["id"]
            if eid in same_day_ids:
                continue  # already shown in same-day section
            if eid not in entry_entities_map:
                entry_entities_map[eid] = {
                    "id": rd["id"],
                    "permalink": rd["permalink"],
                    "title": rd["title"],
                    "date_iso": rd["date_iso"],
                    "date_precision": rd["date_precision"],
                    "content_type": rd["content_type"],
                    "source_title": rd["source_title"],
                    "preview": rd["preview"],
                    "entities": [],
                }
            entry_entities_map[eid]["entities"].append(
                {"name": rd["entity_name"], "type": rd["entity_type"]}
            )

        # Deduplicate entity names per entry and limit display
        for eid, data in entry_entities_map.items():
            seen = set()
            unique = []
            for ent in data["entities"]:
                if ent["name"] not in seen:
                    seen.add(ent["name"])
                    unique.append(ent)
            data["entities"] = unique[:5]  # show max 5 entity names
            data["date_display"] = format_date_chinese(data.get("date_iso"), data.get("date_precision"))

        entity_related = list(entry_entities_map.values())[:15]

        # Fetch duplicate entries (same content in different sources)
        duplicate_cursor = db.conn.execute("""
            SELECT e.id, e.permalink, e.title, e.date_iso, e.date_precision, e.content_type,
                   s.title as source_title,
                   substr(e.content_clean, 1, 60) as preview,
                   ed.similarity_score
            FROM entry_duplicates ed
            JOIN entries e ON e.id = ed.duplicate_entry_id
            JOIN sources s ON e.source_id = s.id
            WHERE ed.entry_id = ?
            ORDER BY ed.similarity_score DESC
        """, (entry_id,))
        duplicates = [dict(r) for r in duplicate_cursor.fetchall()]
        for dup in duplicates:
            dup["date_display"] = format_date_chinese(dup.get("date_iso"), dup.get("date_precision"))

        # Entities mentioned in this entry (for "本文提及" chips)
        own_entities = [dict(r) for r in db.conn.execute("""
            SELECT en.id, en.name, en.type
            FROM entry_entities ee JOIN entities en ON en.id = ee.entity_id
            WHERE ee.entry_id = ?
            ORDER BY en.name
            LIMIT 20
        """, (entry_id,)).fetchall()]

    return render_template('entry_detail.html',
                           entry=entry,
                           prev_entry=prev_data,
                           next_entry=next_data,
                           same_day=same_day,
                           entity_related=entity_related,
                           recipients=recipients,
                           duplicates=duplicates,
                           own_entities=own_entities,
                           favorited=_entry_is_favorited(entry_id),
                           entry_notes=_entry_notes(entry_id))


@views_bp.route('/browse/recipients')
def browse_recipients():
    """Recipient directory: list all recipients with letter counts."""
    query = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'count')
    limit = clamp_int(request.args.get('limit'), 50, 1, 200)
    offset = clamp_int(request.args.get('offset'), 0, 0, 1_000_000)

    with get_db() as db:
        recipients, total = db.get_recipients(
            query=query or None, sort=sort, limit=limit, offset=offset
        )

    return render_template('recipient_list.html',
                           recipients=recipients,
                           total=total,
                           limit=limit,
                           offset=offset,
                           query=query,
                           sort=sort)


@views_bp.route('/browse/recipient/<int:recipient_id>')
def browse_recipient(recipient_id):
    """Recipient profile: all letters to this recipient."""
    with get_db() as db:
        recipient = db.get_recipient(recipient_id)
        if not recipient:
            return render_template('404.html'), 404

        entries = db.get_recipient_entries(recipient_id)
        prev_recipient, next_recipient = db.get_recipient_neighbors(recipient_id)

        # Format entries for display
        letters = []
        for e in entries:
            e = add_pdf_page_fields(dict(e))
            date_display = format_date_chinese(e["date_iso"], e["date_precision"])
            preview = (e["content_clean"] or "")[:120] + "..." if e["content_clean"] and len(e["content_clean"]) > 120 else (e["content_clean"] or "")
            letters.append({
                "id": e["id"],
                "permalink": e.get("permalink"),
                "date_display": date_display,
                "date_iso": e["date_iso"],
                "title": e.get("title") or f"致{recipient['name']}",
                "content_type": e.get("content_type") or "shuxin",
                "preview": preview,
                "start_page": e["start_page"],
                "end_page": e["end_page"],
                "pdf_start_page": e["pdf_start_page"],
                "pdf_end_page": e["pdf_end_page"],
                "source_id": e["source_id"],
                "source_title": e["source_title"],
                "pdf_available": bool(e["pdf_path"]),
                "total_pages": e["total_pages"],
            })

        # Date range
        date_range = ""
        if letters:
            first = letters[0]["date_display"]
            last = letters[-1]["date_display"]
            date_range = f"{first} – {last}" if first != last else first

    return render_template('recipient_detail.html',
                           recipient=recipient,
                           letters=letters,
                           date_range=date_range,
                           prev_recipient=prev_recipient,
                           next_recipient=next_recipient)


@views_bp.route('/date/')
@views_bp.route('/timeline')
def browse_timeline():
    """Calendar heatmap index; ?date= redirects to a specific day."""
    date_str = request.args.get('date', '').strip()
    if date_str:
        return redirect(url_for('views.browse_date', date_iso=date_str))

    with get_db() as db:
        rows = db.conn.execute("""
            SELECT date_iso, COUNT(*) AS n FROM entries
            WHERE length(date_iso) = 10
              AND substr(date_iso, 6, 2) BETWEEN '01' AND '12'
              AND substr(date_iso, 9, 2) BETWEEN '01' AND '31'
            GROUP BY date_iso
        """).fetchall()

    counts = {r["date_iso"]: r["n"] for r in rows}
    year_counts = {}
    for iso, n in counts.items():
        y = int(iso[:4])
        year_counts[y] = year_counts.get(y, 0) + n
    years = sorted(year_counts)

    selected_year = request.args.get('year', type=int)
    if selected_year not in year_counts:
        selected_year = None

    valid_days = set()
    if selected_year:
        for m in range(1, 13):
            for d in range(1, calendar.monthrange(selected_year, m)[1] + 1):
                valid_days.add(f"{m:02d}-{d:02d}")

    year_range = range(years[0] // 10 * 10, years[-1] + 1) if years else range(0)

    return render_template('date_index.html',
                           counts=counts,
                           years=years,
                           year_counts=year_counts,
                           year_range=year_range,
                           max_year_count=max(year_counts.values(), default=0),
                           selected_year=selected_year,
                           valid_days=valid_days)


@views_bp.route('/date/<path:date_iso>')
def browse_date(date_iso):
    """Browse all entries for a given date, month, or year."""
    date_iso = date_iso.strip()

    with get_db() as db:
        # Determine date precision from format
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_iso):
            # Exact date
            entries = db.conn.execute("""
                SELECT e.id, e.title, e.date_iso, e.date_precision, e.content_type,
                       e.start_page, e.end_page, e.page_numbers, e.source_id,
                       substr(e.content_clean, 1, 120) as preview,
                       s.title as source_title, s.pdf_path, s.total_pages
                FROM entries e
                JOIN sources s ON e.source_id = s.id
                WHERE e.date_iso = ?
                ORDER BY e.content_type, e.date_iso, e.entry_order
            """, (date_iso,)).fetchall()

            # Previous/next days with entries
            prev_day = db.conn.execute("""
                SELECT date_iso FROM entries
                WHERE date_iso < ? AND length(date_iso) >= 10 AND date_precision = 'exact'
                ORDER BY date_iso DESC LIMIT 1
            """, (date_iso,)).fetchone()
            next_day = db.conn.execute("""
                SELECT date_iso FROM entries
                WHERE date_iso > ? AND length(date_iso) >= 10 AND date_precision = 'exact'
                ORDER BY date_iso LIMIT 1
            """, (date_iso,)).fetchone()

        elif re.match(r'^\d{4}-\d{2}$', date_iso):
            # Month
            entries = db.conn.execute("""
                SELECT e.id, e.title, e.date_iso, e.date_precision, e.content_type,
                       e.start_page, e.end_page, e.page_numbers, e.source_id,
                       substr(e.content_clean, 1, 120) as preview,
                       s.title as source_title, s.pdf_path, s.total_pages
                FROM entries e
                JOIN sources s ON e.source_id = s.id
                WHERE e.date_iso LIKE ?
                ORDER BY e.content_type, e.date_iso, e.entry_order
            """, (date_iso + '%',)).fetchall()

            prev_day = db.conn.execute("""
                SELECT DISTINCT date_iso FROM entries
                WHERE date_iso < ? AND length(date_iso) >= 7
                ORDER BY date_iso DESC LIMIT 1
            """, (date_iso,)).fetchone()
            next_day = db.conn.execute("""
                SELECT DISTINCT date_iso FROM entries
                WHERE date_iso > ? AND length(date_iso) >= 7
                ORDER BY date_iso LIMIT 1
            """, (date_iso + '￿',)).fetchone()

        elif re.match(r'^\d{4}$', date_iso):
            # Year
            year = int(date_iso)
            entries = db.conn.execute("""
                SELECT e.id, e.title, e.date_iso, e.date_precision, e.content_type,
                       e.start_page, e.end_page, e.page_numbers, e.source_id,
                       substr(e.content_clean, 1, 120) as preview,
                       s.title as source_title, s.pdf_path, s.total_pages
                FROM entries e
                JOIN sources s ON e.source_id = s.id
                WHERE e.year = ?
                ORDER BY e.content_type, e.date_iso, e.entry_order
            """, (year,)).fetchall()

            prev_day = db.conn.execute("""
                SELECT CAST(year AS TEXT) FROM entries
                WHERE year < ? AND year IS NOT NULL
                ORDER BY year DESC LIMIT 1
            """, (year,)).fetchone()
            next_day = db.conn.execute("""
                SELECT CAST(year AS TEXT) FROM entries
                WHERE year > ? AND year IS NOT NULL
                ORDER BY year LIMIT 1
            """, (year,)).fetchone()

        else:
            return render_template('404.html'), 400

        # Format entries and group by content type
        entries_list = []
        grouped = {"nianpu": [], "wenji": [], "shuxin": []}
        for row in entries:
            e = add_pdf_page_fields(dict(row))
            e["date_display"] = format_date_chinese(e.get("date_iso"), e.get("date_precision"))
            e["preview"] = e.get("preview", "") or ""
            entries_list.append(e)
            ct = e.get("content_type", "nianpu") or "nianpu"
            if ct in grouped:
                grouped[ct].append(e)

        # Approximately-dated entries relevant to this date
        # (month/year/season precision, day ranges covering the date)
        approx_list = []
        approx_rows = []
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_iso):
            month_prefix = date_iso[:7]
            year_prefix = date_iso[:4]
            approx_rows = db.conn.execute("""
                SELECT e.id, e.title, e.date_iso, e.date_precision, e.content_type,
                       e.start_page, e.end_page, e.page_numbers, e.source_id,
                       substr(e.content_clean, 1, 120) as preview,
                       s.title as source_title, s.pdf_path, s.total_pages
                FROM entries e
                JOIN sources s ON e.source_id = s.id
                WHERE (
                    (e.date_iso = ? AND e.date_precision IN ('month', 'relative'))
                    OR (e.date_iso = ? AND e.date_precision IN ('year', 'season'))
                    OR (e.date_precision = 'day_range'
                        AND substr(e.date_iso, 1, 10) <= ?
                        AND substr(e.date_iso, 12, 10) >= ?)
                )
                ORDER BY e.content_type, e.date_iso, e.entry_order
            """, (month_prefix, year_prefix, date_iso, date_iso)).fetchall()
        elif re.match(r'^\d{4}-\d{2}$', date_iso):
            year_prefix = date_iso[:4]
            approx_rows = db.conn.execute("""
                SELECT e.id, e.title, e.date_iso, e.date_precision, e.content_type,
                       e.start_page, e.end_page, e.page_numbers, e.source_id,
                       substr(e.content_clean, 1, 120) as preview,
                       s.title as source_title, s.pdf_path, s.total_pages
                FROM entries e
                JOIN sources s ON e.source_id = s.id
                WHERE (
                    (e.date_iso = ? AND e.date_precision IN ('year', 'season'))
                    OR (e.date_precision = 'day_range'
                        AND substr(e.date_iso, 1, 7) <= ?
                        AND substr(e.date_iso, 12, 7) >= ?)
                )
                ORDER BY e.content_type, e.date_iso, e.entry_order
            """, (year_prefix, date_iso, date_iso)).fetchall()

        approx_grouped = {"nianpu": [], "wenji": [], "shuxin": []}
        shown_ids = {e["id"] for e in entries_list}
        for row in approx_rows:
            if row["id"] in shown_ids:
                continue
            e = add_pdf_page_fields(dict(row))
            e["date_display"] = format_date_chinese(e.get("date_iso"), e.get("date_precision"))
            e["preview"] = e.get("preview", "") or ""
            approx_list.append(e)
            ct = e.get("content_type", "nianpu") or "nianpu"
            if ct in approx_grouped:
                approx_grouped[ct].append(e)

        # Entities mentioned on this date
        all_shown = entries_list + approx_list
        entities_on_date = []
        if all_shown:
            placeholders = ",".join("?" * len(all_shown))
            entities_on_date = db.conn.execute(f"""
                SELECT en.id, en.name, en.type, COUNT(*) AS mentions
                FROM entry_entities ee
                JOIN entities en ON en.id = ee.entity_id
                WHERE ee.entry_id IN ({placeholders})
                GROUP BY en.id
                ORDER BY mentions DESC, en.name
                LIMIT 16
            """, [e["id"] for e in all_shown]).fetchall()

        # Format display date
        if len(date_iso) == 10:
            # YYYY-MM-DD
            parts = date_iso.split('-')
            display_date = f"{parts[0]}年{int(parts[1])}月{int(parts[2])}日"
        elif len(date_iso) == 7:
            # YYYY-MM
            parts = date_iso.split('-')
            display_date = f"{parts[0]}年{int(parts[1])}月"
        else:
            display_date = f"{date_iso}年"

    count = len(entries_list)

    return render_template('date.html',
                           date_iso=date_iso,
                           display_date=display_date,
                           entries=entries_list,
                           grouped=grouped,
                           approx_grouped=approx_grouped,
                           approx_count=len(approx_list),
                           entities_on_date=entities_on_date,
                           count=count,
                           prev_date=prev_day[0] if prev_day else None,
                           next_date=next_day[0] if next_day else None)

@views_bp.route('/browse/entities')
def browse_entities():
    """Entity directory: persons and organizations."""
    query = request.args.get('q', '').strip()
    entity_type = request.args.get('type', '').strip()  # person, organization, or '' for all
    sort = request.args.get('sort', 'total')
    limit = clamp_int(request.args.get('limit'), 50, 1, 200)
    offset = clamp_int(request.args.get('offset'), 0, 0, 1_000_000)

    with get_db() as db:
        entities, total = db.get_entities(
            entity_type=entity_type or None,
            query=query or None,
            sort=sort,
            limit=limit,
            offset=offset
        )

    return render_template('entity_list.html',
                           entities=entities,
                           total=total,
                           limit=limit,
                           offset=offset,
                           query=query,
                           entity_type=entity_type,
                           sort=sort)


@views_bp.route('/browse/entity/<int:entity_id>')
def browse_entity(entity_id):
    """Entity profile page."""
    sort = request.args.get('sort', 'nianpu')

    with get_db() as db:
        entity = db.get_entity(entity_id)
        if not entity:
            return render_template('404.html'), 404

        entries = db.get_entity_entries(entity_id)
        prev_entity, next_entity = db.get_entity_neighbors(entity_id, sort=sort)

        # Format entries
        formatted = []
        for e in entries:
            e = add_pdf_page_fields(dict(e))
            date_display = format_date_chinese(e.get("date_iso"), e.get("date_precision"))
            content_type = e.get("content_type", "nianpu") or "nianpu"
            preview = (e.get("content_clean") or "")[:120]
            if len(e.get("content_clean") or "") > 120:
                preview += "..."
            formatted.append({
                "id": e["id"],
                "permalink": e.get("permalink"),
                "date_display": date_display,
                "date_iso": e.get("date_iso"),
                "content_type": content_type,
                "title": e.get("title"),
                "preview": preview,
                "start_page": e.get("start_page"),
                "end_page": e.get("end_page"),
                "pdf_start_page": e.get("pdf_start_page"),
                "pdf_end_page": e.get("pdf_end_page"),
                "source_id": e.get("source_id"),
                "source_title": e.get("source_title"),
                "pdf_available": bool(e.get("pdf_path")),
                "total_pages": e.get("total_pages"),
            })

        # Date range
        date_range = ""
        if formatted:
            first_date = formatted[0].get("date_display", "")
            last_date = formatted[-1].get("date_display", "")
            date_range = f"{first_date} – {last_date}" if first_date != last_date else first_date

        # Find matching recipient for shuxin link
        recipient_id = None
        if entity.get("shuxin_count"):
            row = db.conn.execute(
                "SELECT id FROM recipients WHERE name = ?", (entity["name"],)
            ).fetchone()
            if row:
                recipient_id = row[0]

    return render_template('entity_detail.html',
                           entity=entity,
                           entries=formatted,
                           date_range=date_range,
                           prev_entity=prev_entity,
                           next_entity=next_entity,
                           sort=sort,
                           recipient_id=recipient_id)


@views_bp.route('/about')
def about():
    """About page."""
    stats = {}
    with get_db() as db:
        stats = db.get_stats()
        stats['recipient_count'] = db.conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0]
        stats['entity_count'] = db.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        stats['nianpu_count'] = db.conn.execute("SELECT COUNT(*) FROM entries WHERE content_type='nianpu'").fetchone()[0]
        stats['wenji_count'] = db.conn.execute("SELECT COUNT(*) FROM entries WHERE content_type='wenji'").fetchone()[0]
        stats['shuxin_count'] = db.conn.execute("SELECT COUNT(*) FROM entries WHERE content_type='shuxin'").fetchone()[0]
    return render_template('about.html', stats=stats)


@views_bp.route('/mcp')
def mcp_guide():
    """MCP (Model Context Protocol) usage guide."""
    return render_template('mcp.html')


@views_bp.route('/mcp/install.md')
def mcp_install_doc():
    """Public, unauthenticated install instructions for AI clients.

    This document is designed to be handed to an agent (paste the URL into
    an OpenCode or Claude chat). The agent follows the steps inside and
    writes the MCP server entry itself. It never contains a real token —
    the user is prompted to supply theirs.
    """
    from flask import Response
    body = """# Install the Qian Wenku MCP server

Give these instructions to your AI agent (OpenCode, Claude Code, Claude
Desktop, Codex, Cursor, or any MCP-aware assistant). The agent will edit the
appropriate config file for you.

## What this installs

A remote MCP server pointing at this site. It has five tools:

- **search**: substring search over corpus entries; filters by year range, content
  type (nianpu / wenji / shuxin), and source volume.
- **get_entry**: fetch one entry by ID; returns full transcript, date, source,
  page references, plus prev/next/same-day navigation.
- **list_sources**: list every volume with entry counts and year ranges, so you
  know what to filter by.
- **list_favorites**: the user's favorited entries (needs their personal MCP
  token), so the agent can work with a personal reading list.
- **list_notes**: the user's annotations on entries, including highlighted
  quotes and comment text (needs their personal MCP token).

The server is hosted — nothing runs on your machine. Your agent calls it
over HTTPS.

## What you need

Your **MCP token** for this site. Sign in at
https://wenku.qianxuesen.org/login, then create one on the account page.

Ask the user for that MCP token now if you don't already have it. Do
NOT proceed with a placeholder.

## How to install

Follow the steps for the user's client. Do not invent config paths or
fields — look them up if unsure.

### OpenCode

Config file:
- Prefer `~/.config/opencode/opencode.jsonc` if it exists (JSON with
  comments, the default for many installs).
- Otherwise use `~/.config/opencode/opencode.json`.

Read the existing file, preserving all existing top-level keys and any
existing `mcp` entries. Merge in:

```jsonc
{
  "mcp": {
    "qian-wenku": {
      "type": "remote",
      "url": "https://wenku.qianxuesen.org/mcp/rpc",
      "headers": { "Authorization": "Bearer USER_MCP_TOKEN" }
    }
  }
}
```

Replace `USER_MCP_TOKEN` with the user's MCP token. Write the file
back, preserving the user's `$schema` declaration and any comments.

If `mcp.qian-wenku` already exists, stop and ask the user whether to
overwrite. Never clobber silently.

Tell the user: reload OpenCode (it is not hot-reloaded), then in the next
session they can use tools named like `mcp_qian-wenku_search`.

### Claude Code (CLI)

Tell the user to run:

```bash
claude mcp add --transport http qian-wenku \\
  https://wenku.qianxuesen.org/mcp/rpc \\
  --header "Authorization: Bearer USER_MCP_TOKEN"
```

Scope to current project by default; `--scope user` for all projects.

### Claude Desktop

Edit `claude_desktop_config.json`:
- macOS: `~/Library/Application Support/Claude/`
- Windows: `%APPDATA%\\Claude\\`

Inside the existing `mcpServers` object, add:

```jsonc
"qian-wenku": {
  "type": "http",
  "url": "https://wenku.qianxuesen.org/mcp/rpc",
  "headers": { "Authorization": "Bearer USER_MCP_TOKEN" }
}
```

Tell the user to fully quit and restart Claude Desktop.

### Codex (CLI, IDE extension, ChatGPT desktop app)

Edit `~/.codex/config.toml` (project-scoped servers may live in
`.codex/config.toml` for trusted projects). Read the existing file,
preserving all other entries, and merge in:

```toml
[mcp_servers.qian-wenku]
url = "https://wenku.qianxuesen.org/mcp/rpc"
bearer_token_env_var = "WENKU_MCP_TOKEN"
```

Then the user sets the environment variable, e.g. add to their shell
profile (`~/.zshrc` / `~/.bashrc`):

```bash
export WENKU_MCP_TOKEN="USER_MCP_TOKEN"
```

Replace `USER_MCP_TOKEN` with the user's MCP token. As an alternative to
the environment variable, a static header also works:

```toml
[mcp_servers.qian-wenku]
url = "https://wenku.qianxuesen.org/mcp/rpc"
http_headers = { "Authorization" = "Bearer USER_MCP_TOKEN" }
```

If `[mcp_servers.qian-wenku]` already exists, stop and ask the user
whether to overwrite. Never clobber silently.

Tell the user to restart Codex (CLI / IDE extension / ChatGPT desktop
app — they share this config), then in a new session use `/mcp` to
confirm the server is connected, or just ask it to search qian-wenku.

### Other MCP clients

Any client that supports remote HTTP MCP servers with a static
Authorization header works. The two things to wire up are always:

- **URL**: `https://wenku.qianxuesen.org/mcp/rpc`
- **Header**: `Authorization: Bearer USER_MCP_TOKEN`

## Verify it worked

After the user reloads their client, ask them to type something like:
"List the sources in qian-wenku" or "Search qian-wenku for 钱学森 in 1956".
The tool call should return real data, not an error. If it fails, have them
confirm the MCP token is active on the account page and try again.

## Privacy note

The MCP token gives read access to the same corpus the website exposes.
Do not share it. Do not commit config files containing it to version
control.
"""
    return Response(body, content_type='text/markdown; charset=utf-8')
