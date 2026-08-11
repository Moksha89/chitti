from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright


server = ThreadingHTTPServer(("127.0.0.1", 8765), SimpleHTTPRequestHandler)
Thread(target=server.serve_forever, daemon=True).start()
with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto("http://127.0.0.1:8765/index.html")
    page.screenshot(path=str(Path("/workspace/preview.png")), full_page=True)
    browser.close()
server.shutdown()
