import json
import argparse
import subprocess
import sys
import time
from urllib.parse import quote
from pathlib import Path


POSTER_LAYOUT_SCRIPT = """() => {
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const tolerance = 1;
  const candidates = new Set([
    document.documentElement,
    document.body,
    ...document.querySelectorAll(
      "canvas, [data-poster-root], [data-poster-canvas], "
        + "#poster, .poster, #poster-root, .poster-root, "
        + "#poster-canvas, .poster-canvas, #canvas, .canvas"
    ),
  ]);
  for (const element of document.querySelectorAll("*")) {
    const style = getComputedStyle(element);
    const hasDirectText = [...element.childNodes].some(
      (node) =>
        node.nodeType === Node.TEXT_NODE &&
        node.textContent.trim() &&
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        style.opacity !== "0"
    );
    if (hasDirectText) candidates.add(element);
  }
  const bounds = {
    left: 0,
    top: 0,
    right: viewportWidth,
    bottom: viewportHeight,
  };
  const labels = [];
  for (const element of candidates) {
    const rect = element.getBoundingClientRect();
    if (!rect.width && !rect.height) continue;
    bounds.left = Math.min(bounds.left, rect.left);
    bounds.top = Math.min(bounds.top, rect.top);
    bounds.right = Math.max(bounds.right, rect.right);
    bounds.bottom = Math.max(bounds.bottom, rect.bottom);
    const label =
      element.getAttribute("data-name") ||
      element.id ||
      (typeof element.className === "string" ? element.className : "") ||
      element.tagName.toLowerCase();
    labels.push({
      label,
      left: Math.round(rect.left * 100) / 100,
      top: Math.round(rect.top * 100) / 100,
      right: Math.round(rect.right * 100) / 100,
      bottom: Math.round(rect.bottom * 100) / 100,
    });
  }
  const horizontal = Math.max(
    0,
    bounds.left < -tolerance
      ? -bounds.left
      : bounds.right - viewportWidth > tolerance
        ? bounds.right - viewportWidth
        : 0,
  );
  const vertical = Math.max(
    0,
    bounds.top < -tolerance
      ? -bounds.top
      : bounds.bottom - viewportHeight > tolerance
        ? bounds.bottom - viewportHeight
        : 0,
  );
  const errors = [];
  if (horizontal > tolerance) {
    const amount = Math.round(horizontal * 100) / 100;
    const offenders = labels.filter(
      (item) => item.left < -tolerance || item.right > viewportWidth + tolerance,
    );
    errors.push({
      kind: "poster-overflow",
      axis: "horizontal",
      overflow: amount,
      message: `poster overflow: horizontal by ${amount} CSS pixels in ${offenders
        .map((item) => `${item.label} [${item.left},${item.top},${item.right},${item.bottom}]`)
        .join(", ")}`,
      elements: offenders,
    });
  }
  if (vertical > tolerance) {
    const amount = Math.round(vertical * 100) / 100;
    const offenders = labels.filter(
      (item) => item.top < -tolerance || item.bottom > viewportHeight + tolerance,
    );
    errors.push({
      kind: "poster-overflow",
      axis: "vertical",
      message: `poster overflow: vertical by ${amount} CSS pixels in ${offenders
        .map((item) => `${item.label} [${item.left},${item.top},${item.right},${item.bottom}]`)
        .join(", ")}`,
      overflow: amount,
      elements: offenders,
    });
  }
  return errors;
}"""

WEBSITE_LAYOUT_SCRIPT = """() => {
  const errors = [];
  const viewport = document.documentElement.clientWidth;
  if (document.documentElement.scrollWidth > viewport + 1) {
    errors.push({
      kind: "document-overflow",
      scrollWidth: document.documentElement.scrollWidth,
      viewportWidth: viewport,
    });
  }
  for (const element of document.querySelectorAll(
    "h1,h2,h3,h4,p,button,a,li,span"
  )) {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    const text = (element.innerText || "").trim();
    if (!text || style.visibility === "hidden") continue;
    if (
      element.scrollWidth > element.clientWidth + 1 ||
      rect.left < -1 ||
      rect.right > viewport + 1
    ) {
      errors.push({
        kind: "text-overflow",
        tag: element.tagName.toLowerCase(),
        text: text.slice(0, 160),
        left: Math.round(rect.left),
        right: Math.round(rect.right),
        elementWidth: Math.round(rect.width),
        scrollWidth: element.scrollWidth,
        clientWidth: element.clientWidth,
      });
    }
  }
  return errors.slice(0, 50);
}"""


