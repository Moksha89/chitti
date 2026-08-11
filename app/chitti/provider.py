import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

CODER_ROUTE = "coder"
REVIEWER_ROUTE = "reviewer"
REQUIRED_GATEWAY_ROUTES = frozenset({CODER_ROUTE, REVIEWER_ROUTE})
CODER_MAX_OUTPUT_TOKENS = 8192


class GatewayValidationError(RuntimeError):
    """Base error for runner gateway preflight failures."""


class GatewayMisconfigurationError(GatewayValidationError):
    """The configured gateway credential or routes cannot be used."""


class GatewayTransientError(GatewayValidationError):
    """The gateway could not be checked due to a temporary failure."""


@dataclass(frozen=True)
class ExtractedMemory:
    key: str
    value: str
    rationale: str | None
    project: str | None
    source: str = "chitti_inferred"


@dataclass(frozen=True)
class ModelCompletion:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    finish_reason: str | None = None
    message_fields: tuple[str, ...] = ()


class ModelProvider(Protocol):
    async def validate_gateway(self) -> None: ...

    async def chat(self, system: str, messages: list[dict[str, str]], role: str) -> str: ...

    async def plan(
        self, brief: str, project: str, beliefs: list[dict[str, object]], rejection: str | None = None
    ) -> str: ...

    async def extract_memories(
        self,
        profile: str,
        user_message: str,
        assistant_message: str,
        existing_keys: list[str] | None = None,
    ) -> list[ExtractedMemory]: ...

    async def agent_completion(
        self, messages: list[dict[str, str]], role: str
    ) -> ModelCompletion: ...


def _diagnostic_message_fields(message: dict[object, object]) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(field)
            for field, value in message.items()
            if field not in {"content", "role"}
            and value not in (None, "", [], {})
        )
    )


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
        self.base_url = base_url.rstrip("/")
        self.url = self.base_url + "/v1/chat/completions"
        self.api_key = api_key

    async def validate_gateway(self) -> None:
        if not self.api_key.strip():
            raise GatewayMisconfigurationError("gateway credential is missing")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    self.base_url + "/v1/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise GatewayTransientError("gateway did not respond during preflight") from exc
        except httpx.HTTPError as exc:
            raise GatewayTransientError("gateway request failed during preflight") from exc
        if response.status_code in {401, 403}:
            raise GatewayMisconfigurationError("gateway credential was rejected")
        if response.status_code >= 500:
            raise GatewayTransientError(
                f"gateway returned HTTP {response.status_code} during preflight"
            )
        if response.status_code >= 400:
            raise GatewayMisconfigurationError(
                f"gateway rejected the preflight request with HTTP {response.status_code}"
            )
        try:
            payload = response.json()
            model_ids = {
                str(item["id"])
                for item in payload.get("data", [])
                if isinstance(item, dict) and "id" in item
            }
        except (TypeError, ValueError, AttributeError) as exc:
            raise GatewayMisconfigurationError(
                "gateway returned an invalid model list"
            ) from exc
        missing = sorted(REQUIRED_GATEWAY_ROUTES - model_ids)
        if missing:
            raise GatewayMisconfigurationError(
                f"gateway routes unavailable: {', '.join(missing)}"
            )

    async def _completion(self, messages: list[dict[str, str]], role: str) -> str:
        return (await self.agent_completion(messages, role)).content

    async def agent_completion(
        self, messages: list[dict[str, str]], role: str
    ) -> ModelCompletion:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": role,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": 1200 if role == "reviewer" else CODER_MAX_OUTPUT_TOKENS,
                    "thinking": {"type": "disabled"},
                },
            )
            response.raise_for_status()
            body = response.json()
            choice = body["choices"][0]
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                message = {}
            content = message.get("content")
            usage = body.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
            total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens))
            cost = body.get("cost", response.headers.get("x-litellm-response-cost", 0.0))
            return ModelCompletion(
                content=content if isinstance(content, str) else "",
                model=str(body.get("model", role)),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=float(cost or 0.0),
                finish_reason=(
                    str(choice["finish_reason"])
                    if choice.get("finish_reason") is not None
                    else None
                ),
                message_fields=_diagnostic_message_fields(message),
            )

    async def chat(self, system: str, messages: list[dict[str, str]], role: str) -> str:
        return await self._completion([{"role": "system", "content": system}, *messages], role)

    async def plan(
        self, brief: str, project: str, beliefs: list[dict[str, object]], rejection: str | None = None
    ) -> str:
        memory = "\n".join(
            f"- {item['decision_key']}: {item['decision']}" for item in beliefs
        ) or "(none)"
        feedback = f"\nREJECTION FEEDBACK:\n{rejection}" if rejection else ""
        prompt = (
            "Create a delivery plan as strict JSON with exactly these top-level keys: "
            "title, summary, tasks, memory_decisions. Each task must have id, title, "
            "description, dependencies, and done_condition. Dependencies are task ids. "
            "Tasks must be ordered and independently testable. memory_decisions must list "
            "which supplied beliefs influenced the plan, with decision_key and influence. "
            "Do not include markdown or extra keys.\n"
            f"PROJECT: {project}\nBRIEF: {brief}\nACTIVE BELIEFS:\n{memory}{feedback}"
        )
        return await self._completion(
            [
                {"role": "system", "content": "You create validated project plans as strict JSON."},
                {"role": "user", "content": prompt},
            ],
            "planner",
        )

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
    async def validate_gateway(self) -> None:
        return

    async def chat(self, system: str, messages: list[dict[str, str]], role: str) -> str:
        latest = messages[-1]["content"] if messages else ""
        return f"[fake:{role}] I heard you: {latest}"

    async def plan(
        self, brief: str, project: str, beliefs: list[dict[str, object]], rejection: str | None = None
    ) -> str:
        return json.dumps(
            {
                "title": f"{project}: {brief[:80]}",
                "summary": f"Deliver the requested project for {project}.",
                "memory_decisions": [
                    {
                        "decision_key": str(item["decision_key"]),
                        "influence": "Applied as a project constraint.",
                    }
                    for item in beliefs
                ],
                "tasks": [
                    {
                        "id": "brief",
                        "title": "Turn the brief into an implementation checklist",
                        "description": brief,
                        "dependencies": [],
                        "done_condition": "The checklist is explicit, ordered, and testable.",
                    },
                    {
                        "id": "review",
                        "title": "Review the proposed delivery plan",
                        "description": "Confirm scope, constraints, and acceptance criteria.",
                        "dependencies": ["brief"],
                        "done_condition": "The owner has approved the exact plan revision.",
                    },
                ],
            }
        )

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

    async def agent_completion(
        self, messages: list[dict[str, str]], role: str
    ) -> ModelCompletion:
        return ModelCompletion(
            content=json.dumps(
                {"tool": "finish", "arguments": {"summary": "Fake provider completed the task."}}
            ),
            model=f"fake:{role}",
            prompt_tokens=sum(len(item["content"]) for item in messages),
            completion_tokens=12,
            total_tokens=sum(len(item["content"]) for item in messages) + 12,
            cost_usd=0.0,
        )
