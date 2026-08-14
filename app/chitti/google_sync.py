from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from .db import Database
from .google_crypto import CredentialCipher, CredentialError
from .google_provider import (
    GoogleApiProvider,
    GoogleCursorInvalid,
    GoogleProviderError,
    GoogleReadProvider,
)
from .google_store import credential_for_account, mark_account_failure, mark_account_synced
from .runner_access import sync_sql
from .settings import get_settings

logger = logging.getLogger(__name__)


async def sync_account(
    database: Database,
    account: dict[str, Any],
    provider_factory: Callable[[dict[str, Any], str], GoogleReadProvider] = GoogleApiProvider,
) -> None:
    settings = get_settings()
    account_id = int(account["id"])
    namespace = str(account["namespace"])
    async with database.sessions() as session:
        credential = await credential_for_account(session, account_id, namespace)
        if credential is None:
            return
        try:
            cipher = CredentialCipher(settings.google_credentials_key)
            try:
                client_config = json.loads(
                    cipher.decrypt(str(credential["client_config_ciphertext"]))
                )
                refresh_token = cipher.decrypt(
                    str(credential["refresh_token_ciphertext"])
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CredentialError("Stored Google credentials are invalid") from exc
            provider = provider_factory(client_config, refresh_token)
            await _sync_gmail(
                database,
                account,
                provider,
                settings.google_recent_mail_days,
                settings.google_initial_mail_limit,
            )
            await _sync_calendar(
                database, account, provider, settings.google_calendar_window_days
            )
            await mark_account_synced(session, account_id, namespace)
            await session.execute(
                sync_sql(text(
                    "INSERT INTO runner_health "
                    "(component, status, detail, first_failed_at, last_failed_at, "
                    "consecutive_failures, resolved_at, last_succeeded_at) "
                    "VALUES ('google_sync', 'healthy', 'Google sync completed', "
                    "now(), now(), 0, now(), now()) "
                    "ON CONFLICT (component) DO UPDATE SET status = 'healthy', "
                    "detail = EXCLUDED.detail, consecutive_failures = 0, "
                    "resolved_at = now(), last_succeeded_at = now()"
                ))
            )
            await session.commit()
        except (GoogleProviderError, CredentialError) as exc:
            await mark_account_failure(session, account_id, namespace, str(exc))
            await session.execute(
                sync_sql(text(
                    "INSERT INTO runner_health "
                    "(component, status, detail, first_failed_at, last_failed_at, "
                    "consecutive_failures, resolved_at, last_succeeded_at) "
                    "VALUES ('google_sync', 'failed', :detail, now(), now(), 1, NULL, NULL) "
                    "ON CONFLICT (component) DO UPDATE SET status = 'failed', "
                    "detail = EXCLUDED.detail, last_failed_at = now(), "
                    "consecutive_failures = runner_health.consecutive_failures + 1, "
                    "resolved_at = NULL"
                )),
                {"detail": str(exc)[:2000]},
            )
            await session.commit()


async def _sync_gmail(
    database: Database,
    account: dict[str, Any],
    provider: GoogleReadProvider,
    recent_days: int,
    initial_limit: int,
) -> None:
    account_id = int(account["id"])
    namespace = str(account["namespace"])
    async with database.sessions() as session:
        result = await session.execute(
            sync_sql(text(
                "SELECT gmail_history_id, gmail_full_sync_after FROM google_sync_state "
                "WHERE account_id = :account_id AND namespace = :namespace"
            )),
            {"account_id": account_id, "namespace": namespace},
        )
        state = result.mappings().one()
    history_id = state["gmail_history_id"]
    after = state["gmail_full_sync_after"]
    if history_id is None and after is None:
        after = datetime.now(UTC) - timedelta(days=recent_days)
    try:
        newest, messages = await asyncio.to_thread(
            provider.gmail_messages, history_id, after, initial_limit
        )
    except GoogleCursorInvalid:
        async with database.sessions() as session:
            await session.execute(
                sync_sql(text(
                    "UPDATE google_sync_state SET gmail_history_id = NULL, "
                    "gmail_full_sync_after = :after, last_error = :detail "
                    "WHERE account_id = :account_id AND namespace = :namespace"
                )),
                {
                    "after": datetime.now(UTC) - timedelta(days=recent_days),
                    "detail": "Gmail history cursor invalid; full recent-mail resync scheduled",
                    "account_id": account_id,
                    "namespace": namespace,
                },
            )
            await session.commit()
        newest, messages = await asyncio.to_thread(
            provider.gmail_messages,
            None,
            datetime.now(UTC) - timedelta(days=recent_days),
            initial_limit,
        )
    async with database.sessions() as session:
        for message in messages:
            await session.execute(
                sync_sql(text(
                    "INSERT INTO google_gmail_messages "
                    "(account_id, namespace, gmail_message_id, thread_id, history_id, "
                    "internal_date, sender, recipients, subject, snippet, body) "
                    "VALUES (:account_id, :namespace, :message_id, :thread_id, :history_id, "
                    ":internal_date, :sender, :recipients, :subject, :snippet, :body) "
                    "ON CONFLICT (account_id, gmail_message_id) DO UPDATE SET "
                    "thread_id = EXCLUDED.thread_id, history_id = EXCLUDED.history_id, "
                    "internal_date = EXCLUDED.internal_date, sender = EXCLUDED.sender, "
                    "recipients = EXCLUDED.recipients, subject = EXCLUDED.subject, "
                    "snippet = EXCLUDED.snippet, body = EXCLUDED.body"
                )),
                {
                    "account_id": account_id,
                    "namespace": namespace,
                    "message_id": message.message_id,
                    "thread_id": message.thread_id,
                    "history_id": message.history_id,
                    "internal_date": message.internal_date,
                    "sender": message.sender,
                    "recipients": message.recipients,
                    "subject": message.subject,
                    "snippet": message.snippet,
                    "body": message.body,
                },
            )
        await session.execute(
            sync_sql(text(
                "UPDATE google_sync_state SET gmail_history_id = :history_id, "
                "gmail_full_sync_after = NULL, last_sync_finished_at = now(), last_error = NULL "
                "WHERE account_id = :account_id AND namespace = :namespace"
            )),
            {"history_id": newest, "account_id": account_id, "namespace": namespace},
        )
        await session.commit()


async def _sync_calendar(
    database: Database, account: dict[str, Any], provider: GoogleReadProvider, window_days: int
) -> None:
    account_id = int(account["id"])
    namespace = str(account["namespace"])
    async with database.sessions() as session:
        result = await session.execute(
            sync_sql(text(
                "SELECT calendar_sync_token FROM google_sync_state "
                "WHERE account_id = :account_id AND namespace = :namespace"
            )),
            {"account_id": account_id, "namespace": namespace},
        )
        token = result.scalar_one()
    try:
        next_token, events = await asyncio.to_thread(
            provider.calendar_events,
            token,
            datetime.now(UTC),
            datetime.now(UTC) + timedelta(days=window_days),
        )
    except GoogleCursorInvalid:
        next_token, events = await asyncio.to_thread(
            provider.calendar_events,
            None,
            datetime.now(UTC),
            datetime.now(UTC) + timedelta(days=window_days),
        )
    async with database.sessions() as session:
        for event in events:
            await session.execute(
                sync_sql(text(
                    "INSERT INTO google_calendar_events "
                    "(account_id, namespace, calendar_id, event_id, etag, summary, description, "
                    "start_at, end_at, status, html_link) VALUES "
                    "(:account_id, :namespace, :calendar_id, :event_id, :etag, :summary, "
                    ":description, :start_at, :end_at, :status, :html_link) "
                    "ON CONFLICT (account_id, calendar_id, event_id) DO UPDATE SET "
                    "etag = EXCLUDED.etag, summary = EXCLUDED.summary, description = EXCLUDED.description, "
                    "start_at = EXCLUDED.start_at, end_at = EXCLUDED.end_at, status = EXCLUDED.status, "
                    "html_link = EXCLUDED.html_link"
                )),
                {
                    "account_id": account_id,
                    "namespace": namespace,
                    "calendar_id": event.calendar_id,
                    "event_id": event.event_id,
                    "etag": event.etag,
                    "summary": event.summary,
                    "description": event.description,
                    "start_at": event.start_at,
                    "end_at": event.end_at,
                    "status": event.status,
                    "html_link": event.html_link,
                },
            )
        await session.execute(
            sync_sql(text(
                "UPDATE google_sync_state SET calendar_sync_token = :token "
                "WHERE account_id = :account_id AND namespace = :namespace"
            )),
            {"token": next_token, "account_id": account_id, "namespace": namespace},
        )
        await session.commit()


async def sync_forever(database: Database) -> None:
    settings = get_settings()
    while True:
        async with database.sessions() as session:
            result = await session.execute(
                sync_sql(text(
                    "SELECT id, namespace FROM google_provider_accounts "
                    "WHERE status IN ('connected', 'error') ORDER BY id"
                ))
            )
            accounts = [dict(row) for row in result.mappings()]
        for account in accounts:
            await sync_account(database, account)
        await asyncio.sleep(settings.google_sync_interval_seconds)


async def main() -> None:
    database = Database(get_settings())
    try:
        await sync_forever(database)
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
