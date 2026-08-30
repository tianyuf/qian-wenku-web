#!/usr/bin/env python3
"""Flask application factory for the public corpus viewer."""

import os
import logging
from flask import Flask, jsonify, request, render_template

from .database import NianpuDatabase


def create_app(db_path=None, mapping_path=None, manifest_path=None):
    """Application factory."""
    app = Flask(__name__,
                template_folder='templates',
                static_folder='static')

    artifact_dir = os.environ.get("WENKU_ARTIFACT_DIR", "artifacts")
    app.config['DATABASE_PATH'] = str(db_path or os.path.join(artifact_dir, "corpus.db"))
    app.config['PAGE_IMAGES_MAPPING'] = str(
        mapping_path or os.path.join(artifact_dir, "page_images.json")
    )
    app.config['MANIFEST_PATH'] = str(
        manifest_path or os.path.join(artifact_dir, "manifest.json")
    )
    app.config['R2_CDN_BASE'] = os.getenv("R2_CDN_BASE", "https://dewey.tfang.info")
    app.config['R2_PREFIX'] = os.getenv("R2_PREFIX", "qian")
    app.config['R2_CDN_URL'] = (
        f"{app.config['R2_CDN_BASE'].rstrip('/')}/{app.config['R2_PREFIX'].strip('/')}"
    )

    from .api.search import search_bp
    from .api.browse import browse_bp
    from .api.pdf import pdf_bp
    from .api.meta import meta_bp
    from .views import views_bp

    app.register_blueprint(search_bp, url_prefix='/api/search')
    app.register_blueprint(browse_bp, url_prefix='/api/browse')
    app.register_blueprint(pdf_bp, url_prefix='/api/pdf')
    app.register_blueprint(meta_bp, url_prefix='/api/meta')

    app.register_blueprint(views_bp)

    # Join app logger to gunicorn's error logger when running under gunicorn
    gunicorn_logger = logging.getLogger('gunicorn.error')
    if gunicorn_logger.handlers:
        app.logger.handlers = gunicorn_logger.handlers
        app.logger.setLevel(gunicorn_logger.level)

    @app.context_processor
    def inject_asset_config():
        return {"r2_cdn_url": app.config["R2_CDN_URL"]}

    @app.route('/health')
    def health_check():
        """Health check endpoint."""
        try:
            db_path = app.config['DATABASE_PATH']
            if not os.path.exists(db_path):
                return jsonify({"status": "unhealthy", "error": "database file missing"}), 500
            with NianpuDatabase(db_path) as db:
                stats = db.get_stats()
            mapping_ok = os.path.exists(app.config['PAGE_IMAGES_MAPPING'])
            return jsonify({
                "status": "healthy" if mapping_ok else "degraded",
                "mapping_ok": mapping_ok,
                "stats": stats
            })
        except Exception as e:
            app.logger.error(f"Health check failed: {e}")
            return jsonify({"status": "unhealthy", "error": "database error"}), 500

    @app.errorhandler(404)
    def not_found(error):
        # API routes return JSON, page routes return HTML
        if request.path.startswith('/api/'):
            return jsonify({"error": "Not found"}), 404
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def internal_error(error):
        if request.path.startswith('/api/'):
            return jsonify({"error": "Internal server error"}), 500
        return render_template('500.html'), 500

    return app


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=None,
                        help='Database path')
    parser.add_argument('--host', default='127.0.0.1',
                        help='Host to bind to')
    parser.add_argument('--port', type=int, default=5000,
                        help='Port to bind to')
    parser.add_argument('--debug', action='store_true',
                        help='Enable debug mode')
    args = parser.parse_args()

    app = create_app(db_path=args.db)
    app.run(host=args.host, port=args.port, debug=args.debug)
