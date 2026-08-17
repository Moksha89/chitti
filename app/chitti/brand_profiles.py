from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .brand_colors import validate_brand_color
from .memory import normalize_namespace
from .namespaces import SHARED_NAMESPACE
from .runner_access import application_only_sql, runner_sql


def _font_manifest_path() -> Path:
    configured = os.environ.get("CHITTI_FONT_MANIFEST")
    if configured:
        return Path(configured)
    container_path = Path("/app/sandbox/available_fonts.txt")
    if container_path.is_file():
        return container_path
    return Path(__file__).resolve().parents[2] / "sandbox" / "available_fonts.txt"


def available_font_families() -> tuple[str, ...]:
    path = _font_manifest_path()
    return tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def validate_font_family(value: str) -> str:
    candidate = value.strip()
    available = available_font_families()
    matches = {family.casefold(): family for family in available}
    if candidate.casefold() not in matches:
        raise ValueError(
            "choose an offline font available in the sandbox: "
            + ", ".join(available)
        )
    return matches[candidate.casefold()]


@dataclass(frozen=True)
class BrandProfile:
    namespace: str
    brand_colors: tuple[str, ...]
    typography: str
    poster_formats: tuple[str, ...]
    audience: str
    voice: str
    do_not_use: tuple[str, ...]
    updated_by: str
    updated_at: datetime


def _profile_from_row(row: Any) -> BrandProfile:
    return BrandProfile(
        namespace=str(row["namespace"]),
        brand_colors=tuple(str(item) for item in row["brand_colors"]),
        typography=str(row["typography"]),
        poster_formats=tuple(str(item) for item in row["poster_formats"]),
        audience=str(row["audience"]),
        voice=str(row["voice"]),
        do_not_use=tuple(str(item) for item in row["do_not_use"]),
        updated_by=str(row["updated_by"]),
        updated_at=row["updated_at"],
    )


def _clean_list(values: list[str], field: str, *, required: bool = True) -> list[str]:
    cleaned = [value.strip() for value in values if value.strip()]
    if required and not cleaned:
        raise ValueError(f"{field} must contain at least one value")
    return cleaned


def validate_profile(
    *,
    brand_colors: list[str],
    typography: str,
    poster_formats: list[str],
    audience: str,
    voice: str,
    do_not_use: list[str],
) -> dict[str, object]:
    colors = _clean_list(brand_colors, "brand colours")
    if any(len(color) > 64 for color in colors):
        raise ValueError("brand colours must be 64 characters or fewer")
    for color in colors:
        validate_brand_color(color)
    formats = _clean_list(poster_formats, "poster and social formats")
    if len(audience.strip()) == 0 or len(voice.strip()) == 0:
        raise ValueError("audience and voice are required")
    return {
        "brand_colors": colors,
        "typography": validate_font_family(typography),
        "poster_formats": formats,
        "audience": audience.strip(),
        "voice": voice.strip(),
        "do_not_use": _clean_list(do_not_use, "do-not-use list", required=False),
    }


async def get_brand_profile(
    session: AsyncSession, namespace: str = SHARED_NAMESPACE
) -> BrandProfile | None:
    namespace = normalize_namespace(namespace)
    result = await session.execute(
        runner_sql(text(
            "SELECT namespace, brand_colors, typography, poster_formats, audience, voice, "
            "do_not_use, updated_by, updated_at FROM brand_profiles "
            "WHERE namespace IN (:namespace, :shared) "
            "ORDER BY namespace = :namespace DESC LIMIT 1"
        )),
        {"namespace": namespace, "shared": SHARED_NAMESPACE},
    )
    row = result.mappings().one_or_none()
    return _profile_from_row(row) if row is not None else None


async def save_brand_profile(
    session: AsyncSession,
    namespace: str,
    *,
    brand_colors: list[str],
    typography: str,
    poster_formats: list[str],
    audience: str,
    voice: str,
    do_not_use: list[str],
    actor: str,
) -> BrandProfile:
    namespace = normalize_namespace(namespace)
    values = validate_profile(
        brand_colors=brand_colors,
        typography=typography,
        poster_formats=poster_formats,
        audience=audience,
        voice=voice,
        do_not_use=do_not_use,
    )
    snapshot = json.dumps(values)
    await session.execute(
            application_only_sql(text(
                "INSERT INTO brand_profile_history "
            "(namespace, profile, changed_by) VALUES "
            "(:namespace, CAST(:profile AS jsonb), :actor)"
            )),
        {"namespace": namespace, "profile": snapshot, "actor": actor},
    )
    result = await session.execute(
            application_only_sql(text(
                "INSERT INTO brand_profiles "
            "(namespace, brand_colors, typography, poster_formats, audience, voice, "
            "do_not_use, updated_by) VALUES "
            "(:namespace, CAST(:brand_colors AS jsonb), :typography, "
            "CAST(:poster_formats AS jsonb), :audience, :voice, "
            "CAST(:do_not_use AS jsonb), :actor) "
            "ON CONFLICT (namespace) DO UPDATE SET "
            "brand_colors = EXCLUDED.brand_colors, typography = EXCLUDED.typography, "
            "poster_formats = EXCLUDED.poster_formats, audience = EXCLUDED.audience, "
            "voice = EXCLUDED.voice, do_not_use = EXCLUDED.do_not_use, "
            "updated_by = EXCLUDED.updated_by, updated_at = now() "
            "RETURNING namespace, brand_colors, typography, poster_formats, audience, "
            "voice, do_not_use, updated_by, updated_at"
            )),
        {
            "namespace": namespace,
            "brand_colors": json.dumps(values["brand_colors"]),
            "typography": values["typography"],
            "poster_formats": json.dumps(values["poster_formats"]),
            "audience": values["audience"],
            "voice": values["voice"],
            "do_not_use": json.dumps(values["do_not_use"]),
            "actor": actor,
        },
    )
    return _profile_from_row(result.mappings().one())


async def remove_brand_profile(
    session: AsyncSession, namespace: str, *, actor: str
) -> bool:
    namespace = normalize_namespace(namespace)
    result = await session.execute(
        application_only_sql(
            text(
                "SELECT namespace, brand_colors, typography, poster_formats, "
                "audience, voice, do_not_use, updated_by, updated_at "
                "FROM brand_profiles WHERE namespace = :namespace"
            )
        ),
        {"namespace": namespace},
    )
    row = result.mappings().one_or_none()
    if row is None:
        return False
    profile = _profile_from_row(row)
    snapshot = json.dumps(
        {
            "action": "removed",
            "brand_colors": list(profile.brand_colors),
            "typography": profile.typography,
            "poster_formats": list(profile.poster_formats),
            "audience": profile.audience,
            "voice": profile.voice,
            "do_not_use": list(profile.do_not_use),
            "updated_by": profile.updated_by,
            "updated_at": profile.updated_at.isoformat(),
        }
    )
    await session.execute(
        application_only_sql(
            text(
                "INSERT INTO brand_profile_history "
                "(namespace, profile, changed_by) VALUES "
                "(:namespace, CAST(:profile AS jsonb), :actor)"
            )
        ),
        {"namespace": namespace, "profile": snapshot, "actor": actor},
    )
    await session.execute(
        application_only_sql(
            text("DELETE FROM brand_profiles WHERE namespace = :namespace")
        ),
        {"namespace": namespace},
    )
    return True
