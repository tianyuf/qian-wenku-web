"""PDF page image API — redirects to pre-rendered JPEGs on Cloudflare R2."""

import json
import os
from flask import Blueprint, redirect, jsonify, current_app

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
