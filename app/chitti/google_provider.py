from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Protocol, cast

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

GOOGLE_READ_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)
GOOGLE_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GOOGLE_SCOPES = GOOGLE_READ_SCOPES + (GOOGLE_SEND_SCOPE,)


class GoogleProviderError(RuntimeError):
    pass


class GoogleCursorInvalid(GoogleProviderError):
    pass


@dataclass(frozen=True)
class GmailMessage:
    message_id: str
    thread_id: str | None
    history_id: str | None
    internal_date: datetime | None
    sender: str | None
    recipients: str | None
    subject: str | None
    snippet: str | None
    body: str | None


@dataclass(frozen=True)
class CalendarEvent:
    calendar_id: str
    event_id: str
    etag: str | None
    summary: str | None
    description: str | None
    start_at: datetime | None
    end_at: datetime | None
    status: str | None
    html_link: str | None


class GoogleReadProvider(Protocol):
    def account_email(self) -> str: ...

    def gmail_messages(
        self, history_id: str | None, after: datetime | None, limit: int
    ) -> tuple[str, list[GmailMessage]]: ...

    def calendar_events(
        self, sync_token: str | None, time_min: datetime, time_max: datetime
    ) -> tuple[str | None, list[CalendarEvent]]: ...

    def revoke(self) -> None: ...

    def send_email(self, action: dict[str, Any]) -> str: ...


class GoogleApiProvider:
    def __init__(self, client_config: dict[str, Any], refresh_token: str) -> None:
        self.credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=str(client_config["web"]["client_id"]),
            client_secret=str(client_config["web"]["client_secret"]),
            scopes=list(GOOGLE_SCOPES),
        )
        self.gmail = build("gmail", "v1", credentials=self.credentials, cache_discovery=False)
        self.calendar = build("calendar", "v3", credentials=self.credentials, cache_discovery=False)

    def account_email(self) -> str:
        try:
            profile = self.gmail.users().getProfile(userId="me").execute()
        except Exception as exc:
            raise GoogleProviderError("Google account profile lookup failed") from exc
        return str(profile["emailAddress"])

    def gmail_messages(
        self, history_id: str | None, after: datetime | None, limit: int
    ) -> tuple[str, list[GmailMessage]]:
        try:
            if history_id:
                response = cast(
                    dict[str, Any],
                    self.gmail.users()
                    .history()
                    .list(
                        userId="me", startHistoryId=history_id, historyTypes=["messageAdded"]
                    )
                    .execute(),
                )
                if response.get("historyId") is None and response.get("history") is None:
                    raise GoogleCursorInvalid("Gmail history cursor is invalid")
                message_ids = [
                    item["messages"][0]["id"]
                    for item in response.get("history", [])
                    if item.get("messages")
                ]
                newest = str(response.get("historyId", history_id))
            else:
                query = f"after:{int(after.timestamp())}" if after else None
                response = cast(
                    dict[str, Any],
                    self.gmail.users().messages().list(
                        userId="me", q=query, maxResults=limit
                    ).execute(),
                )
                message_ids = [item["id"] for item in response.get("messages", [])][:limit]
                profile = self.gmail.users().getProfile(userId="me").execute()
                newest = str(profile.get("historyId", ""))
            messages = [self._message(str(message_id)) for message_id in message_ids]
            return newest, messages
        except GoogleCursorInvalid:
            raise
        except Exception as exc:
            text = str(exc).lower()
            if "historyid" in text or "history id" in text or "404" in text:
                raise GoogleCursorInvalid("Gmail history cursor is invalid") from exc
            raise GoogleProviderError("Gmail synchronization failed") from exc

    def _message(self, message_id: str) -> GmailMessage:
        raw = cast(
            dict[str, Any],
            self.gmail.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute(),
        )
        headers = {
            str(item.get("name", "")).lower(): str(item.get("value", ""))
            for item in raw.get("payload", {}).get("headers", [])
        }
        return GmailMessage(
            message_id=message_id,
            thread_id=raw.get("threadId"),
            history_id=str(raw.get("historyId")) if raw.get("historyId") else None,
            internal_date=datetime.fromtimestamp(int(raw["internalDate"]) / 1000) if raw.get("internalDate") else None,
            sender=headers.get("from"),
            recipients=headers.get("to"),
            subject=headers.get("subject"),
            snippet=raw.get("snippet"),
            body=_decode_body(raw.get("payload", {})),
        )

    def calendar_events(
        self, sync_token: str | None, time_min: datetime, time_max: datetime
    ) -> tuple[str | None, list[CalendarEvent]]:
        try:
            params: dict[str, Any] = {
                "calendarId": "primary",
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": 250,
            }
            if sync_token:
                params["syncToken"] = sync_token
            else:
                params["timeMin"] = time_min.isoformat()
                params["timeMax"] = time_max.isoformat()
            response = cast(
                dict[str, Any], self.calendar.events().list(**params).execute()
            )
            events = [
                CalendarEvent(
                    calendar_id="primary",
                    event_id=str(item["id"]),
                    etag=item.get("etag"),
                    summary=item.get("summary"),
                    description=item.get("description"),
                    start_at=_event_time(item.get("start", {})),
                    end_at=_event_time(item.get("end", {})),
                    status=item.get("status"),
                    html_link=item.get("htmlLink"),
                )
                for item in response.get("items", [])
            ]
            return response.get("nextSyncToken"), events
        except Exception as exc:
            text = str(exc).lower()
            if "410" in text or "sync token" in text:
                raise GoogleCursorInvalid("Calendar sync cursor is invalid") from exc
            raise GoogleProviderError("Calendar synchronization failed") from exc

    def revoke(self) -> None:
        try:
            response = Request().session.post(
                "https://oauth2.googleapis.com/revoke",
                data={"token": self.credentials.refresh_token},
                timeout=20,
            )
            if response.status_code >= 400:
                raise GoogleProviderError("Google token revocation was rejected")
        except Exception as exc:
            raise GoogleProviderError("Google token revocation failed") from exc

    def send_email(self, action: dict[str, Any]) -> str:
        message = EmailMessage()
        message["To"] = ", ".join(str(value) for value in action["to_recipients"])
        if action["cc_recipients"]:
            message["Cc"] = ", ".join(str(value) for value in action["cc_recipients"])
        if action["bcc_recipients"]:
            message["Bcc"] = ", ".join(str(value) for value in action["bcc_recipients"])
        message["Subject"] = str(action["subject"])
        message.set_content(str(action["body"]))
        attachments = cast(list[dict[str, Any]], action["attachments"])
        if attachments:
            raise GoogleProviderError(
                "Email attachments are recorded but their content is not available"
            )
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        try:
            response = cast(
                dict[str, Any],
                self.gmail.users()
                .messages()
                .send(userId="me", body={"raw": encoded})
                .execute(),
            )
        except Exception as exc:
            raise GoogleProviderError("Google email send failed") from exc
        message_id = response.get("id")
        if not message_id:
            raise GoogleProviderError("Google email send returned no message id")
        return str(message_id)


def _decode_body(payload: dict[str, Any]) -> str | None:
    body = payload.get("body", {})
    if body.get("data"):
        return base64.urlsafe_b64decode(str(body["data"]).encode("ascii")).decode("utf-8", "replace")
    for part in payload.get("parts", []):
        value = _decode_body(part)
        if value:
            return value
    return None


def _event_time(value: dict[str, Any]) -> datetime | None:
    raw = value.get("dateTime") or value.get("date")
    if not raw:
        return None
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
