"""Checks GitHub for a newer Comic Scraper release (used by both the Windows and Android apps).

Every tagged release (v1.6.0, ...) built by GitHub Actions has two files:
  ComicScraper-Setup.exe     the Windows installer
  ComicScraper-Android.apk   the Android app
"""
import os
import re
import time

import requests

REPO = "JashtonColon/Comic-Scraper"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
WINDOWS_FILE = "ComicScraper-Setup.exe"
ANDROID_FILE = "ComicScraper-Android.apk"


def parse_version(text):
    """'v1.6.0' / '1.6.0' / '1.6.0-3-gabc123' -> (1, 6, 0). Anything else -> None."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", str(text or ""))
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def latest_release(timeout=15):
    """The newest published release: {'version', 'notes', 'page', 'exe', 'apk'} (urls may be '')."""
    r = requests.get(LATEST_URL, timeout=timeout,
                     headers={"Accept": "application/vnd.github+json", "User-Agent": "ComicScraper-Updater"})
    if r.status_code == 404:
        raise RuntimeError("no releases yet")
    if r.status_code == 403:
        raise RuntimeError("GitHub is limiting update checks right now; try again in an hour")
    r.raise_for_status()
    data = r.json()
    files = {a.get("name"): a.get("browser_download_url", "") for a in data.get("assets") or []}
    notes = (data.get("body") or "").strip()
    return {"version": str(data.get("tag_name", "")).lstrip("v"),
            "notes": notes[:1500],
            "page": data.get("html_url") or RELEASES_PAGE,
            "exe": files.get(WINDOWS_FILE, ""),
            "apk": files.get(ANDROID_FILE, "")}


def check(current):
    """The latest release if it's newer than `current`, else None."""
    rel = latest_release()
    return rel if is_newer(rel["version"], current) else None


def download(url, dest, progress=None):
    """Download to `dest` (via a .part file). progress(percent) is called every 10%."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    with requests.get(url, stream=True, timeout=60, headers={"User-Agent": "ComicScraper-Updater"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done, shown = 0, -10
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                f.write(chunk)
                done += len(chunk)
                if total and progress:
                    pct = int(done * 100 / total)
                    if pct >= shown + 10:
                        shown = pct - pct % 10
                        progress(shown)
    if total and done < total:
        raise RuntimeError("the download was cut off; try again")
    for attempt in range(5):                     # Windows may hold an old copy open for a moment
        try:
            os.replace(tmp, dest)
            return dest
        except OSError:
            time.sleep(0.5)
    os.replace(tmp, dest)
    return dest
