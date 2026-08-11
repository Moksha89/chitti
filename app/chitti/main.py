import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import cast

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    database = Database(settings)
    service = build_service(settings)
    poller = TelegramPoller(settings, service, database.sessions)
    poller.start()
    app.state.settings = settings
    app.state.database = database
    app.state.service = service
    app.state.project_state = ProjectState(settings.project_root)
    try:
        yield
    finally:
        await poller.stop()
        await database.close()


app = FastAPI(title="Chitti", version="0.1.0", lifespan=lifespan)


@app.get("/health")  # type: ignore[misc]
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat")  # type: ignore[misc]
async def chat(payload: ChatRequest, request: Request) -> dict[str, object]:
    database: Database = request.app.state.database
    service: ChittiService = request.app.state.service
    try:
        async with database.sessions() as session:
            result = await service.turn(session, payload.message, payload.project, payload.history)
    except Exception as exc:
        logging.getLogger(__name__).exception("chat_provider_failed")
        raise HTTPException(status_code=503, detail="model provider unavailable") from exc
    return {"reply": result.reply, "conflicts": [asdict(item) for item in result.conflicts]}


@app.get("/projects/{project}/state")  # type: ignore[misc]
async def project_state(project: str, request: Request) -> dict[str, str]:
    state = cast(ProjectState, request.app.state.project_state)
    return state.read(project)
