import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from .memory import Conflict, MemoryStore, Recall
from .provider import ModelProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnResult:
    reply: str
    conflicts: list[Conflict]
    recall: list[Recall]


class ChittiService:
    def __init__(self, provider: ModelProvider, memory: MemoryStore, profile: str) -> None:
        self.provider = provider
        self.memory = memory
        self.profile = profile

    def system_prompt(self, recall: list[Recall]) -> str:
        recalled = "\n".join(f"- {item.content}" for item in recall) or "(none)"
        stable_prefix = (
            "You are Chitti, a careful personal assistant. You talk, remember, and plan, "
            "but you do not write code or dispatch workers in Phase 1. "
            "The following identity profile is authoritative and must be included verbatim:\n"
            f"{self.profile}"
        )
        return f"{stable_prefix}\n\nRelevant semantic recall:\n{recalled}"

    async def turn(
        self,
        session: AsyncSession,
        user_message: str,
        project: str | None,
        history: list[dict[str, str]] | None = None,
    ) -> TurnResult:
        recall = await self.memory.recall(session, user_message)
        messages: list[dict[str, object]] = [
            {"role": item["role"], "content": item["content"]}
            for item in history or []
        ]
        messages.append({"role": "user", "content": user_message})
        reply = await self.provider.chat(self.system_prompt(recall), messages, "chitti-chat")
        await self.memory.add_chunk(
            session, user_message, "conversation_user", project, {"project": project}
        )
        await self.memory.add_chunk(
            session, reply, "conversation_assistant", project, {"project": project}
        )
        existing_keys = await self.memory.active_keys(session)
        extracted = await self.provider.extract_memories(
            self.profile, user_message, reply, existing_keys
        )
        conflicts = await self.memory.record_memories(session, extracted)
        await session.commit()
        if conflicts:
            logger.info("memory_conflict", extra={"keys": [item.key for item in conflicts]})
            reply += (
                "\n\nI found a conflicting memory. Please tell me which version to keep: "
                + "; ".join(
                    f"{item.key}: existing “{item.existing}” vs proposed “{item.proposed}”"
                    for item in conflicts
                )
            )
        return TurnResult(reply, conflicts, recall)
