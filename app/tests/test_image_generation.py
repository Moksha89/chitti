from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

import chitti.image_generation as module
from chitti.image_generation import (
    ImageBudgetExceeded,
    ImageManifestRefused,
    ImageProviderFailure,
    _call_runpod,
    _cutout_image,
    _request_digest,
    _request_payload,
    generate_manifest_images,
    verify_export_assets,
)
from chitti.worker import DockerSandboxDispatcher, FixedOperation


class _ProviderResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.content = json.dumps(payload).encode()
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass

    def read(self) -> bytes:
        return self.content


def test_runpod_polls_pending_job_until_completed(monkeypatch) -> None:
    responses = iter(
        [
            _ProviderResponse({"id": "job-1", "status": "IN_QUEUE"}),
            _ProviderResponse({"id": "job-1", "status": "COMPLETED", "output": {}}),
        ]
    )
    calls = []
    monkeypatch.setattr(
        "chitti.image_generation.urllib.request.urlopen",
        lambda request, timeout: calls.append(request) or next(responses),
    )
    monkeypatch.setattr("chitti.image_generation.time.sleep", lambda _seconds: None)

    result = _call_runpod("endpoint", "secret-key", {"input": {}})

    assert result["status"] == "COMPLETED"
    assert len(calls) == 2
    assert calls[0].full_url.endswith("/run")
    assert calls[1].full_url.endswith("/status/job-1")


def test_runpod_terminal_failure_is_not_polled(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "chitti.image_generation.urllib.request.urlopen",
        lambda request, timeout: calls.append(request)
        or _ProviderResponse(
            {
                "id": "job-2",
                "status": "FAILED",
                "error": {"code": "WORKER_FAILED", "message": "worker stopped"},
            }
        ),
    )

    with pytest.raises(ImageProviderFailure) as raised:
        _call_runpod("endpoint", "secret-key", {"input": {}})

    assert len(calls) == 1
    assert raised.value.diagnostic["failure_class"] == "terminal provider failure"
    assert raised.value.diagnostic["provider_error_code"] == "WORKER_FAILED"


def test_runpod_http_error_preserves_safe_structured_evidence(monkeypatch) -> None:
    body = io.BytesIO(
        json.dumps(
            {
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "see https://signed.example/image?sig=secret",
                }
            }
        ).encode()
    )
    error = urllib.error.HTTPError(
        "https://api.runpod.ai/v2/endpoint/run",
        429,
        "rate limited",
        None,
        body,
    )
    monkeypatch.setattr(
        "chitti.image_generation.urllib.request.urlopen",
        lambda request, timeout: (_ for _ in ()).throw(error),
    )

    with pytest.raises(ImageProviderFailure) as raised:
        _call_runpod("endpoint", "secret-key", {"input": {}})

    failure = raised.value
    assert failure.diagnostic["failure_class"] == "http error"
    assert failure.diagnostic["http_status"] == "429"
    assert failure.diagnostic["provider_error_code"] == "RATE_LIMITED"
    assert "<redacted-url>" in str(failure)
    assert "secret-key" not in str(failure)
    assert "Authorization" not in str(failure)
    assert "signed.example" not in str(failure)


def test_runpod_submit_transport_failure_is_structured(monkeypatch) -> None:
    error = urllib.error.URLError("connection reset")
    monkeypatch.setattr(
        "chitti.image_generation.urllib.request.urlopen",
        lambda request, timeout: (_ for _ in ()).throw(error),
    )

    with pytest.raises(ImageProviderFailure) as raised:
        _call_runpod("endpoint", "secret-key", {"input": {}})

    failure = raised.value
    assert failure.diagnostic["failure_class"] == "transport error"
    assert failure.diagnostic["provider_job_id"] is None
    assert "URLError" in failure.diagnostic["provider_error_message"]


def test_runpod_queue_timeout_is_distinct_and_keeps_job_id(monkeypatch) -> None:
    monkeypatch.setattr(
        "chitti.image_generation.urllib.request.urlopen",
        lambda request, timeout: _ProviderResponse(
            {"id": "job-3", "status": "IN_QUEUE"}
        ),
    )
    monotonic = iter([0.0, 601.0])
    monkeypatch.setattr(
        "chitti.image_generation.time.monotonic", lambda: next(monotonic)
    )
    monkeypatch.setattr("chitti.image_generation.time.sleep", lambda _seconds: None)

    with pytest.raises(ImageProviderFailure) as raised:
        _call_runpod("endpoint", "secret-key", {"input": {}})

    assert raised.value.diagnostic["failure_class"] == "queue timeout"
    assert raised.value.diagnostic["provider_job_id"] == "job-3"
    assert raised.value.diagnostic["http_status"] == "200"


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


