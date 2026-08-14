from __future__ import annotations

import base64
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from chitti.google_crypto import CredentialCipher
from chitti.google_email import (
    action_for_namespace,
    create_action,
    list_actions,
    record_approval,
    send_pending_action,
)
from chitti.google_oauth import OAuthStateStore, authorization_url, validate_scopes
from chitti.google_provider import (
    GOOGLE_READ_SCOPES,
    GOOGLE_SCOPES,
    GoogleApiProvider,
    GoogleCursorInvalid,
    GoogleProviderError,
)
from chitti.google_store import (
    account_for_namespace,
    account_summary,
    create_account,
    mark_account_failure,
    recent_messages,
    upcoming_events,
)
from chitti.google_sync import sync_account
from chitti.google_sync_access import reconcile_sync_privileges, sync_grants
from chitti.runner_access import derived_grants, owned_sequences

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


def test_google_contract_is_read_and_send_only() -> None:
    assert GOOGLE_READ_SCOPES == (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    )
    assert GOOGLE_SCOPES == GOOGLE_READ_SCOPES + (
        "https://www.googleapis.com/auth/gmail.send",
    )
    validate_scopes(list(GOOGLE_SCOPES))
    with pytest.raises(ValueError, match="outside"):
        validate_scopes([*GOOGLE_READ_SCOPES, "https://www.googleapis.com/auth/gmail.modify"])


