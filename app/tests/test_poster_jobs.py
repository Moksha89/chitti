from __future__ import annotations

import pytest

from chitti.job_types import (
    MAX_POSTER_CSS_DIMENSION,
    POSTER_POLICY,
    WEBSITE_POLICY,
    policy_for,
    poster_config,
)
from chitti.worker import _model_system_prompt, _reviewer_system_prompt


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


def test_poster_prompts_require_brand_and_honest_visual_review() -> None:
    prompt = _model_system_prompt(POSTER_POLICY, {"typography": "FreeSans"})
    review = _reviewer_system_prompt(POSTER_POLICY)
    assert "do not invent" in prompt
    assert "visual quality was not assessed" in review
