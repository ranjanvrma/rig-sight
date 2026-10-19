"""Renders the architecture diagram (dark + light) to PNG with the installed Edge browser."""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge")
    pg = b.new_page(viewport={"width": 1600, "height": 960}, device_scale_factor=2)
    for theme in ["dark", "light"]:
        pg.goto((HERE / "architecture.html").as_uri() + ("?light" if theme == "light" else ""))
        pg.wait_for_timeout(400)
        pg.locator("#d").screenshot(path=str(OUT / f"architecture_{theme}.png"))
    b.close()
print("ok")