def test_google_authorization_requests_send_scope(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FlowStub:
        redirect_uri = ""

        @classmethod
        def from_client_config(cls, _config, scopes, state=None):
            seen["scopes"] = scopes
            seen["state"] = state
            return cls()

        def authorization_url(self, **_kwargs):
            return "https://example.test/oauth", None

    monkeypatch.setattr("chitti.google_oauth.Flow", FlowStub)
    authorization_url(
        SimpleNamespace(
            google_client_id="id",
            google_client_secret="secret",
            google_oauth_redirect_uri="https://example.test/callback",
        ),
        "state",
    )
    assert seen["scopes"] == list(GOOGLE_SCOPES)


def test_google_provider_builds_exact_mime_payload() -> None:
    captured: dict[str, object] = {}

    class SendRequest:
        def execute(self):
            return {"id": "provider-id"}

    class Messages:
        def send(self, **kwargs):
            captured.update(kwargs)
            return SendRequest()

    class Users:
        def messages(self):
            return Messages()

    provider = object.__new__(GoogleApiProvider)
    provider.gmail = SimpleNamespace(users=lambda: Users())
    action = {
        "to_recipients": ["to@example.com"],
        "cc_recipients": ["cc@example.com"],
        "bcc_recipients": ["bcc@example.com"],
        "subject": "Exact subject",
        "body": "Exact body",
        "attachments": [],
    }
    assert provider.send_email(action) == "provider-id"
    raw = base64.urlsafe_b64decode(str(captured["body"]["raw"]) + "===")
    assert b"To: to@example.com" in raw
    assert b"Cc: cc@example.com" in raw
    assert b"Bcc: bcc@example.com" in raw
    assert b"Subject: Exact subject" in raw
    assert b"Exact body" in raw


def test_sync_grants_are_limited_to_google_sync_tables() -> None:
    grants = sync_grants(
        {
            "google_provider_accounts",
            "google_oauth_credentials",
            "google_sync_state",
            "google_gmail_messages",
            "google_calendar_events",
            "runner_health",
            "google_email_actions",
            "google_email_action_approvals",
        }
    )

    assert set(grants) <= {
        "google_provider_accounts",
        "google_oauth_credentials",
        "google_sync_state",
        "google_gmail_messages",
        "google_calendar_events",
        "runner_health",
        "google_email_actions",
        "google_email_action_approvals",
    }


@pytest.mark.asyncio
@db_test
async def test_sync_grants_apply_tables_and_owned_sequences(google_database) -> None:
    database_url = google_database.engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    parsed = urlsplit(database_url)
    admin = await asyncpg.connect(database_url)
    role = f"google_sync_{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex
    try:
        await admin.execute(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\'')
        await admin.execute(
            f'GRANT CONNECT ON DATABASE "{parsed.path.lstrip("/")}" TO "{role}"'
        )
        await admin.execute('GRANT USAGE ON SCHEMA public TO "' + role + '"')
        await reconcile_sync_privileges(admin, role)

        role_url = urlunsplit(
            (
                parsed.scheme,
                f"{role}:{password}@{parsed.hostname}:{parsed.port}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
        connection = await asyncpg.connect(role_url)
        try:
            known_tables = {
                str(row["table_name"])
                for row in await admin.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            }
            grants = sync_grants(known_tables)
            assert grants
            sequence_grants = 0
            for table, privileges in grants.items():
                for privilege in privileges:
                    assert await connection.fetchval(
                        "SELECT has_table_privilege($1, $2, $3)",
                        role,
                        table,
                        privilege,
                    )
                if "INSERT" in privileges:
                    sequences = await owned_sequences(admin, table)
                    for sequence in sequences:
                        sequence_grants += 1
                        assert await connection.fetchval(
                            "SELECT has_sequence_privilege($1, $2, 'USAGE')",
                            role,
                            sequence,
                        )
            assert sequence_grants
            for table in ("brand_profiles", "decisions"):
                assert not await connection.fetchval(
                    "SELECT has_table_privilege($1, $2, 'SELECT')",
                    role,
                    table,
                )
        finally:
            await connection.close()
    finally:
        await admin.execute(f'DROP OWNED BY "{role}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


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


@pytest.mark.asyncio
@db_test
async def test_email_action_approval_sends_once_and_records_provider_id(google_database) -> None:
    account_id = await _account(google_database, "jsv-fashion", "jsv@example.com")
    async with google_database.begin() as session:
        action_id = await create_action(
            session,
            namespace="jsv-fashion",
            account_id=account_id,
            to_recipients=["recipient@example.com"],
            cc_recipients=[],
            bcc_recipients=[],
            subject="Exact subject",
            body="Exact body",
            attachments=[],
            requested_by="owner",
        )
        action = await action_for_namespace(session, action_id, "jsv-fashion")
        assert action is not None
        await record_approval(
            session,
            action,
            decision="approved",
            reason=None,
            approved_by="owner",
        )
    calls: list[dict[str, Any]] = []

    class SendProvider:
        def send_email(self, action: dict[str, Any]) -> str:
            calls.append(action)
            return "provider-message-1"

    account = {
        "id": account_id,
        "namespace": "jsv-fashion",
        "scopes": list(GOOGLE_SCOPES),
    }
    await send_pending_action(google_database, account, SendProvider(), action_id)
    with pytest.raises(GoogleProviderError, match="already executed"):
        await send_pending_action(google_database, account, SendProvider(), action_id)
    assert len(calls) == 1
    async with google_database.begin() as session:
        row = (
            await session.execute(
                text(
                    "SELECT state, provider_message_id FROM google_email_actions WHERE id = :id"
                ),
                {"id": action_id},
            )
        ).one()
    assert row == ("sent", "provider-message-1")


@pytest.mark.asyncio
@db_test
async def test_email_action_requires_approval_and_rejects_tampering(google_database) -> None:
    account_id = await _account(google_database, "andhrawala", "andhra@example.com")
    async with google_database.begin() as session:
        action_id = await create_action(
            session,
            namespace="andhrawala",
            account_id=account_id,
            to_recipients=["recipient@example.com"],
            cc_recipients=[],
            bcc_recipients=[],
            subject="Subject",
            body="Body",
            attachments=[],
            requested_by="owner",
        )
    account = {"id": account_id, "namespace": "andhrawala", "scopes": list(GOOGLE_SCOPES)}

    class SendProvider:
        def send_email(self, action: dict[str, Any]) -> str:
            raise AssertionError("unapproved action was sent")

    with pytest.raises(GoogleProviderError, match="unapproved"):
        await send_pending_action(google_database, account, SendProvider(), action_id)
    async with google_database.begin() as session:
        action = await action_for_namespace(session, action_id, "andhrawala")
        assert action is not None
        await record_approval(
            session,
            action,
            decision="approved",
            reason=None,
            approved_by="owner",
        )
        await session.execute(
            text(
                "ALTER TABLE google_email_actions "
                "DISABLE TRIGGER reject_google_email_action_mutation_trigger"
            )
        )
        await session.execute(
            text("UPDATE google_email_actions SET body = 'tampered' WHERE id = :id"),
            {"id": action_id},
        )
        await session.execute(text("ALTER TABLE google_email_actions ENABLE TRIGGER reject_google_email_action_mutation_trigger"))
    with pytest.raises(GoogleProviderError, match="content hash mismatch"):
        await send_pending_action(google_database, account, SendProvider(), action_id)


@pytest.mark.asyncio
@db_test
async def test_email_action_rejects_attachments_before_creation(google_database) -> None:
    account_id = await _account(google_database, "general", "owner@example.com")
    async with google_database.begin() as session:
        with pytest.raises(ValueError, match="trusted attachment bytes"):
            await create_action(
                session,
                namespace="general",
                account_id=account_id,
                to_recipients=["recipient@example.com"],
                cc_recipients=[],
                bcc_recipients=[],
                subject="Subject",
                body="Body",
                attachments=[{"name": "brief.txt", "size": 12, "sha256": "a" * 64}],
                requested_by="owner",
            )


@pytest.mark.asyncio
@db_test
async def test_email_approval_expiry_and_namespace_isolation(google_database) -> None:
    first = await _account(google_database, "pj-digi", "pj@example.com")
    second = await _account(google_database, "vsports", "vsports@example.com")
    async with google_database.begin() as session:
        action_id = await create_action(
            session,
            namespace="pj-digi",
            account_id=first,
            to_recipients=["recipient@example.com"],
            cc_recipients=[],
            bcc_recipients=[],
            subject="Subject",
            body="Body",
            attachments=[],
            requested_by="owner",
        )
        action = await action_for_namespace(session, action_id, "pj-digi")
        assert action is not None
        await record_approval(
            session,
            action,
            decision="approved",
            reason=None,
            approved_by="owner",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        assert await action_for_namespace(session, action_id, "vsports") is None
        assert len(await list_actions(session, "pj-digi")) == 1
        assert await list_actions(session, "vsports") == []
    account = {"id": first, "namespace": "pj-digi", "scopes": list(GOOGLE_SCOPES)}
    with pytest.raises(GoogleProviderError, match="expired"):
        await send_pending_action(google_database, account, object(), action_id)
    assert second != first


@pytest.mark.asyncio
@db_test
async def test_missing_send_scope_requires_reconnect(google_database) -> None:
    account_id = await _account(google_database, "general", "owner@example.com")
    async with google_database.begin() as session:
        action_id = await create_action(
            session,
            namespace="general",
            account_id=account_id,
            to_recipients=["recipient@example.com"],
            cc_recipients=[],
            bcc_recipients=[],
            subject="Subject",
            body="Body",
            attachments=[],
            requested_by="owner",
        )
    account = {"id": account_id, "namespace": "general", "scopes": list(GOOGLE_READ_SCOPES)}
    with pytest.raises(GoogleProviderError, match="reconnect needed"):
        await send_pending_action(google_database, account, object(), action_id)


@pytest.mark.asyncio
@db_test
async def test_old_connection_is_visible_as_reconnect_needed(google_database) -> None:
    account_id = await _account(google_database, "general", "owner@example.com")
    async with google_database.begin() as session:
        await session.execute(
            text(
                "UPDATE google_provider_accounts SET scopes = CAST(:scopes AS jsonb) "
                "WHERE id = :id"
            ),
            {"scopes": json.dumps(list(GOOGLE_READ_SCOPES)), "id": account_id},
        )
        summary = await account_summary(session, "general")
    assert summary is not None
    assert summary["status"] == "reconnect_needed"
    assert "predates Gmail send permission" in str(summary["reconnect_reason"])
