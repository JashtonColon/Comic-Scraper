import os
import time
import zipfile
import requests
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --- CONFIGURATION ---
FINAL_ROOT = "finalcomics"
SUB_PATH = "Batman"
FOLDER_NAME = "Long Halloween"
FILENAME_PREFIX = "LongHalloween"
ARCHIVE_EXT = ".cbr"   # contents are a ZIP, so ".cbz" is technically the correct extension

# Link to the FIRST issue you want (series id / issue id)
START_URL = "https://batcave.biz/reader/9264/60124"
START_ISSUE = 1
END_ISSUE = 4

# --- BROWSER SETTINGS ---
# USE_MY_CHROME = True: the script attaches to a Chrome window you started yourself,
# which gets past Cloudflare. Before running the script, open Command Prompt and run:
#
#   "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome-scraper"
#
# In that Chrome window, go to batcave.biz, pass the check, open any comic once,
# then leave the window open and run this script.
USE_MY_CHROME = True
DEBUG_PORT = 9222
HEADLESS = False       # only used when USE_MY_CHROME = False

# --- SPEED SETTINGS ---
PAGE_WORKERS = 8       # parallel image downloads per issue
STEP_DELAY = 0.3       # seconds between page turns
MAX_STALLS = 8         # give up flipping after this many turns with no progress

# Reads every page slot in the reader and whatever image URL it currently has
COLLECT_JS = """
() => {
  const wraps = Array.from(document.querySelectorAll('.reader__item-wrap'));
  const active = document.querySelector('.reader__item-wrap.active');
  return {
    total: wraps.length,
    active: active ? active.dataset.page : null,
    pages: wraps.map(w => {
      const img = w.querySelector('img');
      const src = img ? (img.currentSrc || img.getAttribute('src') || img.dataset.src || '') : '';
      return [parseInt(w.dataset.page), src];
    })
  };
}
"""

ACTIVE_JS = "() => { const a = document.querySelector('.reader__item-wrap.active'); return a ? a.dataset.page : null; }"


def wait_for_active_change(page, prev, timeout=1.5):
    end = time.time() + timeout
    while time.time() < end:
        if page.evaluate(ACTIVE_JS) != prev:
            return True
        time.sleep(0.1)
    return False


def turn_page(page, prev_active):
    """Go to the next page: try the arrow key first, then click the right side of the reader."""
    page.keyboard.press("ArrowRight")
    if wait_for_active_change(page, prev_active):
        return True
    vp = page.viewport_size or {"width": 1280, "height": 900}
    page.mouse.click(int(vp["width"] * 0.85), int(vp["height"] * 0.5))
    return wait_for_active_change(page, prev_active)


