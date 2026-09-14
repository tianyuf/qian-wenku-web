#!/usr/bin/env python3
"""Flask application factory for the public corpus viewer."""

import os
import logging
import hmac
import secrets
import time
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
    count_all_accounts,
    consume_magic_link,
    create_mcp_token,
    delete_magic_link,
    get_access_grant,
    initialize_access_database,
    is_access_grant_active,
    issue_magic_link,
    list_all_accounts,
    list_mcp_tokens,
    normalize_email,
    reinstate_account,
    revoke_account,
    revoke_mcp_token,
    send_magic_link,
    send_token_revoked_notice,
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
    app.config['ADMIN_EMAILS'] = {
        email.casefold().strip()
        for email in os.getenv("ADMIN_EMAILS", "").split(",")
        if email.strip()
    }
    app.config['ACCESS_DATABASE_PATH'] = os.getenv("ACCESS_DATABASE_PATH", "")
    app.config['ACCESS_CODE_SECRET'] = os.getenv("ACCESS_CODE_SECRET", "")
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
        raise RuntimeError("ACCESS_CODE_SECRET is required for login and MCP tokens")
    if app.config['ACCESS_DATABASE_PATH']:
        initialize_access_database(app.config['ACCESS_DATABASE_PATH'])
    auth_enabled = bool(
        app.config['BETA_PASSPHRASE'] or app.config['ACCESS_DATABASE_PATH']
    )
    magic_login_enabled = bool(
        app.config['ACCESS_DATABASE_PATH']
        and app.config['RESEND_API_KEY']
        and app.config['ACCESS_FROM_EMAIL']
        and app.config['TURNSTILE_SITE_KEY']
        and app.config['TURNSTILE_SECRET_KEY']
        and app.config['TURNSTILE_HOSTNAMES']
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

    def session_is_admin():
        """Admin = operator passphrase session, or email in ADMIN_EMAILS."""
        if not session.get("beta_authenticated"):
            return False
        grant_id = session.get("access_grant_id")
        if grant_id is None:
            return bool(app.config['BETA_PASSPHRASE'])
        if not app.config['ACCESS_DATABASE_PATH'] or not app.config['ADMIN_EMAILS']:
            return False
        email = get_access_grant(app.config['ACCESS_DATABASE_PATH'], grant_id)
        return email is not None and email.casefold() in app.config['ADMIN_EMAILS']

    @app.before_request
    def require_beta_login():
        if not auth_enabled:
            return None
        if request.endpoint in {"login", "static", "health_check"}:
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

    @app.after_request
    def protect_login_tokens(response):
        if request.path == "/login":
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if not auth_enabled:
            return redirect(url_for("views.index"))
        error = None
        submitted = False
        next_url = safe_next_url(request.values.get("next"))
        csrf_token = session.get("login_csrf")
        if not csrf_token:
            csrf_token = secrets.token_urlsafe(24)
            session["login_csrf"] = csrf_token
        if request.method == 'POST':
            if request.form.get("action") == "consume_magic_link":
                if not app.config['ACCESS_DATABASE_PATH']:
                    error = "邮件登录暂时不可用。"
                elif not hmac.compare_digest(
                    request.form.get("csrf_token", "").encode(), csrf_token.encode()
                ):
                    error = "请刷新页面后重试。"
                else:
                    consumed = consume_magic_link(
                        app.config['ACCESS_DATABASE_PATH'],
                        app.config['ACCESS_CODE_SECRET'],
                        request.form.get("token", ""),
                    )
                    if consumed is not None:
                        grant_id, stored_next_url = consumed
                        session.clear()
                        session["beta_authenticated"] = True
                        session["access_grant_id"] = grant_id
                        session.permanent = True
                        return redirect(safe_next_url(stored_next_url))
                    error = "登录链接无效、已使用或已过期。请重新获取。"
            elif "passphrase" in request.form:
                supplied = request.form.get("passphrase", "")
                fallback_valid = bool(app.config['BETA_PASSPHRASE']) and hmac.compare_digest(
                    supplied.encode(), app.config['BETA_PASSPHRASE'].encode()
                )
                if fallback_valid:
                    session.clear()
                    session["beta_authenticated"] = True
                    session.permanent = True
                    return redirect(next_url)
                error = "管理员口令不正确。"
            elif not magic_login_enabled:
                error = "邮件登录暂时不可用。"
            elif not hmac.compare_digest(
                request.form.get("csrf_token", "").encode(), csrf_token.encode()
            ):
                error = "请刷新页面后重试。"
            elif request.form.get("website"):
                submitted = True
            elif not verify_turnstile(
                app.config['TURNSTILE_SECRET_KEY'],
                request.form.get("cf-turnstile-response", ""),
                request.headers.get("CF-Connecting-IP", request.remote_addr),
                "login",
                app.config['TURNSTILE_HOSTNAMES'],
            ):
                error = "请完成人机验证后重试。"
            else:
                email = normalize_email(request.form.get("email", ""))
                if not email:
                    error = "请输入有效的邮箱地址。"
                else:
                    try:
                        link = issue_magic_link(
                            app.config['ACCESS_DATABASE_PATH'],
                            app.config['ACCESS_CODE_SECRET'],
                            email,
                            next_url,
                            terms_version=app.config['ACCESS_TERMS_VERSION'],
                            create_account=True,
                            hourly_limit=app.config['ACCESS_HOURLY_LIMIT'],
                        )
                    except AccessRequestLimitError:
                        error = "登录请求较多，请稍后再试。"
                    else:
                        if link is not None:
                            link_id, login_token = link
                            try:
                                send_magic_link(
                                    app.config['RESEND_API_KEY'],
                                    app.config['ACCESS_FROM_EMAIL'],
                                    email,
                                    login_token,
                                    link_id,
                                    app.config['PUBLIC_BASE_URL'],
                                )
                            except EmailDeliveryError:
                                delete_magic_link(
                                    app.config['ACCESS_DATABASE_PATH'], link_id
                                )
                                app.logger.exception("Magic-link delivery failed")
                        submitted = True
        return render_template(
            'login.html',
            error=error,
            submitted=submitted,
            next_url=next_url,
            csrf_token=csrf_token,
            magic_login_enabled=magic_login_enabled,
            operator_login_enabled=bool(app.config['BETA_PASSPHRASE']),
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

        email = grant
        error = None
        message = None
        new_mcp_token = None
        if request.method == 'POST':
            supplied_csrf = request.form.get("csrf_token", "")
            if not hmac.compare_digest(supplied_csrf.encode(), csrf_token.encode()):
                error = "请刷新页面后重试。"
            elif request.form.get("action") == "create_mcp_token":
                token_name = request.form.get("token_name", "").strip()
                try:
                    created = create_mcp_token(
                        app.config['ACCESS_DATABASE_PATH'],
                        app.config['ACCESS_CODE_SECRET'],
                        grant_id,
                        token_name,
                    )
                except ValueError:
                    error = "请输入 1 至 80 个字符的令牌名称。"
                else:
                    if created is None:
                        session.clear()
                        return redirect(url_for("login"))
                    _, new_mcp_token = created
                    message = "MCP 令牌已创建。"
            elif request.form.get("action") == "revoke_mcp_token":
                try:
                    token_id = int(request.form.get("token_id", ""))
                except ValueError:
                    error = "MCP 令牌无效。"
                else:
                    revoked_token = None
                    for token in list_mcp_tokens(
                        app.config['ACCESS_DATABASE_PATH'], grant_id
                    ):
                        if token["id"] == token_id and token["revoked_at"] is None:
                            revoked_token = token
                            break
                    if revoked_token is not None and revoke_mcp_token(
                        app.config['ACCESS_DATABASE_PATH'], grant_id, token_id
                    ):
                        message = "MCP 令牌已撤销。"
                        try:
                            send_token_revoked_notice(
                                app.config['RESEND_API_KEY'],
                                app.config['ACCESS_FROM_EMAIL'],
                                email,
                                str(revoked_token["name"]),
                                str(revoked_token["hint"]),
                            )
                        except EmailDeliveryError:
                            app.logger.exception("Token-revoked notice failed")
                    else:
                        error = "MCP 令牌不存在或已撤销。"
            else:
                error = "未知操作。"

        mcp_tokens = list_mcp_tokens(app.config['ACCESS_DATABASE_PATH'], grant_id)
        for mcp_token in mcp_tokens:
            mcp_token["created_date"] = datetime.fromtimestamp(
                int(mcp_token["created_at"]), timezone.utc
            ).strftime("%Y-%m-%d")
            if mcp_token["last_used_at"] is not None:
                mcp_token["last_used_date"] = datetime.fromtimestamp(
                    int(mcp_token["last_used_at"]), timezone.utc
                ).strftime("%Y-%m-%d")

        return render_template(
            'account.html',
            account_email=email,
            csrf_token=csrf_token,
            error=error,
            message=message,
            mcp_tokens=mcp_tokens,
            new_mcp_token=new_mcp_token,
        )

    @app.route('/admin', methods=['GET', 'POST'])
    def admin():
        # Operator console: passphrase session, or an account whose email is
        # listed in ADMIN_EMAILS.
        if not (app.config['ACCESS_DATABASE_PATH'] and session_is_admin()):
            return redirect(url_for("views.index"))

        csrf_token = session.get("account_csrf")
        if not csrf_token:
            csrf_token = secrets.token_urlsafe(24)
            session["account_csrf"] = csrf_token

        error = None
        message = None
        if request.method == 'POST':
            supplied_csrf = request.form.get("csrf_token", "")
            if not hmac.compare_digest(supplied_csrf.encode(), csrf_token.encode()):
                error = "请刷新页面后重试。"
            else:
                action = request.form.get("action")
                try:
                    grant_id = int(request.form.get("grant_id", ""))
                except ValueError:
                    grant_id = 0
                if action == "revoke_account":
                    revoked_email = get_access_grant(
                        app.config['ACCESS_DATABASE_PATH'], grant_id
                    )
                    if revoke_account(app.config['ACCESS_DATABASE_PATH'], grant_id):
                        message = "账户已撤销。"
                        if revoked_email and app.config['RESEND_API_KEY']:
                            try:
                                send_token_revoked_notice(
                                    app.config['RESEND_API_KEY'],
                                    app.config['ACCESS_FROM_EMAIL'],
                                    revoked_email,
                                    "全部 MCP 令牌",
                                    "qx_legacy",
                                    revoked_by_operator=True,
                                )
                            except EmailDeliveryError:
                                app.logger.exception("Account-revoked notice failed")
                    else:
                        error = "账户不存在或已撤销。"
                elif action == "reinstate_account":
                    if reinstate_account(app.config['ACCESS_DATABASE_PATH'], grant_id):
                        message = "账户已恢复。"
                    else:
                        error = "账户不存在或未撤销。"
                else:
                    error = "未知操作。"

        try:
            page = max(1, int(request.values.get("page", "1")))
        except ValueError:
            page = 1
        per_page = 50
        total = count_all_accounts(app.config['ACCESS_DATABASE_PATH'])
        accounts = list_all_accounts(
            app.config['ACCESS_DATABASE_PATH'],
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        now_ts = int(time.time())
        for account in accounts:
            account["created_date"] = datetime.fromtimestamp(
                int(account["created_at"]), timezone.utc
            ).strftime("%Y-%m-%d")
            if account["last_used_at"] is not None:
                account["last_used_date"] = datetime.fromtimestamp(
                    int(account["last_used_at"]), timezone.utc
                ).strftime("%Y-%m-%d")
            else:
                account["last_used_date"] = None

        return render_template(
            'admin.html',
            csrf_token=csrf_token,
            error=error,
            message=message,
            accounts=accounts,
            page=page,
            total=total,
            per_page=per_page,
            now_ts=now_ts,
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
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        try:
            style_version = int(os.path.getmtime(os.path.join(static_dir, "style.css")))
        except OSError:
            style_version = 0
        return {
            "r2_cdn_url": app.config["R2_CDN_URL"],
            "style_version": style_version,
            "beta_auth_enabled": auth_enabled,
            "individual_account": session.get("access_grant_id") is not None,
            "operator_session": bool(app.config['BETA_PASSPHRASE']) and session_is_admin(),
            "magic_login_enabled": magic_login_enabled,
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
