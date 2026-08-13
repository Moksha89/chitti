from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import text

from .runner_access import runner_sql
from .settings import get_settings

WORKFLOW_TEMPLATE_ID = "sdxl-base-v1"
WORKER_IMAGE = "runpod/worker-comfyui:5.8.6-sdxl"
MAX_DIMENSION = 1024
MAX_PROMPT = 4000


class ImageRequestRefused(RuntimeError):
    pass


def _png_size(raw: bytes) -> tuple[int, int]:
    if raw[:8] != b"\x89PNG\r\n\x1a\n" or len(raw) < 24:
        raise ImageRequestRefused("image provider returned a non-PNG image")
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


def _request_payload(
    item: Mapping[str, Any], seed: int, source_raw: bytes | None = None
) -> dict[str, Any]:
    prompt = str(item.get("prompt", "")).strip()
    negative = str(item.get("negative_prompt", "")).strip()
    width, height = int(item["width"]), int(item["height"])
    if not prompt or len(prompt) > MAX_PROMPT:
        raise ImageRequestRefused("image prompt is missing or too long")
    if width < 64 or height < 64 or width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ImageRequestRefused("image dimensions exceed the 1024-pixel ceiling")
    workflow = {
        "3": {"inputs": {"seed": seed, "steps": 25, "cfg": 7.0,
                         "sampler_name": "euler", "scheduler": "normal",
                         "denoise": float(item.get("denoise", 1.0)),
                         "model": ["4", 0], "positive": ["6", 0],
                         "negative": ["7", 0], "latent_image": ["5", 0]},
              "class_type": "KSampler"},
        "4": {"inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"},
              "class_type": "CheckpointLoaderSimple"},
        "5": {"inputs": {"width": width, "height": height, "batch_size": 1},
              "class_type": "EmptyLatentImage"},
        "6": {"inputs": {"text": prompt, "clip": ["4", 1]},
              "class_type": "CLIPTextEncode"},
        "7": {"inputs": {"text": negative, "clip": ["4", 1]},
              "class_type": "CLIPTextEncode"},
        "8": {"inputs": {"samples": ["3", 0], "vae": ["4", 2]},
              "class_type": "VAEDecode"},
        "9": {"inputs": {"filename_prefix": "chitti-generated", "images": ["8", 0]},
              "class_type": "SaveImage"},
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
        payload["images"] = [{
            "name": "reference.png",
            "image": "data:image/png;base64," + base64.b64encode(source_raw).decode(),
        }]
    return {"input": payload}


