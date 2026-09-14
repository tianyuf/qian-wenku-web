#!/usr/bin/env python3
"""Flask application factory for the public corpus viewer."""

import os
import logging
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .database import NianpuDatabase
from .access import (
    AccessRequestLimitError,
    EmailDeliveryError,
    access_database_is_healthy,
    get_access_grant,
    initialize_access_database,
    is_access_grant_active,
    issue_access_grant,
    normalize_email,
    send_access_code,
    update_access_grant_email,
    verify_access_code,
    verify_turnstile,
)


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
    app.config['BETA_PASSPHRASE'] = os.getenv("BETA_PASSPHRASE", "")
    app.config['ACCESS_DATABASE_PATH'] = os.getenv("ACCESS_DATABASE_PATH", "")
    app.config['ACCESS_CODE_SECRET'] = os.getenv("ACCESS_CODE_SECRET", "")
    app.config['ACCESS_CODE_TTL_DAYS'] = int(os.getenv("ACCESS_CODE_TTL_DAYS", "90"))
    app.config['ACCESS_HOURLY_LIMIT'] = int(os.getenv("ACCESS_HOURLY_LIMIT", "100"))
    app.config['ACCESS_TERMS_VERSION'] = os.getenv("ACCESS_TERMS_VERSION", "2026-09")
    app.config['RESEND_API_KEY'] = os.getenv("RESEND_API_KEY", "")
    app.config['TURNSTILE_SITE_KEY'] = os.getenv("TURNSTILE_SITE_KEY", "")
    app.config['TURNSTILE_SECRET_KEY'] = os.getenv("TURNSTILE_SECRET_KEY", "")
    app.config['TURNSTILE_HOSTNAMES'] = {
        hostname.strip()
        for hostname in os.getenv("TURNSTILE_HOSTNAMES", "").split(",")
        if hostname.strip()
    }
    app.config['ACCESS_FROM_EMAIL'] = os.getenv(
        "ACCESS_FROM_EMAIL",
        "qianxuesen.org <no-reply@send.fangemail.com>",
    )
    app.config['PUBLIC_BASE_URL'] = os.getenv(
        "PUBLIC_BASE_URL", "https://wenku.qianxuesen.org"
    )
    app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "")
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower()
        in {"1", "true", "yes"},
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )
    if app.config['ACCESS_DATABASE_PATH'] and not app.config['ACCESS_CODE_SECRET']:
        raise RuntimeError("ACCESS_CODE_SECRET is required for individual access codes")
    if app.config['ACCESS_DATABASE_PATH']:
        initialize_access_database(app.config['ACCESS_DATABASE_PATH'])
    auth_enabled = bool(
        app.config['BETA_PASSPHRASE'] or app.config['ACCESS_DATABASE_PATH']
    )
    if auth_enabled and not app.config['SECRET_KEY']:
        raise RuntimeError("SECRET_KEY is required when access control is enabled")

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

    def safe_next_url(value):
        parsed = urlsplit(value or "")
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
            return url_for("views.index")
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")

    @app.before_request
    def require_beta_login():
        if not auth_enabled:
            return None
        if request.endpoint in {"login", "request_access", "static", "health_check"}:
            return None
        # Public AI install doc: no auth required so chat harnesses can fetch it.
        if request.path == "/mcp/install.md":
            return None
        if session.get("beta_authenticated"):
            grant_id = session.get("access_grant_id")
            if grant_id is None and app.config['BETA_PASSPHRASE']:
                return None
            if grant_id is not None and app.config['ACCESS_DATABASE_PATH']:
                if is_access_grant_active(app.config['ACCESS_DATABASE_PATH'], grant_id):
                    return None
            session.clear()
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if not auth_enabled:
            return redirect(url_for("views.index"))
        error = None
        next_url = safe_next_url(request.values.get("next"))
        if request.method == 'POST':
            supplied = request.form.get("passphrase", "")
            grant_id = None
            if app.config['ACCESS_DATABASE_PATH']:
                grant_id = verify_access_code(
                    app.config['ACCESS_DATABASE_PATH'],
                    app.config['ACCESS_CODE_SECRET'],
                    supplied,
                    mark_used=True,
                )
            fallback_valid = bool(app.config['BETA_PASSPHRASE']) and hmac.compare_digest(
                supplied.encode(), app.config['BETA_PASSPHRASE'].encode()
            )
            if grant_id is not None or fallback_valid:
                session.clear()
                session["beta_authenticated"] = True
                if grant_id is not None:
                    session["access_grant_id"] = grant_id
                session.permanent = True
                return redirect(next_url)
            error = "访问码不正确或已过期"
        return render_template('login.html', error=error, next_url=next_url)

    @app.route('/request-access', methods=['GET', 'POST'])
    def request_access():
        requests_enabled = bool(
            app.config['ACCESS_DATABASE_PATH']
            and app.config['RESEND_API_KEY']
            and app.config['ACCESS_FROM_EMAIL']
            and app.config['TURNSTILE_SITE_KEY']
            and app.config['TURNSTILE_SECRET_KEY']
            and app.config['TURNSTILE_HOSTNAMES']
        )
        if not requests_enabled:
            return render_template('request_access.html', unavailable=True), 503

        csrf_token = session.get("access_request_csrf")
        if not csrf_token:
            csrf_token = secrets.token_urlsafe(24)
            session["access_request_csrf"] = csrf_token

        error = None
        submitted = False
        if request.method == 'POST':
            supplied_csrf = request.form.get("csrf_token", "")
            if not hmac.compare_digest(supplied_csrf.encode(), csrf_token.encode()):
                error = "请刷新页面后重试。"
            elif request.form.get("website"):
                submitted = True
            elif not verify_turnstile(
                app.config['TURNSTILE_SECRET_KEY'],
                request.form.get("cf-turnstile-response", ""),
                request.headers.get("CF-Connecting-IP", request.remote_addr),
                "request_access",
                app.config['TURNSTILE_HOSTNAMES'],
            ):
                error = "请完成人机验证后重试。"
            elif not request.form.get("accept_terms"):
                error = "请先勾选同意文库使用条款。"
            else:
                email = normalize_email(request.form.get("email", ""))
                if not email:
                    error = "请输入有效的邮箱地址。"
                else:
                    try:
                        grant = issue_access_grant(
                            app.config['ACCESS_DATABASE_PATH'],
                            app.config['ACCESS_CODE_SECRET'],
                            email,
                            app.config['ACCESS_TERMS_VERSION'],
                            app.config['ACCESS_CODE_TTL_DAYS'],
                            hourly_limit=app.config['ACCESS_HOURLY_LIMIT'],
                        )
                    except AccessRequestLimitError:
                        return render_template(
                            'request_access.html',
                            csrf_token=csrf_token,
                            error="访问申请较多，请稍后再试。",
                            submitted=False,
                            unavailable=False,
                            ttl_days=app.config['ACCESS_CODE_TTL_DAYS'],
                        ), 429
                    if grant is not None:
                        grant_id, code, expires_at = grant
                        try:
                            send_access_code(
                                app.config['RESEND_API_KEY'],
                                app.config['ACCESS_FROM_EMAIL'],
                                email,
                                code,
                                expires_at,
                                grant_id,
                                app.config['PUBLIC_BASE_URL'],
                            )
                        except EmailDeliveryError:
                            app.logger.exception("Access-code email delivery failed")
                            error = (
                                "Delivery could not be confirmed. Check your email before "
                                "trying again."
                            )
                    if error is None:
                        submitted = True

        return render_template(
            'request_access.html',
            csrf_token=csrf_token,
            error=error,
            submitted=submitted,
            unavailable=False,
            ttl_days=app.config['ACCESS_CODE_TTL_DAYS'],
            turnstile_site_key=app.config['TURNSTILE_SITE_KEY'],
        )

    @app.route('/account', methods=['GET', 'POST'])
    def account():
        grant_id = session.get("access_grant_id")
        if grant_id is None or not app.config['ACCESS_DATABASE_PATH']:
            return redirect(url_for("views.index"))

        grant = get_access_grant(app.config['ACCESS_DATABASE_PATH'], grant_id)
        if grant is None:
            session.clear()
            return redirect(url_for("login"))

        csrf_token = session.get("account_csrf")
        if not csrf_token:
            csrf_token = secrets.token_urlsafe(24)
            session["account_csrf"] = csrf_token

        email, expires_at = grant
        error = None
        message = None
        if request.method == 'POST':
            supplied_csrf = request.form.get("csrf_token", "")
            if not hmac.compare_digest(supplied_csrf.encode(), csrf_token.encode()):
                error = "请刷新页面后重试。"
            else:
                new_email = normalize_email(request.form.get("email", ""))
                if not new_email:
                    error = "请输入有效的邮箱地址。"
                elif not update_access_grant_email(
                    app.config['ACCESS_DATABASE_PATH'], grant_id, new_email
                ):
                    session.clear()
                    return redirect(url_for("login"))
                else:
                    email = new_email
                    message = "邮箱已更新。"

        expiration = datetime.fromtimestamp(expires_at, timezone.utc).strftime(
            "%Y-%m-%d"
        )
        return render_template(
            'account.html',
            account_email=email,
            expiration=expiration,
            csrf_token=csrf_token,
            error=error,
            message=message,
        )

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # Join app logger to gunicorn's error logger when running under gunicorn
    gunicorn_logger = logging.getLogger('gunicorn.error')
    if gunicorn_logger.handlers:
        app.logger.handlers = gunicorn_logger.handlers
        app.logger.setLevel(gunicorn_logger.level)

    @app.context_processor
    def inject_asset_config():
        return {
            "r2_cdn_url": app.config["R2_CDN_URL"],
            "beta_auth_enabled": auth_enabled,
            "individual_account": session.get("access_grant_id") is not None,
            "access_requests_enabled": bool(
                app.config['ACCESS_DATABASE_PATH']
                and app.config['RESEND_API_KEY']
                and app.config['TURNSTILE_SITE_KEY']
                and app.config['TURNSTILE_SECRET_KEY']
                and app.config['TURNSTILE_HOSTNAMES']
            ),
        }

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
            access_ok = not app.config['ACCESS_DATABASE_PATH'] or access_database_is_healthy(
                app.config['ACCESS_DATABASE_PATH']
            )
            return jsonify({
                "status": "healthy" if mapping_ok and access_ok else "degraded",
                "mapping_ok": mapping_ok,
                "access_store_ok": access_ok,
                "stats": stats
            }), 200 if access_ok else 500
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
