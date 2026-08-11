import json
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

server = subprocess.Popen(
    ["npm", "run", "start", "--", "-p", "3000", "-H", "127.0.0.1"],
    cwd="/workspace",
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
try:
    deadline = time.monotonic() + 30
    browser_errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width, name in ((390, "phone"), (1440, "desktop")):
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.on(
                "console",
                lambda message, name=name, width=width: browser_errors.append(
                    {"kind": "console", "width": width, "name": name,
                     "type": message.type, "text": message.text}
                )
                if message.type == "error" else None,
            )
            page.on(
                "pageerror",
                lambda error, name=name, width=width: browser_errors.append(
                    {"kind": "pageerror", "width": width, "name": name,
                     "text": str(error)}
                ),
            )
            page.on(
                "requestfailed",
                lambda request, name=name, width=width: browser_errors.append(
                    {"kind": "requestfailed", "width": width, "name": name,
                     "url": request.url, "failure": request.failure}
                )
                if request.failure else None,
            )
            while True:
                try:
                    page.goto("http://127.0.0.1:3000", wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    body_text = page.locator("body").inner_text()
                    if "Application error:" in body_text:
                        browser_errors.append(
                            {
                                "kind": "next-error-overlay",
                                "width": width,
                                "name": name,
                                "text": body_text[:2000],
                            }
                        )
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.5)
            page.screenshot(path=f"/workspace/artifacts/{name}.png", full_page=True)
            page.close()
        browser.close()
    with open("/workspace/artifacts/browser-errors.json", "w", encoding="utf-8") as handle:
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
    server.wait(timeout=10)
