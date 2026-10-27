# Comic Scraper engine. The app window (gui.py) fills in these settings for you;
# they're only used directly if you run:  python app/scraper.py
#
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
ALL_ISSUES = False               # True = ignore START/END_ISSUE and download everything in the series


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
STEP_DELAY = 0.15        # seconds between page turns (lower = faster)
MAX_STALLS = 8           # give up flipping after this many turns with no progress
JPEG_QUALITY = 92        # quality when converting WebP/AVIF pages to JPEG
DEBUG_SCREENSHOT = "debug_stuck.png"   # saved in the app data folder
DEBUG_HTML = "debug_stuck.html"


# =====================================================================
#  CODE BELOW  -  no settings past this line
# =====================================================================

import contextlib
import io
import os
import re
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


SERIES_PAGE_RE = re.compile(r"^https?://[^/]+/(\d+)-[^/?#]+\.html", re.I)


def parse_batcave_link(url):
    """A Batcave link of either kind:
         series page  https://batcave.biz/7303-action-comics-1938.html  -> ("series", "7303", None)
         issue        https://batcave.biz/reader/9264/60124             -> ("reader", "9264", 60124)
       Raises ValueError for anything else."""
    url = url.strip()
    m = SERIES_PAGE_RE.match(url)
    if m:
        return "series", m.group(1), None
    series, issue = parse_reader_url(url)
    return "reader", series, issue


def series_page_url(url):
    """Just the series page address, without '#chapters' and the like."""
    m = SERIES_PAGE_RE.match(url.strip())
    return m.group(0) if m else None


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
      // full address even before the image has loaded (a bare attribute can be a short path)
      const raw = img ? (img.getAttribute('src') || img.dataset.src || '') : '';
      let src = img && img.currentSrc ? img.currentSrc : (raw ? new URL(raw, location.href).href : '');
      if (src === location.href) src = '';
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


_TASKBAR_HIDDEN = set()     # window handles we took off the taskbar


def set_taskbar_button(show):
    """Windows only: remove the hidden scraper Chrome window's taskbar (and Alt+Tab) button,
    or put it back. Only touches Chrome windows parked off-screen that are NOT minimized, which
    is how the scraper's window sits; your own Chrome windows (minimized or not) are left alone."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x00000080, 0x00040000
        SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
        if show:
            targets = list(_TASKBAR_HIDDEN)
            _TASKBAR_HIDDEN.clear()
        else:
            targets = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def visit(hwnd, _):
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, cls, 64)
                if cls.value == "Chrome_WidgetWin_1" and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                    r = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(r))
                    if r.left <= -20000 and r.top <= -20000:       # parked off-screen by us
                        targets.append(hwnd)
                return True
            user32.EnumWindows(visit, 0)
            _TASKBAR_HIDDEN.update(targets)
        for hwnd in targets:
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if show:
                new = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            else:
                new = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            if new != style:
                user32.ShowWindow(hwnd, SW_HIDE)              # the taskbar only notices after a hide/show
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new)
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    except Exception as e:
        detail(f"   (couldn't change Chrome's taskbar button: {e})")


def set_chrome_visible(page, visible):
    """Move the scraper's Chrome window on-screen or off-screen (it keeps running either way)."""
    if not (USE_MY_CHROME and HIDE_CHROME):
        return
    if visible:
        set_taskbar_button(True)
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
    if not visible:
        time.sleep(0.2)                  # let Windows register the move before checking positions
        set_taskbar_button(False)


READER_SELECTOR = ".reader__item-wrap, img.reader__item"


