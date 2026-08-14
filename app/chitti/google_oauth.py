from __future__ import annotations

import secrets
import time
from typing import Any

from google_auth_oauthlib.flow import Flow

from .google_provider import GOOGLE_SCOPES
from .settings import Settings


def validate_scopes(scopes: list[str]) -> None:
    if set(scopes) != set(GOOGLE_SCOPES):
        raise ValueError("Google returned scopes outside the read-and-send contract")


class OAuthStateStore:
    def __init__(self) -> None:
        self._states: dict[str, tuple[str, str, float]] = {}

    def create(self, csrf_token: str, namespace: str) -> str:
        state = secrets.token_urlsafe(32)
        self._states[state] = (csrf_token, namespace, time.time() + 600)
        return state

    def consume(self, state: str, csrf_token: str) -> str:
        value = self._states.pop(state, None)
        if value is None or value[2] <= time.time() or not secrets.compare_digest(value[0], csrf_token):
            raise ValueError("invalid or expired Google OAuth state")
        return value[1]


def authorization_url(settings: Settings, state: str) -> str:
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.google_oauth_redirect_uri],
            }
        },
        scopes=list(GOOGLE_SCOPES),
        state=state,
    )
    flow.redirect_uri = settings.google_oauth_redirect_uri
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent",
    )
    return str(url)


def exchange_code(settings: Settings, code: str) -> dict[str, Any]:
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.google_oauth_redirect_uri],
            }
        },
        scopes=list(GOOGLE_SCOPES),
    )
    flow.redirect_uri = settings.google_oauth_redirect_uri
    flow.fetch_token(code=code)
    credentials = flow.credentials
    if not credentials.refresh_token:
        raise ValueError("Google did not return an offline refresh token")
    return {
        "refresh_token": str(credentials.refresh_token),
        "scopes": list(credentials.scopes or GOOGLE_SCOPES),
        "client_config": {
            "web": {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.google_oauth_redirect_uri],
            }
        },
    }
