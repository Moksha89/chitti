import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import cast
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

from .auth import AuthManager, Session
from .db import Database
from .embedding import FakeEmbedder, get_embedder
from .memory import MemoryStore
from .project_state import ProjectState
from .provider import FakeProvider, LiteLLMProvider
from .service import ChittiService
from .settings import Settings, get_settings
from .telegram import TelegramPoller

logging.basicConfig(
    level=logging.INFO,
    format='{"level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)

SESSION_COOKIE = "chitti_session"
CSRF_FIELD = "csrf_token"


class ChatRequest(BaseModel):
    message: str
    project: str | None = None
    history: list[dict[str, str]] = Field(default_factory=list)


def build_service(settings: Settings) -> ChittiService:
    with open(settings.profile_path, encoding="utf-8") as profile_file:
        profile = profile_file.read()
    provider = (
        FakeProvider()
        if settings.chitti_provider == "fake"
        else LiteLLMProvider(settings.litellm_base_url, settings.litellm_master_key)
    )
    embedder = FakeEmbedder() if settings.chitti_provider == "fake" else get_embedder(
        settings.embedding_model
    )
    return ChittiService(provider, MemoryStore(embedder), profile)


def request_is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return auth_manager(request).is_trusted_proxy(request) and request.headers.get("X-Forwarded-Proto") == "https"


def set_session_cookie(response: RedirectResponse | HTMLResponse, token: str, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=8 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def clear_session_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(SESSION_COOKIE)


def auth_manager(request: Request) -> AuthManager:
    return cast(AuthManager, request.app.state.auth)


def current_session(request: Request) -> tuple[str, Session]:
    token = request.cookies.get(SESSION_COOKIE)
    session = auth_manager(request).get_session(token)
    if not token or session is None or session.username is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return token, session


def safe_next_path(value: str | None) -> str:
    candidate = value or "/"
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return "/"


def change_password_location(next_path: str) -> str:
    if next_path == "/":
        return "/change-password"
    return f"/change-password?next={quote(next_path, safe='')}"


def login_redirect(request: Request, *, include_next: bool = True) -> RedirectResponse:
    if not include_next:
        return RedirectResponse("/login", status_code=303)
    destination = request.url.path
    if request.url.query:
        destination = f"{destination}?{request.url.query}"
    return RedirectResponse(f"/login?next={quote(destination, safe='')}", status_code=303)


def browser_session(request: Request, *, include_next: bool = True) -> tuple[str, Session] | RedirectResponse:
    token = request.cookies.get(SESSION_COOKIE)
    session = auth_manager(request).get_session(token)
    if not token or session is None or session.username is None:
        return login_redirect(request, include_next=include_next)
    return token, session


def require_csrf(request: Request, session: Session, form_token: str | None = None) -> None:
    token = form_token or request.headers.get("X-CSRF-Token")
    if not auth_manager(request).csrf_valid(session, token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    database = Database(settings)
    service = build_service(settings)
    auth = AuthManager(
        settings.chitti_username,
        settings.chitti_password_hash,
        settings.chitti_auth_state_path,
        settings.chitti_session_ttl_minutes,
        settings.chitti_trusted_proxy_ip,
    )
    auth.initialize()
    poller = TelegramPoller(settings, service, database.sessions)
    poller.start()
    app.state.settings = settings
    app.state.database = database
    app.state.service = service
    app.state.project_state = ProjectState(settings.project_root)
    app.state.auth = auth
    try:
        yield
    finally:
        await poller.stop()
        await database.close()


app = FastAPI(title="Chitti", version="0.2.0", lifespan=lifespan)
template_directory = "/app/templates"
if not Path(template_directory).exists():
    template_directory = str(Path(__file__).resolve().parents[1] / "templates")
templates = Jinja2Templates(directory=template_directory)
markdown = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")


def render_markdown(value: str) -> str:
    return cast(str, markdown.render(value))


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(request: Request) -> HTMLResponse | RedirectResponse:
    manager = auth_manager(request)
    session = manager.get_session(request.cookies.get(SESSION_COOKIE))
    if session and session.username:
        next_path = safe_next_path(request.query_params.get("next"))
        destination = (
            change_password_location(next_path)
            if manager.must_change_password
            else "/"
        )
        return RedirectResponse(destination, status_code=303)
    token, session = manager.create_session()
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "csrf_token": session.csrf_token,
            "error": None,
            "next": safe_next_path(request.query_params.get("next")),
        },
    )
    set_session_cookie(response, token, request_is_https(request))
    return response


@app.post("/login", response_class=HTMLResponse, response_model=None)
async def login(request: Request) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    manager = auth_manager(request)
    old_token = request.cookies.get(SESSION_COOKIE)
    old_session = manager.get_session(old_token)
    csrf_token = str(form.get(CSRF_FIELD, ""))
    next_path = safe_next_path(str(form.get("next", "")))
    if old_session is None:
        response = RedirectResponse(f"/login?next={quote(next_path, safe='')}", status_code=303)
        clear_session_cookie(response)
        return response
    if not manager.csrf_valid(old_session, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    if not manager.authenticate(username, password, manager.client_key(request)):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "csrf_token": old_session.csrf_token,
                "error": "Invalid credentials or login temporarily locked.",
                "next": next_path,
            },
            status_code=401,
        )
    token, session = manager.rotate_authenticated_session(old_token or "", manager.username)
    destination = (
        change_password_location(next_path)
        if manager.must_change_password
        else next_path
    )
    response = RedirectResponse(destination, status_code=303)
    set_session_cookie(response, token, request_is_https(request))
    return response


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    result = browser_session(request, include_next=False)
    if isinstance(result, RedirectResponse):
        return result
    token, session = result
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    manager = auth_manager(request)
    manager.delete_session(token)
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response


