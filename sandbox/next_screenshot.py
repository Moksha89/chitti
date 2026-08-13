import json
import argparse
import subprocess
import sys
import time
from pathlib import Path


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
                        page.goto("http://127.0.0.1:3000", wait_until="domcontentloaded")
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
                        layout_errors = page.evaluate(
                            """() => {
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
                        )
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
    args = parser.parse_args()
    capture(
        width=args.width if args.poster else None,
        height=args.height if args.poster else None,
        scale=args.scale,
        artifact="poster.png" if args.poster else None,
    )