def test_cutout_removes_uniform_background_and_records_parameters() -> None:
    from PIL import Image

    image = Image.new("RGB", (20, 20), (230, 230, 230))
    for x in range(7, 13):
        for y in range(5, 15):
            image.putpixel((x, y), (20, 80, 180))
    source = io.BytesIO()
    image.save(source, format="PNG")
    transformed, parameters = _cutout_image(source.getvalue())
    result = Image.open(io.BytesIO(transformed))
    assert result.mode == "RGBA"
    assert result.getpixel((0, 0))[3] == 0
    assert result.getpixel((10, 10))[3] > 0
    assert parameters["tolerance"] == 24
    assert parameters["subject_bbox"] == [7, 5, 12, 14]
    assert hashlib.sha256(transformed).hexdigest()


def test_cutout_refuses_non_uniform_background() -> None:
    from PIL import Image

    image = Image.new("RGB", (20, 20), (230, 230, 230))
    image.putpixel((0, 0), (10, 10, 10))
    source = io.BytesIO()
    image.save(source, format="PNG")
    with pytest.raises(ImageManifestRefused, match="not near-uniform"):
        _cutout_image(source.getvalue())


def test_cutout_allows_edge_touching_subject_and_enclosed_background() -> None:
    from PIL import Image

    image = Image.new("RGB", (20, 20), (230, 230, 230))
    for x in range(7, 13):
        for y in range(5, 20):
            image.putpixel((x, y), (20, 80, 180))
    for x in range(9, 11):
        for y in range(8, 11):
            image.putpixel((x, y), (230, 230, 230))
    source = io.BytesIO()
    image.save(source, format="PNG")
    transformed, _parameters = _cutout_image(source.getvalue())
    result = Image.open(io.BytesIO(transformed))
    assert result.getpixel((10, 19))[3] > 0
    assert result.getpixel((10, 9))[3] < 255


def test_manifest_cutout_persists_transformed_provenance(monkeypatch, tmp_path) -> None:
    from PIL import Image

    image = Image.new("RGB", (64, 64), (230, 230, 230))
    for x in range(25, 39):
        for y in range(15, 49):
            image.putpixel((x, y), (20, 80, 180))
    source = io.BytesIO()
    image.save(source, format="PNG")
    (tmp_path / "image_manifest.json").write_text(
        json.dumps(
            {
                "images": [
                    {
                        "id": "figure",
                        "purpose": "subject",
                        "prompt": "figure",
                        "negative_prompt": "",
                        "width": 64,
                        "height": 64,
                        "cutout": True,
                    }
                ]
            }
        )
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
    monkeypatch.setattr(
        module,
        "_call_runpod",
        lambda *_args: {
            "status": "COMPLETED",
            "output": {
                "images": [{"data": base64.b64encode(source.getvalue()).decode()}]
            },
        },
    )

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

        async def execute(self, statement, params):
            sql = str(statement)
            if "cache_digest" in sql and "SELECT" in sql:
                return Result()
            if "COUNT(*)" in sql or "SUM(cost_usd)" in sql:
                return Result(value=0)
            if "INSERT INTO worker_image_jobs" in sql:
                self.database.insert_params = params
            return Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            return None

    class Database:
        insert_params: dict[str, object] = {}

        def sessions(self):
            return Session(self)

    database = Database()
    detail = asyncio.run(
        generate_manifest_images(
            database,
            1,
            tmp_path,
            SimpleNamespace(image_request_count=6, image_spend_usd=0.05),
        )
    )
    resolved = json.loads((tmp_path / "image_manifest.resolved.json").read_text())
    written = (tmp_path / "out" / "generated" / "figure.png").read_bytes()
    parameters = json.loads(str(database.insert_params["parameters"]))
    assert "resolved manifest" in detail
    assert resolved["images"][0]["cutout_applied"] is True
    assert parameters["cutout_applied"] is True
    assert parameters["cutout_parameters"]["tolerance"] == 24
    assert parameters["written_sha256"] == hashlib.sha256(written).hexdigest()
    assert resolved["images"][0]["sha256"] == hashlib.sha256(written).hexdigest()


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
    dispatcher.database = object()
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
