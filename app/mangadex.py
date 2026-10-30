"""Downloads manga from MangaDex (mangadex.org) into .cbz files.

MangaDex has a public API, so this doesn't need Chrome at all:
  - a chapter link or a title link tells us which manga it is
  - /manga/<id>/feed lists every chapter in one language
  - /at-home/server/<chapter id> says where that chapter's page images are

The scraper's settings (START_ISSUE, END_ISSUE, ALL_ISSUES, FINAL_ROOT, SUB_PATH, FOLDER_NAME,
FILENAME_PREFIX) are used the same way as for Batcave; the numbers are chapter numbers.
"""
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests

import scraper as S

# ======================= SETTINGS =======================
LANGUAGE = "en"            # chapters in this language (MangaDex code: en, es-la, pt-br, fr, ...)
DATA_SAVER = False         # True = smaller, compressed pages
CONTENT_RATINGS = ["safe", "suggestive", "erotica"]
PAGE_WORKERS = 6           # parallel page downloads per chapter
# ========================================================

API = "https://api.mangadex.org"
REPORT_URL = "https://api.mangadex.network/report"
USER_AGENT = "ComicScraper/1.0"
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
LINK_RE = re.compile(rf"mangadex\.org/(title|chapter)/({UUID})", re.I)


def is_mangadex(url):
    return "mangadex.org" in (url or "").lower()


def parse_link(url):
    """'https://mangadex.org/chapter/<id>/2' -> ('chapter', id); '.../title/<id>/name' -> ('title', id)."""
    m = LINK_RE.search(url or "")
    if not m:
        raise ValueError(url)
    return m.group(1).lower(), m.group(2).lower()


# ---------------------------------------------------------------- API
class Api:
    """Small MangaDex API client: stays under the rate limit and waits when told to slow down."""

    def __init__(self):
        self.http = requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT
        self._lock = threading.Lock()
        self._last = 0.0

    def get(self, path, params=None, gap=0.25):
        for attempt in range(5):
            with self._lock:                    # about 4 requests a second (the limit is 5)
                wait = self._last + gap - time.time()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.time()
            try:
                r = self.http.get(API + path, params=params, timeout=30)
            except requests.RequestException as e:
                if attempt == 4:
                    raise
                S.detail(f"      (MangaDex didn't answer: {e}; trying again)")
                time.sleep(2 + attempt * 2)
                continue
            if r.status_code == 429:            # too many requests: wait as long as it says
                retry_at = float(r.headers.get("X-RateLimit-Retry-After") or time.time() + 10)
                time.sleep(max(1.0, retry_at - time.time()))
                continue
            if r.status_code >= 500 and attempt < 4:
                time.sleep(2 + attempt * 2)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"MangaDex kept refusing {path}")


def _title_of(attrs):
    titles = attrs.get("title") or {}
    if titles.get("en"):
        return titles["en"]
    for alt in attrs.get("altTitles") or []:
        if alt.get("en"):
            return alt["en"]
    return next(iter(titles.values()), "Untitled")


def chapter_info(api, chapter_id):
    data = api.get(f"/chapter/{chapter_id}", {"includes[]": ["manga", "scanlation_group"]})["data"]
    rel = data.get("relationships") or []
    manga = next((r for r in rel if r["type"] == "manga"), None)
    return {
        "id": data["id"],
        "manga": manga["id"] if manga else None,
        "manga_title": _title_of(manga.get("attributes") or {}) if manga and manga.get("attributes") else None,
        "groups": [r["id"] for r in rel if r["type"] == "scanlation_group"],
        "chapter": data["attributes"].get("chapter"),
        "language": data["attributes"].get("translatedLanguage"),
    }


def manga_title(api, manga_id):
    return _title_of(api.get(f"/manga/{manga_id}")["data"]["attributes"])


