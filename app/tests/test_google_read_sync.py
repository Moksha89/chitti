from __future__ import annotations

import base64

import pytest

from chitti.google_crypto import CredentialCipher
from chitti.google_oauth import OAuthStateStore
from chitti.google_provider import GOOGLE_SCOPES
from chitti.google_sync_access import sync_grants
from chitti.runner_access import derived_grants


def test_google_refresh_token_is_ciphertext_at_rest() -> None:
    cipher = CredentialCipher(base64.urlsafe_b64encode(b"k" * 32).decode())
    ciphertext = cipher.encrypt("refresh-token-value")

    assert "refresh-token-value" not in ciphertext
    assert cipher.decrypt(ciphertext) == "refresh-token-value"


def test_oauth_state_is_single_use_and_bound_to_csrf_and_namespace() -> None:
    states = OAuthStateStore()
    state = states.create("csrf-token", "jsv-fashion")

    assert states.consume(state, "csrf-token") == "jsv-fashion"
    with pytest.raises(ValueError, match="invalid or expired"):
        states.consume(state, "csrf-token")


def test_google_contract_is_read_only() -> None:
    assert GOOGLE_SCOPES == (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    )


def test_sync_grants_are_limited_to_google_sync_tables() -> None:
    grants = sync_grants(
        {
            "google_provider_accounts",
            "google_oauth_credentials",
            "google_sync_state",
            "google_gmail_messages",
            "google_calendar_events",
            "runner_health",
        }
    )

    assert set(grants) <= {
        "google_provider_accounts",
        "google_oauth_credentials",
        "google_sync_state",
        "google_gmail_messages",
        "google_calendar_events",
        "runner_health",
    }


def test_runner_rejects_google_sensitive_tables() -> None:
    with pytest.raises(SystemExit, match="google_oauth_credentials"):
        derived_grants(
            ["SELECT refresh_token_ciphertext FROM google_oauth_credentials"],
            {"google_oauth_credentials"},
        )
