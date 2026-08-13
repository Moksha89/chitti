from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "sandbox" / "validate_poster.py"
)
SPEC = importlib.util.spec_from_file_location("validate_poster", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_same_document_fragment_url_is_allowed() -> None:
    MODULE.validate_poster_source(
        '<svg><linearGradient id="gradient"/></svg>'
        '<style>.shape { fill: url(#gradient); }</style>',
        {"freesans"},
    )


@pytest.mark.parametrize(
    "source",
    [
        '<img src="https://example.test/image.png">',
        '<img src="//example.test/image.png">',
        '<img src="data:image/png;base64,AAAA">',
        '<script>fetch("/remote")</script>',
        "<style>.shape { fill: url(data:image/png;base64,AAAA); }</style>",
        "<style>.shape { fill: url(https://example.test/image.png); }</style>",
    ],
)
def test_network_and_fetch_forms_are_refused(source: str) -> None:
    with pytest.raises(SystemExit, match="remote URL or runtime fetch"):
        MODULE.validate_poster_source(source, {"freesans"})


def test_missing_colour_error_separates_declared_colours() -> None:
    with pytest.raises(SystemExit, match="TRIAL TEAL, TRIAL GOLD"):
        MODULE.validate_poster_source(
            "body { color: TRIAL TEAL; }",
            {"freesans"},
            colors=("TRIAL TEAL", "TRIAL GOLD"),
        )