def chapter_list(api, manga_id, language):
    """Every chapter of the manga in one language, oldest first. Chapters that are only hosted on
    another site (MangaPlus and so on) are left out: MangaDex has no pages for those."""
    chapters, offset = [], 0
    while True:
        params = {
            "translatedLanguage[]": [language],
            "contentRating[]": CONTENT_RATINGS,
            "includes[]": ["scanlation_group"],
            "order[volume]": "asc",
            "order[chapter]": "asc",
            "includeExternalUrl": 0,
            "includeEmptyPages": 0,
            "includeFuturePublishAt": 0,
            "limit": 500,
            "offset": offset,
        }
        page = api.get(f"/manga/{manga_id}/feed", params)
        for c in page["data"]:
            a = c["attributes"]
            if a.get("externalUrl") or not a.get("pages"):
                continue
            groups = [r for r in c.get("relationships") or [] if r["type"] == "scanlation_group"]
            chapters.append({
                "id": c["id"],
                "chapter": a.get("chapter"),
                "volume": a.get("volume"),
                "title": a.get("title") or "",
                "pages": a.get("pages") or 0,
                "groups": [g["id"] for g in groups],
                "group_names": [(g.get("attributes") or {}).get("name", "") for g in groups],
            })
        offset += len(page["data"])
        if not page["data"] or offset >= page.get("total", 0) or offset >= 10000:
            break
    return chapters


def chapter_number(ch):
    try:
        return float(ch["chapter"])
    except (TypeError, ValueError):
        return None


def pick_versions(chapters, preferred_groups=()):
    """MangaDex often has the same chapter from several translation groups. Keep one of each:
    the group from the link you pasted if it did that chapter, otherwise the group that did the most
    chapters, otherwise the one with the most pages. Returns them in reading order."""
    usage = Counter(g for c in chapters for g in c["groups"])
    preferred = set(preferred_groups)

    def rank(c):
        return (bool(preferred & set(c["groups"])), max((usage[g] for g in c["groups"]), default=0), c["pages"])

    by_key = {}
    for c in chapters:
        n = chapter_number(c)
        key = n if n is not None else ("oneshot", c["title"] or c["id"])
        if key not in by_key or rank(c) > rank(by_key[key]):
            by_key[key] = c
    picked = list(by_key.values())
    picked.sort(key=lambda c: (chapter_number(c) is None, chapter_number(c) or 0, c["title"]))
    return picked


