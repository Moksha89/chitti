import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractedMemory:
    key: str
    value: str
    rationale: str | None
    project: str | None
    source: str = "chitti_inferred"


class ModelProvider(Protocol):
    async def chat(self, system: str, messages: list[dict[str, str]], role: str) -> str: ...

    async def extract_memories(
        self,
        profile: str,
        user_message: str,
        assistant_message: str,
        existing_keys: list[str] | None = None,
    ) -> list[ExtractedMemory]: ...


def _json_payload(text: str) -> object:
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return []
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.warning("memory extraction returned invalid JSON")
        return []


class LiteLLMProvider:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.api_key = api_key

    async def _completion(self, messages: list[dict[str, str]], role: str) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": role, "messages": messages, "temperature": 0.2},
            )
            response.raise_for_status()
            body = response.json()
            return str(body["choices"][0]["message"]["content"])

    async def chat(self, system: str, messages: list[dict[str, str]], role: str) -> str:
        return await self._completion([{"role": "system", "content": system}, *messages], role)

    async def extract_memories(
        self,
        profile: str,
        user_message: str,
        assistant_message: str,
        existing_keys: list[str] | None = None,
    ) -> list[ExtractedMemory]:
        keys = ", ".join(existing_keys or []) or "(none)"
        prompt = (
            "Extract only durable facts, preferences, or decisions from this turn. "
            "Return a JSON array of objects with key, value, rationale, project, source. "
            "Write each value as a complete, self-contained direct statement of the rule "
            "that a person can understand without the key. Use first person or imperative "
            "wording as appropriate; do not write 'The user ...'. Never return a bare time, color, product name, "
            "framework name, version, or code; include the subject while preserving the "
            "exact value. "
            "Use source user_stated when the user explicitly states it, otherwise "
            "chitti_inferred. Return [] when there is nothing durable. "
            "When a fact matches an existing key, reuse that key exactly instead of "
            "creating a synonym. Existing active keys:\n"
            f"{keys}\n"
            f"PROFILE:\n{profile}\nUSER:\n{user_message}\nASSISTANT:\n{assistant_message}"
        )
        raw = await self._completion(
            [
                {"role": "system", "content": "You extract durable memory as strict JSON."},
                {"role": "user", "content": prompt},
            ],
            "planner",
        )
        result = _json_payload(raw)
        if not isinstance(result, list):
            return []
        return [
            ExtractedMemory(
                key=str(item["key"]),
                value=str(item["value"]),
                rationale=str(item["rationale"]) if item.get("rationale") else None,
                project=str(item["project"]) if item.get("project") else None,
                source=str(item.get("source", "chitti_inferred")),
            )
            for item in result
            if isinstance(item, dict) and item.get("key") and item.get("value")
        ]


class FakeProvider:
    async def chat(self, system: str, messages: list[dict[str, str]], role: str) -> str:
        latest = messages[-1]["content"] if messages else ""
        return f"[fake:{role}] I heard you: {latest}"

    async def extract_memories(
        self,
        profile: str,
        user_message: str,
        assistant_message: str,
        existing_keys: list[str] | None = None,
    ) -> list[ExtractedMemory]:
        lowered = user_message.lower()
        memories: list[ExtractedMemory] = []
        if "prefer" in lowered:
            value = user_message.split("prefer", 1)[1].strip(" .")
            memories.append(
                ExtractedMemory(
                    "preference", value, "User stated a preference.", None, "user_stated"
                )
            )
        if "always use" in lowered:
            value = user_message.split("always use", 1)[1].strip(" .")
            memories.append(
                ExtractedMemory("hard_rule", value, "User stated a hard rule.", None, "user_stated")
            )
        return memories
