import asyncio
import json
import logging
import mimetypes
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import AuthManager, Session
from .brand_profiles import (
    available_font_families,
    get_brand_profile,
    save_brand_profile,
)
from .briefings import compose_briefing
from .db import Database
from .diff_parser import parse_diff as _parse_diff
from .embedding import FakeEmbedder, get_embedder
from .memory import (
    MemoryStore,
    namespace_options,
    normalize_namespace,
)
from .namespaces import SHARED_NAMESPACE
from .notifications import (
    acknowledge_notification,
    notifications_after,
    recent_notifications,
)
from .plans import (
    PlanManager,
    approve_revision,
    latest_revisions,
    reject_revision,
    revision_by_id,
)
from .previews import manifest_from_json, safe_preview_file
from .project_state import ProjectState
from .provider import FakeProvider, LiteLLMProvider
from .reminders import cancel_reminder, create_reminder, recent_reminders
from .run_context import RunContextError, build_run_evidence
from .run_status import TERMINAL_RUN_STATUSES
from .runner_health import recent_runner_health
from .service import ChittiService
from .settings import Settings, get_settings
from .telegram import TelegramPoller
from .transcripts import recent_entries
from .worker import WorkerLimits, WorkerRunManager

logging.basicConfig(
    level=logging.INFO,
    format='{"level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)

SESSION_COOKIE = "chitti_session"
CSRF_FIELD = "csrf_token"
RUN_EVENT_POLL_SECONDS = 1.0
RUN_EVENT_HEARTBEAT_SECONDS = 15.0
WORKSPACE_RUN_LIST_LIMIT = 25
MAX_DIFF_BODY_BYTES = 12_000
PROMOTION_APPROVAL_ACTORS = {"owner", "agent", "system"}


class ChatRequest(BaseModel):
    message: str
    project: str | None = None
    namespace: str = SHARED_NAMESPACE
    plan_requested: bool = False
    history: list[dict[str, str]] = Field(default_factory=list)
    run_id: int | None = None


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


def requested_namespace(request: Request, value: str | None = None) -> str:
    candidate = value if value is not None else request.query_params.get("namespace")
    try:
        return normalize_namespace(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    app.state.plan_manager = PlanManager(database, service.provider, service.memory)
    app.state.worker_manager = WorkerRunManager(database)
    app.state.project_state = ProjectState(settings.project_root)
    app.state.auth = auth
    await app.state.plan_manager.resume_queued()
    try:
        yield
    finally:
        await poller.stop()
        await database.close()


app = FastAPI(title="Chitti", version="0.3.0", lifespan=lifespan)
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


async def dashboard_context(
    request: Request, session: Session, namespace: str = SHARED_NAMESPACE
) -> dict[str, object]:
    namespace = normalize_namespace(namespace)
    database: Database = request.app.state.database
    memory = MemoryStore(
        FakeEmbedder()
        if request.app.state.settings.chitti_provider == "fake"
        else get_embedder(request.app.state.settings.embedding_model)
    )
    async with database.sessions() as db_session:
        decisions = await memory.decisions(db_session, namespace)
        conflicts = memory.group_conflicts(await memory.conflicts(db_session, namespace))
        plans = await latest_revisions(db_session, namespace)
        transcript = await recent_entries(db_session, namespace)
        reminders = await recent_reminders(database, namespace)
        notifications = await recent_notifications(database, namespace)
        brand_profile = await get_brand_profile(db_session, namespace)
        runner_health = await recent_runner_health(database)
        for plan in plans:
            approval_result = await db_session.execute(
                text(
                    "SELECT decision, reason, content_hash, created_at FROM plan_approvals "
                    "WHERE revision_id = :revision ORDER BY id DESC LIMIT 1"
                ),
                {"revision": plan["id"]},
            )
            approval = approval_result.mappings().one_or_none()
            plan["approval"] = dict(approval) if approval else None
    for decision in decisions:
        decision["display_key"] = humanize_belief_key(str(decision["decision_key"]))
        decision["display_value"] = str(decision["decision"])
    for conflict in conflicts:
        conflict["display_key"] = humanize_belief_key(str(conflict["decision_key"]))
        conflict["display_existing"] = str(conflict["existing_value"])
        for proposal in cast(list[dict[str, object]], conflict["proposals"]):
            proposal["display_proposed"] = str(proposal["proposed_value"])
    now = datetime.now(ZoneInfo(request.app.state.settings.display_timezone))
    display_zone = ZoneInfo(request.app.state.settings.display_timezone)
    for reminder in reminders:
        due_at = reminder.get("due_at")
        if isinstance(due_at, datetime):
            reminder["due_at"] = due_at.astimezone(display_zone).isoformat(
                timespec="minutes"
            )
    for notification in notifications:
        created_at = notification.get("created_at")
        if isinstance(created_at, datetime):
            notification["created_at"] = created_at.astimezone(display_zone).isoformat(
                timespec="minutes"
            )
    for health in runner_health:
        succeeded_at = health.get("last_succeeded_at")
        if isinstance(succeeded_at, datetime):
            health["last_succeeded_at"] = succeeded_at.astimezone(
                display_zone
            ).isoformat(timespec="minutes")
    briefing = await compose_briefing(
        database, namespace, request.app.state.settings.display_timezone, now.astimezone(UTC)
    )
    if now.hour < 12:
        greeting = "Good morning"
    elif now.hour < 18:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"
    return {
        "csrf_token": session.csrf_token,
        "decisions": decisions,
        "conflicts": conflicts,
        "plans": plans,
        "greeting": greeting,
        "owner_name": None,
        "display_timezone": request.app.state.settings.display_timezone,
        "namespace": namespace,
        "namespace_options": namespace_options(),
        "transcript": transcript,
        "reminders": reminders,
        "notifications": notifications,
        "runner_health": runner_health,
        "briefing": briefing,
        "brand_profile": brand_profile,
        "available_fonts": available_font_families(),
        "brand_error": request.query_params.get("brand_error"),
    }


def humanize_belief_key(value: str) -> str:
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    return re.sub(r"[^a-zA-Z0-9]+", " ", spaced).strip().capitalize()


def project_from_brief(
    project: str | None, plan_requested: bool = False
) -> str | None:
    if not plan_requested or not project or not project.strip():
        return None
    value = re.sub(r"[^a-zA-Z0-9]+", "-", project.strip()).strip("-").lower()
    return value or None


def _run_status(detail: dict[str, object]) -> str:
    events = cast(list[dict[str, object]], detail.get("events", []))
    if not events:
        return "queued"
    return str(events[-1].get("status", "queued"))


def _diff_body(payload: bytes, index: int) -> dict[str, object] | None:
    text_payload = payload.decode("utf-8", errors="replace")
    blocks = re.split(r"(?=^diff --git )", text_payload, flags=re.MULTILINE)
    blocks = [block for block in blocks if block.startswith("diff --git ")]
    if index < 0 or index >= len(blocks):
        return None
    encoded = blocks[index].encode("utf-8")
    clipped = len(encoded) > MAX_DIFF_BODY_BYTES
    body = encoded[:MAX_DIFF_BODY_BYTES].decode("utf-8", errors="replace")
    match = re.match(r"diff --git a/(.+) b/(.+)\n?$", blocks[index].splitlines()[0])
    path = match.group(2) if match else f"file-{index}"
    return {
        "path": path,
        "body": body,
        "clipped": clipped,
    }


def _tree_nodes(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    root: dict[str, object] = {}
    for entry in entries:
        cursor = root
        parts = Path(str(entry["path"])).parts
        for part in parts[:-1]:
            children = cast(dict[str, object], cursor.setdefault("children", {}))
            cursor = cast(dict[str, object], children.setdefault(part, {}))
        children = cast(dict[str, object], cursor.setdefault("children", {}))
        children[parts[-1]] = {"entry": entry}

    def render(node: dict[str, object]) -> list[dict[str, object]]:
        children = cast(dict[str, dict[str, object]], node.get("children", {}))
        result: list[dict[str, object]] = []
        for name in sorted(children):
            child = children[name]
            entry = child.get("entry")
            item: dict[str, object] = {"name": name}
            if entry is not None:
                item["entry"] = entry
            else:
                item["children"] = render(child)
            result.append(item)
        return result

    return render(root)


def _prepare_files_view(
    payload: bytes | None, artifact: dict[str, object] | None
) -> dict[str, object]:
    if artifact is None:
        return {"state": "empty", "tree": [], "entries": []}
    if payload is None:
        return {
            "state": "expired",
            "tree": [],
            "entries": [],
            "artifact_id": artifact["id"],
        }
    entries = _parse_diff(payload)
    return {
        "state": "available",
        "tree": _tree_nodes(entries),
        "authored_tree": _tree_nodes(
            [entry for entry in entries if entry["role"] == "authored"]
        ),
        "generated_tree": _tree_nodes(
            [entry for entry in entries if entry["role"] == "generated"]
        ),
        "entries": entries,
        "artifact_id": artifact["id"],
        "authored_count": sum(entry["role"] == "authored" for entry in entries),
        "generated_count": sum(entry["role"] == "generated" for entry in entries),
    }


def _prepare_workspace_run(detail: dict[str, object]) -> dict[str, object]:
    detail.setdefault("files_view", {"state": "empty", "tree": [], "entries": []})
    detail.setdefault("export_view", {"state": "empty", "tree": [], "entries": []})
    operations = []
    artifacts = cast(list[dict[str, object]], detail.get("artifacts", []))
    output_artifacts: dict[tuple[int, str], int] = {}
    for artifact in artifacts:
        operation_id = artifact.get("operation_id")
        kind = str(artifact.get("kind", ""))
        if operation_id is not None and kind in {"stdout", "stderr"}:
            output_artifacts[(int(str(operation_id)), kind)] = int(str(artifact["id"]))
    for operation in cast(list[dict[str, object]], detail.get("operations", [])):
        item = dict(operation)
        item["stdout_preview"] = str(operation.get("stdout", ""))[-4000:]
        item["stderr_preview"] = str(operation.get("stderr", ""))[-4000:]
        operation_id = int(str(operation["id"]))
        item["stdout_artifact_id"] = output_artifacts.get((operation_id, "stdout"))
        item["stderr_artifact_id"] = output_artifacts.get((operation_id, "stderr"))
        operations.append(item)
    detail["operations"] = operations
    detail["latest_status"] = _run_status(detail)
    detail["is_terminal"] = detail["latest_status"] in TERMINAL_RUN_STATUSES
    run = cast(dict[str, object], detail["run"])
    created_at = run.get("created_at")
    if isinstance(created_at, datetime):
        detail["elapsed_seconds"] = max(
            0, int((datetime.now(created_at.tzinfo) - created_at).total_seconds())
        )
    else:
        detail["elapsed_seconds"] = 0
    calls = cast(list[dict[str, object]], detail.get("model_calls", []))
    detail["model_call_count"] = len(calls)
    total_tokens = int(str(detail.get("token_totals", 0)))
    reasoning_tokens = int(str(detail.get("reasoning_token_totals", 0)))
    detail["reasoning_share"] = (
        (reasoning_tokens / total_tokens * 100) if total_tokens else 0.0
    )
    latest_capture: dict[tuple[str, str], dict[str, object]] = {}
    for artifact in artifacts:
        kind = str(artifact.get("kind", ""))
        if kind not in {"screenshot", "browser_evidence"}:
            continue
        key = (kind, str(artifact.get("path", "")))
        current = latest_capture.get(key)
        if current is None or int(str(artifact.get("id", 0))) > int(
            str(current.get("id", 0))
        ):
            latest_capture[key] = artifact
    detail["screenshots"] = sorted(
        (
            artifact
            for (kind, _), artifact in latest_capture.items()
            if kind == "screenshot"
        ),
        key=lambda artifact: str(artifact.get("path", "")),
    )
    detail["browser_errors"] = next(
        (
            artifact
            for (kind, _), artifact in latest_capture.items()
            if kind == "browser_evidence"
        ),
        None,
    )
    return detail


async def _run_event_stream(
    request: Request,
    manager: WorkerRunManager,
    run_id: int,
    cursor: int,
    chunk_cursor: int = 0,
    initial_status: str | None = None,
) -> AsyncIterator[str]:
    if await request.is_disconnected():
        return
    if initial_status is None:
        initial_status = await manager.latest_status(run_id)
    if initial_status is None or initial_status in TERMINAL_RUN_STATUSES:
        return
    last_heartbeat = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        output_chunks_after = getattr(manager, "output_chunks_after", None)
        pending_chunks = (
            await output_chunks_after(run_id, chunk_cursor)
            if output_chunks_after is not None
            else []
        )
        for chunk in pending_chunks:
            chunk_id = int(str(chunk["id"]))
            chunk_cursor = chunk_id
            payload = json.dumps(chunk, default=str, separators=(",", ":"))
            yield f"event: output\ndata: {payload}\n\n"
            last_heartbeat = time.monotonic()
        pending = await manager.events_after(run_id, cursor)
        for event in pending:
            event_id = int(str(event["id"]))
            cursor = event_id
            terminal = str(event.get("status", "")) in TERMINAL_RUN_STATUSES
            payload_event = {**event, "terminal": terminal}
            payload = json.dumps(payload_event, default=str, separators=(",", ":"))
            yield f"id: {event_id}\nevent: run\ndata: {payload}\n\n"
            last_heartbeat = time.monotonic()
            if terminal:
                return
        now = time.monotonic()
        if now - last_heartbeat >= RUN_EVENT_HEARTBEAT_SECONDS:
            yield ": heartbeat\n\n"
            last_heartbeat = now
        await asyncio.sleep(RUN_EVENT_POLL_SECONDS)




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
        context=await dashboard_context(request, session, requested_namespace(request)),
    )


@app.post("/reminders")
async def create_dashboard_reminder(request: Request) -> RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    namespace = requested_namespace(request, str(form.get("namespace", "")) or None)
    text_value = str(form.get("text", "")).strip()
    local_value = str(form.get("due_local", "")).strip()
    recurrence = str(form.get("recurrence", "")).strip() or None
    if not text_value or not local_value or recurrence not in {None, "daily", "weekly"}:
        raise HTTPException(status_code=400, detail="invalid reminder")
    try:
        local_due = datetime.fromisoformat(local_value).replace(
            tzinfo=ZoneInfo(request.app.state.settings.display_timezone)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid reminder time") from exc
    await create_reminder(
        request.app.state.database,
        namespace,
        text_value,
        local_due.astimezone(UTC),
        recurrence,
    )
    return RedirectResponse(f"/?namespace={namespace}", status_code=303)


@app.post("/reminders/{reminder_id}/cancel")
async def cancel_dashboard_reminder(
    reminder_id: int, request: Request
) -> RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    namespace = requested_namespace(request, str(form.get("namespace", "")) or None)
    await cancel_reminder(request.app.state.database, namespace, reminder_id)
    return RedirectResponse(f"/?namespace={namespace}", status_code=303)


@app.post("/brand-profile")
async def save_brand_profile_route(request: Request) -> RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    namespace = requested_namespace(request, str(form.get("namespace", "")) or None)

    def split_lines(key: str) -> list[str]:
        return str(form.get(key, "")).splitlines()

    try:
        async with request.app.state.database.sessions() as db_session:
            await save_brand_profile(
                db_session,
                namespace,
                brand_colors=split_lines("brand_colors"),
                typography=str(form.get("typography", "")),
                poster_formats=split_lines("poster_formats"),
                audience=str(form.get("audience", "")),
                voice=str(form.get("voice", "")),
                do_not_use=split_lines("do_not_use"),
                actor=session.username or "owner",
            )
            await db_session.commit()
    except ValueError as exc:
        return RedirectResponse(
            f"/?namespace={quote(namespace, safe='')}&brand_error={quote(str(exc), safe='')}",
            status_code=303,
        )
    return RedirectResponse(f"/?namespace={quote(namespace, safe='')}", status_code=303)


@app.post("/notifications/{notification_id}/acknowledge")
async def acknowledge_dashboard_notification(
    notification_id: int, request: Request
) -> RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    namespace = requested_namespace(request, str(form.get("namespace", "")) or None)
    await acknowledge_notification(request.app.state.database, namespace, notification_id)
    return RedirectResponse(f"/?namespace={namespace}", status_code=303)


async def _notification_stream(
    request: Request, database: Database, namespace: str, cursor: int
) -> AsyncIterator[str]:
    last_heartbeat = time.monotonic()
    while not await request.is_disconnected():
        for notification in await notifications_after(database, namespace, cursor):
            cursor = int(str(notification["id"]))
            created_at = notification.get("created_at")
            if isinstance(created_at, datetime):
                notification["created_at"] = created_at.astimezone(
                    ZoneInfo(request.app.state.settings.display_timezone)
                ).isoformat(timespec="minutes")
            payload = json.dumps(notification, default=str, separators=(",", ":"))
            yield f"id: {cursor}\nevent: notification\ndata: {payload}\n\n"
            last_heartbeat = time.monotonic()
        if time.monotonic() - last_heartbeat >= RUN_EVENT_HEARTBEAT_SECONDS:
            yield ": heartbeat\n\n"
            last_heartbeat = time.monotonic()
        await asyncio.sleep(RUN_EVENT_POLL_SECONDS)


@app.get("/notifications/events")
async def notification_events(request: Request) -> StreamingResponse:
    current_session(request)
    namespace = requested_namespace(request)
    try:
        cursor = max(
            0,
            int(request.headers.get("Last-Event-ID", request.query_params.get("event_cursor", "0"))),
        )
    except ValueError:
        cursor = 0
    return StreamingResponse(
        _notification_stream(request, request.app.state.database, namespace, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/workspace/runs/{run_id}", response_class=HTMLResponse, response_model=None)
async def workspace_run_page(
    run_id: int, request: Request
) -> HTMLResponse | RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    if auth_manager(request).must_change_password:
        return RedirectResponse("/change-password", status_code=303)
    manager: WorkerRunManager = request.app.state.worker_manager
    run = await manager.detail(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="worker run not found")
    run = _prepare_workspace_run(run)
    database: Database = request.app.state.database
    async with database.sessions() as db_session:
        run_record = cast(dict[str, object], run["run"])
        revision = await revision_by_id(
            db_session,
            int(str(run_record["revision_id"])),
            str(run_record.get("namespace", SHARED_NAMESPACE)),
        )
        if revision is None:
            raise HTTPException(status_code=404, detail="plan revision not found")
        run_rows = await db_session.execute(
            text(
                "SELECT r.id, r.revision_id, r.created_at, p.project, p.revision, "
                "COALESCE(latest.status, 'queued') AS status "
                "FROM worker_runs r JOIN plan_revisions p ON p.id = r.revision_id "
                "LEFT JOIN LATERAL ("
                "SELECT status FROM worker_run_events "
                "WHERE run_id = r.id ORDER BY id DESC LIMIT 1"
                ") latest ON TRUE "
                "ORDER BY r.id DESC LIMIT :limit"
            ),
            {"limit": WORKSPACE_RUN_LIST_LIMIT},
        )
        run_links = []
        for row in run_rows.mappings():
            run_links.append(
                {
                    **dict(row),
                    "is_open": int(row["id"]) == run_id,
                }
            )
        task_events = await db_session.execute(
            text(
                "SELECT task_id, status FROM plan_task_events "
                "WHERE revision_id = :revision ORDER BY id"
            ),
            {"revision": revision.id},
        )
        task_statuses: dict[str, str] = {}
        for event in task_events:
            task_statuses[str(event.task_id)] = str(event.status)
        promotion_result = await db_session.execute(
            text(
                "SELECT m.id AS manifest_id, m.digest, m.total_bytes, "
                "a.id AS approval_id, a.decision, a.approved_by, a.reason, "
                "a.created_at AS approval_created_at, p.preview_id, p.expires_at "
                "FROM export_manifests m "
                "LEFT JOIN promotion_approvals a ON a.manifest_id = m.id "
                "LEFT JOIN previews p ON p.manifest_id = m.id "
                "WHERE m.run_id = :run_id"
            ),
            {"run_id": run_id},
        )
        run["promotion"] = promotion_result.mappings().one_or_none()
        diff_result = await db_session.execute(
            text(
                "SELECT a.id, a.byte_size, a.sha256, p.content "
                "FROM worker_artifacts a "
                "LEFT JOIN worker_artifact_payloads p ON p.artifact_id = a.id "
                "WHERE a.run_id = :run_id AND a.kind = 'diff' "
                "ORDER BY a.id DESC LIMIT 1"
            ),
            {"run_id": run_id},
        )
        diff_row = diff_result.mappings().one_or_none()
        diff_artifact = (
            {key: value for key, value in diff_row.items() if key != "content"}
            if diff_row is not None
            else None
        )
        files_view = _prepare_files_view(
            bytes(diff_row["content"]) if diff_row and diff_row["content"] is not None else None,
            diff_artifact,
        )
        run["files_view"] = files_view
        if files_view["state"] == "available" and diff_artifact is not None:
            files_view["artifact_url"] = (
                f"/runs/{run_id}/artifacts/{diff_artifact['id']}"
            )
        manifest_result = await db_session.execute(
            text(
                "SELECT manifest, digest, total_bytes, file_count, max_depth "
                "FROM export_manifests WHERE run_id = :run_id "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"run_id": run_id},
        )
        manifest_row = manifest_result.mappings().one_or_none()
        export_view: dict[str, object] = {"state": "empty", "tree": [], "entries": []}
        if manifest_row is not None:
            try:
                manifest = manifest_from_json(
                    manifest_row["manifest"], str(manifest_row["digest"])
                )
                entries = [
                    {
                        "path": entry.path,
                        "size": entry.size,
                        "sha256": entry.sha256,
                    }
                    for entry in manifest.entries
                ]
                export_view = {
                    "state": "available",
                    "tree": _tree_nodes(
                        [
                            {
                                **entry,
                                "index": index,
                                "kind": "manifest",
                                "role": "generated",
                                "summary": f"{entry['size']} bytes",
                            }
                            for index, entry in enumerate(entries)
                        ]
                    ),
                    "entries": entries,
                    "file_count": int(manifest_row["file_count"]),
                    "total_bytes": int(manifest_row["total_bytes"]),
                    "digest": str(manifest_row["digest"]),
                }
            except (TypeError, ValueError, KeyError):
                export_view = {"state": "invalid", "tree": [], "entries": []}
        run["export_view"] = export_view
        reviewer_result = await db_session.execute(
            text(
                "SELECT p.content FROM worker_artifacts a "
                "JOIN worker_artifact_payloads p ON p.artifact_id = a.id "
                "WHERE a.run_id = :run_id AND a.kind = 'reviewer_report' "
                "ORDER BY a.id DESC LIMIT 1"
            ),
            {"run_id": run_id},
        )
        reviewer_payload = reviewer_result.scalar_one_or_none()
        if reviewer_payload is not None:
            try:
                run["reviewer_verdict"] = json.loads(reviewer_payload)
            except (TypeError, json.JSONDecodeError):
                run["reviewer_verdict"] = {"verdict": "invalid"}
    current_task = next(
        (
            task.id
            for task in revision.document.tasks
            if task_statuses.get(task.id) == "running"
        ),
        None,
    )
    return templates.TemplateResponse(
        request=request,
        name="workspace.html",
        context={
            "csrf_token": session.csrf_token,
            "revision": revision,
            "run": run,
            "run_links": run_links,
            "task_statuses": task_statuses,
            "current_task": current_task,
        },
    )


@app.get("/workspace/runs/{run_id}/diff/{file_index}", response_model=None)
async def workspace_diff_file(
    run_id: int, file_index: int, request: Request
) -> dict[str, object]:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        raise HTTPException(status_code=401, detail="authentication required")
    database: Database = request.app.state.database
    async with database.sessions() as session:
        artifact_result = await session.execute(
            text(
                "SELECT a.id, p.content FROM worker_artifacts a "
                "LEFT JOIN worker_artifact_payloads p ON p.artifact_id = a.id "
                "WHERE a.run_id = :run_id AND a.kind = 'diff' "
                "ORDER BY a.id DESC LIMIT 1"
            ),
            {"run_id": run_id},
        )
        artifact = artifact_result.mappings().one_or_none()
    if artifact is None:
        raise HTTPException(status_code=404, detail="run has no diff artifact")
    if artifact["content"] is None:
        raise HTTPException(status_code=410, detail="diff artifact payload has expired")
    body = _diff_body(bytes(artifact["content"]), file_index)
    if body is None:
        raise HTTPException(status_code=404, detail="diff file not found")
    return {
        **body,
        "full_artifact_url": f"/runs/{run_id}/artifacts/{artifact['id']}",
    }


@app.get("/workspace")
async def workspace_index(request: Request) -> RedirectResponse:
    current_session(request)
    if auth_manager(request).must_change_password:
        return RedirectResponse("/change-password", status_code=303)
    database: Database = request.app.state.database
    async with database.sessions() as session:
        result = await session.execute(
            text("SELECT id FROM worker_runs ORDER BY id DESC LIMIT 1")
        )
        run_id = result.scalar_one_or_none()
    if run_id is None:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(f"/workspace/runs/{int(run_id)}", status_code=303)


@app.get("/workspace/runs/{run_id}/events")
async def workspace_run_events(run_id: int, request: Request) -> StreamingResponse:
    current_session(request)
    manager: WorkerRunManager = request.app.state.worker_manager
    current_status = await manager.latest_status(run_id)
    if current_status is None:
        raise HTTPException(status_code=404, detail="worker run not found")
    try:
        cursor_value = request.headers.get(
            "Last-Event-ID", request.query_params.get("event_cursor", "0")
        )
        cursor = max(0, int(cursor_value))
    except ValueError:
        cursor = 0
    try:
        chunk_cursor = max(0, int(request.query_params.get("chunk_cursor", "0")))
    except ValueError:
        chunk_cursor = 0
    return StreamingResponse(
        _run_event_stream(
            request, manager, run_id, cursor, chunk_cursor, current_status
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
    memory = MemoryStore(
        FakeEmbedder()
        if request.app.state.settings.chitti_provider == "fake"
        else get_embedder(request.app.state.settings.embedding_model)
    )
    async with database.sessions() as db_session:
        try:
            await memory.resolve_conflict(db_session, conflict_id, choice, session.username)
            await db_session.commit()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/", status_code=303)


@app.post("/plans/{revision_id}/approve")
async def approve_plan(revision_id: int, request: Request) -> RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    if auth_manager(request).must_change_password:
        return RedirectResponse("/change-password", status_code=303)
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    reason = str(form.get("reason", "")).strip() or None
    namespace = requested_namespace(request, str(form.get("namespace", "")) or None)
    database: Database = request.app.state.database
    async with database.sessions() as db_session:
        try:
            revision = await revision_by_id(db_session, revision_id, namespace)
            if revision is None:
                raise ValueError("plan revision not found")
            await approve_revision(db_session, revision_id, reason, namespace)
            await db_session.commit()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/", status_code=303)


@app.get("/plans/{revision_id}", response_class=HTMLResponse, response_model=None)
async def plan_page(revision_id: int, request: Request) -> HTMLResponse | RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    if auth_manager(request).must_change_password:
        return RedirectResponse("/change-password", status_code=303)
    namespace = requested_namespace(request)
    database: Database = request.app.state.database
    async with database.sessions() as db_session:
        revision = await revision_by_id(db_session, revision_id, namespace)
        if revision is None:
            raise HTTPException(status_code=404, detail="plan revision not found")
        approval_result = await db_session.execute(
            text(
                "SELECT decision, reason, content_hash, created_at "
                "FROM plan_approvals WHERE revision_id = :revision ORDER BY id DESC LIMIT 1"
            ),
            {"revision": revision_id},
        )
        approval = approval_result.mappings().one_or_none()
        events = await db_session.execute(
            text(
                "SELECT task_id, status FROM plan_task_events "
                "WHERE revision_id = :revision ORDER BY id"
            ),
            {"revision": revision_id},
        )
        task_statuses: dict[str, str] = {}
        for event in events:
            task_statuses[str(event.task_id)] = str(event.status)
        runs_result = await db_session.execute(
            text(
                "SELECT id FROM worker_runs WHERE revision_id = :revision ORDER BY id DESC"
            ),
            {"revision": revision_id},
        )
        runs = [
            await request.app.state.worker_manager.detail(int(row.id))
            for row in runs_result
        ]
        for run in runs:
            if run is None:
                continue
            promotion_result = await db_session.execute(
                text(
                    "SELECT m.id AS manifest_id, m.digest, m.total_bytes, "
                    "a.id AS approval_id, a.decision, a.approved_by, a.reason, "
                    "a.created_at AS approval_created_at, p.preview_id, p.expires_at "
                    "FROM export_manifests m "
                    "LEFT JOIN promotion_approvals a ON a.manifest_id = m.id "
                    "LEFT JOIN previews p ON p.manifest_id = m.id "
                    "WHERE m.run_id = :run_id"
                ),
                {"run_id": int(run["run"]["id"])},
            )
            run["promotion"] = promotion_result.mappings().one_or_none()
            promotion_event_result = await db_session.execute(
                text(
                    "SELECT status, detail FROM worker_run_events "
                    "WHERE run_id = :run_id AND status IN "
                    "('preview_failed', 'preview_blocked') "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"run_id": int(run["run"]["id"])},
            )
            run["promotion_event"] = promotion_event_result.mappings().one_or_none()
            reviewer_result = await db_session.execute(
                text(
                    "SELECT p.content FROM worker_artifacts a "
                    "JOIN worker_artifact_payloads p ON p.artifact_id = a.id "
                    "WHERE a.run_id = :run_id AND a.kind = 'reviewer_report' "
                    "ORDER BY a.id DESC LIMIT 1"
                ),
                {"run_id": int(run["run"]["id"])},
            )
            reviewer_payload = reviewer_result.scalar_one_or_none()
            if reviewer_payload is not None:
                try:
                    run["reviewer_verdict"] = json.loads(reviewer_payload)
                except (TypeError, json.JSONDecodeError):
                    run["reviewer_verdict"] = {"verdict": "invalid"}
    return templates.TemplateResponse(
        request=request,
        name="plan.html",
        context={
            "csrf_token": session.csrf_token,
            "namespace": revision.namespace,
            "revision": revision,
            "approval": dict(approval) if approval else None,
            "task_statuses": task_statuses,
            "runs": [run for run in runs if run is not None],
        },
    )


async def record_promotion_approval(
    db_session: AsyncSession,
    manifest: dict[str, object],
    *,
    actor: str,
    reason: str | None,
) -> None:
    if actor not in PROMOTION_APPROVAL_ACTORS:
        raise ValueError("invalid promotion approval actor")
    await db_session.execute(
        text(
            "INSERT INTO promotion_approvals "
            "(run_id, revision_id, revision_content_hash, manifest_id, "
            "reviewer_artifact_id, reviewer_sha256, diff_artifact_id, "
            "diff_sha256, manifest_digest, decision, reason, approved_by) VALUES "
            "(:run_id, :revision_id, :revision_hash, :manifest_id, "
            ":reviewer_id, :reviewer_sha256, :diff_id, :diff_sha256, "
            ":digest, 'approved', :reason, :actor)"
        ),
        {
            "run_id": manifest["run_id"],
            "revision_id": manifest["revision_id"],
            "revision_hash": manifest["revision_content_hash"],
            "manifest_id": manifest["manifest_id"],
            "reviewer_id": manifest["reviewer_artifact_id"],
            "reviewer_sha256": manifest["reviewer_sha256"],
            "diff_id": manifest["diff_artifact_id"],
            "diff_sha256": manifest["diff_sha256"],
            "digest": manifest["digest"],
            "reason": reason,
            "actor": actor,
        },
    )


@app.post("/runs/{run_id}/approve-result")
async def approve_result(run_id: int, request: Request) -> RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    reason = str(form.get("reason", "")).strip() or None
    database: Database = request.app.state.database
    async with database.sessions() as db_session:
        manifest_result = await db_session.execute(
            text(
                "SELECT r.revision_id, r.id AS run_id, e.revision_content_hash, "
                "e.id AS manifest_id, e.reviewer_artifact_id, e.diff_artifact_id, "
                "e.digest, e.revision_content_hash AS manifest_revision_hash, "
                "ra.sha256 AS reviewer_sha256, da.sha256 AS diff_sha256 "
                "FROM export_manifests e "
                "JOIN worker_runs r ON r.id = e.run_id "
                "JOIN worker_artifacts ra ON ra.id = e.reviewer_artifact_id "
                "JOIN worker_artifacts da ON da.id = e.diff_artifact_id "
                "WHERE e.run_id = :run_id"
            ),
            {"run_id": run_id},
        )
        manifest = manifest_result.mappings().one_or_none()
        if manifest is None:
            raise HTTPException(
                status_code=400,
                detail="this run is not promotable; static export evidence is missing",
            )
        try:
            await record_promotion_approval(
                db_session,
                dict(manifest),
                actor="owner",
                reason=reason,
            )
            await db_session.commit()
        except Exception as exc:
            await db_session.rollback()
            raise HTTPException(status_code=409, detail="result approval already exists") from exc
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


@app.post("/plans/{revision_id}/runs")
async def start_run(revision_id: int, request: Request) -> RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    if auth_manager(request).must_change_password:
        return RedirectResponse("/change-password", status_code=303)
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    namespace = requested_namespace(request, str(form.get("namespace", "")) or None)
    manager: WorkerRunManager = request.app.state.worker_manager
    try:
        run_id = await manager.enqueue(revision_id, WorkerLimits(), namespace)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(
        f"/plans/{revision_id}?run={run_id}&namespace={namespace}", status_code=303
    )


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int, request: Request) -> RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    await request.app.state.worker_manager.cancel(run_id)
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


@app.get("/runs/{run_id}")
async def run_detail(run_id: int, request: Request) -> dict[str, object]:
    current_session(request)
    detail = await request.app.state.worker_manager.detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="worker run not found")
    return cast(dict[str, object], detail)


@app.get("/workspace/runs/{run_id}/operations/{operation_index}")
async def workspace_operation(
    run_id: int, operation_index: int, request: Request
) -> dict[str, object]:
    current_session(request)
    database: Database = request.app.state.database
    async with database.sessions() as session:
        operation_result = await session.execute(
            text(
                "SELECT id, task_id, operation_index, name, status, stdout, stderr, "
                "exit_code, started_at, finished_at "
                "FROM worker_operations WHERE run_id = :run_id "
                "AND operation_index = :operation_index"
            ),
            {"run_id": run_id, "operation_index": operation_index},
        )
        operation = operation_result.mappings().one_or_none()
        if operation is None:
            raise HTTPException(status_code=404, detail="operation not found")
        artifacts_result = await session.execute(
            text(
                "SELECT id, kind FROM worker_artifacts "
                "WHERE run_id = :run_id AND operation_id = :operation_id"
            ),
            {"run_id": run_id, "operation_id": operation["id"]},
        )
    output_artifacts = {
        str(row["kind"]): int(str(row["id"])) for row in artifacts_result.mappings()
    }
    return {
        "operation": {
            **dict(operation),
            "stdout": str(operation["stdout"])[-4000:],
            "stderr": str(operation["stderr"])[-4000:],
            "stdout_artifact_id": output_artifacts.get("stdout"),
            "stderr_artifact_id": output_artifacts.get("stderr"),
        }
    }


@app.get("/runs/{run_id}/artifacts/{artifact_id}")
async def worker_artifact(run_id: int, artifact_id: int, request: Request) -> Response:
    current_session(request)
    database: Database = request.app.state.database
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT a.kind, p.content FROM worker_artifacts a "
                "LEFT JOIN worker_artifact_payloads p ON p.artifact_id = a.id "
                "WHERE a.id = :artifact AND a.run_id = :run"
            ),
            {"artifact": artifact_id, "run": run_id},
        )
        artifact = result.mappings().one_or_none()
    if artifact is None:
        raise HTTPException(status_code=404, detail="worker artifact not found")
    if artifact["content"] is None:
        raise HTTPException(status_code=410, detail="artifact payload has expired")
    kind = str(artifact["kind"])
    media_type = "image/png" if kind == "screenshot" else "text/plain"
    return Response(content=bytes(artifact["content"]), media_type=media_type)


@app.get("/previews/{preview_id}/{path:path}")
async def preview_file(preview_id: str, path: str, request: Request) -> StreamingResponse:
    current_session(request)
    database: Database = request.app.state.database
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT 1 FROM previews "
                "WHERE preview_id = :preview_id AND expires_at > now()"
            ),
            {"preview_id": preview_id},
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="preview not found")
    candidates = [path]
    if not path or path.endswith("/"):
        candidates.insert(0, f"{path}index.html")
    file_descriptor = None
    filename = ""
    for candidate in candidates:
        try:
            file_descriptor, filename = safe_preview_file(
                Path(request.app.state.settings.preview_root), preview_id, candidate
            )
            break
        except (OSError, ValueError):
            continue
    if file_descriptor is None:
        raise HTTPException(status_code=404, detail="preview file not found")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return StreamingResponse(os.fdopen(file_descriptor, "rb"), media_type=media_type)


@app.post("/plans/{revision_id}/reject")
async def reject_plan(revision_id: int, request: Request) -> RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    if auth_manager(request).must_change_password:
        return RedirectResponse("/change-password", status_code=303)
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    reason = str(form.get("reason", "")).strip()
    database: Database = request.app.state.database
    async with database.sessions() as db_session:
        namespace = requested_namespace(request, str(form.get("namespace", "")) or None)
        revision = await revision_by_id(db_session, revision_id, namespace)
        if revision is None:
            raise HTTPException(status_code=404, detail="plan revision not found")
        try:
            await reject_revision(db_session, revision_id, reason, namespace)
            await db_session.commit()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    manager: PlanManager = request.app.state.plan_manager
    await manager.enqueue(
        revision.project,
        revision.brief,
        revision_id,
        reason,
        revision.namespace,
    )
    return RedirectResponse("/", status_code=303)


@app.post("/memory/decisions/{decision_id}/forget")
async def forget_decision(decision_id: int, request: Request) -> RedirectResponse:
    result = browser_session(request)
    if isinstance(result, RedirectResponse):
        return result
    _, session = result
    if auth_manager(request).must_change_password:
        return RedirectResponse("/change-password", status_code=303)
    form = await request.form()
    require_csrf(request, session, str(form.get(CSRF_FIELD, "")))
    database: Database = request.app.state.database
    memory = MemoryStore(
        FakeEmbedder()
        if request.app.state.settings.chitti_provider == "fake"
        else get_embedder(request.app.state.settings.embedding_model)
    )
    async with database.sessions() as db_session:
        try:
            await memory.forget_decision(db_session, decision_id)
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
    namespace = requested_namespace(request, payload.namespace)
    database: Database = request.app.state.database
    service: ChittiService = request.app.state.service
    plan_project = project_from_brief(payload.project, payload.plan_requested)
    if payload.plan_requested and plan_project is None:
        raise HTTPException(
            status_code=400, detail="a named project is required for planning"
        )
    try:
        async with database.sessions() as db_session:
            run_evidence = (
                await build_run_evidence(db_session, payload.run_id, namespace)
                if payload.run_id is not None
                else None
            )
            result = await service.turn(
                db_session,
                payload.message,
                payload.project,
                payload.history,
                namespace,
                run_evidence,
            )
    except RunContextError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logging.getLogger(__name__).exception("chat_provider_failed")
        raise HTTPException(status_code=503, detail="model provider unavailable") from exc
    plan_job_id = None
    reply = result.reply
    if plan_project:
        manager: PlanManager = request.app.state.plan_manager
        plan_job_id = await manager.enqueue(
            plan_project, payload.message, namespace=namespace
        )
        reply += (
            "\n\nI created a plan draft for "
            f"{plan_project}. It is waiting for your approval; nothing will execute."
        )
    response: dict[str, object] = {
        "reply": reply,
        "reply_html": render_markdown(reply),
        "conflicts": [asdict(item) for item in result.conflicts],
        "plan_job_id": plan_job_id,
    }
    if run_evidence is not None:
        response["evidence_used"] = list(run_evidence.evidence_used)
        response["evidence_clipped"] = run_evidence.clipped
    return response


@app.get("/plans/jobs/{job_id}")
async def plan_job(job_id: int, request: Request) -> dict[str, object]:
    current_session(request)
    if auth_manager(request).must_change_password:
        raise HTTPException(status_code=403, detail="password change required")
    manager: PlanManager = request.app.state.plan_manager
    job = await manager.job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="planning job not found")
    return job


@app.get("/projects/{project}/state")
async def project_state(project: str, request: Request) -> dict[str, str]:
    current_session(request)
    if auth_manager(request).must_change_password:
        raise HTTPException(status_code=403, detail="password change required")
    state = cast(ProjectState, request.app.state.project_state)
    return state.read(project)
