from __future__ import annotations

import json
from dataclasses import dataclass

WEBSITE_JOB = "website"
POSTER_JOB = "poster"
DEFAULT_JOB = WEBSITE_JOB
MAX_POSTER_CSS_DIMENSION = 2048
MAX_POSTER_SCALE = 2


@dataclass(frozen=True)
class JobTypePolicy:
    name: str
    required_gates: tuple[str, ...]
    model_commands: tuple[str, ...]

    @property
    def is_poster(self) -> bool:
        return self.name == POSTER_JOB


WEBSITE_POLICY = JobTypePolicy(
    WEBSITE_JOB,
    ("build", "test", "export"),
    ("sync-lockfile", "install", "build", "test", "export"),
)
POSTER_POLICY = JobTypePolicy(
    POSTER_JOB,
    ("poster-export", "capture_screenshot"),
    ("poster-export",),
)


def policy_for(job_type: str | None) -> JobTypePolicy:
    if job_type == POSTER_JOB:
        return POSTER_POLICY
    return WEBSITE_POLICY


def normalize_job_type(value: object) -> str:
    candidate = str(value or DEFAULT_JOB).strip().lower()
    if candidate not in {WEBSITE_JOB, POSTER_JOB}:
        raise ValueError("job type must be website or poster")
    return candidate


def poster_config(value: object) -> dict[str, object]:
    if value is None:
        return {"artifact": "poster.html", "width": 1080, "height": 1350, "scale": 1}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("poster configuration must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("poster configuration must be an object")
    artifact = str(value.get("artifact", "poster.html")).strip()
    width = int(value.get("width", 1080))
    height = int(value.get("height", 1350))
    scale = int(value.get("scale", 1))
    if not artifact or artifact.startswith("/") or ".." in artifact.split("/"):
        raise ValueError("poster artifact must be a relative export path")
    if width < 1 or height < 1 or width > MAX_POSTER_CSS_DIMENSION or height > MAX_POSTER_CSS_DIMENSION:
        raise ValueError(
            f"poster dimensions must be between 1 and {MAX_POSTER_CSS_DIMENSION} CSS pixels"
        )
    if scale < 1 or scale > MAX_POSTER_SCALE:
        raise ValueError(f"poster device scale must be between 1 and {MAX_POSTER_SCALE}")
    if width * height * scale * scale * 4 > 256 * 1024 * 1024:
        raise ValueError("poster dimensions exceed the 256 MiB browser cage budget")
    return {"artifact": artifact, "width": width, "height": height, "scale": scale}


def config_json(value: object) -> str:
    return json.dumps(poster_config(value), separators=(",", ":"))


def poster_config_within_ceiling(
    requested: object,
    approved: object,
) -> dict[str, object]:
    requested_config = poster_config(requested)
    approved_config = poster_config(approved)
    requested_width = int(str(requested_config["width"]))
    requested_height = int(str(requested_config["height"]))
    requested_scale = int(str(requested_config["scale"]))
    approved_width = int(str(approved_config["width"]))
    approved_height = int(str(approved_config["height"]))
    approved_scale = int(str(approved_config["scale"]))
    if requested_config["artifact"] != approved_config["artifact"]:
        raise ValueError(
            "poster artifact does not match approved plan: "
            f"requested {requested_config['artifact']}, "
            f"approved {approved_config['artifact']}"
        )
    if (
        requested_width > approved_width
        or requested_height > approved_height
        or requested_scale > approved_scale
    ):
        raise ValueError(
            "poster format exceeds approved plan: "
            f"requested {requested_width}x{requested_height} "
            f"at scale {requested_scale}, "
            f"approved {approved_width}x{approved_height} "
            f"at scale {approved_scale}"
        )
    return requested_config
