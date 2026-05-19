"""Auth Blueprint: /auth/<provider>/login, /auth/<provider>/callback, /auth/logout, /auth/me.

Supported providers: github, google, meta, microsoft.
"""

import logging

from authlib.integrations.base_client.errors import OAuthError
from flask import Blueprint, abort, jsonify, url_for
from flask_login import current_user, login_required, login_user, logout_user

from .db import get_db
from .models import User, upsert_user
from .oauth import ALLOWED_PROVIDERS, oauth

log = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


# ---------------------------------------------------------------------------
# User-info normalisation — each provider returns a different shape
# ---------------------------------------------------------------------------

def _fetch_and_normalize(provider: str, token: dict) -> dict:
    """Fetch user profile from the provider API and return a normalised dict.

    Returns:
        {provider_id, username, email, avatar_url}

    Raises:
        RuntimeError: if the provider API call fails or returns unexpected data.
    """
    try:
        if provider == "github":
            return _normalize_github(token)
        elif provider == "google":
            return _normalize_google(token)
        elif provider == "meta":
            return _normalize_meta(token)
        elif provider == "microsoft":
            return _normalize_microsoft(token)
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Unexpected response shape from {provider}: {exc}") from exc

    raise RuntimeError(f"No normaliser for provider '{provider}'")  # unreachable


def _normalize_github(token: dict) -> dict:
    client = oauth.github
    info = client.get("user", token=token).json()
    emails = client.get("user/emails", token=token).json()
    email = next(
        (e["email"] for e in emails if isinstance(e, dict) and e.get("primary") and e.get("verified")),
        info.get("email", ""),
    )
    return {
        "provider_id": str(info["id"]),
        "username": info.get("login", ""),
        "email": email,
        "avatar_url": info.get("avatar_url", ""),
    }


def _normalize_google(token: dict) -> dict:
    # userinfo() parses the OIDC ID token and/or calls the userinfo endpoint.
    info = oauth.google.userinfo(token=token)
    return {
        "provider_id": info["sub"],
        "username": info.get("name", ""),
        "email": info.get("email", ""),
        "avatar_url": info.get("picture", ""),
    }


def _normalize_meta(token: dict) -> dict:
    # Request the fields we need explicitly; Meta Graph API omits them otherwise.
    resp = oauth.meta.get(
        "me?fields=id,name,email,picture.type(large)", token=token
    ).json()
    return {
        "provider_id": resp["id"],
        "username": resp.get("name", ""),
        "email": resp.get("email", ""),
        "avatar_url": resp.get("picture", {}).get("data", {}).get("url", ""),
    }


def _normalize_microsoft(token: dict) -> dict:
    # userinfo() decodes the OIDC ID token claims included in the token response.
    info = oauth.microsoft.userinfo(token=token)
    return {
        # Azure supplies "oid" (object ID) as the stable per-tenant subject.
        "provider_id": info.get("oid") or info.get("sub", ""),
        "username": info.get("name", ""),
        "email": info.get("email") or info.get("preferred_username", ""),
        "avatar_url": "",  # Microsoft Graph /me/photo requires a separate call
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _validate_provider(provider: str) -> None:
    """Abort with 404 for unknown providers so dynamic routes don't leak info."""
    if provider not in ALLOWED_PROVIDERS:
        abort(404)


@auth_bp.get("/<provider>/login")
def login(provider: str):
    """Redirect the browser to the provider's OAuth authorization page.

    If the user is already authenticated their profile is returned directly so
    programmatic clients can detect an existing session without a redirect.

    Args:
        provider: One of github | google | meta | microsoft.

    Returns:
        302  Redirect to provider authorization page.
        200  {"status": "already_authenticated", "user": {...}}  if already logged in.
        404  Unknown provider.
    """
    _validate_provider(provider)
    if current_user.is_authenticated:
        return jsonify({"status": "already_authenticated", "user": current_user.to_dict()})
    redirect_uri = url_for("auth.callback", provider=provider, _external=True)
    return getattr(oauth, provider).authorize_redirect(redirect_uri)


@auth_bp.get("/<provider>/callback")
def callback(provider: str):
    """Handle the OAuth callback from a provider.

    Exchanges the authorization code for tokens, normalises the user profile,
    upserts the user in MongoDB, and establishes a Flask-Login session.

    Args:
        provider: One of github | google | meta | microsoft.

    Returns:
        200  {"status": "authenticated", "user": {...}}
        400  {"error": "...", "detail": "..."}  — OAuth error (denied, bad state, etc.)
        502  {"error": "..."}                   — provider API unreachable / bad response
        404  Unknown provider.
    """
    _validate_provider(provider)

    try:
        token = getattr(oauth, provider).authorize_access_token()
    except OAuthError as exc:
        log.warning("[%s] OAuth error in callback: %s", provider, exc)
        return jsonify({"error": "OAuth authorization failed.", "detail": str(exc)}), 400

    try:
        normalized = _fetch_and_normalize(provider, token)
    except Exception as exc:
        log.warning("[%s] Failed to fetch/normalize user info: %s", provider, exc)
        return jsonify({"error": "Failed to retrieve user profile from provider."}), 502

    from config import get_settings
    db = get_db(get_settings().mongodb_uri)

    doc = upsert_user(
        db,
        provider=provider,
        provider_id=normalized["provider_id"],
        username=normalized["username"],
        email=normalized["email"],
        avatar_url=normalized["avatar_url"],
        access_token=token.get("access_token", ""),
    )

    user = User(doc)
    login_user(user, remember=True)
    log.info("[%s] User '%s' (provider_id=%s) authenticated.", provider, user.username, user.provider_id)
    return jsonify({"status": "authenticated", "user": user.to_dict()})


@auth_bp.get("/logout")
@login_required
def logout():
    """Clear the current user's session.

    Returns:
        200  {"status": "logged_out"}
    """
    username = current_user.username
    logout_user()
    log.info("User '%s' logged out.", username)
    return jsonify({"status": "logged_out"})


@auth_bp.get("/me")
@login_required
def me():
    """Return the authenticated user's profile.

    Returns:
        200  {"id": "...", "provider": "...", "provider_id": "...",
               "username": "...", "email": "...", "avatar_url": "..."}
        401  {"error": "Authentication required."}
    """
    return jsonify(current_user.to_dict())
