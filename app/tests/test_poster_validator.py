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
    with pytest.raises(SystemExit, match="TRIAL TEAL: #0EA5A8, TRIAL GOLD: #D4AF37"):
        MODULE.validate_poster_source(
            "body { color: #0ea5a8; }",
            {"freesans"},
            colors=("TRIAL TEAL: #0EA5A8", "TRIAL GOLD: #D4AF37"),
        )


def test_css_variable_brand_value_is_recognized() -> None:
    MODULE.validate_poster_source(
        ":root { --trial-teal: #00a; --trial-gold: #fc0; }"
        "body { font-family: FreeSans; }",
        {"freesans"},
        colors=("TRIAL TEAL: #00a", "TRIAL GOLD: #fc0"),
        font="FreeSans",
    )


def test_missing_font_error_names_offline_remediation() -> None:
    with pytest.raises(SystemExit, match=r"font-family: FreeSans"):
        MODULE.validate_poster_source(
            "body { color: #0ea5a8; }",
            {"freesans"},
            colors=("TRIAL TEAL: #0EA5A8",),
            font="FreeSans",
        )


def test_declared_relative_raster_asset_is_allowed() -> None:
    MODULE.validate_poster_source(
        '<img src="generated/stadium.png">'
        '<style>.hero { background-image: url("generated/stadium.png"); }</style>',
        {"freesans"},
        assets={"generated/stadium.png"},
    )


def test_height_only_asset_placement_uses_height_dimension() -> None:
    MODULE._validate_asset_scale(
        '<img src="generated/figure.png" height="1024">',
        {"generated/figure.png": (768, 1024)},
    )


def test_height_only_asset_placement_rejects_oversize_height() -> None:
    with pytest.raises(SystemExit, match="above its pixels"):
        MODULE._validate_asset_scale(
            '<img src="generated/figure.png" height="1025">',
            {"generated/figure.png": (768, 1024)},
        )


def test_max_width_and_min_height_are_not_placements() -> None:
    MODULE._validate_asset_scale(
        '<img src="generated/figure.png" '
        'style="max-width: 1200px; min-height: 1200px">',
        {"generated/figure.png": (768, 1024)},
    )


def test_generated_background_plate_is_required_and_used() -> None:
    MODULE._validate_generated_image_usage(
        '<img src="generated/stadium.png">'
        '<img src="generated/figure.png">',
        [
            {
                "path": "generated/stadium.png",
                "purpose": "full-bleed background plate",
                "cutout": False,
            },
            {
                "path": "generated/figure.png",
                "purpose": "India subject",
                "cutout": True,
            },
        ],
    )


def test_missing_generated_background_plate_is_refused() -> None:
    with pytest.raises(SystemExit, match="generated full-bleed background plate"):
        MODULE._validate_generated_image_usage(
            '<img src="generated/figure.png">',
            [
                {
                    "path": "generated/figure.png",
                    "purpose": "India subject",
                    "cutout": True,
                }
            ],
        )


def test_subject_raw_png_is_refused_without_matted_cutout() -> None:
    with pytest.raises(SystemExit, match="without a matted cutout"):
        MODULE._validate_generated_image_usage(
            '<img src="generated/figure.png">',
            [
                {
                    "path": "generated/stadium.png",
                    "purpose": "background plate",
                    "cutout": False,
                },
                {
                    "path": "generated/figure.png",
                    "purpose": "India subject",
                    "cutout": False,
                },
            ],
        )


def test_scale_validation_without_manifest_is_noop() -> None:
    MODULE._validate_asset_scale(
        '<img src="generated/figure.png" width="2000">',
        {},
        set(),
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
