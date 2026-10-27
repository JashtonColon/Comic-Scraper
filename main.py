# =====================================================================
#  WHAT TO DOWNLOAD  -  change these for each comic run
# =====================================================================

# Link to the FIRST issue you want. Format: https://batcave.biz/reader/<series id>/<issue id>
START_URL = "https://batcave.biz/reader/9264/60124"
START_ISSUE = 1          # number used for the first file (LongHalloween_001)
END_ISSUE = 4            # last issue number to download

# Where the files are saved:  FINAL_ROOT \ SUB_PATH \ FOLDER_NAME \ FILENAME_PREFIX_001.cbz
FINAL_ROOT = "finalcomics"
SUB_PATH = "Batman"              # set to "" to skip this level
FOLDER_NAME = "Long Halloween"
FILENAME_PREFIX = "LongHalloween"
ARCHIVE_EXT = ".cbz"             # ZIP comic archive; works in Calibre and ComicsPlusPlus


# =====================================================================
#  CHROME SETUP  -  set once for your computer, then leave alone
# =====================================================================

# Where Chrome is installed. Leave "" to find Chrome (or Microsoft Edge) automatically.
CHROME_PATH = ""

# Separate browser profile the scraper uses (keeps your Cloudflare pass between runs).
# Leave "" to use the app's own data folder.
CHROME_PROFILE = ""

USE_MY_CHROME = True     # True = open/reuse that normal Chrome window (gets past Cloudflare)
HIDE_CHROME = True       # True = keep the Chrome window off-screen; it pops up only for a "verify you are human" check
DEBUG_PORT = 9222        # only change if something else uses this port
HEADLESS = False         # only used when USE_MY_CHROME = False


# =====================================================================
#  ADVANCED  -  usually no need to touch
# =====================================================================

VERBOSE = False          # True = show every step; False = one line per issue (details only on problems)
SITE_URL = "https://batcave.biz"
PAGE_WORKERS = 8         # parallel image downloads per issue
STEP_DELAY = 0.3         # seconds between page turns (lower = faster)
MAX_STALLS = 8           # give up flipping after this many turns with no progress
JPEG_QUALITY = 92        # quality when converting WebP/AVIF pages to JPEG
DEBUG_SCREENSHOT = "debug_stuck.png"   # saved in the app data folder
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


# ---- where the app keeps its own files (settings, browser profile, debug output) ----
if os.name == "nt":
    APP_DATA = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ComicScraper")
else:
    APP_DATA = os.path.join(os.path.expanduser("~"), ".comicscraper")
os.makedirs(APP_DATA, exist_ok=True)


def find_browser():
    """Return the path of an installed Chrome, or Edge (built into Windows), or "" if neither."""
    if os.name != "nt":
        return ""
    roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    candidates = []
    for sub in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
        candidates += [os.path.join(r, sub) for r in roots if r]
    for path in candidates:
        if os.path.exists(path):
            return path
    return ""


def browser_path():
    return CHROME_PATH if CHROME_PATH and os.path.exists(CHROME_PATH) else find_browser()


def profile_path():
    return CHROME_PROFILE or os.path.join(APP_DATA, "browser-profile")


def parse_reader_url(url):
    """'https://batcave.biz/reader/9264/60124' -> ('9264', 60124). Raises ValueError if it isn't one."""
    clean = url.split("#")[0].split("?")[0].strip().rstrip("/")
    parts = clean.split("/")
    if "/reader/" not in clean or len(parts) < 2:
        raise ValueError("not a reader link")
    series, issue = parts[-2], parts[-1]
    if not series.isdigit() or not issue.isdigit():
        raise ValueError("not a reader link")
    return series, int(issue)


# ---- output: step-by-step detail is held back unless VERBOSE or something goes wrong ----
_held = []
_show_all = False


def detail(msg):
    """A step-by-step line: shown in verbose mode, otherwise kept in case of a problem."""
    if VERBOSE or _show_all:
        print(msg)
    else:
        _held.append(msg)


def problem(msg):
    """Something went wrong: show what led up to it, then the problem itself."""
    global _show_all
    if not VERBOSE and not _show_all:
        for line in _held:
            print(line)
    _held.clear()
    _show_all = True       # keep showing detail for the rest of this issue
    print(msg)


def new_issue_log():
    global _show_all
    _held.clear()
    _show_all = False


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


def set_chrome_visible(page, visible):
    """Move the scraper's Chrome window on-screen or off-screen (it keeps running either way)."""
    if not (USE_MY_CHROME and HIDE_CHROME):
        return
    try:
        cdp = page.context.new_cdp_session(page)
        win = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": "normal"}})
        if visible:
            bounds = {"left": 100, "top": 60, "width": 1280, "height": 900}
        else:
            bounds = {"left": -32000, "top": -32000, "width": 1280, "height": 900}
        cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": bounds})
        if visible:
            page.bring_to_front()
        cdp.detach()
    except Exception as e:
        detail(f"   (couldn't {'show' if visible else 'hide'} Chrome: {e})")


READER_SELECTOR = ".reader__item-wrap, img.reader__item"


def wait_for_reader(page, first_load):
    """Wait for the comic to load. If it doesn't show up quickly while Chrome is hidden,
    it's almost always a Cloudflare check, so show the window for the user to click."""
    long_wait = 90000 if first_load else 45000
    if not (USE_MY_CHROME and HIDE_CHROME):
        page.wait_for_selector(READER_SELECTOR, timeout=long_wait)
        return
    try:
        page.wait_for_selector(READER_SELECTOR, timeout=12000)
        return
    except PWTimeout:
        pass
    print("🔓 Chrome needs you: click the 'verify you are human' box in the window that just popped up.")
    set_chrome_visible(page, True)
    try:
        page.wait_for_selector(READER_SELECTOR, timeout=max(long_wait, 120000))
        print("   ✔️  Thanks! Hiding Chrome again.")
    finally:
        set_chrome_visible(page, False)


