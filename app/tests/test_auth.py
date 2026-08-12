import re
import time
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_fresh_auth_bootstraps_a_forced_change_credential(tmp_path) -> None:
    auth = AuthManager("akirah", "", str(tmp_path / "state.json"), 1)
    auth.initialize()
    bootstrap = tmp_path / "bootstrap_password.txt"
    password = bootstrap.read_text()
    assert len(password) == 32
    assert auth.must_change_password
    assert auth.authenticate("akirah", password, "fresh-client")
    assert auth.state_path.exists()
    auth.change_password("new-secure-password")
    assert not bootstrap.exists()


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


def test_forwarded_client_addresses_are_proxy_scoped(tmp_path) -> None:
    auth, _ = make_auth(tmp_path)
    trusted = SimpleNamespace(
        client=SimpleNamespace(host="172.31.250.2"),
        headers={"X-Forwarded-For": "203.0.113.10, 172.31.250.2"},
    )
    assert auth.client_key(trusted) == "203.0.113.10"
    untrusted = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.20"),
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert auth.client_key(untrusted) == "203.0.113.20"


def test_lockout_backoff_expires_and_quiet_failures_decay(tmp_path, monkeypatch) -> None:
    auth, password = make_auth(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr("chitti.auth.time.time", lambda: clock[0])
    for _ in range(5):
        assert not auth.authenticate("akirah", "wrong", "client-a")
    assert not auth.authenticate("akirah", password, "client-a")
    clock[0] += 61
    assert auth.authenticate("akirah", password, "client-a")
    for _ in range(5):
        assert not auth.authenticate("akirah", "wrong", "client-a")
    clock[0] += auth.quiet_decay_seconds + 1
    assert auth.authenticate("akirah", password, "client-a")


def test_wrong_username_still_verifies_a_dummy_hash(tmp_path, monkeypatch) -> None:
    auth, _ = make_auth(tmp_path)
    verify = Mock(return_value=False)
    monkeypatch.setattr(PasswordHasher, "verify", verify)
    assert not auth.authenticate("other-user", "wrong", "client-a")
    verify.assert_called_once_with(auth.dummy_password_hash, "wrong")


def test_cookie_secure_flag_matches_scheme(tmp_path) -> None:
    make_auth(tmp_path)
    http_client = TestClient(app, base_url="http://testserver")
    http_cookie = http_client.get("/login").headers["set-cookie"]
    assert " Secure" not in http_cookie
    https_client = TestClient(app, base_url="https://testserver")
    https_cookie = https_client.get("/login").headers["set-cookie"]
    assert " Secure" in https_cookie


def test_unauthenticated_page_routes_redirect_to_login(tmp_path) -> None:
    make_auth(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/", follow_redirects=False).headers["location"] == "/login?next=%2F"
    assert client.get("/change-password", follow_redirects=False).headers["location"] == "/login?next=%2Fchange-password"
    assert client.post("/logout", follow_redirects=False).headers["location"] == "/login"
    assert client.post("/memory/conflicts/1/resolve", follow_redirects=False).headers["location"] == (
        "/login?next=%2Fmemory%2Fconflicts%2F1%2Fresolve"
    )
    assert client.post("/memory/decisions/1/forget", follow_redirects=False).headers["location"] == (
        "/login?next=%2Fmemory%2Fdecisions%2F1%2Fforget"
    )


def test_result_approval_requires_session_and_csrf(tmp_path) -> None:
    auth, _ = make_auth(tmp_path)
    auth.must_change_password = False
    client = TestClient(app, base_url="https://testserver")

    response = client.post("/runs/1/approve-result", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fruns%2F1%2Fapprove-result"

    token, _ = auth.create_session("akirah")
    client.cookies.set("chitti_session", token)
    assert client.post("/runs/1/approve-result").status_code == 403


def test_unauthenticated_api_routes_keep_generic_401(tmp_path) -> None:
    make_auth(tmp_path)
    client = TestClient(app, base_url="https://testserver")
    for response in (
        client.get("/health"),
        client.post("/chat", json={"message": "hello"}),
        client.get("/projects/demo/state"),
    ):
        assert response.status_code == 401
        assert response.json() == {"detail": "authentication required"}


def test_expired_session_on_html_route_redirects(tmp_path) -> None:
    auth, _ = make_auth(tmp_path)
    token, _ = auth.create_session("akirah")
    auth.sessions[token].expires_at = time.time() - 1
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set("chitti_session", token)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


def test_authenticated_user_on_login_is_redirected(tmp_path) -> None:
    auth, _ = make_auth(tmp_path)
    auth.must_change_password = False
    token, _ = auth.create_session("akirah")
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set("chitti_session", token)
    response = client.get("/login?next=%2Fchange-password", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