@app.get("/change-password", response_class=HTMLResponse, response_model=None)
async def change_password_page(request: Request) -> HTMLResponse | RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    return templates.TemplateResponse(
        request=request,
        name="change_password.html",
        context={
            "csrf_token": session.csrf_token,
            "error": None,
            "next": safe_next_path(request.query_params.get("next")),
        },
    )


@app.post("/change-password", response_class=HTMLResponse, response_model=None)
async def change_password(request: Request) -> HTMLResponse | RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    password = str(form.get("password", ""))
    confirmation = str(form.get("confirmation", ""))
    next_path = safe_next_path(str(form.get("next", "")))
    if len(password) < 12 or password != confirmation:
        return templates.TemplateResponse(
            request=request,
            name="change_password.html",
            context={
                "csrf_token": session.csrf_token,
                "error": "Passwords must match and be at least 12 characters.",
                "next": next_path,
            },
            status_code=400,
        )
    auth_manager(request).change_password(password)
    return RedirectResponse(next_path, status_code=303)


async def dashboard_context(request: Request, session: Session) -> dict[str, object]:
    database: Database = request.app.state.database
    memory = MemoryStore(
        FakeEmbedder()
        if request.app.state.settings.chitti_provider == "fake"
        else get_embedder(request.app.state.settings.embedding_model)
    )
    async with database.sessions() as db_session:
        decisions = await memory.decisions(db_session)
        conflicts = await memory.conflicts(db_session)
    return {"csrf_token": session.csrf_token, "decisions": decisions, "conflicts": conflicts}


@app.get("/", response_class=HTMLResponse, response_model=None)
async def dashboard(request: Request) -> HTMLResponse | RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    if auth_manager(request).must_change_password:
        return RedirectResponse("/change-password", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=await dashboard_context(request, session),
    )


@app.post("/memory/conflicts/{conflict_id}/resolve")
async def resolve_conflict(conflict_id: int, request: Request) -> RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    if auth_manager(request).must_change_password:
        return RedirectResponse("/change-password", status_code=303)
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    choice = str(form.get("choice", ""))
    database: Database = request.app.state.database
    memory = MemoryStore(FakeEmbedder())
    async with database.sessions() as db_session:
        try:
            await memory.resolve_conflict(db_session, conflict_id, choice)
            await db_session.commit()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/", status_code=303)


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    current_session(request)
    return {"status": "ok"}


@app.post("/chat")
async def chat(payload: ChatRequest, request: Request) -> dict[str, object]:
    _, session = current_session(request)
    if auth_manager(request).must_change_password:
        raise HTTPException(status_code=403, detail="password change required")
    require_csrf(request, session)
    database: Database = request.app.state.database
    service: ChittiService = request.app.state.service
    try:
        async with database.sessions() as db_session:
            result = await service.turn(db_session, payload.message, payload.project, payload.history)
    except Exception as exc:
        logging.getLogger(__name__).exception("chat_provider_failed")
        raise HTTPException(status_code=503, detail="model provider unavailable") from exc
    return {
        "reply": result.reply,
        "reply_html": render_markdown(result.reply),
        "conflicts": [asdict(item) for item in result.conflicts],
    }


@app.get("/projects/{project}/state")
async def project_state(project: str, request: Request) -> dict[str, str]:
    current_session(request)
    if auth_manager(request).must_change_password:
        raise HTTPException(status_code=403, detail="password change required")
    state = cast(ProjectState, request.app.state.project_state)
    return state.read(project)
