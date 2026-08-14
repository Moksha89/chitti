from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from sqlalchemy import text

from .runner_access import runner_sql
from .settings import get_settings

WORKFLOW_TEMPLATE_ID = "sdxl-base-v1"
WORKER_IMAGE = "runpod/worker-comfyui:5.8.6-sdxl"
MAX_DIMENSION = 1024
MAX_PROMPT = 4000
RUNPOD_HTTP_TIMEOUT_SECONDS = 30
RUNPOD_POLL_INTERVAL_SECONDS = 5
# Ten minutes covers scale-to-zero SDXL cold starts while remaining bounded
# well inside the runner's two-hour run deadline.
RUNPOD_POLL_TIMEOUT_SECONDS = 600
RUNPOD_PENDING_STATUSES = frozenset({"IN_QUEUE", "IN_PROGRESS"})


class ImageManifestRefused(RuntimeError):
    pass


class ImageBudgetExceeded(RuntimeError):
    pass


class ImageProviderUnavailable(RuntimeError):
    pass


class ImageProviderFailure(RuntimeError):
    def __init__(
        self,
        detail: str | None = None,
        *,
        failure_class: str = "terminal provider failure",
        endpoint: object = None,
        provider_job_id: object = None,
        provider_status: object = None,
        http_status: object = None,
        error_code: object = None,
        error_message: object = None,
    ) -> None:
        self.diagnostic = {
            "failure_class": failure_class,
            "endpoint_id": endpoint,
            "provider_job_id": _safe_value(provider_job_id),
            "provider_status": _safe_value(provider_status),
            "http_status": _safe_value(http_status),
            "provider_error_code": _safe_value(error_code),
            "provider_error_message": _safe_error_message(error_message or detail),
        }
        fields = [
            f"{key}={value}"
            for key, value in self.diagnostic.items()
            if value not in (None, "")
        ]
        super().__init__("image provider request failed: " + " ".join(fields))


def _safe_value(value: object) -> str | None:
    if value is None:
        return None
    return re.sub(r"https?://\S+", "<redacted-url>", str(value))[:200]


def _safe_error_message(value: object) -> str | None:
    if value is None:
        return None
    message = str(value).replace("\n", " ").strip()
    message = re.sub(r"https?://\S+", "<redacted-url>", message)
    return message[:500] or None


def _provider_error_fields(result: Mapping[str, Any]) -> tuple[object, object]:
    error = result.get("error")
    if isinstance(error, Mapping):
        return (
            error.get("code", error.get("type", error.get("error_code"))),
            error.get("message", error.get("detail")),
        )
    return (
        result.get("errorCode", result.get("error_code", result.get("code"))),
        error or result.get("message"),
    )


def _png_size(raw: bytes) -> tuple[int, int]:
    if raw[:8] != b"\x89PNG\r\n\x1a\n" or len(raw) < 24:
        raise ImageProviderFailure("image provider returned a non-PNG image")
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