def collect_image_urls(page, url, first_load=False):
    print("   🌐 Opening reader...")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # Long wait on the very first load in case a Cloudflare check needs solving
    print("   ⏳ Waiting for reader pages to appear...")
    try:
        page.wait_for_selector(".reader__item-wrap, img.reader__item",
                               timeout=90000 if first_load else 45000)
    except PWTimeout:
        page.screenshot(path="debug_stuck.png", full_page=True)
        with open("debug_stuck.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        print(f"   📸 Reader didn't load. Page title: '{page.title()}'")
        print("   📸 Saved debug_stuck.png and debug_stuck.html - send them to Claude")
        raise
    time.sleep(1)

    found = {}
    stalls = 0
    while True:
        data = page.evaluate(COLLECT_JS)
        before = len(found)
        for num, src in data["pages"]:
            if src and not src.startswith("data:") and "batcave" in src and num not in found:
                found[num] = src

        total = data["total"]
        print(f"   🔎 On page {data['active']} | image URLs found: {len(found)}/{total}")
        if total and len(found) >= total:
            break

        stalls = 0 if len(found) > before else stalls + 1
        if stalls >= MAX_STALLS:
            print(f"   ⚠️ Stopped flipping at {len(found)}/{total} pages")
            break

        turn_page(page, data["active"])
        time.sleep(STEP_DELAY)

    return [found[k] for k in sorted(found)]


def make_session(context, user_agent):
    s = requests.Session()
    s.headers["User-Agent"] = user_agent
    for c in context.cookies():
        s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
    return s


def ext_for(content_type, url):
    ct = (content_type or "").lower()
    if "webp" in ct:
        return ".webp"
    if "png" in ct:
        return ".png"
    low = url.lower().split("?")[0]
    for e in (".webp", ".png", ".jpeg"):
        if low.endswith(e):
            return e
    return ".jpg"


def download_with_requests(session, url, referer):
    headers = {
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = session.get(url, headers=headers, timeout=20)
        if r.status_code == 200 and r.content:
            return ext_for(r.headers.get("Content-Type"), url), r.content
    except Exception:
        pass
    return None, None


def download_with_browser(context, url, referer):
    try:
        r = context.request.get(url, headers={"Referer": referer}, timeout=15000)
        if r.ok:
            return ext_for(r.headers.get("content-type"), url), r.body()
        print(f"   ⚠️ Image failed: {url[:80]}... (Status: {r.status})")
    except Exception as e:
        print(f"   ⚠️ Image error: {url[:80]}... ({e})")
    return None, None


def process_issue(issue_num, series_id, start_issue_id, page, context, user_agent, first_load):
    issue_padded = str(issue_num).zfill(3)
    final_dir = os.path.join(FINAL_ROOT, SUB_PATH, FOLDER_NAME) if SUB_PATH else os.path.join(FINAL_ROOT, FOLDER_NAME)
    os.makedirs(final_dir, exist_ok=True)
    final_save_path = os.path.join(final_dir, f"{FILENAME_PREFIX}_{issue_padded}{ARCHIVE_EXT}")

    if os.path.exists(final_save_path):
        return f"⏭️  Skipping {issue_num}"

    issue_id = start_issue_id + (issue_num - START_ISSUE)
    active_url = f"https://batcave.biz/reader/{series_id}/{issue_id}"
    print(f"📖 Issue {issue_num}: {active_url}")

    try:
        urls = collect_image_urls(page, active_url, first_load)
    except PWTimeout:
        return f"❌ Reader never loaded for issue {issue_num}"
    if not urls:
        return f"❌ No image URLs found for issue {issue_num}"

    # Fast path: parallel downloads with the browser's cookies
    print(f"   ⬇️  Downloading {len(urls)} pages...")
    session = make_session(context, user_agent)
    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
        results = list(ex.map(lambda u: download_with_requests(session, u, active_url), urls))

    failed = [i for i, r in enumerate(results) if r[1] is None]
    print(f"   ✔️  Fast download: {len(urls) - len(failed)}/{len(urls)} OK")

    # Fallback: anything that failed gets retried through the browser itself
    for n, i in enumerate(failed, 1):
        print(f"   🔁 Browser retry {n}/{len(failed)}")
        results[i] = download_with_browser(context, urls[i], active_url)

    valid_pages = [p for p in results if p[1] is not None]
    if not valid_pages:
        return f"❌ No valid images downloaded for {issue_num}"

    with zipfile.ZipFile(final_save_path, "w") as archive:
        for idx, (ext, content) in enumerate(valid_pages):
            archive.writestr(f"{str(idx + 1).zfill(3)}{ext}", content)

    missing = len(urls) - len(valid_pages)
    note = f", {missing} missing" if missing else ""
    return f"✅ Issue {issue_num} saved (Pages: {len(valid_pages)}{note})"


if __name__ == "__main__":
    print(f"📡 Starting Scrape. Nested Path: {os.path.join(FINAL_ROOT, SUB_PATH or '', FOLDER_NAME)}")

    parts = START_URL.split("#")[0].strip("/").split("/")
    start_issue_id = int(parts[-1])
    series_id = parts[-2]

    with sync_playwright() as p:
        if USE_MY_CHROME:
            # Attach to a Chrome window YOU started (see instructions at top of file)
            try:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
            except Exception:
                print("❌ Couldn't connect to Chrome. Start it with the command at the top of main.py first.")
                raise SystemExit(1)
            context = browser.contexts[0]
        else:
            browser = p.chromium.launch(channel="chrome", headless=HEADLESS,
                                        args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(viewport={"width": 1280, "height": 900})

        page = context.new_page()
        user_agent = page.evaluate("navigator.userAgent")

        first = True
        for issue in range(START_ISSUE, END_ISSUE + 1):
            print(process_issue(issue, series_id, start_issue_id, page, context, user_agent, first))
            first = False

        page.close()   # leaves your Chrome window open
        if not USE_MY_CHROME:
            browser.close()
