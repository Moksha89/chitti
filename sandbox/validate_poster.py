import os
import re
from pathlib import Path

artifact = os.environ.get("CHITTI_POSTER_ARTIFACT", "")
if not artifact or Path(artifact).is_absolute() or ".." in Path(artifact).parts:
    raise SystemExit("poster artifact path is invalid")
path = Path("/workspace/out") / artifact
if not path.is_file():
    raise SystemExit(f"poster artifact is missing: {artifact}")
if path.suffix.lower() not in {".html", ".svg"}:
    raise SystemExit("poster artifact must be HTML or SVG")
allowed_extensions = {".html", ".css", ".svg"}
for output in Path("/workspace/out").rglob("*"):
    if output.is_file() and output.suffix.lower() not in allowed_extensions:
        raise SystemExit(f"poster export contains a non HTML/CSS/SVG file: {output.name}")
source = path.read_text(encoding="utf-8", errors="replace")
if re.search(r"https?://|url\s*\(|fetch\s*\(", source, re.IGNORECASE):
    raise SystemExit("poster source contains a remote URL or runtime fetch")
available = {
    line.strip().casefold()
    for line in Path("/opt/available_fonts.txt").read_text().splitlines()
    if line.strip() and not line.lstrip().startswith("#")
}
for declaration in re.findall(
    r"font-family\s*:\s*([^;}]+)", source, flags=re.IGNORECASE
):
    for family in declaration.split(","):
        family = family.strip().strip("'\"")
        if family and family.casefold() not in available and family.casefold() not in {
            "serif",
            "sans-serif",
            "monospace",
        }:
            raise SystemExit(f"poster source uses a font outside the offline manifest: {family}")
for color in filter(None, os.environ.get("CHITTI_POSTER_COLORS", "").split("|")):
    if color.casefold() not in source.casefold():
        raise SystemExit(f"poster source omits declared brand colour: {color}")
font = os.environ.get("CHITTI_POSTER_FONT", "")
if font and font.casefold() not in source.casefold():
    raise SystemExit(f"poster source omits declared sandbox font: {font}")
print(f"poster artifact validated: {artifact}")