def _request_payload(item: Mapping[str, Any], seed: int, source_raw: bytes | None = None) -> dict[str, Any]:
    prompt = str(item.get("prompt", "")).strip()
    negative = str(item.get("negative_prompt", "")).strip()
    width, height = int(item["width"]), int(item["height"])
    if not prompt or len(prompt) > MAX_PROMPT:
        raise ImageManifestRefused("image prompt is missing or too long")
    if width < 64 or height < 64 or width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ImageManifestRefused("image dimensions exceed the 1024-pixel ceiling")
    denoise = float(item.get("denoise", 1.0))
    if not 0 < denoise <= 1:
        raise ImageManifestRefused("image denoise must be greater than 0 and at most 1")
    workflow = {
        "3": {
            "inputs": {
                "seed": seed,
                "steps": 25,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": denoise,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
            "class_type": "KSampler",
        },
        "4": {"inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}, "class_type": "CheckpointLoaderSimple"},
        "5": {"inputs": {"width": width, "height": height, "batch_size": 1}, "class_type": "EmptyLatentImage"},
        "6": {"inputs": {"text": prompt, "clip": ["4", 1]}, "class_type": "CLIPTextEncode"},
        "7": {"inputs": {"text": negative, "clip": ["4", 1]}, "class_type": "CLIPTextEncode"},
        "8": {"inputs": {"samples": ["3", 0], "vae": ["4", 2]}, "class_type": "VAEDecode"},
        "9": {"inputs": {"filename_prefix": "chitti-generated", "images": ["8", 0]}, "class_type": "SaveImage"},
    }
    payload: dict[str, Any] = {"workflow": workflow}
    if source_raw is not None:
        workflow["1"] = {
            "inputs": {"image": "reference.png", "upload": "image"},
            "class_type": "LoadImage",
        }
        workflow["5"] = {
            "inputs": {"pixels": ["1", 0], "vae": ["4", 2]},
            "class_type": "VAEEncode",
        }
        payload["images"] = [
            {
                "name": "reference.png",
                "image": "data:image/png;base64," + base64.b64encode(source_raw).decode(),
            }
        ]
    return {"input": payload}


def _call_runpod(endpoint: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    def request_json(request: urllib.request.Request) -> tuple[dict[str, Any], int]:
        try:
            with urllib.request.urlopen(
                request, timeout=RUNPOD_HTTP_TIMEOUT_SECONDS
            ) as response:
                return cast(dict[str, Any], json.load(response)), response.status
        except urllib.error.HTTPError as exc:
            try:
                body = json.load(exc)
            except (ValueError, OSError):
                body = {}
            if not isinstance(body, Mapping):
                body = {}
            error_code, error_message = _provider_error_fields(body)
            raise ImageProviderFailure(
                failure_class="http error",
                endpoint=endpoint,
                http_status=exc.code,
                error_code=error_code,
                error_message=error_message or exc.reason,
            ) from exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise ImageProviderFailure(
                failure_class="transport error",
                endpoint=endpoint,
                error_message=f"{type(exc).__name__}: {exc}",
            ) from exc

    request = urllib.request.Request(
        f"https://api.runpod.ai/v2/{endpoint}/run",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    result, http_status = request_json(request)
    status = result.get("status")
    provider_job_id = result.get("id")
    if status == "COMPLETED":
        return result
    if status in RUNPOD_PENDING_STATUSES:
        deadline = time.monotonic() + RUNPOD_POLL_TIMEOUT_SECONDS
        while provider_job_id and time.monotonic() < deadline:
            time.sleep(RUNPOD_POLL_INTERVAL_SECONDS)
            status_request = urllib.request.Request(
                f"https://api.runpod.ai/v2/{endpoint}/status/{provider_job_id}",
                headers={"Authorization": f"Bearer {key}"},
                method="GET",
            )
            result, http_status = request_json(status_request)
            status = result.get("status")
            if status == "COMPLETED":
                return result
            if status not in RUNPOD_PENDING_STATUSES:
                error_code, error_message = _provider_error_fields(result)
                raise ImageProviderFailure(
                    failure_class="terminal provider failure",
                    endpoint=endpoint,
                    provider_job_id=provider_job_id,
                    provider_status=status,
                    http_status=http_status,
                    error_code=error_code,
                    error_message=error_message,
                )
        error_code, error_message = _provider_error_fields(result)
        raise ImageProviderFailure(
            failure_class="queue timeout",
            endpoint=endpoint,
            provider_job_id=provider_job_id,
            provider_status=status,
            http_status=http_status,
            error_code=error_code,
            error_message=error_message,
        )
    error_code, error_message = _provider_error_fields(result)
    raise ImageProviderFailure(
        failure_class="terminal provider failure",
        endpoint=endpoint,
        provider_job_id=provider_job_id,
        provider_status=status,
        http_status=http_status,
        error_code=error_code,
        error_message=error_message,
    )


def _digest(item: Mapping[str, Any], source_digest: str) -> str:
    canonical = {
        "template": WORKFLOW_TEMPLATE_ID,
        "prompt": item.get("prompt"),
        "negative_prompt": item.get("negative_prompt", ""),
        "width": item.get("width"),
        "height": item.get("height"),
        "seed": item.get("seed"),
        "denoise": item.get("denoise", 1.0),
        "source_digest": source_digest,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def _request_digest(item: Mapping[str, Any], source_digest: str) -> str:
    without_seed = {key: value for key, value in item.items() if key != "seed"}
    return _digest(without_seed, source_digest)


async def verify_export_assets(database: Any, run_id: int, workspace: Path) -> None:
    export_root = workspace / "out"
    if not export_root.is_dir():
        return
    raster_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
    for path in sorted(export_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in raster_suffixes:
            continue
        relative = str(path.relative_to(workspace)).replace("\\", "/")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        async with database.sessions() as session:
            result = await session.execute(
                runner_sql(
                    text(
                        "SELECT id FROM worker_image_jobs "
                        "WHERE path = :path AND sha256 = :sha256 "
                        "AND status = 'completed' ORDER BY id DESC LIMIT 1"
                    )
                ),
                {"path": relative, "sha256": digest},
            )
            if result.scalar_one_or_none() is None:
                raise ImageManifestRefused(f"poster export contains an unverified raster asset: {relative}")


async def generate_manifest_images(
    database: Any,
    run_id: int,
    workspace: Path,
    limits: Any,
) -> str:
    manifest_path = workspace / "image_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImageManifestRefused(f"image manifest is invalid: {exc}") from exc
    images = manifest.get("images") if isinstance(manifest, dict) else None
    if not isinstance(images, list) or not images:
        raise ImageManifestRefused("image manifest must contain a non-empty images list")
    settings = get_settings()
    if not settings.runpod_api_key or not settings.runpod_endpoint_id:
        raise ImageProviderUnavailable(
            "image generation is unavailable: RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID are not configured"
        )
    output_dir = workspace / "out" / "generated"
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved: list[dict[str, Any]] = []
    for item in images:
        if not isinstance(item, dict) or set(item) - {
            "id",
            "purpose",
            "prompt",
            "negative_prompt",
            "width",
            "height",
            "seed",
            "denoise",
            "reference",
        }:
            raise ImageManifestRefused("image manifest contains fields outside the declared contract")
        identifier = str(item.get("id", "")).strip()
        if not identifier or "/" in identifier or "\\" in identifier:
            raise ImageManifestRefused("image manifest image id is invalid")
        source_digest = ""
        source_raw: bytes | None = None
        if item.get("reference"):
            source = workspace / "out" / "generated" / str(item["reference"])
            if not source.is_file():
                raise ImageManifestRefused("image reference is not an already-generated asset")
            source_raw = source.read_bytes()
            source_digest = hashlib.sha256(source_raw).hexdigest()
        request_item = dict(item)
        if item.get("seed") is None:
            seed_digest = _request_digest(request_item, source_digest)
            seed = int(seed_digest[:16], 16)
            request_item["seed"] = seed
        else:
            seed = int(item["seed"])
        digest = _digest(request_item, source_digest)
        async with database.sessions() as session:
            cached = await session.execute(
                runner_sql(
                    text(
                        "SELECT width, height, sha256, byte_size, content FROM worker_image_jobs "
                        "WHERE cache_digest = :digest AND status = 'completed' ORDER BY id DESC LIMIT 1"
                    )
                ),
                {"digest": digest},
            )
            cached_row = cached.mappings().first()
        target = output_dir / f"{identifier}.png"
        if cached_row:
            if cached_row["content"]:
                content = bytes(cached_row["content"])
                target.write_bytes(content)
                async with database.sessions() as session:
                    await session.execute(
                        runner_sql(
                            text(
                                "INSERT INTO worker_image_jobs "
                                "(run_id, cache_digest, prompt, negative_prompt, parameters, "
                                "workflow_template_id, endpoint_id, worker_image, "
                                "delay_time_ms, execution_time_ms, cost_usd, path, width, "
                                "height, sha256, byte_size, content, status, cache_hit) VALUES "
                                "(:run_id, :digest, :prompt, :negative, CAST(:parameters AS jsonb), "
                                ":template, :endpoint, :worker, 0, 0, 0, :path, :width, :height, "
                                ":sha, :bytes, :content, 'completed', true)"
                            )
                        ),
                        {
                            "run_id": run_id,
                            "digest": digest,
                            "prompt": item["prompt"],
                            "negative": item.get("negative_prompt", ""),
                            "parameters": json.dumps(request_item),
                            "template": WORKFLOW_TEMPLATE_ID,
                            "endpoint": settings.runpod_endpoint_id,
                            "worker": WORKER_IMAGE,
                            "path": f"out/generated/{identifier}.png",
                            "width": cached_row["width"],
                            "height": cached_row["height"],
                            "sha": cached_row["sha256"],
                            "bytes": cached_row["byte_size"],
                            "content": content,
                        },
                    )
                    await session.commit()
                resolved.append(
                    {
                        "id": identifier,
                        "purpose": item.get("purpose", ""),
                        "path": f"generated/{identifier}.png",
                        "width": cached_row["width"],
                        "height": cached_row["height"],
                        "sha256": cached_row["sha256"],
                        "byte_size": cached_row["byte_size"],
                        "cache_hit": True,
                    }
                )
                continue
        async with database.sessions() as session:
            count = await session.execute(
                runner_sql(
                    text(
                        "SELECT COUNT(*) FROM worker_image_jobs WHERE run_id = :run_id "
                        "AND status = 'completed' AND cache_hit = false"
                    )
                ),
                {"run_id": run_id},
            )
            spent = await session.execute(
                runner_sql(
                    text(
                        "SELECT COALESCE(SUM(cost_usd), 0) FROM worker_image_jobs WHERE run_id = :run_id AND status = 'completed'"
                    )
                ),
                {"run_id": run_id},
            )
        if int(count.scalar_one()) >= limits.image_request_count:
            raise ImageBudgetExceeded("image request budget exhausted")
        if float(spent.scalar_one()) >= limits.image_spend_usd:
            raise ImageBudgetExceeded("image spend budget exhausted")
        result = await asyncio.to_thread(
            _call_runpod,
            settings.runpod_endpoint_id,
            settings.runpod_api_key,
            _request_payload(request_item, seed, source_raw),
        )
        image = (result.get("output") or {}).get("images", [{}])[0]
        encoded = image.get("data") or image.get("image")
        if not isinstance(encoded, str):
            raise ImageProviderFailure("image provider returned no image data")
        if encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        raw = base64.b64decode(encoded)
        width, height = _png_size(raw)
        target.write_bytes(raw)
        delay = int(result.get("delayTime") or 0)
        execution = int(result.get("executionTime") or 0)
        cost = ((delay + execution) / 1000.0) * settings.runpod_gpu_rate_usd / 3600.0
        sha = hashlib.sha256(raw).hexdigest()
        async with database.sessions() as session:
            await session.execute(
                runner_sql(
                    text(
                        "INSERT INTO worker_image_jobs "
                        "(run_id, cache_digest, prompt, negative_prompt, parameters, workflow_template_id, "
                        "endpoint_id, worker_image, delay_time_ms, execution_time_ms, cost_usd, path, "
                        "width, height, sha256, byte_size, content, status, cache_hit) VALUES "
                        "(:run_id, :digest, :prompt, :negative, CAST(:parameters AS jsonb), :template, "
                        ":endpoint, :worker, :delay, :execution, :cost, :path, :width, :height, :sha, :bytes, "
                        ":content, 'completed', false)"
                    )
                ),
                {
                    "run_id": run_id,
                    "digest": digest,
                    "prompt": item["prompt"],
                    "negative": item.get("negative_prompt", ""),
                    "parameters": json.dumps(dict(item)),
                    "template": WORKFLOW_TEMPLATE_ID,
                    "endpoint": settings.runpod_endpoint_id,
                    "worker": WORKER_IMAGE,
                    "delay": delay,
                    "execution": execution,
                    "cost": cost,
                    "path": f"out/generated/{identifier}.png",
                    "width": width,
                    "height": height,
                    "sha": sha,
                    "bytes": len(raw),
                    "content": raw,
                },
            )
            await session.commit()
        resolved.append(
            {
                "id": identifier,
                "purpose": item.get("purpose", ""),
                "path": f"generated/{identifier}.png",
                "width": width,
                "height": height,
                "sha256": sha,
                "byte_size": len(raw),
                "cache_hit": False,
            }
        )
    resolved_path = workspace / "image_manifest.resolved.json"
    resolved_path.write_text(json.dumps({"images": resolved}, indent=2), encoding="utf-8")
    return f"generated {len(resolved)} image(s); resolved manifest: image_manifest.resolved.json"
