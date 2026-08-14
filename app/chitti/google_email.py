from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .google_provider import GOOGLE_SEND_SCOPE, GoogleProviderError
from .runner_access import application_only_sql, sync_sql

ACTION_TYPE = "gmail.send"
EMAIL_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _recipients(values: list[str]) -> list[str]:
    result = [value.strip() for value in values if value.strip()]
    if not result or any(not EMAIL_ADDRESS.fullmatch(value) for value in result):
        raise ValueError("every recipient must be a valid email address")
    return result


def parse_recipient_field(value: str) -> list[str]:
    parsed = [address for _, address in getaddresses([value.replace(";", ",")])]
    return _recipients(parsed)


def canonical_action(
    *,
    to_recipients: list[str],
    cc_recipients: list[str],
    bcc_recipients: list[str],
    subject: str,
    body: str,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "action_type": ACTION_TYPE,
        "to_recipients": _recipients(to_recipients),
        "cc_recipients": _recipients(cc_recipients) if cc_recipients else [],
        "bcc_recipients": _recipients(bcc_recipients) if bcc_recipients else [],
        "subject": subject,
        "body": body,
        "attachments": attachments,
    }


def content_hash(action: dict[str, Any]) -> str:
    encoded = json.dumps(action, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _action_mapping(row: Any) -> dict[str, Any]:
    action = dict(row)
    for key in ("to_recipients", "cc_recipients", "bcc_recipients", "attachments"):
        if isinstance(action[key], str):
            action[key] = json.loads(action[key])
    return action


async def create_action(
    session: AsyncSession,
    *,
    namespace: str,
    account_id: int,
    to_recipients: list[str],
    cc_recipients: list[str],
    bcc_recipients: list[str],
    subject: str,
    body: str,
    attachments: list[dict[str, Any]],
    requested_by: str,
    expires_at: datetime | None = None,
) -> int:
    action = canonical_action(
        to_recipients=to_recipients,
        cc_recipients=cc_recipients,
        bcc_recipients=bcc_recipients,
        subject=subject,
        body=body,
        attachments=attachments,
    )
    expires_at = expires_at or datetime.now(UTC) + timedelta(hours=24)
    result = await session.execute(
        application_only_sql(
            text(
                "INSERT INTO google_email_actions "
                "(namespace, account_id, action_type, to_recipients, cc_recipients, "
                "bcc_recipients, subject, body, attachments, content_hash, requested_by, "
                "expires_at, idempotency_key) VALUES "
                "(:namespace, :account_id, :action_type, CAST(:to AS jsonb), "
                "CAST(:cc AS jsonb), CAST(:bcc AS jsonb), :subject, :body, "
                "CAST(:attachments AS jsonb), :content_hash, :requested_by, "
                ":expires_at, :idempotency_key) RETURNING id"
            )
        ),
        {
            "namespace": namespace,
            "account_id": account_id,
            "action_type": ACTION_TYPE,
            "to": json.dumps(action["to_recipients"]),
            "cc": json.dumps(action["cc_recipients"]),
            "bcc": json.dumps(action["bcc_recipients"]),
            "subject": subject,
            "body": body,
            "attachments": json.dumps(attachments, sort_keys=True),
            "content_hash": content_hash(action),
            "requested_by": requested_by,
            "expires_at": expires_at,
            "idempotency_key": secrets.token_urlsafe(24),
        },
    )
    return int(result.scalar_one())


async def list_actions(session: AsyncSession, namespace: str) -> list[dict[str, Any]]:
    result = await session.execute(
        application_only_sql(
            text(
                "SELECT a.*, p.decision, p.reason AS approval_reason, p.approved_by, "
                "p.expires_at AS approval_expires_at "
                "FROM google_email_actions a LEFT JOIN LATERAL ("
                "SELECT decision, reason, approved_by, expires_at FROM google_email_action_approvals "
                "WHERE action_id = a.id ORDER BY id DESC LIMIT 1) p ON TRUE "
                "WHERE a.namespace = :namespace ORDER BY a.id DESC LIMIT 50"
            )
        ),
        {"namespace": namespace},
    )
    return [_action_mapping(row) for row in result.mappings()]


async def action_for_namespace(
    session: AsyncSession, action_id: int, namespace: str
) -> dict[str, Any] | None:
    result = await session.execute(
        application_only_sql(
            text(
                "SELECT a.*, p.decision, p.reason AS approval_reason, p.approved_by, "
                "p.content_hash AS approval_content_hash, p.expires_at AS approval_expires_at "
                "FROM google_email_actions a LEFT JOIN LATERAL ("
                "SELECT decision, reason, approved_by, content_hash, expires_at "
                "FROM google_email_action_approvals WHERE action_id = a.id ORDER BY id DESC LIMIT 1) p ON TRUE "
                "WHERE a.id = :id AND a.namespace = :namespace"
            )
        ),
        {"id": action_id, "namespace": namespace},
    )
    row = result.mappings().one_or_none()
    return _action_mapping(row) if row else None


async def record_approval(
    session: AsyncSession,
    action: dict[str, Any],
    *,
    decision: str,
    reason: str | None,
    approved_by: str,
    expires_at: datetime | None = None,
) -> None:
    if decision not in {"approved", "rejected"}:
        raise ValueError("invalid email action decision")
    current = canonical_action(
        to_recipients=list(action["to_recipients"]),
        cc_recipients=list(action["cc_recipients"]),
        bcc_recipients=list(action["bcc_recipients"]),
        subject=str(action["subject"]),
        body=str(action["body"]),
        attachments=list(action["attachments"]),
    )
    if content_hash(current) != str(action["content_hash"]):
        raise ValueError("email action content hash mismatch")
    if action["state"] != "pending":
        raise ValueError("email action is no longer pending")
    existing = await session.execute(
        application_only_sql(
            text(
                "SELECT 1 FROM google_email_action_approvals "
                "WHERE action_id = :action_id LIMIT 1"
            )
        ),
        {"action_id": action["id"]},
    )
    if existing.first() is not None:
        raise ValueError("email action already has an owner decision")
    await session.execute(
        application_only_sql(
            text(
                "INSERT INTO google_email_action_approvals "
                "(action_id, content_hash, decision, reason, approved_by, expires_at) "
                "VALUES (:action_id, :content_hash, :decision, :reason, :approved_by, :expires_at)"
            )
        ),
        {
            "action_id": action["id"],
            "content_hash": action["content_hash"],
            "decision": decision,
            "reason": reason,
            "approved_by": approved_by,
            "expires_at": expires_at or action["expires_at"],
        },
    )
    if decision == "rejected":
        await session.execute(
            application_only_sql(
                text(
                    "UPDATE google_email_actions SET state = 'rejected', execution_state = 'rejected' "
                    "WHERE id = :id AND state = 'pending'"
                )
            ),
            {"id": action["id"]},
        )


async def claim_action(
    session: AsyncSession, action_id: int, namespace: str, account_id: int
) -> dict[str, Any]:
    result = await session.execute(
        sync_sql(
            text(
                "SELECT a.*, p.decision, p.content_hash AS approval_content_hash, "
                "p.expires_at AS approval_expires_at FROM google_email_actions a "
                "LEFT JOIN LATERAL (SELECT decision, content_hash, expires_at "
                "FROM google_email_action_approvals WHERE action_id = a.id "
                "ORDER BY id DESC LIMIT 1) p ON TRUE "
                "WHERE a.id = :id AND a.namespace = :namespace AND a.account_id = :account_id"
            )
        ),
        {"id": action_id, "namespace": namespace, "account_id": account_id},
    )
    current_row = result.mappings().one_or_none()
    if current_row is None:
        raise GoogleProviderError("email action is not found in this namespace")
    current = _action_mapping(current_row)
    current_hash = content_hash(
        canonical_action(
            to_recipients=list(current["to_recipients"]),
            cc_recipients=list(current["cc_recipients"]),
            bcc_recipients=list(current["bcc_recipients"]),
            subject=str(current["subject"]),
            body=str(current["body"]),
            attachments=list(current["attachments"]),
        )
    )
    if current_hash != str(current["content_hash"]):
        raise GoogleProviderError("email action content hash mismatch")
    result = await session.execute(
        sync_sql(
            text(
                "UPDATE google_email_actions a SET state = 'sending', execution_state = 'claimed' "
                "WHERE a.id = :id AND a.namespace = :namespace AND a.account_id = :account_id "
                "AND a.state = 'pending' "
                "AND a.expires_at > now() AND EXISTS ("
                "SELECT 1 FROM google_email_action_approvals p WHERE p.action_id = a.id "
                "AND p.decision = 'approved' AND p.content_hash = a.content_hash "
                "AND p.expires_at > now()) RETURNING a.*"
            )
        ),
        {"id": action_id, "namespace": namespace, "account_id": account_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise GoogleProviderError("email action is unapproved, expired, changed, or already executed")
    return _action_mapping(row)


async def pending_actions(
    session: AsyncSession, account_id: int, namespace: str
) -> list[int]:
    result = await session.execute(
        sync_sql(
            text(
                "SELECT id FROM google_email_actions "
                "WHERE account_id = :account_id AND namespace = :namespace "
                "AND state = 'pending' ORDER BY id"
            )
        ),
        {"account_id": account_id, "namespace": namespace},
    )
    return [int(row[0]) for row in result]


async def record_execution(
    session: AsyncSession,
    action_id: int,
    *,
    message_id: str | None,
    failure: str | None,
) -> None:
    await session.execute(
        sync_sql(
            text(
                "UPDATE google_email_actions SET state = :state, execution_state = :execution_state, "
                "provider_message_id = :message_id, failure_detail = :failure "
                "WHERE id = :id AND state = 'sending'"
            )
        ),
        {
            "id": action_id,
            "state": "sent" if message_id else "failed",
            "execution_state": "succeeded" if message_id else "failed",
            "message_id": message_id,
            "failure": failure,
        },
    )


def has_send_scope(scopes: Any) -> bool:
    return GOOGLE_SEND_SCOPE in {str(scope) for scope in scopes}


async def send_pending_action(
    database: Any,
    account: dict[str, Any],
    provider: Any,
    action_id: int,
) -> None:
    namespace = str(account["namespace"])
    if not has_send_scope(account.get("scopes", [])):
        raise GoogleProviderError("reconnect needed: Gmail send scope is missing")
    async with database.sessions() as session:
        # Gmail users.messages.send has no provider idempotency-key contract. The durable
        # sending fence prevents automatic duplicate attempts; a crash after acceptance
        # and before recording leaves the result unknown and is never retried automatically.
        action = await claim_action(session, action_id, namespace, int(account["id"]))
        await session.commit()
    try:
        message_id = await asyncio.to_thread(provider.send_email, action)
    except GoogleProviderError as exc:
        async with database.sessions() as session:
            await record_execution(session, action_id, message_id=None, failure=str(exc))
            await session.commit()
        return
    async with database.sessions() as session:
        await record_execution(session, action_id, message_id=message_id, failure=None)
        await session.commit()
