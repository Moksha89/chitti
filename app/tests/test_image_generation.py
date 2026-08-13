from __future__ import annotations

import asyncio
import base64
import hashlib
from types import SimpleNamespace

import pytest

from chitti.image_generation import (
    ImageBudgetExceeded,
    ImageManifestRefused,
    _request_digest,
    _request_payload,
    generate_manifest_images,
    verify_export_assets,
)
from chitti.worker import DockerSandboxDispatcher, FixedOperation


def test_image_request_owns_comfy_workflow_and_accepts_only_intent() -> None:
    payload = _request_payload(
        {
            "prompt": "cinematic cricket stadium",
            "negative_prompt": "text",
            "width": 1024,
            "height": 1024,
            "denoise": 1.0,
        },
        42,
    )
    workflow = payload["input"]["workflow"]
    assert workflow["4"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"
    assert workflow["5"]["inputs"]["width"] == 1024
    assert workflow["3"]["inputs"]["seed"] == 42
    assert "class_type" in workflow["9"]
    assert "images" not in payload["input"]


def test_reference_intent_is_translated_to_host_owned_img2img_workflow() -> None:
    payload = _request_payload(
        {
            "prompt": "cinematic lighting",
            "negative_prompt": "",
            "width": 1024,
            "height": 1024,
            "denoise": 0.35,
        },
        7,
        b"\x89PNG\r\n\x1a\n" + b"\0" * 16,
    )
    workflow = payload["input"]["workflow"]
    assert workflow["1"]["class_type"] == "LoadImage"
    assert workflow["5"]["class_type"] == "VAEEncode"
    assert payload["input"]["images"][0]["name"] == "reference.png"
    assert base64.b64decode(payload["input"]["images"][0]["image"].split(",", 1)[1]).startswith(b"\x89PNG")


def test_omitted_seed_is_deterministic() -> None:
    item = {
        "prompt": "stadium",
        "negative_prompt": "",
        "width": 1024,
        "height": 1024,
    }
    assert _request_digest(item, "") == _request_digest(item, "")


def test_cache_hit_reuses_image_without_external_call(monkeypatch, tmp_path) -> None:
    import chitti.image_generation as module

    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    (tmp_path / "image_manifest.json").write_text(
        '{"images":[{"id":"stadium","purpose":"hero","prompt":"stadium","negative_prompt":"","width":64,"height":64}]}'
    )
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(
            runpod_api_key="key",
            runpod_endpoint_id="endpoint",
            runpod_gpu_rate_usd=0.34,
        ),
    )
    calls = 0

    def fake_provider(*_args):
        nonlocal calls
        calls += 1
        return {
            "status": "COMPLETED",
            "output": {"images": [{"data": base64.b64encode(png).decode()}]},
        }

    monkeypatch.setattr(module, "_call_runpod", fake_provider)

    class Result:
        def __init__(self, row=None, value=0):
            self.row = row
            self.value = value

        def mappings(self):
            return self

        def first(self):
            return self.row

        def scalar_one(self):
            return self.value

    class Session:
        def __init__(self, database):
            self.database = database

        async def execute(self, _statement, _params):
            self.database.calls += 1
            if self.database.calls == 1:
                return Result()
            if self.database.calls == 2 or self.database.calls == 3:
                return Result(value=0)
            if self.database.calls == 5:
                return Result(
                    row={
                        "width": 1,
                        "height": 1,
                        "sha256": hashlib.sha256(png).hexdigest(),
                        "byte_size": len(png),
                        "content": png,
                    }
                )
            return Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            return None

    class Database:
        calls = 0

        def sessions(self):
            return Session(self)

    database = Database()
    limits = SimpleNamespace(image_request_count=6, image_spend_usd=0.05)
    asyncio.run(generate_manifest_images(database, 1, tmp_path, limits))
    asyncio.run(generate_manifest_images(database, 2, tmp_path, limits))
    assert calls == 1


