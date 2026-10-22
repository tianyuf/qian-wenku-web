"""Small presentation and request-bound helpers shared by HTML and APIs."""

# Longest free-text search term accepted from a request. Anything longer is
# truncated: SQLite rejects LIKE patterns over ~50k bytes with an error, and
# every extra byte widens a full-table scan for no benefit to the reader.
MAX_SEARCH_TERM = 200


def clamp_term(value, maximum=MAX_SEARCH_TERM):
    """Return a stripped, length-capped free-text search term."""
    return (value or "").strip()[:maximum]


def like_contains(term):
    """Return a LIKE pattern that matches `term` literally anywhere in a column.

    `%`, `_` and the escape character itself are escaped so user input cannot
    act as a wildcard (``q=%`` must not match every row). Pair the result with
    ``LIKE ? ESCAPE '\\'`` in the SQL text.
    """
    escaped = (
        term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def clamp_int(value, default, minimum, maximum):
    """Return an integer constrained to a safe inclusive range."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def format_date_chinese(date_iso, precision):
    """Format an ISO date or date range for display in Chinese."""
    if not date_iso:
        return "日期不详"

    iso = date_iso.split("/")[0]
    if precision == "year":
        return f"{iso.split('-')[0]}年"

    parts = iso.split("-")
    try:
        year = parts[0]
        month = int(parts[1]) if len(parts) >= 2 else None
        day = int(parts[2]) if len(parts) >= 3 else None
    except (ValueError, IndexError):
        return date_iso

    if precision == "month" and month:
        return f"{year}年{month}月"
    if month and day:
        return f"{year}年{month}月{day}日"
    if month:
        return f"{year}年{month}月"
    return f"{year}年"


def parse_page_numbers(page_numbers):
    """Return valid integer page numbers from the provenance field."""
    if not page_numbers:
        return []
    return [int(part) for part in str(page_numbers).split(",") if part.strip().isdigit()]


def add_pdf_page_fields(row):
    """Add viewer pages without replacing bibliographic page fields."""
    pages = parse_page_numbers(row.get("page_numbers"))
    row["pdf_start_page"] = pages[0] if pages else row.get("start_page")
    row["pdf_end_page"] = pages[-1] if pages else row.get("end_page")
    return row
