import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

import httpx

logger = logging.getLogger(__name__)

CODER_ROUTE = "coder"
REVIEWER_ROUTE = "reviewer"
BULK_ROUTE = "bulk"
VISION_ROUTE = "vision"
DEPLOYMENT_GATEWAY_ROUTES = frozenset(
    {"chitti-chat", "planner", CODER_ROUTE, REVIEWER_ROUTE, BULK_ROUTE, VISION_ROUTE}
)
REQUIRED_GATEWAY_ROUTES = frozenset(
    {CODER_ROUTE, REVIEWER_ROUTE, BULK_ROUTE, VISION_ROUTE}
)
CODER_MAX_OUTPUT_TOKENS = 32768
REVIEWER_MAX_OUTPUT_TOKENS = 4096
VISION_MAX_OUTPUT_TOKENS = 1024
VISION_INPUT_COST_PER_TOKEN = 0.00000004
VISION_OUTPUT_COST_PER_TOKEN = 0.0000004
MODEL_GATEWAY_TIMEOUT_SECONDS = 600
MODEL_CLIENT_TIMEOUT_SECONDS = 660


class GatewayValidationError(RuntimeError):
    """Base error for runner gateway preflight failures."""


class GatewayMisconfigurationError(GatewayValidationError):
    """The configured gateway credential or routes cannot be used."""


class GatewayTransientError(GatewayValidationError):
    """The gateway could not be checked due to a temporary failure."""


class PlannerBrandProfile(Protocol):
    namespace: str
    brand_colors: tuple[str, ...]
    typography: str
    poster_formats: tuple[str, ...]
    audience: str
    voice: str
    do_not_use: tuple[str, ...]
    updated_by: str
    updated_at: datetime


class ModelTransportError(RuntimeError):
    """The model gateway could not complete a request over the network."""


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
    reasoning_tokens: int = 0
    finish_reason: str | None = None
    message_fields: tuple[str, ...] = ()
    tool_calls: tuple["ModelToolCall", ...] = ()


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, object]


