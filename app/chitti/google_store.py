from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .google_crypto import CredentialCipher
from .google_provider import GOOGLE_SEND_SCOPE
from .memory import normalize_namespace
from .runner_access import application_only_sql, sync_sql


async def account_for_namespace(
    session: AsyncSession, account_id: int, namespace: str
) -> dict[str, Any] | None:
    result = await session.execute(
        application_only_sql(text(
            "SELECT id, namespace, google_email, google_subject, scopes, status, "
            "last_synced_at, last_error FROM google_provider_accounts "
            "WHERE id = :id AND namespace = :namespace"
        )),
        {"id": account_id, "namespace": normalize_namespace(namespace)},
    )
    row = result.mappings().one_or_none()
    if not row:
        return None
    account = dict(row)
    if account["status"] == "connected" and GOOGLE_SEND_SCOPE not in {
        str(scope) for scope in account["scopes"]
    }:
        account["status"] = "reconnect_needed"
        account["reconnect_reason"] = (
            "Reconnect required: this Google connection predates Gmail send permission."
        )
    return account


async def account_summary(
    session: AsyncSession, namespace: str
) -> dict[str, Any] | None:
    result = await session.execute(
        application_only_sql(text(
            "SELECT id, namespace, google_email, google_subject, scopes, status, "
            "last_synced_at, last_error FROM google_provider_accounts "
            "WHERE namespace = :namespace ORDER BY id DESC LIMIT 1"
        )),
        {"namespace": normalize_namespace(namespace)},
    )
    row = result.mappings().one_or_none()
    if not row:
        return None
    account = dict(row)
    if account["status"] == "connected" and GOOGLE_SEND_SCOPE not in {
        str(scope) for scope in account["scopes"]
    }:
        account["status"] = "reconnect_needed"
        account["reconnect_reason"] = (
            "Reconnect required: this Google connection predates Gmail send permission."
        )
    return account


async def create_account(
    session: AsyncSession,
    namespace: str,
    google_email: str,
    google_subject: str | None,
    scopes: list[str],
    client_config: dict[str, Any],
    refresh_token: str,
    cipher: CredentialCipher,
) -> int:
    namespace = normalize_namespace(namespace)
    result = await session.execute(
        application_only_sql(text(
            "INSERT INTO google_provider_accounts "
            "(namespace, google_email, google_subject, scopes, status) "
            "VALUES (:namespace, :email, :subject, CAST(:scopes AS jsonb), 'connected') "
            "ON CONFLICT (namespace, google_email) DO UPDATE SET "
            "google_subject = EXCLUDED.google_subject, scopes = EXCLUDED.scopes, "
            "status = 'connected', last_error = NULL, updated_at = now() "
            "RETURNING id"
        )),
        {
            "namespace": namespace,
            "email": google_email,
            "subject": google_subject,
            "scopes": json.dumps(scopes),
        },
    )
    account_id = int(result.scalar_one())
    await session.execute(
        application_only_sql(text(
            "INSERT INTO google_oauth_credentials "
            "(account_id, client_config_ciphertext, refresh_token_ciphertext, key_version) "
            "VALUES (:account_id, :client_config, :refresh_token, :key_version) "
            "ON CONFLICT (account_id) DO UPDATE SET "
            "client_config_ciphertext = EXCLUDED.client_config_ciphertext, "
            "refresh_token_ciphertext = EXCLUDED.refresh_token_ciphertext, "
            "key_version = EXCLUDED.key_version, updated_at = now()"
        )),
        {
            "account_id": account_id,
            "client_config": cipher.encrypt(client_config),
            "refresh_token": cipher.encrypt(refresh_token),
            "key_version": cipher.version,
        },
    )
    await session.execute(
        application_only_sql(text(
            "INSERT INTO google_sync_state (account_id, namespace) VALUES (:account_id, :namespace) "
            "ON CONFLICT (account_id) DO UPDATE SET namespace = EXCLUDED.namespace"
        )),
        {"account_id": account_id, "namespace": namespace},
    )
    return account_id


