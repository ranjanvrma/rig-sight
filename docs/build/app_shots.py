"""Takes genuine screenshots of the running Streamlit app (real runs, real model) for the deck and report.
Usage: python app_shots.py <repo> <out_dir>"""
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
SCRATCH = Path(__file__).parent
PORT = 8507
URL = f"http://127.0.0.1:{PORT}"

mem = SCRATCH / "shots_memory.sqlite"
shutil.copy(REPO / "data" / "incidents.sqlite", mem)
env = os.environ | {"RIG_LLM": "auto", "RIG_MEMORY_DB": str(mem), "PYTHONIOENCODING": "utf-8"}
srv = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", str(PORT),
                        "--server.headless", "true"], cwd=REPO, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try:
        urllib.request.urlopen(URL, timeout=1)
        break
    except Exception:
        time.sleep(1)


def y_of(page, kind, name, edge="top"):
    loc = (page.get_by_role("heading", name=name, exact=False) if kind == "h"
           else page.get_by_text(name, exact=False))
    bb = loc.first.bounding_box()
    return bb["y"] if edge == "top" else bb["y"] + bb["height"]


def crop(page, name, y0, y1):
    box = page.locator('[data-testid="stMain"]').bounding_box()
    page.screenshot(path=str(OUT / name), clip={"x": box["x"] + 40, "y": max(0, y0),
                                                 "width": box["width"] - 80, "height": y1 - y0})


def top_part(page, name):
    y0 = y_of(page, "h", "Daily Drilling Report") - 175
    crop(page, name, y0, y_of(page, "h", "Where the 24 hours went") - 8)


def pick(page, label):
    page.locator('[data-testid="stSelectbox"]').first.click()
    page.keyboard.type(label)
    page.keyboard.press("Enter")
    page.wait_for_timeout(1500)


def run_agents(page):
    page.get_by_role("button", name="Run agents").click()
    page.get_by_text("Engineer review").first.wait_for(timeout=240_000)
    page.wait_for_timeout(2500)


try:
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge")
        page = b.new_page(viewport={"width": 1600, "height": 2600}, device_scale_factor=1.5)
        page.goto(URL)
        page.get_by_role("button", name="Run agents").wait_for(timeout=120_000)
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "app_home.png"), clip={"x": 0, "y": 0, "width": 1600, "height": 1000})

        # warm the model so the main run is representative
        pick(page, "W-01 · Day 10")
        run_agents(page)
        top_part(page, "app_clean_day.png")

        # the key demo case
        pick(page, "W-02 · Day 14")
        run_agents(page)
        page.screenshot(path=str(OUT / "app_w02d14_full.png"))
        top_part(page, "app_w02d14_top.png")
        crop(page, "app_w02d14_charts.png", y_of(page, "h", "Where the 24 hours went") - 12,
             y_of(page, "t", "Similar past incidents (memory)", "bottom") + 24)
        crop(page, "app_w02d14_report.png", y_of(page, "h", "Daily Insight Report") - 24,
             y_of(page, "t", "Lines labelled by", "bottom") + 30)

        # engineer rejects the slip-and-cut mislabel, then approves
        page.locator('textarea[aria-label="Comment"]').fill("line 7 is planned, not NPT")
        page.get_by_role("button", name="Reject and re-run").click()
        page.get_by_text("Engineer REJECTED").first.wait_for(timeout=240_000)
        page.get_by_role("button", name="Approve").wait_for(timeout=240_000)
        page.wait_for_timeout(3000)
        top_part(page, "app_w02d14_after_reject.png")
        page.locator('textarea[aria-label="Comment"]').fill("Agree. Carry a backup MWD on the next run.")
        page.get_by_role("button", name="Approve").click()
        page.get_by_text("Computed · Sourced · Approved").first.wait_for(timeout=120_000)
        page.wait_for_timeout(1500)
        crop(page, "app_w02d14_approved.png", y_of(page, "h", "Daily Insight Report") - 24,
             y_of(page, "t", "Lines labelled by", "bottom") + 30)

        # custom report with an invalid timestamp
        pick(page, "Custom report")
        run_agents(page)
        top_part(page, "app_custom_error.png")
        crop(page, "app_custom_report.png", y_of(page, "h", "Daily Insight Report") - 24,
             y_of(page, "t", "Lines labelled by", "bottom") + 30)

        # evaluation and memory tabs
        page.get_by_role("tab", name="Evaluation").click()
        page.wait_for_timeout(2000)
        crop(page, "app_evaluation.png", y_of(page, "h", "Evaluation on all 15 reports") - 16,
             y_of(page, "t", "The mock's keywords", "bottom") + 24)
        page.get_by_role("tab", name="Memory").click()
        page.wait_for_timeout(2000)
        crop(page, "app_memory.png", y_of(page, "h", "Long-term memory") - 16,
             y_of(page, "t", "Engineer decisions", "bottom") + 260)
        b.close()
finally:
    srv.terminate()
print("ok", sorted(f.name for f in OUT.glob("app_*.png")))
