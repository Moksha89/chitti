from __future__ import annotations

import base64
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from chitti.google_crypto import CredentialCipher
from chitti.google_oauth import OAuthStateStore
from chitti.google_provider import GOOGLE_SCOPES, GoogleCursorInvalid
from chitti.google_store import (
    account_for_namespace,
    account_summary,
    create_account,
    mark_account_failure,
    recent_messages,
    upcoming_events,
)
from chitti.google_sync import sync_account
from chitti.google_sync_access import sync_grants
from chitti.runner_access import derived_grants

db_test = pytest.mark.skipif(
    not os.getenv("RUN_DB_TESTS"),
    reason="set RUN_DB_TESTS=1 to run PostgreSQL integration tests",
)
REPO_ROOT = Path(__file__).resolve().parents[2]


class _IntegrationDatabase:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    def begin(self):
        return self.engine.begin()


@pytest.fixture
async def google_database():
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql+asyncpg"
        )
        subprocess.run(
            ["python", "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            cwd=REPO_ROOT,
            env={**os.environ, "DATABASE_URL": url},
            check=True,
        )
        engine = create_async_engine(url)
        yield _IntegrationDatabase(engine)
        await engine.dispose()


def _cipher() -> CredentialCipher:
    return CredentialCipher(base64.urlsafe_b64encode(b"k" * 32).decode())


async def _account(database: Any, namespace: str, email: str) -> int:
    async with database.begin() as session:
        return await create_account(
            session,
            namespace,
            email,
            f"subject-{namespace}",
            list(GOOGLE_SCOPES),
            {"web": {"client_id": "client-id", "client_secret": "client-secret"}},
            f"refresh-token-{namespace}",
            _cipher(),
        )


class FakeProvider:
    def __init__(self, calls: list[tuple[str, object]], invalid_gmail: bool = False, invalid_calendar: bool = False) -> None:
        self.calls = calls
        self.invalid_gmail = invalid_gmail
        self.invalid_calendar = invalid_calendar

    def gmail_messages(self, history_id, after, limit):
        self.calls.append(("gmail", history_id))
        if self.invalid_gmail and history_id == "invalid":
            self.invalid_gmail = False
            raise GoogleCursorInvalid("invalid history")
        return "history-2", [
            type(
                "Message",
                (),
                {
                    "message_id": "message-1",
                    "thread_id": "thread-1",
                    "history_id": "history-2",
                    "internal_date": datetime.now(UTC),
                    "sender": "owner@example.com",
                    "recipients": "owner@example.com",
                    "subject": "Subject",
                    "snippet": "Snippet",
                    "body": "Body",
                },
            )()
        ]

    def calendar_events(self, sync_token, time_min, time_max):
        self.calls.append(("calendar", sync_token))
        if self.invalid_calendar and sync_token == "invalid":
            self.invalid_calendar = False
            raise GoogleCursorInvalid("invalid token")
        return "calendar-2", [
            type(
                "Event",
                (),
                {
                    "calendar_id": "primary",
                    "event_id": "event-1",
                    "etag": "etag-1",
                    "summary": "Event",
                    "description": "Description",
                    "start_at": time_min + timedelta(hours=1),
                    "end_at": time_min + timedelta(hours=2),
                    "status": "confirmed",
                    "html_link": "https://calendar.google.com/event-1",
                },
            )()
        ]

    def revoke(self):
        return None

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


@pytest.mark.asyncio
@db_test
async def test_database_credential_row_contains_only_ciphertext(google_database) -> None:
    account_id = await _account(google_database, "jsv-fashion", "jsv@example.com")
    async with google_database.begin() as session:
        row = (
            await session.execute(
                text(
                    "SELECT client_config_ciphertext, refresh_token_ciphertext "
                    "FROM google_oauth_credentials WHERE account_id = :account_id"
                ),
                {"account_id": account_id},
            )
        ).mappings().one()

    stored = " ".join(str(value) for value in row.values())
    assert "refresh-token-jsv-fashion" not in stored
    assert "client-secret" not in stored


@pytest.mark.asyncio
@db_test
async def test_google_reads_are_namespace_scoped(google_database) -> None:
    jsv_account = await _account(google_database, "jsv-fashion", "jsv@example.com")
    andhra_account = await _account(google_database, "andhrawala", "andhra@example.com")
    async with google_database.begin() as session:
        await session.execute(
            text(
                "INSERT INTO google_gmail_messages "
                "(account_id, namespace, gmail_message_id, subject) "
                "VALUES (:account, :namespace, :message, :subject)"
            ),
            {"account": jsv_account, "namespace": "jsv-fashion", "message": "same-id", "subject": "JSV"},
        )
        await session.execute(
            text(
                "INSERT INTO google_calendar_events "
                "(account_id, namespace, calendar_id, event_id, summary, end_at) "
                "VALUES (:account, :namespace, 'primary', 'same-id', :summary, now() + interval '1 day')"
            ),
            {"account": jsv_account, "namespace": "jsv-fashion", "summary": "JSV event"},
        )
        assert await account_for_namespace(session, jsv_account, "andhrawala") is None
        assert [row["subject"] for row in await recent_messages(session, "andhrawala")] == []
        assert [row["summary"] for row in await upcoming_events(session, "andhrawala")] == []
        assert [row["subject"] for row in await recent_messages(session, "jsv-fashion")] == ["JSV"]
        assert [row["summary"] for row in await upcoming_events(session, "jsv-fashion")] == ["JSV event"]
    assert andhra_account != jsv_account


@pytest.mark.asyncio
@db_test
async def test_invalid_cursors_trigger_bounded_full_resync(google_database, monkeypatch) -> None:
    account_id = await _account(google_database, "vsports", "vsports@example.com")
    async with google_database.begin() as session:
        await session.execute(
            text(
                "UPDATE google_sync_state SET gmail_history_id = 'invalid', "
                "calendar_sync_token = 'invalid' WHERE account_id = :account_id"
            ),
            {"account_id": account_id},
        )
        account = dict(
            (
                await session.execute(
                    text("SELECT id, namespace FROM google_provider_accounts WHERE id = :id"),
                    {"id": account_id},
                )
            ).mappings().one()
        )
    calls: list[tuple[str, object]] = []
    fake = FakeProvider(calls, invalid_gmail=True, invalid_calendar=True)
    monkeypatch.setattr(
        "chitti.google_sync.get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "google_credentials_key": base64.urlsafe_b64encode(b"k" * 32).decode(),
                "google_recent_mail_days": 30,
                "google_initial_mail_limit": 10,
                "google_calendar_window_days": 30,
            },
        )(),
    )
    await sync_account(google_database, account, lambda _config, _token: fake)
    assert calls == [("gmail", "invalid"), ("gmail", None), ("calendar", "invalid"), ("calendar", None)]
    async with google_database.begin() as session:
        assert (await recent_messages(session, "vsports"))[0]["gmail_message_id"] == "message-1"
        assert (await upcoming_events(session, "vsports"))[0]["event_id"] == "event-1"


@pytest.mark.asyncio
@db_test
async def test_revoked_token_status_requires_reconnect(google_database) -> None:
    account_id = await _account(google_database, "pj-digi", "pj@example.com")
    async with google_database.begin() as session:
        await mark_account_failure(session, account_id, "pj-digi", "Google token was revoked; reconnect needed")
        summary = await account_summary(session, "pj-digi")
    assert summary is not None
    assert summary["status"] == "error"
    assert "reconnect needed" in str(summary["last_error"])
