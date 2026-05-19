"""Auth package public API: call init_auth(app) once during app startup."""

import logging

from flask import Flask, jsonify
from flask_login import LoginManager

from .db import ensure_indexes, get_db
from .models import User, find_user_by_id
from .oauth import init_oauth
from .routes import auth_bp

log = logging.getLogger(__name__)

login_manager = LoginManager()


def init_auth(app: Flask) -> None:
    """Wire auth into the Flask app.

    In order:
      1. Push OAuth credentials and SECRET_KEY into app.config.
      2. Configure LoginManager (user loader, 401 handler).
      3. Register Authlib OAuth clients (GitHub, Google, Meta, Microsoft).
      4. Register the /auth blueprint.
      5. Ensure MongoDB indexes exist (logged as warning if the DB is unreachable
         at startup — the app still starts so health checks pass).

    Args:
        app: The Flask application instance.
    """
    from config import get_settings
    s = get_settings()

    # Only SECRET_KEY is required in app.config — Flask uses it to sign session
    # cookies.  OAuth credentials are read from Settings directly in init_oauth.
    app.config["SECRET_KEY"] = s.flask_secret_key.get_secret_value()

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        """Reload the User object from the MongoDB store on every request."""
        db = get_db(s.mongodb_uri)
        doc = find_user_by_id(db, user_id)
        return User(doc) if doc else None

    @login_manager.unauthorized_handler
    def unauthorized():
        """Return JSON 401 instead of redirecting to a login page."""
        return jsonify({"error": "Authentication required."}), 401

    login_manager.init_app(app)
    init_oauth(app)
    app.register_blueprint(auth_bp)

    try:
        db = get_db(s.mongodb_uri)
        ensure_indexes(db)
        log.info("MongoDB indexes verified.")
    except Exception as exc:
        log.warning("Could not verify MongoDB indexes at startup: %s", exc)
