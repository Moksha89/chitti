import json
import os
import re
from pathlib import Path


def validate_poster_source(
    source: str,
    available: set[str],
    *,
    colors: tuple[str, ...] = (),
    font: str = "",
    assets: set[str] | None = None,
) -> None:
    if re.search(r"(?:https?:)?//|data:|fetch\s*\(", source, re.IGNORECASE):
        raise SystemExit("poster source contains a remote URL or runtime fetch")
    url_starts = list(re.finditer(r"url\s*\(", source, flags=re.IGNORECASE))
    url_matches = list(re.finditer(r"url\s*\(([^)]*)\)", source, flags=re.IGNORECASE))
    if len(url_matches) != len(url_starts):
        raise SystemExit("poster source contains a remote URL or runtime fetch")
    declared_assets = assets or set()
    references: list[str] = []
    for match in re.finditer(
        r"<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']",
        source,
        re.IGNORECASE,
    ):
        references.append(match.group(1).strip())
    for match in re.finditer(
        r"<image\b[^>]*\b(?:href|xlink:href)\s*=\s*[\"']([^\"']+)[\"']",
        source,
        re.IGNORECASE,
    ):
        references.append(match.group(1).strip())
    for match in url_matches:
        value = match.group(1).strip().strip("'\"").strip()
        if value.startswith("#"):
            continue
        references.append(value)
    for value in references:
        if value.startswith(("/", "\\")) or value not in declared_assets:
            raise SystemExit("poster source contains a remote URL or runtime fetch")
    for declaration in re.findall(
        r"font-family\s*:\s*([^;}]+)", source, flags=re.IGNORECASE
    ):
        for family in declaration.split(","):
            family = family.strip().strip("'\"")
            if (
                family
                and family.casefold() not in available
                and family.casefold()
                not in {
                    "serif",
                    "sans-serif",
                    "monospace",
                }
            ):
                raise SystemExit(
                    f"poster source uses a font outside the offline manifest: {family}"
                )
    for color in colors:
        if color.casefold() not in source.casefold():
            declared = ", ".join(colors)
            raise SystemExit(
                "poster source omits declared brand colour: "
                f"{color} (declared colours: {declared})"
            )
    if font and font.casefold() not in source.casefold():
        raise SystemExit(f"poster source omits declared sandbox font: {font}")


def validate_export_assets(export_root: Path, declared_assets: set[str]) -> None:
    allowed_extensions = {".html", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp"}
    for output in export_root.rglob("*"):
        relative = str(output.relative_to(export_root)).replace("\\", "/")
        if not output.is_file():
            continue
        if output.suffix.lower() not in allowed_extensions:
            raise SystemExit(
                f"poster export contains a non-publishable file at "
                f"'{relative}'; remove it from out/ or move working notes to "
                "artifacts/ (only HTML, CSS, SVG, and declared raster assets "
                "may ship)"
            )
        if (
            output.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            and relative not in declared_assets
        ):
            raise SystemExit(
                f"poster export contains an unverified raster asset at "
                f"'{relative}'; declare it in image_manifest.json and rerun "
                "generate-images, or remove it from out/"
            )


def main() -> None:
    artifact = os.environ.get("CHITTI_POSTER_ARTIFACT", "")
    if not artifact or Path(artifact).is_absolute() or ".." in Path(artifact).parts:
        raise SystemExit("poster artifact path is invalid")
    path = Path("/workspace/out") / artifact
    if not path.is_file():
        raise SystemExit(f"poster artifact is missing: {artifact}")
    if path.suffix.lower() not in {".html", ".svg"}:
        raise SystemExit("poster artifact must be HTML or SVG")
    manifest_path = Path("/workspace/image_manifest.resolved.json")
    declared_assets: set[str] = set()
    if manifest_path.is_file():
        try:
            resolved = json.loads(manifest_path.read_text(encoding="utf-8"))
            declared_assets = {
                str(item["path"]).replace("\\", "/").lstrip("./")
                for item in resolved.get("images", [])
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            raise SystemExit("poster resolved image manifest is invalid")
    validate_export_assets(Path("/workspace/out"), declared_assets)
    source = path.read_text(encoding="utf-8", errors="replace")
    available = {
        line.strip().casefold()
        for line in Path("/opt/available_fonts.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    validate_poster_source(
        source,
        available,
        colors=tuple(
            filter(None, os.environ.get("CHITTI_POSTER_COLORS", "").split("|"))
        ),
        font=os.environ.get("CHITTI_POSTER_FONT", ""),
        assets=declared_assets,
    )
    print(f"poster artifact validated: {artifact}")


if __name__ == "__main__":
    main()
