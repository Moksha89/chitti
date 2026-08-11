import subprocess
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
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width, name in ((390, "phone"), (1440, "desktop")):
            page = browser.new_page(viewport={"width": width, "height": 900})
            while True:
                try:
                    page.goto("http://127.0.0.1:3000", wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.5)
            page.screenshot(path=f"/workspace/{name}.png", full_page=True)
            page.close()
        browser.close()
finally:
    server.terminate()
    server.wait(timeout=10)