def _serve_export(workspace: Path) -> subprocess.Popen[bytes]:
    export = workspace / "out"
    if not export.is_dir():
        raise RuntimeError(
            f"static export directory is missing: {export}; run export before capture"
        )
    if not any(export.iterdir()):
        raise RuntimeError(
            f"static export directory is empty: {export}; run export before capture"
        )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "http.server",
            "3000",
            "--bind",
            "127.0.0.1",
            "--directory",
            str(export),
        ],
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def capture(
    workspace: Path = Path("/workspace"),
    playwright_factory=None,
    *,
    width: int | None = None,
    height: int | None = None,
    scale: int = 1,
    artifact: str | None = None,
) -> None:
    if playwright_factory is None:
        from playwright.sync_api import sync_playwright

        playwright_factory = sync_playwright
    server = _serve_export(workspace)
    try:
        if artifact is not None:
            artifact_path = workspace / "out" / artifact
            if (
                not artifact
                or ".." in artifact_path.relative_to(workspace / "out").parts
                or not artifact_path.is_file()
            ):
                raise RuntimeError(
                    "poster capture requires the declared artifact to exist: "
                    f"{artifact}"
                )
        deadline = time.monotonic() + 30
        browser_errors = []
        with playwright_factory() as playwright:
            browser = playwright.chromium.launch()
            captures = (
                [(width, height or 900, "poster")]
                if width is not None
                else [(390, 900, "phone"), (1440, 900, "desktop")]
            )
            for capture_width, capture_height, name in captures:
                page = browser.new_page(
                    viewport={"width": capture_width, "height": capture_height},
                    device_scale_factor=scale,
                )
                page.on(
                    "console",
                    lambda message, name=name, width=capture_width: browser_errors.append(
                        {"kind": "console", "width": width, "name": name,
                         "type": message.type, "text": message.text}
                    )
                    if message.type == "error" else None,
                )
                page.on(
                    "pageerror",
                    lambda error, name=name, width=capture_width: browser_errors.append(
                        {"kind": "pageerror", "width": width, "name": name,
                         "text": str(error)}
                    ),
                )
                page.on(
                    "requestfailed",
                    lambda request, name=name, width=capture_width: browser_errors.append(
                        {"kind": "requestfailed", "width": width, "name": name,
                         "url": request.url, "failure": request.failure}
                    )
                    if request.failure else None,
                )
                while True:
                    if server.poll() is not None:
                        output = server.stdout.read().decode(errors="replace") if server.stdout else ""
                        raise RuntimeError(
                            f"static export server exited before capture: {output[-2000:]}"
                        )
                    try:
                        target = (
                            f"/{quote(artifact, safe='/')}"
                            if artifact is not None
                            else "/"
                        )
                        response = page.goto(
                            f"http://127.0.0.1:3000{target}",
                            wait_until="domcontentloaded",
                        )
                        if artifact is not None and response is not None:
                            status = response.status
                            if status != 200:
                                browser_errors.append(
                                    {
                                        "kind": "poster-artifact",
                                        "name": name,
                                        "status": status,
                                        "message": (
                                            "declared poster artifact did not render "
                                            f"successfully: {artifact} returned HTTP {status}"
                                        ),
                                    }
                                )
                        page.wait_for_timeout(2000)
                        body_text = page.locator("body").inner_text()
                        if "Application error:" in body_text:
                            browser_errors.append(
                                {
                                    "kind": "next-error-overlay",
                                  "width": capture_width,
                                    "name": name,
                                    "text": body_text[:2000],
                                }
                            )
                        layout_script = (
                            POSTER_LAYOUT_SCRIPT
                            if artifact is not None
                            else WEBSITE_LAYOUT_SCRIPT
                        )
                        layout_errors = page.evaluate(layout_script)
                        browser_errors.extend(
                            {
                                "kind": "layout",
                                "viewport_width": capture_width,
                                "name": name,
                                **error,
                            }
                            for error in layout_errors
                        )
                        break
                    except Exception:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.5)
                screenshot_path = (
                    workspace / "artifacts" / f"{name}.png"
                    if artifact is None
                    else workspace / "artifacts" / "poster.png"
                )
                page.screenshot(path=str(screenshot_path), full_page=artifact is None)
                page.close()
            browser.close()
        with (workspace / "artifacts/browser-errors.json").open("w", encoding="utf-8") as handle:
            json.dump(browser_errors, handle, indent=2)
        print(json.dumps({"browser_errors": browser_errors}))
        if browser_errors:
            print(
                "BROWSER_FAILURE: " + json.dumps(browser_errors),
                file=sys.stderr,
            )
            raise SystemExit(1)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--scale", type=int, default=1)
    parser.add_argument("--poster", action="store_true")
    parser.add_argument("--artifact")
    args = parser.parse_args()
    capture(
        width=args.width if args.poster else None,
        height=args.height if args.poster else None,
        scale=args.scale,
        artifact=args.artifact if args.poster else None,
    )