def test_malformed_manifest_is_repairable(monkeypatch, tmp_path) -> None:
    (tmp_path / "image_manifest.json").write_text(
        '{"images":[{"id":"x","prompt":"bad","width":1024,"height":1024,"unexpected":true}]}'
    )
    monkeypatch.setattr(
        "chitti.image_generation.get_settings",
        lambda: SimpleNamespace(runpod_api_key="key", runpod_endpoint_id="endpoint"),
    )

    with pytest.raises(ImageManifestRefused):
        asyncio.run(generate_manifest_images(None, 1, tmp_path, SimpleNamespace(image_request_count=6, image_spend_usd=0.05)))


def test_host_rejects_forged_resolved_asset(tmp_path) -> None:
    generated = tmp_path / "out" / "generated"
    generated.mkdir(parents=True)
    asset = generated / "fake.png"
    asset.write_bytes(b"forged")

    class Result:
        def scalar_one_or_none(self):
            return None

    class Session:
        async def execute(self, _statement, _params):
            return Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Database:
        def sessions(self):
            return Session()

    with pytest.raises(ImageManifestRefused, match="unverified raster asset"):
        asyncio.run(verify_export_assets(Database(), 1, tmp_path))


def test_asset_added_after_initial_check_is_rejected_on_next_check(tmp_path) -> None:
    generated = tmp_path / "out" / "generated"
    generated.mkdir(parents=True)
    first = generated / "first.png"
    first.write_bytes(b"first")

    class Result:
        def scalar_one_or_none(self):
            return True

    class Session:
        async def execute(self, _statement, _params):
            return Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Database:
        def sessions(self):
            return Session()

    database = Database()
    asyncio.run(verify_export_assets(database, 1, tmp_path))
    second = generated / "second.png"
    second.write_bytes(b"second")

    class RejectingSession(Session):
        async def execute(self, _statement, params):
            result = Result()
            result.scalar_one_or_none = lambda: (True if params["path"].endswith("first.png") else None)
            return result

    class RejectingDatabase:
        def sessions(self):
            return RejectingSession()

    with pytest.raises(ImageManifestRefused, match="second.png"):
        asyncio.run(verify_export_assets(RejectingDatabase(), 1, tmp_path))


def test_verification_refusal_is_recorded_as_operation_failure(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    dispatcher = object.__new__(DockerSandboxDispatcher)
    dispatcher.database = None
    dispatcher._operation = AsyncMock()

    async def reject(*_args):
        raise ImageManifestRefused("unverified raster asset: generated/fake.png")

    monkeypatch.setattr("chitti.worker.verify_export_assets", reject)
    operation = FixedOperation("task", "poster-export", ("true",))

    with pytest.raises(RuntimeError, match="generated/fake.png"):
        asyncio.run(dispatcher._verify_poster_assets(1, tmp_path, operation, 3))
    dispatcher._operation.assert_awaited_once()
    assert "generated/fake.png" in dispatcher._operation.await_args.args[5]


def test_budget_exhaustion_is_terminal(monkeypatch, tmp_path) -> None:
    (tmp_path / "image_manifest.json").write_text('{"images":[{"id":"x","prompt":"stadium","width":1024,"height":1024}]}')
    monkeypatch.setattr(
        "chitti.image_generation.get_settings",
        lambda: SimpleNamespace(runpod_api_key="key", runpod_endpoint_id="endpoint"),
    )

    class Result:
        def scalar_one(self):
            return 6

        def mappings(self):
            return self

        def first(self):
            return None

    class Session:
        calls = 0

        async def execute(self, _statement, _params):
            self.calls += 1
            return Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Database:
        def sessions(self):
            return Session()

    with pytest.raises(ImageBudgetExceeded, match="request budget exhausted"):
        asyncio.run(
            generate_manifest_images(
                Database(),
                1,
                tmp_path,
                SimpleNamespace(image_request_count=6, image_spend_usd=0.05),
            )
        )
