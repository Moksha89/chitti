import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


@dataclass
class Session:
    username: str | None
    csrf_token: str
    expires_at: float


@dataclass
class LoginAttempt:
    failures: int
    locked_until: float


class AuthManager:
    def __init__(
        self,
        username: str,
        password_hash: str,
        state_path: str,
        session_ttl_minutes: int = 480,
    ) -> None:
        self.username = username
        self.password_hash = password_hash
        self.state_path = Path(state_path)
        self.session_ttl_seconds = session_ttl_minutes * 60
        self.must_change_password = True
        self.sessions: dict[str, Session] = {}
        self.attempts: dict[str, LoginAttempt] = {}
        self.hasher = PasswordHasher()

    def initialize(self) -> None:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.password_hash = str(state["password_hash"])
            self.must_change_password = bool(state["must_change_password"])
            return
        if not self.password_hash:
            raise RuntimeError("CHITTI_PASSWORD_HASH must be configured")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._save_state()

    def _save_state(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "password_hash": self.password_hash,
                    "must_change_password": self.must_change_password,
                }
            ),
            encoding="utf-8",
        )
        os.chmod(self.state_path, 0o600)

    def create_session(self, username: str | None = None) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        session = Session(
            username=username,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=time.time() + self.session_ttl_seconds,
        )
        self.sessions[token] = session
        return token, session

    def get_session(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = self.sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= time.time():
            self.sessions.pop(token, None)
            return None
        return session

    def delete_session(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)

    def _is_locked(self, client_key: str) -> bool:
        attempt = self.attempts.get(client_key)
        return attempt is not None and attempt.locked_until > time.time()

    def _record_failure(self, client_key: str) -> None:
        attempt = self.attempts.get(client_key, LoginAttempt(0, 0))
        failures = attempt.failures + 1
        lock_seconds = 60 if failures >= 5 else 0
        self.attempts[client_key] = LoginAttempt(failures, time.time() + lock_seconds)

    def authenticate(self, username: str, password: str, client_key: str) -> bool:
        if self._is_locked(client_key):
            return False
        valid_username = hmac.compare_digest(username, self.username)
        valid_password = False
        if valid_username:
            try:
                valid_password = self.hasher.verify(self.password_hash, password)
            except (InvalidHashError, VerificationError, VerifyMismatchError):
                valid_password = False
        if not (valid_username and valid_password):
            self._record_failure(client_key)
            return False
        self.attempts.pop(client_key, None)
        return True

    def change_password(self, password: str) -> None:
        self.password_hash = self.hasher.hash(password)
        self.must_change_password = False
        self._save_state()

    def csrf_valid(self, session: Session, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(session.csrf_token, token or "")

    def rotate_authenticated_session(self, old_token: str, username: str) -> tuple[str, Session]:
        self.delete_session(old_token)
        return self.create_session(username)

    @staticmethod
    def client_key(request: Any) -> str:
        return request.client.host if request.client else "unknown"