def collect_image_urls(page, url, first_load=False):
    detail("   🌐 Opening reader...")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # Long wait on the very first load in case a Cloudflare check needs solving
    detail("   ⏳ Waiting for reader pages to appear...")
    try:
        wait_for_reader(page, first_load)
    except PWTimeout:
        shot = os.path.join(APP_DATA, DEBUG_SCREENSHOT)
        html = os.path.join(APP_DATA, DEBUG_HTML)
        page.screenshot(path=shot, full_page=True)
        with open(html, "w", encoding="utf-8") as f:
            f.write(page.content())
        problem(f"   📸 Reader didn't load. Page title: '{page.title()}'")
        problem(f"   📸 Saved a screenshot of what Chrome showed: {shot}")
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
        detail(f"   🔎 On page {data['active']} | image URLs found: {len(found)}/{total}")
        if total and len(found) >= total:
            break

        stalls = 0 if len(found) > before else stalls + 1
        if stalls >= MAX_STALLS:
            problem(f"   ⚠️ Stopped flipping at {len(found)}/{total} pages")
            break

        if STOP_REQUESTED:
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
        problem(f"   ⚠️ Not an image: {url[:70]}... starts with: {preview!r}")
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
        problem(f"   ⚠️ Couldn't convert {fmt} page ({e}); saving as-is")
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
        problem(f"   ⚠️ Image failed: {url[:80]}... (Status: {r.status})")
    except Exception as e:
        problem(f"   ⚠️ Image error: {url[:80]}... ({e})")
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
    new_issue_log()
    print(f"📖 Issue {issue_num} of {END_ISSUE}...")
    detail(f"   {active_url}")

    try:
        urls = collect_image_urls(page, active_url, first_load)
    except PWTimeout:
        return f"❌ Reader never loaded for issue {issue_num}"
    if not urls:
        return f"❌ No image URLs found for issue {issue_num}"

    # Fast path: parallel downloads with the browser's cookies
    detail(f"   ⬇️  Downloading {len(urls)} pages...")
    session = make_session(context, user_agent)
    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
        results = list(ex.map(lambda u: download_with_requests(session, u, active_url), urls))

    failed = [i for i, r in enumerate(results) if r[1] is None]
    detail(f"   ✔️  Fast download: {len(urls) - len(failed)}/{len(urls)} OK")

    # Fallback: anything that failed gets retried through the browser itself
    for n, i in enumerate(failed, 1):
        detail(f"   🔁 Browser retry {n}/{len(failed)}")
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


STOP_REQUESTED = False   # the app window sets this to stop after the current issue


def run():
    """Run a full scrape using the settings at the top of this file (or the ones the app set)."""
    global STOP_REQUESTED
    STOP_REQUESTED = False
    print(f"📡 Saving to: {os.path.join(FINAL_ROOT, SUB_PATH or '', FOLDER_NAME)}")

    try:
        series_id, start_issue_id = parse_reader_url(START_URL)
    except ValueError:
        print(f"❌ That link doesn't look right: {START_URL}")
        print("   It should look like https://batcave.biz/reader/9264/60124")
        return

    with sync_playwright() as p:
        if USE_MY_CHROME:
            # Attach to the scraper Chrome window, starting it first if it isn't open
            browser = None
            try:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
            except Exception:
                exe = browser_path()
                if not exe:
                    print("❌ Couldn't find Google Chrome or Microsoft Edge. Install Chrome, "
                          "or set its location in Settings.")
                    return
                if HIDE_CHROME:
                    detail("🚀 Starting Chrome in the background...")
                else:
                    print("🚀 Opening Chrome... if you see a 'verify you are human' check, click it.")
                args = [exe, f"--remote-debugging-port={DEBUG_PORT}",
                        f"--user-data-dir={profile_path()}",
                        # keep pages running at full speed even though the window isn't visible
                        "--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--disable-background-timer-throttling",
                        "--no-first-run", "--no-default-browser-check"]
                if HIDE_CHROME:
                    args += ["--window-position=-32000,-32000", "--window-size=1280,900"]
                subprocess.Popen(args + [SITE_URL + "/"])
                for _ in range(30):
                    time.sleep(1)
                    try:
                        browser = p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
                        break
                    except Exception:
                        pass
                if browser is None:
                    print("❌ Chrome opened but the script couldn't connect. Close ALL Chrome windows and try again.")
                    return
            context = browser.contexts[0]
        else:
            browser = p.chromium.launch(channel="chrome", headless=HEADLESS,
                                        args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(viewport={"width": 1280, "height": 900})

        page = context.new_page()
        set_chrome_visible(page, False)
        user_agent = page.evaluate("navigator.userAgent")

        first = True
        for issue in range(START_ISSUE, END_ISSUE + 1):
            if STOP_REQUESTED:
                print("⏹️  Stopped by user")
                break
            result = process_issue(issue, series_id, start_issue_id, page, context, user_agent, first)
            (problem if result.startswith("❌") else print)(result)
            first = False

        page.close()   # leaves your Chrome window open
        if not USE_MY_CHROME:
            browser.close()
    print("🏁 Done")


if __name__ == "__main__":
    run()
