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


def test_network_refusal_names_source_line_and_offline_alternative() -> None:
    with pytest.raises(
        SystemExit,
        match=r"line 2:.*declared local asset.*url\(#gradient\)",
    ):
        MODULE.validate_poster_source(
            "<style>body { color: white; }</style>\n"
            '<img src="https://example.test/image.png">',
            {"freesans"},
        )


def test_missing_colour_error_separates_declared_colours() -> None:
    with pytest.raises(SystemExit, match="TRIAL TEAL, TRIAL GOLD"):
        MODULE.validate_poster_source(
            "body { color: TRIAL TEAL; }",
            {"freesans"},
            colors=("TRIAL TEAL", "TRIAL GOLD"),
        )


def test_declared_relative_raster_asset_is_allowed() -> None:
    MODULE.validate_poster_source(
        '<img src="generated/stadium.png">'
        '<style>.hero { background-image: url("generated/stadium.png"); }</style>',
        {"freesans"},
        assets={"generated/stadium.png"},
    )


def test_undeclared_relative_raster_asset_is_refused() -> None:
    with pytest.raises(SystemExit, match="remote URL or runtime fetch"):
        MODULE.validate_poster_source(
            '<img src="generated/unknown.png">',
            {"freesans"},
            assets={"generated/stadium.png"},
        )


def test_undeclared_svg_image_href_is_refused() -> None:
    with pytest.raises(SystemExit, match="remote URL or runtime fetch"):
        MODULE.validate_poster_source(
            '<svg><image href="generated/unknown.png"/></svg>',
            {"freesans"},
            assets={"generated/stadium.png"},
        )


def test_export_notes_name_relative_path_and_artifacts_remedy(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "evidence.json"
    output.parent.mkdir()
    output.write_text("{}")
    with pytest.raises(SystemExit, match="nested/evidence.json"):
        MODULE.validate_export_assets(tmp_path, set())
    with pytest.raises(SystemExit, match="artifacts/"):
        MODULE.validate_export_assets(tmp_path, set())


def test_export_manifest_in_out_names_workspace_root_remedy(tmp_path: Path) -> None:
    output = tmp_path / "image_manifest.json"
    output.write_text("{}")
    with pytest.raises(SystemExit, match=r"/workspace/image_manifest.json"):
        MODULE.validate_export_assets(tmp_path, set())


def test_export_raster_refusal_names_manifest_remedy(tmp_path: Path) -> None:
    output = tmp_path / "generated" / "figure.png"
    output.parent.mkdir()
    output.write_bytes(b"png")
    with pytest.raises(SystemExit, match="generated/figure.png"):
        MODULE.validate_export_assets(tmp_path, set())
    with pytest.raises(SystemExit, match="rerun generate-images"):
        MODULE.validate_export_assets(tmp_path, set())