def wait_for_reader(page, first_load, selector=READER_SELECTOR, js_condition=None):
    """Wait for the page content to load (the comic by default, or `selector` / `js_condition`).
    If it doesn't show up quickly while Chrome is hidden, it's almost always a Cloudflare
    check, so show the window for the user to click."""
    long_wait = 90000 if first_load else 45000

    def wait(timeout):
        if js_condition:
            page.wait_for_function(js_condition, timeout=timeout)
        else:
            page.wait_for_selector(selector, timeout=timeout)

    if not (USE_MY_CHROME and HIDE_CHROME):
        wait(long_wait)
        return
    try:
        wait(12000)
        return
    except PWTimeout:
        pass
    print("Chrome needs you: click the 'verify you are human' box in the window that just popped up.")
    set_chrome_visible(page, True)
    try:
        wait(max(long_wait, 120000))
        print("Thanks! Hiding Chrome again.")
    finally:
        set_chrome_visible(page, False)


# Image links that appear anywhere in the reader page's code (often the full list is there)
SOURCE_URLS_JS = r"""
() => {
  const html = document.documentElement.innerHTML.replace(/\\\//g, '/').replace(/&amp;/g, '&');
  const re = /https?:\/\/[^"'\s<>()]*batcave[^"'\s<>()]*?\.(?:jpe?g|png|webp|avif|gif)(?:\?[^"'\s<>()]*)?/gi;
  const out = [];
  for (const m of html.matchAll(re)) if (!out.includes(m[0])) out.push(m[0]);
  return out;
}
"""


def urls_from_source(page, data, found):
    """If the page code lists exactly one image link per page, in order, and it agrees with the
    pages already loaded (token included), use it and skip flipping. Otherwise return None."""
    total = data["total"]
    if not total or not found:
        return None
    try:
        listed = page.evaluate(SOURCE_URLS_JS)
    except Exception:
        return None
    # the code also contains the <img> tags already on the page; keep each link once, in order
    if len(listed) != total:
        return None
    for num, src in found.items():
        if not (1 <= num <= total) or listed[num - 1] != src:
            return None
    return listed


def collect_image_urls(page, url, first_load=False):
    detail("Opening reader...")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # Long wait on the very first load in case a Cloudflare check needs solving
    detail("Waiting for reader pages to appear...")
    try:
        wait_for_reader(page, first_load)
    except PWTimeout:
        shot = os.path.join(APP_DATA, DEBUG_SCREENSHOT)
        html = os.path.join(APP_DATA, DEBUG_HTML)
        page.screenshot(path=shot, full_page=True)
        with open(html, "w", encoding="utf-8") as f:
            f.write(page.content())
        problem(f"Reader didn't load. Page title: '{page.title()}'")
        problem(f"Saved a screenshot of what Chrome showed: {shot}")
        raise
    time.sleep(0.6)

    found = {}
    stalls = 0
    first_pass = True
    while True:
        data = page.evaluate(COLLECT_JS)
        before = len(found)
        for num, src in data["pages"]:
            if src and not src.startswith("data:") and "batcave" in src and num not in found:
                found[num] = src
        if first_pass:
            first_pass = False
            listed = urls_from_source(page, data, found)
            if listed:
                detail(f"All {len(listed)} image links were in the page code (no flipping needed)")
                return listed

        total = data["total"]
        detail(f"On page {data['active']} | image URLs found: {len(found)}/{total}")
        if total and len(found) >= total:
            break

        stalls = 0 if len(found) > before else stalls + 1
        if stalls >= MAX_STALLS:
            problem(f"Stopped flipping at {len(found)}/{total} pages")
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
        problem(f"Not an image: {url[:70]}... starts with: {preview!r}")
        return None, None
    # webp / avif / gif -> convert to JPEG for reader compatibility
    try:
        from PIL import Image
        if fmt == "avif":
            try:
                import pillow_avif  # type: ignore  # noqa: F401  (optional, only needed on older Pillow)
            except ImportError:
                pass
        img = Image.open(io.BytesIO(data)).convert("RGB")
        out = io.BytesIO()
        img.save(out, "JPEG", quality=JPEG_QUALITY)
        return ".jpg", out.getvalue()
    except Exception as e:
        problem(f"Couldn't convert {fmt} page ({e}); saving as-is")
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
        problem(f"Image failed: {url[:80]}... (Status: {r.status})")
    except Exception as e:
        problem(f"Image error: {url[:80]}... ({e})")
    return None, None


