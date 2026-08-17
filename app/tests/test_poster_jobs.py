from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from chitti.brand_profiles import available_font_families
from chitti.job_types import (
    MAX_POSTER_CSS_DIMENSION,
    POSTER_POLICY,
    WEBSITE_POLICY,
    policy_for,
    poster_config,
)
from chitti.worker import (
    WorkerRunManager,
    _model_system_prompt,
    _reviewer_system_prompt,
    _validate_screenshot_request,
)


class _Result:
    def scalar_one(self) -> int:
        return 42


class _Session:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(self, statement, params=None):
        self.calls.append({"statement": str(statement), "params": params or {}})
        return _Result()

    async def commit(self) -> None:
        pass


class _Database:
    def __init__(self) -> None:
        self.session = _Session()

    @asynccontextmanager
    async def sessions(self):
        yield self.session


def test_existing_runs_default_to_website_policy() -> None:
    assert policy_for(None) == WEBSITE_POLICY
    assert policy_for("website").model_commands == WEBSITE_POLICY.model_commands


def test_poster_policy_has_no_npm_commands() -> None:
    assert policy_for("poster") == POSTER_POLICY
    assert all("npm" not in command for command in POSTER_POLICY.model_commands)


def test_poster_dimensions_and_scale_are_bounded() -> None:
    assert poster_config({"width": 1080, "height": 1350, "scale": 2})["scale"] == 2
    with pytest.raises(ValueError, match=str(MAX_POSTER_CSS_DIMENSION)):
        poster_config({"width": MAX_POSTER_CSS_DIMENSION + 1, "height": 1})
    with pytest.raises(ValueError, match="device scale"):
        poster_config({"width": 1, "height": 1, "scale": 3})
    with pytest.raises(ValueError, match=r"\.html or \.svg"):
        poster_config({"artifact": "poster.png"})


def test_poster_prompts_require_brand_and_honest_visual_review() -> None:
    prompt = _model_system_prompt(
        POSTER_POLICY,
        {"typography": "FreeSans"},
        {"artifact": "trial-poster.html", "width": 1080, "height": 1350, "scale": 1},
    )
    review = _reviewer_system_prompt(POSTER_POLICY)
    assert "do not invent" in prompt
    for family in available_font_families():
        assert family in prompt
    assert "url(#gradient)" in prompt
    assert "trial-poster.html" in prompt
    assert "width 1080" in prompt
    assert "height 1350" in prompt
    assert "device scale 1" in prompt
    assert "does not need a route argument" in prompt
    assert "do not raise the scale" in prompt
    assert "between 64 and 1024 pixels" in prompt
    assert "1080x1350" in prompt
    assert "useful source aspect ratio" in prompt
    assert "cinematic treatment as required" in prompt
    assert "every text block must maintain clear contrast" in prompt
    assert "generic figures by default" in prompt
    assert "visual quality was not assessed" in review


def test_poster_capture_ignores_route_but_keeps_scale_ceiling() -> None:
    config = {"artifact": "trial-poster.html", "width": 1080, "height": 1350, "scale": 1}
    _validate_screenshot_request(POSTER_POLICY, "not-a-route", 1080, 1350, 1, config)
    with pytest.raises(ValueError, match="poster screenshot exceeds approved capture"):
        _validate_screenshot_request(POSTER_POLICY, "not-a-route", 1080, 1350, 2, config)


@pytest.mark.asyncio
async def test_poster_preflight_refuses_before_inserting_a_run(monkeypatch) -> None:
    database = _Database()
    monkeypatch.setattr("chitti.worker.approved_revision", _approved_revision)
    monkeypatch.setattr("chitti.worker.get_brand_profile", _missing_profile)

    with pytest.raises(ValueError, match="poster run not started"):
        await WorkerRunManager(database).enqueue(7, job_type="poster")

    assert database.session.calls == []


@pytest.mark.asyncio
async def test_poster_preflight_refuses_unconfigured_image_generation(monkeypatch) -> None:
    database = _Database()
    monkeypatch.setattr("chitti.worker.approved_revision", _approved_revision)
    monkeypatch.setattr("chitti.worker.get_brand_profile", _present_profile)
    monkeypatch.setattr(
        "chitti.worker.get_settings",
        lambda: SimpleNamespace(runpod_api_key="", runpod_endpoint_id=""),
    )

    with pytest.raises(ValueError, match="image generation is unavailable"):
        await WorkerRunManager(database).enqueue(7, job_type="poster")

    assert database.session.calls == []


@pytest.mark.asyncio
async def test_poster_run_refuses_invalid_approved_artifact(monkeypatch) -> None:
    database = _Database()

    async def bad_revision(*_args, **_kwargs):
        revision = await _approved_revision()
        revision.job_config["artifact"] = "poster.png"
        return revision

    monkeypatch.setattr("chitti.worker.approved_revision", bad_revision)

    with pytest.raises(ValueError, match=r"\.html or \.svg"):
        await WorkerRunManager(database).enqueue(7, job_type="poster")

    assert database.session.calls == []


@pytest.mark.asyncio
async def test_poster_job_config_persists_only_declared_configuration(monkeypatch) -> None:
    database = _Database()
    monkeypatch.setattr("chitti.worker.approved_revision", _approved_revision)
    monkeypatch.setattr("chitti.worker.get_brand_profile", _present_profile)
    monkeypatch.setattr(
        "chitti.worker.get_settings",
        lambda: SimpleNamespace(runpod_api_key="key", runpod_endpoint_id="endpoint"),
    )
    declared = {"artifact": "campaign/poster.svg", "width": 1200, "height": 628, "scale": 2}

    await WorkerRunManager(database).enqueue(
        7, job_type="poster", job_config=declared
    )

    insert = database.session.calls[0]["params"]
    assert json.loads(str(insert["job_config"])) == {
        **declared,
        "likeness_policy": "generic_figures",
    }
    assert all(not key.startswith("_") for key in json.loads(str(insert["job_config"])))


async def _approved_revision(*_args, **_kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        namespace="pj-digi",
        job_type="poster",
        job_config={
            "artifact": "campaign/poster.svg",
            "width": 1200,
            "height": 628,
            "scale": 2,
        },
    )


async def _missing_profile(*_args, **_kwargs):
    return None


async def _present_profile(*_args, **_kwargs):
    return object()
