import re
import time

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from chitti.auth import AuthManager
from chitti.main import app
from chitti.web_security import safe_next_path


def make_auth(tmp_path):
    password = "initial-password-for-tests"
    auth = AuthManager("akirah", PasswordHasher().hash(password), str(tmp_path / "state.json"), 1)
    auth.initialize()
    app.state.auth = auth
    return auth, password


def test_safe_next_path_rejects_protocol_relative_backslashes() -> None:
    assert safe_next_path("/workspace") == "/workspace"
    assert safe_next_path("/\\evil.com") == "/"
    assert safe_next_path("//evil.com") == "/"


def test_login_forces_first_password_change_and_rejects_csrf(tmp_path) -> None:
    auth, password = make_auth(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    page = client.get("/login")
    assert page.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf
    token = csrf.group(1)
    assert client.post("/login", data={"username": "akirah", "password": password}).status_code == 403
    response = client.post(
        "/login",
        data={"csrf_token": token, "username": "akirah", "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/change-password"
    assert client.post("/chat", json={"message": "hello"}).status_code == 403
    session = auth.get_session(client.cookies.get("chitti_session"))
    assert session is not None
    assert client.post(
        "/change-password",
        data={"csrf_token": session.csrf_token, "password": "new-secure-password", "confirmation": "new-secure-password"},
        follow_redirects=False,
    ).status_code == 303
    assert not auth.must_change_password


def test_wrong_password_lockout_and_unauthenticated_chat(tmp_path) -> None:
    auth, password = make_auth(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    page = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    for _ in range(5):
        response = client.post(
            "/login",
            data={"csrf_token": csrf, "username": "akirah", "password": "wrong"},
        )
        assert response.status_code == 401
    for _ in range(5):
        assert not auth.authenticate("akirah", "wrong", "test-client")
    assert not auth.authenticate("akirah", password, "test-client")
    assert client.post("/chat", json={"message": "hello"}).status_code == 401


def test_session_expiry(tmp_path, monkeypatch) -> None:
    auth, _ = make_auth(tmp_path)
    token, _ = auth.create_session("akirah")
    now = time.time()
    monkeypatch.setattr("chitti.auth.time.time", lambda: now + 61 * 60)
    assert auth.get_session(token) is None