class ModelProvider(Protocol):
    async def validate_gateway(self) -> None: ...

    async def chat(self, system: str, messages: list[dict[str, object]], role: str) -> str: ...

    async def plan(
        self,
        brief: str,
        project: str,
        beliefs: list[dict[str, object]],
        rejection: str | None = None,
        job_type: str = "website",
        job_config: object | None = None,
        brand_profile: object | None = None,
    ) -> str: ...

    async def extract_memories(
        self,
        profile: str,
        user_message: str,
        assistant_message: str,
        existing_keys: list[str] | None = None,
    ) -> list[ExtractedMemory]: ...

    async def agent_completion(
        self,
        messages: list[dict[str, object]],
        role: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
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


def _extract_tool_calls(message: dict[object, object]) -> tuple[ModelToolCall, ...]:
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return ()
    calls: list[ModelToolCall] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            raise ValueError("model tool call was not an object")
        function = raw_call.get("function")
        if not isinstance(function, dict):
            raise ValueError("model tool call did not contain a function")
        name = function.get("name")
        raw_arguments = function.get("arguments", "{}")
        if not isinstance(name, str) or not name:
            raise ValueError("model tool call did not contain a function name")
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise ValueError("model tool call arguments were not valid JSON") from exc
        else:
            arguments = raw_arguments
        if not isinstance(arguments, dict):
            raise ValueError("model tool call arguments must be an object")
        call_id = raw_call.get("id")
        calls.append(
            ModelToolCall(
                id=str(call_id) if call_id else f"call-{len(calls)}",
                name=name,
                arguments={str(key): value for key, value in arguments.items()},
            )
        )
    return tuple(calls)


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
        for route in sorted(REQUIRED_GATEWAY_ROUTES):
            try:
                completion = await self.agent_completion(
                    [{"role": "user", "content": "Return exactly OK."}],
                    route,
                )
            except Exception as exc:
                raise GatewayMisconfigurationError(
                    f"gateway route failed during preflight: {route}"
                ) from exc
            if completion.total_tokens < 1:
                raise GatewayMisconfigurationError(
                    f"gateway route returned no usage during preflight: {route}"
                )

    async def _completion(self, messages: list[dict[str, object]], role: str) -> str:
        return (await self.agent_completion(messages, role)).content

    async def agent_completion(
        self,
        messages: list[dict[str, object]],
        role: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ModelCompletion:
        request: dict[str, object] = {
            "model": role,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": (
                REVIEWER_MAX_OUTPUT_TOKENS
                if role == "reviewer"
                else VISION_MAX_OUTPUT_TOKENS
                if role == VISION_ROUTE
                else CODER_MAX_OUTPUT_TOKENS
            ),
        }
        if tools is not None:
            request["tools"] = tools
        if tool_choice is not None:
            request["tool_choice"] = tool_choice
        try:
            async with httpx.AsyncClient(timeout=MODEL_CLIENT_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    self.url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=request,
                )
        except httpx.TimeoutException as exc:
            raise ModelTransportError(
                f"gateway request timed out after {MODEL_GATEWAY_TIMEOUT_SECONDS} seconds"
            ) from exc
        except httpx.NetworkError as exc:
            raise ModelTransportError("gateway network request failed") from exc
        except httpx.HTTPError:
            raise
        except Exception as exc:
            raise ModelTransportError("gateway transport request failed") from exc
        response.raise_for_status()
        body = response.json()
        choice = body["choices"][0]
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        content = message.get("content")
        native_tool_calls = _extract_tool_calls(message)
        usage = body.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens))
        completion_details = usage.get("completion_tokens_details") or {}
        reasoning_tokens = (
            int(completion_details.get("reasoning_tokens", 0))
            if isinstance(completion_details, dict)
            else 0
        )
        raw_cost = body.get("cost", response.headers.get("x-litellm-response-cost"))
        if raw_cost is None:
            if role != VISION_ROUTE:
                cost = 0.0
            elif prompt_tokens + completion_tokens < 1:
                raise ModelTransportError(
                    "gateway response did not include usable model cost"
                )
            else:
                cost = (
                    prompt_tokens * VISION_INPUT_COST_PER_TOKEN
                    + completion_tokens * VISION_OUTPUT_COST_PER_TOKEN
                )
        else:
            try:
                cost = float(raw_cost)
            except (TypeError, ValueError) as exc:
                raise ModelTransportError(
                    "gateway response contained invalid model cost"
                ) from exc
        if role == VISION_ROUTE and cost <= 0:
            raise ModelTransportError("vision response did not include usable model cost")
        return ModelCompletion(
            content=content if isinstance(content, str) else "",
            model=str(body.get("model", role)),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=float(cost or 0.0),
            reasoning_tokens=reasoning_tokens,
            finish_reason=(
                str(choice["finish_reason"])
                if choice.get("finish_reason") is not None
                else None
            ),
            message_fields=_diagnostic_message_fields(message),
            tool_calls=native_tool_calls,
        )

    async def chat(self, system: str, messages: list[dict[str, object]], role: str) -> str:
        return await self._completion([{"role": "system", "content": system}, *messages], role)

    async def plan(
        self,
        brief: str,
        project: str,
        beliefs: list[dict[str, object]],
        rejection: str | None = None,
        job_type: str = "website",
        job_config: object | None = None,
        brand_profile: object | None = None,
    ) -> str:
        memory = "\n".join(
            f"- {item['decision_key']}: {item['decision']}" for item in beliefs
        ) or "(none)"
        feedback = f"\nREJECTION FEEDBACK:\n{rejection}" if rejection else ""
        format_text = json.dumps(job_config or {}, default=str)
        if brand_profile is None:
            profile_data: dict[str, object] = {}
        elif hasattr(brand_profile, "namespace"):
            profile = cast(PlannerBrandProfile, brand_profile)
            profile_data = {
                "namespace": profile.namespace,
                "brand_colors": list(profile.brand_colors),
                "typography": profile.typography,
                "poster_formats": list(profile.poster_formats),
                "audience": profile.audience,
                "voice": profile.voice,
                "do_not_use": list(profile.do_not_use),
                "updated_by": profile.updated_by,
                "updated_at": profile.updated_at,
            }
        else:
            raise TypeError("planner brand profile must expose its known fields")
        profile_text = json.dumps(profile_data, default=str)
        work_guidance = (
            "Plan one offline poster artifact bound to the supplied brand profile. "
            "Tasks must cover artifact authoring, poster-export, and capture_screenshot; "
            "do not plan npm, framework, website build, or website test work."
            if job_type == "poster"
            else
            "Plan the requested website delivery using the appropriate implementation, "
            "build, test, and export tasks."
        )
        prompt = (
            "Create a delivery plan as strict JSON with exactly these top-level keys: "
            "title, summary, tasks, memory_decisions. Each task must have id, title, "
            "description, dependencies, and done_condition. Task ids and dependency "
            "ids must be JSON strings, for example id \"T1\" and dependencies [\"T1\"], "
            "never numbers. Dependencies are task ids. "
            "Tasks must be ordered and independently testable. memory_decisions must list "
            "which supplied beliefs influenced the plan, with decision_key and influence. "
            "Do not include markdown or extra keys.\n"
            f"PROJECT: {project}\nJOB TYPE: {job_type}\n"
            f"APPROVED FORMAT: {format_text}\n"
            f"NAMESPACE BRAND PROFILE: {profile_text}\n"
            f"WORK TYPE GUIDANCE: {work_guidance}\n"
            f"BRIEF: {brief}\nACTIVE BELIEFS:\n{memory}{feedback}"
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

    async def chat(self, system: str, messages: list[dict[str, object]], role: str) -> str:
        latest = messages[-1]["content"] if messages else ""
        return f"[fake:{role}] I heard you: {latest}"

    async def plan(
        self,
        brief: str,
        project: str,
        beliefs: list[dict[str, object]],
        rejection: str | None = None,
        job_type: str = "website",
        job_config: object | None = None,
        brand_profile: object | None = None,
    ) -> str:
        poster = job_type == "poster"
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
                        "title": (
                            "Author the offline poster artifact"
                            if poster
                            else "Turn the brief into an implementation checklist"
                        ),
                        "description": (
                            f"Create the approved poster artifact: {json.dumps(job_config)}"
                            if poster
                            else brief
                        ),
                        "dependencies": [],
                        "done_condition": "The checklist is explicit, ordered, and testable.",
                    },
                    {
                        "id": "review",
                        "title": (
                            "Export and capture the poster"
                            if poster
                            else "Review the proposed delivery plan"
                        ),
                        "description": (
                            "Run poster-export and capture_screenshot using the approved format."
                            if poster
                            else "Confirm scope, constraints, and acceptance criteria."
                        ),
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
        self,
        messages: list[dict[str, object]],
        role: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ModelCompletion:
        if role != "coder":
            content = json.dumps(
                {
                    "verdict": "pass",
                    "findings": [],
                    "evidence_limitations": [],
                    "summary": "Fake provider reviewer passed the task.",
                }
            )
            prompt_tokens = sum(
                len(item_content)
                for item in messages
                if isinstance(item_content := item.get("content"), str)
            )
            return ModelCompletion(
                content=content,
                model=f"fake:{role}",
                prompt_tokens=prompt_tokens,
                completion_tokens=12,
                total_tokens=prompt_tokens + 12,
                cost_usd=0.0,
                finish_reason="stop",
            )
        prompt_tokens = sum(
            len(item_content)
            for item in messages
            if isinstance(item_content := item.get("content"), str)
        )
        return ModelCompletion(
            content="",
            model=f"fake:{role}",
            prompt_tokens=prompt_tokens,
            completion_tokens=12,
            total_tokens=prompt_tokens + 12,
            cost_usd=0.0,
            finish_reason="tool_calls",
            tool_calls=(
                ModelToolCall(
                    id="fake-finish",
                    name="finish",
                    arguments={"summary": "Fake provider completed the task."},
                ),
            ),
        )
