"""Authlib OAuth client — registers GitHub, Google, Meta, and Microsoft providers."""

from authlib.integrations.flask_client import OAuth

oauth = OAuth()

# Providers that use OIDC discovery (server_metadata_url) need no explicit
# access_token_url / authorize_url — Authlib fetches those from the well-known
# endpoint at first use and caches them.
_PROVIDER_BASE_CONFIG: dict[str, dict] = {
    "github": {
        "access_token_url": "https://github.com/login/oauth/access_token",
        "authorize_url": "https://github.com/login/oauth/authorize",
        "api_base_url": "https://api.github.com/",
        "client_kwargs": {"scope": "read:user user:email", "timeout": 10},
    },
    "google": {
        "server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration",
        "client_kwargs": {"scope": "openid email profile", "timeout": 10},
    },
    "meta": {
        "access_token_url": "https://graph.facebook.com/oauth/access_token",
        "authorize_url": "https://www.facebook.com/dialog/oauth",
        "api_base_url": "https://graph.facebook.com/",
        "client_kwargs": {"scope": "email,public_profile", "timeout": 10},
    },
    # Microsoft tenant is interpolated at init time from settings so all
    # tenants — or a specific org — can be targeted via MICROSOFT_TENANT_ID.
    "microsoft": {
        "server_metadata_url": (
            "https://login.microsoftonline.com/{tenant}/v2.0"
            "/.well-known/openid-configuration"
        ),
        "client_kwargs": {"scope": "openid email profile", "timeout": 10},
    },
}

# Authoritative set of provider names used for route validation.
ALLOWED_PROVIDERS: frozenset[str] = frozenset(_PROVIDER_BASE_CONFIG)


def init_oauth(app) -> None:
    """Bind the OAuth instance to *app* and register all configured providers.

    Credentials are read directly from Settings (which reads from .env) so
    they never need to be copied into app.config first.

    A provider whose client_id is empty is registered anyway so that Authlib's
    attribute access (``oauth.google`` etc.) never raises AttributeError —
    the provider simply won't redirect correctly until credentials are supplied.

    Args:
        app: Flask application instance.
    """
    from config import get_settings
    s = get_settings()

    _credentials: dict[str, tuple[str, str]] = {
        "github":    (s.github_oauth_client_id,    s.github_oauth_client_secret.get_secret_value()),
        "google":    (s.google_oauth_client_id,    s.google_oauth_client_secret.get_secret_value()),
        "meta":      (s.meta_oauth_client_id,      s.meta_oauth_client_secret.get_secret_value()),
        "microsoft": (s.microsoft_oauth_client_id, s.microsoft_oauth_client_secret.get_secret_value()),
    }

    oauth.init_app(app)

    for provider, base_cfg in _PROVIDER_BASE_CONFIG.items():
        cfg = dict(base_cfg)
        client_id, client_secret = _credentials[provider]

        if provider == "microsoft":
            cfg["server_metadata_url"] = cfg["server_metadata_url"].format(
                tenant=s.microsoft_tenant_id or "common"
            )

        oauth.register(
            name=provider,
            client_id=client_id,
            client_secret=client_secret,
            **cfg,
        )
