from __future__ import annotations

import re

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_FUNCTIONAL_COLOR = re.compile(
    r"^(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color)\([^()]+\)$",
    re.IGNORECASE,
)
_NAMED_COLORS = frozenset(
    {
        "black",
        "blue",
        "brown",
        "cyan",
        "gold",
        "gray",
        "green",
        "indigo",
        "lime",
        "magenta",
        "maroon",
        "navy",
        "olive",
        "orange",
        "pink",
        "purple",
        "red",
        "silver",
        "teal",
        "transparent",
        "violet",
        "white",
        "yellow",
    }
)


def split_brand_color(value: str) -> tuple[str | None, str]:
    candidate = value.strip()
    for separator in ("=", ":"):
        if separator in candidate:
            label, color = candidate.split(separator, 1)
            return label.strip() or None, color.strip()
    return None, candidate


def is_browser_color(value: str) -> bool:
    candidate = value.strip()
    return bool(
        _HEX_COLOR.fullmatch(candidate)
        or _FUNCTIONAL_COLOR.fullmatch(candidate)
        or candidate.casefold() in _NAMED_COLORS
    )


def validate_brand_color(value: str) -> str:
    _, color = split_brand_color(value)
    if not is_browser_color(color):
        raise ValueError(
            f"brand colour must contain a browser-renderable CSS colour value: {value}"
        )
    return color