def _referers(referer, urls):
    return referer if isinstance(referer, list) else [referer] * len(urls)


def fetch_pages(urls, referer, session):
    """Download all page images with plain HTTP, in parallel. Safe to run in a background thread.
    referer is one reader link, or one per image (when several parts are joined into one file)."""
    refs = _referers(referer, urls)
    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
        return list(ex.map(lambda pair: download_with_requests(session, pair[0], pair[1]), zip(urls, refs)))


def save_cbz(results, save_path):
    valid_pages = [p for p in results if p[1] is not None]
    if not valid_pages:
        return False, "No valid images downloaded"
    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
    tmp_path = save_path + ".part"          # so a cancelled download never looks finished
    width = max(3, len(str(len(valid_pages))))     # 4 digits for joined TPBs over 999 pages
    with zipfile.ZipFile(tmp_path, "w") as archive:
        for idx, (ext, content) in enumerate(valid_pages):
            archive.writestr(f"{str(idx + 1).zfill(width)}{ext}", content)
    os.replace(tmp_path, save_path)
    missing = len(results) - len(valid_pages)
    note = f", {missing} missing" if missing else ""
    return True, f"Pages: {len(valid_pages)}{note}"


def retry_in_browser(results, urls, reader_url, context):
    """Pages plain HTTP couldn't get are fetched through Chrome itself (main thread only)."""
    refs = _referers(reader_url, urls)
    failed = [i for i, r in enumerate(results) if r[1] is None]
    for n, i in enumerate(failed, 1):
        detail(f"Browser retry {n}/{len(failed)}")
        results[i] = download_with_browser(context, urls[i], refs[i])
    return results


def collect_parts(page, reader_urls, first_load=False):
    """Image links for one comic that may be split into parts on Batcave ("(Part 1)", "(Part 2)"...).
    Returns (urls, referers) with every part's pages in order."""
    if isinstance(reader_urls, str):
        reader_urls = [reader_urls]
    urls, refs = [], []
    for n, reader in enumerate(reader_urls, 1):
        detail(f"   part {n}/{len(reader_urls)}: {reader}" if len(reader_urls) > 1 else f"   {reader}")
        part = collect_image_urls(page, reader, first_load)
        first_load = False
        if not part:
            raise ValueError("No image URLs found" + (f" in part {n}" if len(reader_urls) > 1 else ""))
        urls += part
        refs += [reader] * len(part)
    return urls, refs


def combine_cbz(part_paths, save_path):
    """Join already-downloaded part files into one .cbz (pages renumbered in order)."""
    tmp_path = save_path + ".part"
    n = 0
    with zipfile.ZipFile(tmp_path, "w") as out:
        for p in part_paths:
            with zipfile.ZipFile(p) as z:
                for name in sorted(z.namelist()):
                    if name.endswith("/"):
                        continue
                    n += 1
                    out.writestr(f"{str(n).zfill(4)}{os.path.splitext(name)[1]}", z.read(name))
    os.replace(tmp_path, save_path)
    return n


def download_issue(reader_url, save_path, page, context, user_agent, first_load=False):
    """Download one issue from its reader link into a .cbz at save_path.
    Returns (ok, info) where info is e.g. "Pages: 25" or an error message."""
    try:
        urls, refs = collect_parts(page, reader_url, first_load)
    except PWTimeout:
        return False, "Reader never loaded"
    except ValueError as e:
        return False, str(e)
    detail(f"Downloading {len(urls)} pages...")
    results = fetch_pages(urls, refs, make_session(context, user_agent))
    detail(f"Fast download: {sum(1 for r in results if r[1] is not None)}/{len(urls)} OK")
    return save_cbz(retry_in_browser(results, urls, refs, context), save_path)


