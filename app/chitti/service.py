import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from .memory import Conflict, MemoryStore, Recall
from .provider import ModelProvider
from .run_context import RunEvidence
from .transcripts import append_entry

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

    def system_prompt(
        self, recall: list[Recall], run_evidence: RunEvidence | None = None
    ) -> str:
        recalled = "\n".join(f"- {item.content}" for item in recall) or "(none)"
        stable_prefix = (
            "You are Chitti, a careful personal assistant. You talk, remember, and plan, "
            "but you do not write code or dispatch workers in Phase 1. "
            "The following identity profile is authoritative and must be included verbatim:\n"
            f"{self.profile}"
        )
        prompt = f"{stable_prefix}\n\nRelevant semantic recall:\n{recalled}"
        if run_evidence is not None:
            prompt += (
                "\n\nRun evidence supplied by the server. Answer only from this evidence "
                "and say that the evidence does not contain an answer when it does not. "
                "Do not infer missing details or claim to have inspected files outside it. "
                "Name the evidence sections used in your answer.\n"
                f"{run_evidence.context}"
            )
        return prompt

    async def turn(
        self,
        session: AsyncSession,
        user_message: str,
        project: str | None,
        history: list[dict[str, str]] | None = None,
        namespace: str = "general",
        run_evidence: RunEvidence | None = None,
    ) -> TurnResult:
        recall = await self.memory.recall(session, user_message, namespace)
        messages: list[dict[str, object]] = [
            {"role": item["role"], "content": item["content"]}
            for item in history or []
        ]
        messages.append({"role": "user", "content": user_message})
        reply = await self.provider.chat(
            self.system_prompt(recall, run_evidence), messages, "chitti-chat"
        )
        if run_evidence is not None:
            clipped = (
                f" Clipped categories: {', '.join(run_evidence.clipped_sections)}."
                if run_evidence.clipped_sections
                else ""
            )
            reply += (
                f"\n\nEvidence used: {', '.join(run_evidence.evidence_used)}."
                + (" Context was clipped to the server evidence bound." + clipped
                   if run_evidence.clipped else "")
            )
        await self.memory.add_chunk(
            session,
            user_message,
            "conversation_user",
            project,
            {"project": project},
            namespace,
        )
        await append_entry(session, namespace, "user", user_message)
        await append_entry(session, namespace, "assistant", reply)
        await self.memory.add_chunk(
            session,
            reply,
            "conversation_assistant",
            project,
            {"project": project},
            namespace,
        )
        existing_keys = await self.memory.active_keys(session, namespace)
        extracted = await self.provider.extract_memories(
            self.profile, user_message, reply, existing_keys
        )
        conflicts = await self.memory.record_memories(session, extracted, namespace)
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
