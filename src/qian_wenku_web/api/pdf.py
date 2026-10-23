"""PDF page image API — redirects to pre-rendered JPEGs on Cloudflare R2."""

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import Blueprint, Response, redirect, jsonify, current_app

from ..formatting import format_date_chinese, parse_page_numbers
from ..pdfbuild import InvalidJpegError, PdfStreamBuilder
from .utils import open_db

pdf_bp = Blueprint('pdf', __name__)

# Lazy-load the mapping, invalidated on file mtime change.
_mapping_cache = None
_mapping_path_cache = None
_mapping_mtime_cache = None


def _load_mapping():
    global _mapping_cache, _mapping_path_cache, _mapping_mtime_cache
    mapping_path = current_app.config["PAGE_IMAGES_MAPPING"]
    try:
        mtime = os.path.getmtime(mapping_path)
    except OSError:
        _mapping_cache = {}
        _mapping_path_cache = mapping_path
        _mapping_mtime_cache = None
        return _mapping_cache

    if (_mapping_cache is not None
            and _mapping_path_cache == mapping_path
            and _mapping_mtime_cache == mtime):
        return _mapping_cache

    try:
        with open(mapping_path) as f:
            _mapping_cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        current_app.logger.error(f"Failed to load page images mapping: {mapping_path}")
        _mapping_cache = {}
    _mapping_path_cache = mapping_path
    _mapping_mtime_cache = mtime
    return _mapping_cache


def _page_url(source_id, page_num):
    """Return the R2 CDN URL for a page image, or None if not found."""
    mapping = _load_mapping()
    src = mapping.get(str(source_id))
    if not src:
        return None
    content_hash = src.get(str(page_num))
    if not content_hash:
        return None
    return f"{current_app.config['R2_CDN_URL']}/{content_hash}.jpg"


@pdf_bp.route('/<int:source_id>/page/<int:page_num>')
def get_page_image(source_id, page_num):
    """Redirect to the pre-rendered JPEG on R2."""
    url = _page_url(source_id, page_num)
    if url:
        return redirect(url, code=302)
    return jsonify({"error": "Page image not found"}), 404


@pdf_bp.route('/<int:source_id>/url/<int:page_num>')
def get_page_url(source_id, page_num):
    """Return the R2 CDN URL as JSON (for JS to construct URLs directly)."""
    url = _page_url(source_id, page_num)
    if url:
        return jsonify({"url": url})
    return jsonify({"error": "Page image not found"}), 404


@pdf_bp.route('/mapping')
def get_mapping():
    """Return the full page_images mapping as JSON for client-side use."""
    mapping = _load_mapping()
    return jsonify(mapping)


# --- Whole-entry PDF download -------------------------------------------------

# One entry in the corpus spans 194 pages; everything else is far smaller.
MAX_ENTRY_PDF_PAGES = 200
MAX_PAGE_IMAGE_BYTES = 25 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 15


class PageImageError(RuntimeError):
    """Raised when a page scan cannot be retrieved from the CDN."""


def fetch_image(url):
    """Download one page scan. Separate function so tests can replace it."""
    request = Request(url, headers={"User-Agent": "qian-wenku-web/0.1"})
    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_PAGE_IMAGE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise PageImageError(f"fetch failed: {type(error).__name__}") from error
    if len(data) > MAX_PAGE_IMAGE_BYTES:
        raise PageImageError("page image too large")
    return data


def _entry_pages(row):
    """Viewer page numbers for an entry: the listed pages, else the page range."""
    pages = parse_page_numbers(row["page_numbers"])
    if not pages and row["start_page"] and row["end_page"]:
        pages = list(range(int(row["start_page"]), int(row["end_page"]) + 1))
    return sorted(set(pages))


def _safe_filename(*parts):
    text = "_".join(part for part in parts if part)
    text = re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("._")
    return text[:120] or "entry"


@pdf_bp.route('/entry/<int:entry_id>.pdf')
def entry_pdf(entry_id):
    """Download the original scanned pages of one entry as a single PDF."""
    with open_db() as db:
        row = db.conn.execute(
            """
            SELECT e.id, e.title, e.date_iso, e.date_precision, e.content_type,
                   e.start_page, e.end_page, e.page_numbers, e.source_id,
                   s.title AS source_title, s.pdf_path
            FROM entries e JOIN sources s ON e.source_id = s.id
            WHERE e.id = ?
            """,
            (entry_id,),
        ).fetchone()
    if row is None:
        return jsonify({"error": "Not found"}), 404
    if not row["pdf_path"]:
        return jsonify({"error": "No scanned pages for this entry"}), 404

    pages = _entry_pages(row)
    if len(pages) > MAX_ENTRY_PDF_PAGES:
        return jsonify({"error": "Entry spans too many pages for a single download"}), 413
    urls = [(page, _page_url(row["source_id"], page)) for page in pages]
    urls = [(page, url) for page, url in urls if url]
    if not urls:
        return jsonify({"error": "Page images not found"}), 404

    # Fetch the first scan before committing to a 200 so a CDN outage is
    # reported as an error instead of an empty download.
    try:
        first_image = fetch_image(urls[0][1])
    except PageImageError:
        current_app.logger.warning("Entry PDF: first page fetch failed for entry %s", entry_id)
        return jsonify({"error": "Page images are temporarily unavailable"}), 502

    date_display = format_date_chinese(row["date_iso"], row["date_precision"])
    label = row["title"] if row["content_type"] in ("wenji", "shuxin") and row["title"] else date_display
    page_label = (
        f"第{row['start_page']}页" if row["start_page"] == row["end_page"]
        else f"第{row['start_page']}-{row['end_page']}页"
    )
    title = f"{row['source_title']}，{label}，{page_label}"
    filename = _safe_filename(row["source_title"], label, page_label) + ".pdf"
    logger = current_app.logger

    def generate():
        builder = PdfStreamBuilder()
        yield from builder.begin()
        for index, (page, url) in enumerate(urls):
            try:
                image = first_image if index == 0 else fetch_image(url)
                yield from builder.add_jpeg_page(image)
            except (PageImageError, InvalidJpegError) as error:
                logger.warning("Entry PDF: page %s of entry %s skipped: %s", page, entry_id, error)
                yield from builder.add_notice_page(
                    f"Page {page} could not be retrieved. Please try again later."
                )
        yield from builder.finish(
            title=title,
            author="qianxuesen.org",
            producer="qian-wenku-web",
        )

    headers = {
        "Content-Type": "application/pdf",
        "Content-Disposition": (
            f"attachment; filename=\"entry-{entry_id}.pdf\"; "
            f"filename*=UTF-8''{quote(filename)}"
        ),
        "Cache-Control": "private, max-age=86400",
        # Let nginx stream the response instead of buffering the whole file.
        "X-Accel-Buffering": "no",
    }
    return Response(generate(), headers=headers)