# ---------------------------------------------------------------- files
def _safe(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")
    return name[:120] or "Chapter"


def file_stem(prefix, ch):
    """Chapter 7 -> 'Prefix_007', chapter 10.5 -> 'Prefix_010.5', a oneshot -> 'Prefix - Oneshot'."""
    n = chapter_number(ch)
    if n is None:
        return f"{prefix} - {_safe(ch['title'] or 'Oneshot')}"
    whole, _, frac = ch["chapter"].strip().partition(".")
    stem = f"{prefix}_{int(float(whole or 0)):03d}"
    frac = frac.rstrip("0")
    return stem + (f".{frac}" if frac else "")


# ---------------------------------------------------------------- pages
def page_urls(api, chapter_id, force_443=False):
    params = {"forcePort443": "true"} if force_443 else None
    info = api.get(f"/at-home/server/{chapter_id}", params, gap=1.6)   # this one allows 40 a minute
    quality, files = ("data-saver", info["chapter"]["dataSaver"]) if DATA_SAVER else ("data", info["chapter"]["data"])
    base = info["baseUrl"].rstrip("/")
    return [f"{base}/{quality}/{info['chapter']['hash']}/{f}" for f in files]


def _report(http, url, ok, size, started, cached):
    """MangaDex asks apps to say how each page download from its volunteer servers went."""
    if "mangadex.org" in url:
        return
    try:
        http.post(REPORT_URL, json={"url": url, "success": ok, "bytes": size, "cached": cached,
                                    "duration": int((time.time() - started) * 1000)}, timeout=5)
    except requests.RequestException:
        pass


def _fetch_page(http, url):
    started = time.time()
    try:
        r = http.get(url, timeout=60)
        ok = r.status_code == 200 and bool(r.content)
        _report(http, url, ok, len(r.content) if ok else 0, started, r.headers.get("X-Cache", "").startswith("HIT"))
        if not ok:
            return None, None
        return S.normalize_image(r.content, url)
    except requests.RequestException:
        _report(http, url, False, 0, started, False)
        return None, None


def download_chapter(api, ch, save_path):
    urls = page_urls(api, ch["id"])
    with ThreadPoolExecutor(PAGE_WORKERS) as pool:
        results = list(pool.map(lambda u: _fetch_page(api.http, u), urls))
    bad = [i for i, r in enumerate(results) if r[1] is None]
    if bad:
        # that image server may be having a bad moment: ask for another one and try those pages again
        S.detail(f"      {len(bad)} pages failed; trying another MangaDex server")
        retry = page_urls(api, ch["id"], force_443=True)
        if len(retry) == len(urls):
            for i in bad:
                results[i] = _fetch_page(api.http, retry[i])
    return S.save_cbz(results, save_path)


# ---------------------------------------------------------------- run
def run():
    """Same settings as a Batcave run; the issue boxes are chapter numbers (10 to 20 also gets 10.5)."""
    try:
        kind, link_id = parse_link(S.START_URL)
    except ValueError:
        print(f"That MangaDex link doesn't look right: {S.START_URL}")
        print("   Paste a title page like https://mangadex.org/title/<id>/<name>")
        print("   or a chapter like https://mangadex.org/chapter/<id>")
        return

    api = Api()
    folder = os.path.join(S.FINAL_ROOT, S.SUB_PATH or "", S.FOLDER_NAME)
    print(f"Saving to: {folder}")
    try:
        preferred, language = [], LANGUAGE
        if kind == "chapter":
            info = chapter_info(api, link_id)
            manga_id, preferred = info["manga"], info["groups"]
            language = info["language"] or LANGUAGE      # the language of the chapter you pasted
            title = info["manga_title"] or manga_title(api, manga_id)
        else:
            manga_id = link_id
            title = manga_title(api, manga_id)
        print(f"MangaDex: {title}  (language: {language})")
        chapters = pick_versions(chapter_list(api, manga_id, language), preferred)
    except (requests.RequestException, RuntimeError, KeyError) as e:
        S.problem(f"Couldn't read the chapter list from MangaDex: {e}")
        return
    if not chapters:
        S.problem(f"MangaDex has no readable '{language}' chapters for this manga "
                  "(some are only on other sites, like MangaPlus).")
        return

    if S.ALL_ISSUES:
        todo = chapters
    else:
        todo = [c for c in chapters if chapter_number(c) is not None
                and S.START_ISSUE <= chapter_number(c) < S.END_ISSUE + 1]
        have = {int(chapter_number(c)) for c in todo}
        missing = [str(n) for n in range(S.START_ISSUE, S.END_ISSUE + 1) if n not in have]
        if missing:
            S.problem(f"Not on MangaDex in this language: chapter {', '.join(missing[:30])}"
                      + (" ..." if len(missing) > 30 else ""))
    print(f"{len(todo)} chapters to get")

    os.makedirs(folder, exist_ok=True)
    for ch in todo:
        if S.STOP_REQUESTED:
            print("Stopped by user")
            break
        name = file_stem(S.FILENAME_PREFIX, ch) + S.ARCHIVE_EXT
        save_path = os.path.join(folder, name)
        label = f"Chapter {ch['chapter']}" if ch["chapter"] else (ch["title"] or "Oneshot")
        if os.path.exists(save_path):
            print(f"{label}: already there ({name})")
            continue
        S.new_issue_log()
        try:
            ok, info = download_chapter(api, ch, save_path)
        except (requests.RequestException, RuntimeError, KeyError) as e:
            ok, info = False, str(e)
        if ok:
            print(f"{label} saved ({info})")
        else:
            S.problem(f"Error: {info} for {label.lower()}")
    print("Done")