def _call_runpod(endpoint: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.runpod.ai/v2/{endpoint}/runsync",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        result = json.load(response)
    if result.get("status") != "COMPLETED":
        raise ImageRequestRefused(f"image provider request failed: {result.get('error', 'unknown error')}")
    return result


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
        raise ImageRequestRefused(f"image manifest is invalid: {exc}") from exc
    images = manifest.get("images") if isinstance(manifest, dict) else None
    if not isinstance(images, list) or not images:
        raise ImageRequestRefused("image manifest must contain a non-empty images list")
    settings = get_settings()
    if not settings.runpod_api_key or not settings.runpod_endpoint_id:
        raise ImageRequestRefused(
            "image generation is unavailable: RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID are not configured"
        )
    output_dir = workspace / "out" / "generated"
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved: list[dict[str, Any]] = []
    for item in images:
        if not isinstance(item, dict) or set(item) - {
            "id", "purpose", "prompt", "negative_prompt", "width", "height", "seed",
            "denoise", "reference",
        }:
            raise ImageRequestRefused("image manifest contains fields outside the declared contract")
        identifier = str(item.get("id", "")).strip()
        if not identifier or "/" in identifier or "\\" in identifier:
            raise ImageRequestRefused("image manifest image id is invalid")
        source_digest = ""
        source_raw: bytes | None = None
        if item.get("reference"):
            source = workspace / "out" / "generated" / str(item["reference"])
            if not source.is_file():
                raise ImageRequestRefused("image reference is not an already-generated asset")
            source_raw = source.read_bytes()
            source_digest = hashlib.sha256(source_raw).hexdigest()
        seed = int(item.get("seed") or int.from_bytes(os.urandom(8), "big"))
        request_item = dict(item)
        request_item["seed"] = seed
        digest = _digest(request_item, source_digest)
        async with database.sessions() as session:
            cached = await session.execute(
                runner_sql(text(
                    "SELECT width, height, sha256, byte_size, content FROM worker_image_jobs "
                    "WHERE cache_digest = :digest AND status = 'completed' ORDER BY id DESC LIMIT 1"
                )),
                {"digest": digest},
            )
            cached_row = cached.mappings().first()
        target = output_dir / f"{identifier}.png"
        if cached_row:
            if cached_row["content"]:
                target.write_bytes(bytes(cached_row["content"]))
                resolved.append({"id": identifier, "purpose": item.get("purpose", ""),
                                 "path": f"generated/{identifier}.png",
                                 "width": cached_row["width"], "height": cached_row["height"],
                                 "sha256": cached_row["sha256"], "byte_size": cached_row["byte_size"],
                                 "cache_hit": True})
                continue
        async with database.sessions() as session:
            count = await session.execute(
                runner_sql(text(
                    "SELECT COUNT(*) FROM worker_image_jobs WHERE run_id = :run_id "
                    "AND status = 'completed' AND cache_hit = false"
                )),
                {"run_id": run_id},
            )
            spent = await session.execute(
                runner_sql(text(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM worker_image_jobs "
                    "WHERE run_id = :run_id AND status = 'completed'"
                )),
                {"run_id": run_id},
            )
        if int(count.scalar_one()) >= limits.image_request_count:
            raise ImageRequestRefused("image request budget exhausted")
        if float(spent.scalar_one()) >= limits.image_spend_usd:
            raise ImageRequestRefused("image spend budget exhausted")
        result = await asyncio.to_thread(
            _call_runpod, settings.runpod_endpoint_id, settings.runpod_api_key,
            _request_payload(request_item, seed, source_raw),
        )
        image = (result.get("output") or {}).get("images", [{}])[0]
        encoded = image.get("data") or image.get("image")
        if not isinstance(encoded, str):
            raise ImageRequestRefused("image provider returned no image data")
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
            await session.execute(runner_sql(text(
                "INSERT INTO worker_image_jobs "
                "(run_id, cache_digest, prompt, negative_prompt, parameters, workflow_template_id, "
                "endpoint_id, worker_image, delay_time_ms, execution_time_ms, cost_usd, path, "
                "width, height, sha256, byte_size, content, status, cache_hit) VALUES "
                "(:run_id, :digest, :prompt, :negative, CAST(:parameters AS jsonb), :template, "
                ":endpoint, :worker, :delay, :execution, :cost, :path, :width, :height, :sha, :bytes, "
                ":content, 'completed', false)"
            )), {"run_id": run_id, "digest": digest, "prompt": item["prompt"],
                "negative": item.get("negative_prompt", ""), "parameters": json.dumps(dict(item)),
                "template": WORKFLOW_TEMPLATE_ID, "endpoint": settings.runpod_endpoint_id,
                "worker": WORKER_IMAGE, "delay": delay, "execution": execution, "cost": cost,
                "path": f"out/generated/{identifier}.png", "width": width, "height": height,
                "sha": sha, "bytes": len(raw), "content": raw})
            await session.commit()
        resolved.append({"id": identifier, "purpose": item.get("purpose", ""),
                         "path": f"generated/{identifier}.png", "width": width, "height": height,
                         "sha256": sha, "byte_size": len(raw), "cache_hit": False})
    resolved_path = workspace / "image_manifest.resolved.json"
    resolved_path.write_text(json.dumps({"images": resolved}, indent=2), encoding="utf-8")
    return f"generated {len(resolved)} image(s); resolved manifest: image_manifest.resolved.json"