async def credential_for_account(
    session: AsyncSession, account_id: int, namespace: str
) -> dict[str, Any] | None:
    result = await session.execute(
        application_only_sql(text(
            "SELECT c.client_config_ciphertext, c.refresh_token_ciphertext, c.key_version "
            "FROM google_oauth_credentials c "
            "JOIN google_provider_accounts a ON a.id = c.account_id "
            "WHERE c.account_id = :account_id AND a.namespace = :namespace"
        )),
        {"account_id": account_id, "namespace": normalize_namespace(namespace)},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row else None


async def sync_credential_for_account(
    session: AsyncSession, account_id: int, namespace: str
) -> dict[str, Any] | None:
    result = await session.execute(
        sync_sql(
            text(
                "SELECT c.client_config_ciphertext, c.refresh_token_ciphertext, c.key_version "
                "FROM google_oauth_credentials c JOIN google_provider_accounts a "
                "ON a.id = c.account_id WHERE c.account_id = :account_id "
                "AND a.namespace = :namespace"
            )
        ),
        {"account_id": account_id, "namespace": normalize_namespace(namespace)},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row else None


async def mark_account_failure(
    session: AsyncSession, account_id: int, namespace: str, detail: str
) -> None:
    await session.execute(
        application_only_sql(text(
            "UPDATE google_provider_accounts SET status = 'error', last_error = :detail, "
            "updated_at = now() WHERE id = :id AND namespace = :namespace"
        )),
        {"id": account_id, "namespace": normalize_namespace(namespace), "detail": detail[:2000]},
    )


async def mark_account_synced(
    session: AsyncSession, account_id: int, namespace: str
) -> None:
    await session.execute(
        application_only_sql(text(
            "UPDATE google_provider_accounts SET status = 'connected', last_error = NULL, "
            "last_synced_at = now(), updated_at = now() "
            "WHERE id = :id AND namespace = :namespace"
        )),
        {"id": account_id, "namespace": normalize_namespace(namespace)},
    )


async def sync_mark_account_failure(
    session: AsyncSession, account_id: int, namespace: str, detail: str
) -> None:
    await session.execute(
        sync_sql(
            text(
                "UPDATE google_provider_accounts SET status = 'error', last_error = :detail, "
                "updated_at = now() WHERE id = :id AND namespace = :namespace"
            )
        ),
        {"id": account_id, "namespace": normalize_namespace(namespace), "detail": detail[:2000]},
    )


async def sync_mark_account_synced(
    session: AsyncSession, account_id: int, namespace: str
) -> None:
    await session.execute(
        sync_sql(
            text(
                "UPDATE google_provider_accounts SET status = 'connected', last_error = NULL, "
                "last_synced_at = now(), updated_at = now() "
                "WHERE id = :id AND namespace = :namespace"
            )
        ),
        {"id": account_id, "namespace": normalize_namespace(namespace)},
    )


async def recent_messages(
    session: AsyncSession, namespace: str, limit: int = 20
) -> list[dict[str, Any]]:
    result = await session.execute(
        application_only_sql(text(
            "SELECT id, account_id, gmail_message_id, thread_id, sender, recipients, "
            "subject, snippet, internal_date FROM google_gmail_messages "
            "WHERE namespace = :namespace ORDER BY internal_date DESC NULLS LAST, id DESC "
            "LIMIT :limit"
        )),
        {"namespace": normalize_namespace(namespace), "limit": limit},
    )
    return [dict(row) for row in result.mappings()]


async def upcoming_events(
    session: AsyncSession, namespace: str, limit: int = 20
) -> list[dict[str, Any]]:
    result = await session.execute(
        application_only_sql(text(
            "SELECT id, account_id, calendar_id, event_id, summary, description, "
            "start_at, end_at, status, html_link FROM google_calendar_events "
            "WHERE namespace = :namespace AND end_at >= now() "
            "ORDER BY start_at ASC NULLS LAST, id ASC LIMIT :limit"
        )),
        {"namespace": normalize_namespace(namespace), "limit": limit},
    )
    return [dict(row) for row in result.mappings()]


async def disconnect_account(
    session: AsyncSession, account_id: int, namespace: str, actor: str
) -> None:
    namespace = normalize_namespace(namespace)
    await session.execute(
        application_only_sql(text(
            "INSERT INTO google_account_audit "
            "(account_id, namespace, action, actor, detail) "
            "VALUES (:account_id, :namespace, 'disconnected', :actor, "
            "'credential revoked and deleted')"
        )),
        {"account_id": account_id, "namespace": namespace, "actor": actor},
    )
    await session.execute(
        application_only_sql(text(
            "DELETE FROM google_provider_accounts WHERE id = :id AND namespace = :namespace"
        )),
        {"id": account_id, "namespace": namespace},
    )