class Pipeline:
    """Overlaps work: while one issue's images download in the background, Chrome is
    already collecting the next issue's image links."""

    def __init__(self, context, user_agent, on_done, ahead=2):
        self.context, self.user_agent, self.on_done = context, user_agent, on_done
        self.ex = ThreadPoolExecutor(max_workers=ahead)
        self.ahead = ahead
        self.jobs = []          # (label, reader_url, save_path, urls, future, started)

    def add(self, label, reader_url, save_path, page, first_load=False):
        """Collect the image links now (needs Chrome), download them in the background.
        Returns False if the issue failed before downloading started."""
        try:
            urls, refs = collect_parts(page, reader_url, first_load)
        except PWTimeout:
            self.on_done(label, False, "Reader never loaded")
            return False
        except ValueError as e:
            self.on_done(label, False, str(e))
            return False
        session = make_session(self.context, self.user_agent)
        fut = self.ex.submit(fetch_pages, urls, refs, session)
        self.jobs.append((label, refs, save_path, urls, fut, time.time()))
        # don't run too far ahead of the downloads
        while len([j for j in self.jobs if not j[4].done()]) >= self.ahead:
            time.sleep(0.1)
        self.finish(wait=False)
        return True

    def finish(self, wait=True):
        """Save every finished download (all of them if wait=True)."""
        still = []
        for job in self.jobs:
            label, reader_url, save_path, urls, fut, started = job
            if not fut.done() and not wait:
                still.append(job)
                continue
            try:
                results = fut.result()
                results = retry_in_browser(results, urls, reader_url, self.context)
                ok, info = save_cbz(results, save_path)
            except Exception as e:
                ok, info = False, f"Download error: {e}"
            if ok:
                info = f"{info}, {time.time() - started:.0f}s"
            self.on_done(label, ok, info)
        self.jobs = still

    def close(self):
        self.finish(wait=True)
        self.ex.shutdown(wait=True)


def process_issue(issue_num, series_id, start_issue_id, page, context, user_agent, first_load, pipeline=None,
                  reader_url=None):
    issue_padded = str(issue_num).zfill(3)
    final_dir = os.path.join(FINAL_ROOT, SUB_PATH, FOLDER_NAME) if SUB_PATH else os.path.join(FINAL_ROOT, FOLDER_NAME)
    final_save_path = os.path.join(final_dir, f"{FILENAME_PREFIX}_{issue_padded}{ARCHIVE_EXT}")

    if os.path.exists(final_save_path):
        return True, f"Skipping {issue_num} (already downloaded)"

    if reader_url:
        active_url = reader_url
    else:                             # old way: assume IDs count up by one (not always true)
        issue_id = start_issue_id + (issue_num - START_ISSUE)
        active_url = f"{SITE_URL}/reader/{series_id}/{issue_id}"
    new_issue_log()
    print(f"Issue {issue_num} of {END_ISSUE}...")

    if pipeline:
        pipeline.add(issue_num, active_url, final_save_path, page, first_load)
        return True, None            # reported when its download finishes
    ok, info = download_issue(active_url, final_save_path, page, context, user_agent, first_load)
    if ok:
        return True, f"Issue {issue_num} saved ({info})"
    return False, f"Error: {info} for issue {issue_num}"


STOP_REQUESTED = False   # the app window sets this to stop after the current issue


class BrowserError(Exception):
    """Chrome couldn't be found or connected to. The message says what to do."""


