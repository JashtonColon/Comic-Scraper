# =====================================================================
#  WHAT TO DOWNLOAD  -  change these for each comic run
# =====================================================================

# Link to the FIRST issue you want. Format: https://batcave.biz/reader/<series id>/<issue id>
START_URL = "https://batcave.biz/reader/9264/60124"
START_ISSUE = 1          # number used for the first file (LongHalloween_001)
END_ISSUE = 4       # last issue number to download

# Where the files are saved:  FINAL_ROOT \ SUB_PATH \ FOLDER_NAME \ FILENAME_PREFIX_001.cbz
FINAL_ROOT = "finalcomics"
SUB_PATH = ""              # set to "" to skip this level
FOLDER_NAME = "testNew"
FILENAME_PREFIX = "tets"
ARCHIVE_EXT = ".cbz"             # ZIP comic archive; works in Calibre and ComicsPlusPlus


# =====================================================================
#  CHROME SETUP  -  set once for your computer, then leave alone
# =====================================================================

# Where Chrome is installed. Other common spot:
#   C:\Program Files (x86)\Google\Chrome\Application\chrome.exe
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# Separate Chrome profile the scraper uses (keeps your Cloudflare pass between runs)
CHROME_PROFILE = r"C:\chrome-scraper"

USE_MY_CHROME = True     # True = open/reuse that normal Chrome window (gets past Cloudflare)
DEBUG_PORT = 9222        # only change if something else uses this port
HEADLESS = False         # only used when USE_MY_CHROME = False


# =====================================================================
#  ADVANCED  -  usually no need to touch
# =====================================================================

SITE_URL = "https://batcave.biz"
PAGE_WORKERS = 8         # parallel image downloads per issue
STEP_DELAY = 0.3         # seconds between page turns (lower = faster)
MAX_STALLS = 8           # give up flipping after this many turns with no progress
JPEG_QUALITY = 92        # quality when converting WebP/AVIF pages to JPEG
DEBUG_SCREENSHOT = "debug_stuck.png"
DEBUG_HTML = "debug_stuck.html"


# =====================================================================
#  CODE BELOW  -  no settings past this line
# =====================================================================

import io
import os
import subprocess
import time
import zipfile
import requests
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


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
        page.screenshot(path=DEBUG_SCREENSHOT, full_page=True)
        with open(DEBUG_HTML, "w", encoding="utf-8") as f:
            f.write(page.content())
        print(f"   📸 Reader didn't load. Page title: '{page.title()}'")
        print(f"   📸 Saved {DEBUG_SCREENSHOT} and {DEBUG_HTML} - send them to Claude")
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


def sniff_format(data):
    """Identify the real image type from the file's first bytes (ignores what the server claims)."""
    if not data or len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def normalize_image(data, url):
    """Return (ext, bytes) as JPEG/PNG that any comic reader can open, or (None, None) if not an image."""
    fmt = sniff_format(data)
    if fmt in ("jpg", "png"):
        return "." + fmt, data
    if fmt is None:
        preview = data[:80].decode("utf-8", "replace").replace("\n", " ") if data else "<empty>"
        print(f"   ⚠️ Not an image: {url[:70]}... starts with: {preview!r}")
        return None, None
    # webp / avif / gif -> convert to JPEG for reader compatibility
    try:
        from PIL import Image
        if fmt == "avif":
            try:
                import pillow_avif  # noqa: F401  (only needed on older Pillow)
            except ImportError:
                pass
        img = Image.open(io.BytesIO(data)).convert("RGB")
        out = io.BytesIO()
        img.save(out, "JPEG", quality=JPEG_QUALITY)
        return ".jpg", out.getvalue()
    except Exception as e:
        print(f"   ⚠️ Couldn't convert {fmt} page ({e}); saving as-is")
        return "." + fmt, data


def download_with_requests(session, url, referer):
    headers = {
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = session.get(url, headers=headers, timeout=20)
        if r.status_code == 200 and r.content:
            return normalize_image(r.content, url)
    except Exception:
        pass
    return None, None


def download_with_browser(context, url, referer):
    try:
        r = context.request.get(url, headers={"Referer": referer}, timeout=15000)
        if r.ok:
            return normalize_image(r.body(), url)
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
    active_url = f"{SITE_URL}/reader/{series_id}/{issue_id}"
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
            # Attach to the scraper Chrome window, starting it first if it isn't open
            browser = None
            try:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
            except Exception:
                if not os.path.exists(CHROME_PATH):
                    print(f"❌ Chrome not found at {CHROME_PATH} - fix CHROME_PATH at the top of main.py")
                    raise SystemExit(1)
                print("🚀 Opening Chrome... if you see a 'verify you are human' check, click it.")
                subprocess.Popen([CHROME_PATH, f"--remote-debugging-port={DEBUG_PORT}",
                                  f"--user-data-dir={CHROME_PROFILE}", SITE_URL + "/"])
                for _ in range(30):
                    time.sleep(1)
                    try:
                        browser = p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
                        break
                    except Exception:
                        pass
                if browser is None:
                    print("❌ Chrome opened but the script couldn't connect. Close ALL Chrome windows and try again.")
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
