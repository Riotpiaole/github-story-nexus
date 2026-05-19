"""User model: Flask-Login UserMixin wrapping a MongoDB document + CRUD helpers."""

import logging
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask_login import UserMixin

log = logging.getLogger(__name__)


class User(UserMixin):
    """Thin wrapper around a MongoDB user document for Flask-Login."""

    def __init__(self, doc: dict) -> None:
        self._doc = doc

    # Flask-Login contract
    def get_id(self) -> str:
        return str(self._doc["_id"])

    @property
    def provider(self) -> str:
        return self._doc.get("provider", "")

    @property
    def provider_id(self) -> str:
        return str(self._doc.get("provider_id", ""))

    @property
    def username(self) -> str:
        return self._doc.get("username", "")

    @property
    def email(self) -> str:
        return self._doc.get("email", "")

    @property
    def avatar_url(self) -> str:
        return self._doc.get("avatar_url", "")

    def to_dict(self) -> dict:
        return {
            "id": self.get_id(),
            "provider": self.provider,
            "provider_id": self.provider_id,
            "username": self.username,
            "email": self.email,
            "avatar_url": self.avatar_url,
        }


def upsert_user(
    db,
    *,
    provider: str,
    provider_id: str,
    username: str,
    email: str,
    avatar_url: str,
    access_token: str,
) -> dict:
    """Insert or update a user identified by (provider, provider_id).

    Uses find_one_and_update with upsert=True so the call is idempotent and
    race-condition-safe.  Returns the final document.
    """
    now = datetime.now(timezone.utc)
    doc = db.users.find_one_and_update(
        {"provider": provider, "provider_id": provider_id},
        {
            "$set": {
                "username": username,
                "email": email,
                "avatar_url": avatar_url,
                "access_token": access_token,
                "updated_at": now,
            },
            "$setOnInsert": {
                "provider": provider,
                "provider_id": provider_id,
                "created_at": now,
            },
        },
        upsert=True,
        return_document=True,
    )
    return doc


def find_user_by_id(db, user_id: str) -> dict | None:
    """Look up a user by their MongoDB ObjectId string. Returns None on miss or bad ID."""
    try:
        return db.users.find_one({"_id": ObjectId(user_id)})
    except InvalidId:
        log.debug("Invalid ObjectId supplied to find_user_by_id: %r", user_id)
        return None