@contextlib.contextmanager
def browser_session():
    """Open (or reuse) the scraper's Chrome and yield (page, context, user_agent)."""
    with sync_playwright() as p:
        if USE_MY_CHROME:
            # Attach to the scraper Chrome window, starting it first if it isn't open
            browser = None
            try:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
            except Exception:
                exe = browser_path()
                if not exe:
                    raise BrowserError("Couldn't find Google Chrome or Microsoft Edge. Install Chrome, "
                                       "or set its location in Settings.")
                if HIDE_CHROME:
                    detail("Starting Chrome in the background...")
                else:
                    print("Opening Chrome... if you see a 'verify you are human' check, click it.")
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
                    raise BrowserError("Chrome opened but the script couldn't connect. "
                                       "Close ALL Chrome windows and try again.")
            context = browser.contexts[0]
        else:
            browser = p.chromium.launch(channel="chrome", headless=HEADLESS,
                                        args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(viewport={"width": 1280, "height": 900})

        page = context.new_page()
        set_chrome_visible(page, False)
        user_agent = page.evaluate("navigator.userAgent")
        try:
            yield page, context, user_agent
        finally:
            try:
                page.close()   # leaves your Chrome window open
            except Exception:
                pass
            if not USE_MY_CHROME:
                browser.close()


READER_LINKS_JS = r"""
(sid) => {
  // issue links on the reader page itself (e.g. a table-of-contents list) and the series page link
  const issues = [...document.querySelectorAll(`a[href*="/reader/${sid}/"]`)]
      .map(a => [a.textContent.replace(/\s+/g, ' ').trim(), a.href]).filter(([t]) => /#\s*\d|issue/i.test(t));
  const series = [...document.querySelectorAll('a[href]')]
      .map(a => a.href).find(h => new RegExp(`/${sid}-[^/]+\\.html`).test(h));
  return {issues, series: series ? series.split('#')[0] : null};
}
"""


def series_entries(page, series_id, start_url, first_load, complete=False):
    """Every issue Batcave lists for the series that start_url belongs to, as issue_map() data,
    or None if the list can't be read. complete=True prefers the full series page over any
    issue list shown on the reader page itself."""
    import readinglist as R          # shares the series-page reader with reading lists
    series_url = series_page_url(start_url)
    if series_url:                   # they pasted the series page itself
        issues = R.batcave_series_issues(page, series_url, first_load)
        return R.issue_map(issues) if issues else None
    page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
    try:
        wait_for_reader(page, first_load)
    except PWTimeout:
        return None
    found = page.evaluate(READER_LINKS_JS, str(series_id))
    on_reader = found["issues"] if len(found["issues"]) > 1 else []
    issues = []
    if found["series"] and (complete or not on_reader):
        issues = R.batcave_series_issues(page, found["series"])
    issues = issues or on_reader
    return R.issue_map(issues) if issues else None


def all_issue_files(bc):
    """Everything in a series, in a sensible order, with file names:
    issues by number, then annuals, then named entries (TPBs, specials).
    A comic Batcave splits into parts ("(Part 1)", "(Part 2)"...) becomes one item with all part links."""
    import readinglist as R

    def num_key(n):
        m = re.match(r"(\d+)(.*)", n)
        return (0, int(m.group(1)), m.group(2)) if m else (1, 0, n)

    def natural(t):                                   # "Vol 2" before "Vol 10"
        return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", t)]

    out, seen = [], set()

    def add(label, stem, hrefs):
        stem = R.safe_filename(stem)
        if stem.lower() in seen:                       # two entries with the same name
            stem = f"{stem} ({hrefs[0].rstrip('/').split('/')[-1]})"
        seen.add(stem.lower())
        out.append((label, stem, hrefs if len(hrefs) > 1 else hrefs[0]))

    def grouped(entries, key):
        groups = {}
        for e in entries:
            groups.setdefault(key(e), []).append(e)
        return [(k, [e["href"] for e in sorted(es, key=lambda e: e.get("part") or 0)])
                for k, es in groups.items()]

    regular = [e for e in bc["entries"] if e["explicit"] and not e["annual"]]
    annuals = [e for e in bc["entries"] if e["explicit"] and e["annual"]]
    for n, hrefs in sorted(grouped(regular, lambda e: e["num"]), key=lambda g: num_key(g[0])):
        add(f"#{n}", f"{FILENAME_PREFIX}_{n.zfill(3) if n.isdigit() else n}", hrefs)
    for n, hrefs in sorted(grouped(annuals, lambda e: e["num"]), key=lambda g: num_key(g[0])):
        add(f"Annual #{n}", f"{FILENAME_PREFIX}_Annual_{n.zfill(2) if n.isdigit() else n}", hrefs)

    # the series name every entry starts with, e.g. "L.E.G.I.O.N. " or "Uncanny X-Men (1963) "
    titles = [e["title"] for e in bc["entries"]]
    common = os.path.commonprefix(titles) if len(titles) > 1 else ""
    common = common[:common.rfind(" ") + 1] if " " in common else ""

    def short_name(t):
        rest = t[len(common):].strip() if common and t.startswith(common) else t
        if rest == t and re.match(r"^.+?\(\d{4}[^)]*\)", t):        # "Series (1987) Something"
            rest = t.split(")", 1)[1].strip()
        return R.PART_RE.sub("", rest).strip() or R.PART_RE.sub("", t).strip() or t

    named = [e for e in bc["entries"] if not e["explicit"]]
    for rest, hrefs in sorted(grouped(named, lambda e: short_name(e["title"])), key=lambda g: natural(g[0])):
        add(rest, f"{FILENAME_PREFIX} - {rest}", hrefs)
    return out


def parts_by_number(entries):
    """{issue number: reader link}, or a list of links in order if Batcave split it into parts."""
    groups = {}
    for e in entries:
        groups.setdefault(e["num"], []).append(e)
    out = {}
    for num, es in groups.items():
        parted = [e for e in es if e.get("part")]
        out[num] = [e["href"] for e in sorted(parted, key=lambda e: e["part"])] if len(parted) > 1 else es[0]["href"]
    return out


def issue_links_for_range(page, series_id, start_url, first_load):
    """Batcave's issue IDs don't always count up by one (Uncanny X-Men #1 is 39016, #2 is 39018),
    so read the real issue list and pick issues by number. Returns {file_number: reader_url}
    and a list of issue numbers that aren't on Batcave, or (None, None) if the list can't be read."""
    bc = series_entries(page, series_id, start_url, first_load)
    if not bc:
        return None, None
    entries = [e for e in bc["entries"] if e["explicit"]]
    if series_page_url(start_url):
        # series page: the boxes are real issue numbers, "1 to 10" = issues #1-#10
        by_num = parts_by_number([e for e in entries if not e["annual"]])
        links = {n: by_num[str(n)] for n in range(START_ISSUE, END_ISSUE + 1) if str(n) in by_num}
        missing = [f"#{n}" for n in range(START_ISSUE, END_ISSUE + 1) if str(n) not in by_num]
        detail(f"   Read {len(entries)} issues from Batcave's series page")
        return links, missing
    start_id = start_url.split("#")[0].split("?")[0].rstrip("/").split("/")[-1]
    start = next((e for e in entries if e["href"].rstrip("/").split("/")[-1] == start_id), None)
    if not start or not re.fullmatch(r"\d+", start["num"]):
        return None, None
    by_num = parts_by_number([e for e in entries if e["annual"] == start["annual"]])
    first_num = int(start["num"])
    links, missing = {}, []
    for k in range(END_ISSUE - START_ISSUE + 1):
        n = str(first_num + k)
        if n in by_num:
            links[START_ISSUE + k] = by_num[n]
        else:
            missing.append(f"#{n}")
    detail(f"   Read {len(entries)} issues from Batcave's list; starting at issue #{first_num}")
    return links, missing


def merge_downloaded_parts(folder, stem, count, save_path):
    """Earlier versions saved each part separately ("book1 - 1 (Part 1).cbz" ...). If all of them
    are there, join them into one file without downloading again, then remove the parts."""
    parts = [os.path.join(folder, f"{stem} (Part {n}){ARCHIVE_EXT}") for n in range(1, count + 1)]
    if not all(os.path.exists(p) for p in parts):
        return False
    pages = combine_cbz(parts, save_path)
    for p in parts:
        os.remove(p)
    print(f"Joined {count} parts into {os.path.basename(save_path)} ({pages} pages)")
    return True


def run_all(page, series_id, pipeline):
    """Download every issue, annual and named entry (TPBs, specials) in the series."""
    final_dir = os.path.join(FINAL_ROOT, SUB_PATH, FOLDER_NAME) if SUB_PATH else os.path.join(FINAL_ROOT, FOLDER_NAME)
    try:
        bc = series_entries(page, series_id, START_URL, True, complete=True)
        if not bc:
            problem("Couldn't read this series' issue list from Batcave. Untick 'every issue' and use a range instead.")
            return
        files = all_issue_files(bc)
        kinds = {"issues": sum(1 for f in files if f[0].startswith("#")),
                 "annuals": sum(1 for f in files if f[0].startswith("Annual")),
                 "others (TPBs, specials)": sum(1 for f in files if not f[0].startswith(("#", "Annual")))}
        print(f"Batcave lists {len(files)} entries: " + ", ".join(f"{v} {k}" for k, v in kinds.items() if v))
        skipped, first, merged = 0, False, 0
        for n, (label, stem, href) in enumerate(files, 1):
            if STOP_REQUESTED:
                print("Stopped by user")
                break
            path = os.path.join(final_dir, stem + ARCHIVE_EXT)
            if os.path.exists(path):
                skipped += 1
                continue
            if isinstance(href, list) and merge_downloaded_parts(final_dir, stem, len(href), path):
                merged += 1
                continue
            new_issue_log()
            print(f"[{n}/{len(files)}] {label}...")
            pipeline.add(label, href, path, page, first)
        if merged:
            print(f"Joined {merged} comics from parts you'd already downloaded")
        if skipped:
            print(f"Skipped {skipped} already downloaded")
    finally:
        pipeline.close()
    print("Done")


def run():
    """Run a full scrape using the settings at the top of this file (or the ones the app set)."""
    global STOP_REQUESTED
    STOP_REQUESTED = False
    print(f"Saving to: {os.path.join(FINAL_ROOT, SUB_PATH or '', FOLDER_NAME)}")

    try:
        kind, series_id, start_issue_id = parse_batcave_link(START_URL)
    except ValueError:
        print(f"That link doesn't look right: {START_URL}")
        print("   Paste a series page like https://batcave.biz/7303-action-comics-1938.html")
        print("   or an issue like https://batcave.biz/reader/9264/60124")
        return

    try:
        with browser_session() as (page, context, user_agent):
            def done(issue, ok, info):
                if ok:
                    print(f"Issue {issue} saved ({info})")
                else:
                    problem(f"Error: {info} for issue {issue}")
            pipeline = Pipeline(context, user_agent, done)
            if ALL_ISSUES:
                run_all(page, series_id, pipeline)
                return
            links, missing = issue_links_for_range(page, series_id, START_URL, True)
            if links is None and kind == "series":
                problem("Couldn't read the issue list from that series page.")
                return
            if links is None:
                problem("Couldn't read this series' issue list from Batcave, so counting issue IDs up by one. "
                        "Some series skip IDs, so check that the issues are the right ones.")
            elif missing:
                problem(f"Not on Batcave: {', '.join(missing)}")
            first = links is None
            try:
                for issue in range(START_ISSUE, END_ISSUE + 1):
                    if STOP_REQUESTED:
                        print("Stopped by user")
                        break
                    if links is not None and issue not in links:
                        continue
                    ok, result = process_issue(issue, series_id, start_issue_id, page, context, user_agent,
                                               first, pipeline, reader_url=(links or {}).get(issue))
                    if result:
                        (print if ok else problem)(result)
                    first = False
            finally:
                pipeline.close()          # finish the downloads still running
    except BrowserError as e:
        print(str(e))
        return
    print("Done")


if __name__ == "__main__":
    run()
