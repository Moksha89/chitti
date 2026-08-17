import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from chitti.brand_colors import validate_brand_color
from chitti.brand_profiles import (
    available_font_families,
    get_brand_profile,
    remove_brand_profile,
    save_brand_profile,
    validate_font_family,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_DB_TESTS"), reason="set RUN_DB_TESTS=1 to run PostgreSQL integration tests"
)
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def database():
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql+asyncpg"
        )
        subprocess.run(
            ["python", "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            cwd=REPO_ROOT,
            env={**os.environ, "DATABASE_URL": url},
            check=True,
        )
        engine = create_async_engine(url)
        yield engine
        await engine.dispose()


def profile_values(typography: str) -> dict[str, object]:
    return {
        "brand_colors": ["#112233", "rgb(4, 5, 6)"],
        "typography": typography,
        "poster_formats": ["1080x1350", "1080x1920"],
        "audience": "People who need this business.",
        "voice": "Warm, direct, and confident.",
        "do_not_use": ["webgl-fluid", "Drei presets"],
    }


@pytest.mark.asyncio
async def test_profiles_are_namespace_scoped_and_history_is_attributable(database) -> None:
    font = available_font_families()[0]
    async with database.begin() as session:
        await save_brand_profile(session, "pj-digi", actor="owner", **profile_values(font))
        assert (await get_brand_profile(session, "jsv-fashion")) is None
        profile = await get_brand_profile(session, "pj-digi")
        assert profile is not None
        assert profile.typography == font
        await save_brand_profile(
            session,
            "pj-digi",
            actor="owner",
            **{**profile_values(font), "voice": "Sharper and more playful."},
        )
        history = await session.execute(
            text(
                "SELECT profile->>'voice' AS voice, changed_by "
                "FROM brand_profile_history WHERE namespace = 'pj-digi' ORDER BY id"
            )
        )
        rows = history.mappings().all()
        assert [row["voice"] for row in rows] == [
            "Warm, direct, and confident.",
            "Sharper and more playful.",
        ]
        assert all(row["changed_by"] == "owner" for row in rows)


@pytest.mark.asyncio
async def test_shared_profile_is_visible_like_shared_memory(database) -> None:
    font = available_font_families()[0]
    async with database.begin() as session:
        await save_brand_profile(session, "general", actor="owner", **profile_values(font))
        profile = await get_brand_profile(session, "vsports")
        assert profile is not None
        assert profile.namespace == "general"


@pytest.mark.asyncio
async def test_removing_profile_preserves_attributable_history(database) -> None:
    font = available_font_families()[0]
    async with database.begin() as session:
        await save_brand_profile(session, "general", actor="owner", **profile_values(font))
        assert await remove_brand_profile(session, "general", actor="owner") is True
        assert await get_brand_profile(session, "general") is None
        history = await session.execute(
            text(
                "SELECT profile->>'action' AS action, profile->>'voice' AS voice, "
                "changed_by FROM brand_profile_history "
                "WHERE namespace = 'general' ORDER BY id"
            )
        )
        rows = history.mappings().all()
        assert rows[-1]["action"] == "removed"
        assert rows[-1]["voice"] == "Warm, direct, and confident."
        assert rows[-1]["changed_by"] == "owner"


def test_typography_must_be_present_in_the_offline_manifest() -> None:
    families = available_font_families()
    assert families
    assert validate_font_family(families[0]) == families[0]
    with pytest.raises(ValueError, match="choose an offline font"):
        validate_font_family("Brand Typeface That Is Not Installed")


def test_font_manifest_is_the_sandbox_image_source_of_truth() -> None:
    manifest = Path(__file__).resolve().parents[2] / "sandbox" / "available_fonts.txt"
    dockerfile = manifest.parent / "Dockerfile"
    assert "COPY available_fonts.txt /opt/available_fonts.txt" in dockerfile.read_text()
    assert "fc-match" in dockerfile.read_text()
    assert set(available_font_families()) == {
        line.strip()
        for line in manifest.read_text().splitlines()
        if line.strip()
    }


def test_brand_color_rejects_invalid_hex_length() -> None:
    with pytest.raises(ValueError):
        validate_brand_color("#12345")


def test_brand_color_accepts_only_supported_renderable_forms() -> None:
    assert validate_brand_color("#1234") == "#1234"
    assert validate_brand_color("rgb(1, 2, 3)") == "rgb(1, 2, 3)"
    with pytest.raises(ValueError):
        validate_brand_color("crimson")
