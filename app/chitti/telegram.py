import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from .service import ChittiService
from .settings import Settings

logger = logging.getLogger(__name__)


class TelegramPoller:
    def __init__(
        self,
        settings: Settings,
        service: ChittiService,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self.settings = settings
        self.service = service
        self.session_factory = session_factory
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._offset = 0

    def start(self) -> None:
        if self.settings.telegram_bot_token:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def run(self) -> None:
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/getUpdates"
        async with httpx.AsyncClient(timeout=35) as client:
            while not self._stop.is_set():
                try:
                    response = await client.get(
                        url, params={"timeout": 25, "offset": self._offset + 1}
                    )
                    response.raise_for_status()
                    payload: Any = response.json()
                    for update in payload.get("result", []):
                        self._offset = max(self._offset, int(update["update_id"]))
                        message = update.get("message", {})
                        chat_id = message.get("chat", {}).get("id")
                        if chat_id not in self.settings.allowed_ids:
                            continue
                        text = message.get("text")
                        if not text:
                            continue
                        async with self.session_factory() as session:
                            result = await self.service.turn(session, text, "telegram")
                        await client.post(
                            f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": result.reply},
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("telegram_poll_failed")
                    await asyncio.sleep(5)
