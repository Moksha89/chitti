import json
import os
import re
from pathlib import Path

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_FUNCTIONAL_COLOR = re.compile(
    r"^(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color)\([^()]+\)$",
    re.IGNORECASE,
)


def _split_brand_color(value: str) -> tuple[str | None, str]:
    candidate = value.strip()
    for separator in ("=", ":"):
        if separator in candidate:
            label, color = candidate.split(separator, 1)
            return label.strip() or None, color.strip()
    return None, candidate


def _contains_declared_value(source: str, value: str) -> bool:
    _, value = _split_brand_color(value)
    if value.casefold() in source.casefold():
        return True
    tokens = [token for token in re.split(r"[^A-Za-z0-9]+", value) if token]
    if not tokens:
        return False
    pattern = r"[^A-Za-z0-9]+".join(re.escape(token) for token in tokens)
    return re.search(pattern, source, flags=re.IGNORECASE) is not None


def _first_line_containing(source: str, value: str) -> int | None:
    for number, line in enumerate(source.splitlines(), start=1):
        if _contains_declared_value(line, value):
            return number
    return None


def validate_poster_source(
    source: str,
    available: set[str],
    *,
    colors: tuple[str, ...] = (),
    font: str = "",
    assets: set[str] | None = None,
) -> None:
    def offline_refusal(match: re.Match[str]) -> SystemExit:
        line = source.count("\n", 0, match.start()) + 1
        snippet = source.splitlines()[line - 1].strip()[:180]
        return SystemExit(
            "poster source contains a remote URL or runtime fetch at line "
            f"{line}: {snippet!r}; use a declared local asset under out/generated "
            "or an offline CSS/SVG fragment such as url(#gradient)"
        )

    network_match = re.search(
        r"(?:https?:)?//|data:|fetch\s*\(", source, re.IGNORECASE
    )
    if network_match:
        raise offline_refusal(network_match)
    url_starts = list(re.finditer(r"url\s*\(", source, flags=re.IGNORECASE))
    url_matches = list(re.finditer(r"url\s*\(([^)]*)\)", source, flags=re.IGNORECASE))
    if len(url_matches) != len(url_starts):
        raise offline_refusal(url_starts[-1])
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
            match = re.search(re.escape(value), source)
            raise offline_refusal(match or re.search(r"\S+", source))
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
        _, value = _split_brand_color(color)
        if not _contains_declared_value(source, value):
            declared = ", ".join(colors)
            raise SystemExit(
                "poster source omits declared brand colour: "
                f"{color} (required renderable value: {value}; declared colours: "
                f"{declared}); add the value to a visible style or a CSS variable "
                "used by the poster"
            )
    if font and not _contains_declared_value(source, font):
        raise SystemExit(
            f"poster source omits declared sandbox font: {font}; add "
            f"`font-family: {font}` to the poster stylesheet"
        )


def validate_export_assets(export_root: Path, declared_assets: set[str]) -> None:
    allowed_extensions = {".html", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp"}
    for output in export_root.rglob("*"):
        relative = str(output.relative_to(export_root)).replace("\\", "/")
        if not output.is_file():
            continue
        if output.suffix.lower() not in allowed_extensions:
            if output.name in {"image_manifest.json", "image_manifest.resolved.json"}:
                raise SystemExit(
                    f"poster export contains {output.name} in out/; keep the "
                    "image manifest at the workspace root "
                    f"(/workspace/{output.name}), not in out/"
                )
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
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise SystemExit("poster resolved image manifest is invalid") from exc
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
